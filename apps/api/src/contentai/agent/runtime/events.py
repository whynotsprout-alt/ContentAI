from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum
from threading import RLock, Timer
from time import monotonic
from typing import Any

from contentai.core.config import get_settings
from contentai.models.chat import AgentExecution, AgentExecutionAttempt
from contentai.services.event_stream import (
    EventStreamPublisher,
    EventStreamUnavailable,
    RedisEventStream,
    StreamEvent,
)
from sqlmodel import Session

logger = logging.getLogger(__name__)


EventPayload = dict[str, Any]
RealtimeEmit = Callable[[str, EventPayload], None]

_EVENT_SCHEMA_VERSION = 1
_STREAM_BATCH_INTERVAL_SECONDS = 0.05
_STREAM_BATCH_MAX_CHARS = 256
_CONTRACT_EVENT_TYPES = {
    "state",
    "token",
    "tool_start",
    "tool_progress",
    "tool_end",
    "error",
    "done",
    "heartbeat",
}


class ClosedWriterError(RuntimeError):
    """Raised when operations are attempted on a closed event writer."""


def now_utc() -> datetime:
    return datetime.now(UTC)


def _make_json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _make_json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_make_json_safe(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _make_json_safe(model_dump())
    as_dict = getattr(value, "dict", None)
    if callable(as_dict):
        try:
            return _make_json_safe(as_dict())
        except TypeError:
            return str(value)
    return str(value)


def _make_json_payload(value: EventPayload) -> EventPayload:
    safe = _make_json_safe(value)
    if not isinstance(safe, dict):
        return {"value": str(safe)}
    return safe


class AgentEventWriter:
    def __init__(
        self,
        execution_id: str,
        settings: Any | None = None,
        *,
        trace_id: str | None = None,
        thread_id: str | None = None,
        request_id: str | None = None,
        conversation_id: str | None = None,
        realtime_emit: RealtimeEmit | None = None,
    ) -> None:
        self.execution_id = execution_id
        self.trace_id = trace_id
        self.thread_id = thread_id
        self.request_id = request_id
        self.conversation_id = conversation_id
        self._realtime_emit = realtime_emit
        self._closed = False
        self._closing = False
        agent_settings = getattr(settings, "agent", None)
        configured_interval_ms = max(
            1,
            int(getattr(agent_settings, "event_flush_interval_ms", 50) or 50),
        )
        configured_max_chars = max(
            1,
            int(getattr(agent_settings, "event_flush_max_chars", 256) or 256),
        )
        self._stream_batch_interval_seconds = min(
            _STREAM_BATCH_INTERVAL_SECONDS,
            configured_interval_ms / 1000,
        )
        self._stream_batch_max_chars = min(
            _STREAM_BATCH_MAX_CHARS,
            configured_max_chars,
        )
        self._pending_delta: EventPayload | None = None
        self._pending_delta_started_at = 0.0
        self._pending_delta_timer: Timer | None = None
        self._active_delta_message_id: str | None = None
        self._batch_lock = RLock()

    def emit(self, event: str, data: EventPayload) -> None:
        with self._batch_lock:
            if self._closed:
                raise ClosedWriterError(f"Event writer is closed: execution_id={self.execution_id}")
            if self._closing:
                raise ClosedWriterError(
                    f"Event writer is closing: execution_id={self.execution_id}"
                )
            if not isinstance(event, str) or not event.strip():
                raise ValueError("event must be a non-empty string")
            if not isinstance(data, dict):
                raise TypeError("event data must be a JSON object")

            event_name = event.strip()
            payload = _sanitize_payload(
                dict(data),
                execution_id=self.execution_id,
                trace_id=self.trace_id,
                thread_id=self.thread_id,
                request_id=self.request_id,
                conversation_id=self.conversation_id,
            )

            if event_name == "assistant_message_delta" and not bool(payload.get("done")):
                self._buffer_assistant_delta(payload)
                return
            self._flush_pending_delta_locked()
            if event_name in {"assistant_message_delta", "assistant_message"}:
                self._active_delta_message_id = None
            self._emit_now(event_name, payload)

    def _emit_now(self, event_name: str, payload: EventPayload) -> None:

        contract_event, contract_payload = self._normalize_contract_event(
            event_name,
            payload,
        )
        self._emit_realtime(contract_event, contract_payload)
        self._safe_write_event(contract_event, contract_payload)

    def _buffer_assistant_delta(self, payload: EventPayload) -> None:
        now = monotonic()
        message_id = str(payload.get("message_id") or "")
        if message_id != self._active_delta_message_id:
            self._flush_pending_delta_locked()
            self._active_delta_message_id = message_id
            self._emit_now("assistant_message_delta", payload)
            return

        pending_message_id = str((self._pending_delta or {}).get("message_id") or "")
        if self._pending_delta is not None and message_id != pending_message_id:
            self._flush_pending_delta_locked()

        if self._pending_delta is None:
            self._pending_delta = dict(payload)
            self._pending_delta["chunk"] = ""
            self._pending_delta["done"] = False
            self._pending_delta_started_at = now
            self._schedule_pending_delta_flush_locked()
        chunk = _coerce_content(payload)
        self._pending_delta["chunk"] = str(self._pending_delta.get("chunk") or "") + chunk
        if (
            len(str(self._pending_delta["chunk"])) >= self._stream_batch_max_chars
            or now - self._pending_delta_started_at >= self._stream_batch_interval_seconds
        ):
            self._flush_pending_delta_locked()

    def _flush_pending_delta_locked(self) -> None:
        if self._pending_delta is None:
            return
        payload = self._pending_delta
        self._pending_delta = None
        self._pending_delta_started_at = 0.0
        self._cancel_pending_delta_timer_locked()
        self._emit_now("assistant_message_delta", payload)

    def _schedule_pending_delta_flush_locked(self) -> None:
        self._cancel_pending_delta_timer_locked()
        timer = Timer(self._stream_batch_interval_seconds, self._flush_pending_delta_deadline)
        timer.daemon = True
        self._pending_delta_timer = timer
        timer.start()

    def _cancel_pending_delta_timer_locked(self) -> None:
        timer = self._pending_delta_timer
        self._pending_delta_timer = None
        if timer is not None:
            timer.cancel()

    def _flush_pending_delta_deadline(self) -> None:
        with self._batch_lock:
            if self._closed or self._closing:
                return
            self._flush_pending_delta_locked()

    def flush(self) -> None:
        with self._batch_lock:
            if self._closed and not self._closing:
                raise ClosedWriterError(f"Event writer is closed: execution_id={self.execution_id}")
            self._flush_pending_delta_locked()

    def close(self) -> None:
        with self._batch_lock:
            if self._closed:
                return
            self._closing = True
            try:
                self._flush_pending_delta_locked()
            finally:
                try:
                    self._close_writer()
                finally:
                    self._closing = False
                    self._closed = True

    def _write_event(self, event: str, payload: EventPayload) -> None:
        raise NotImplementedError

    def _close_writer(self) -> None:
        return None

    def _safe_write_event(self, event: str, payload: EventPayload) -> None:
        try:
            self._write_event(event, payload)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to persist event for execution %s: %s",
                self.execution_id,
                event,
            )

    def _emit_realtime(self, event: str, payload: EventPayload) -> None:
        if self._realtime_emit is None:
            return
        try:
            self._realtime_emit(event, payload)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to emit realtime event for execution %s", self.execution_id)

    def _normalize_contract_event(
        self,
        event_name: str,
        payload: EventPayload,
    ) -> tuple[str, EventPayload]:
        normalized_name = event_name.strip()
        contract_payload = _sanitize_payload(
            payload,
            execution_id=self.execution_id,
            trace_id=self.trace_id,
            thread_id=self.thread_id,
            request_id=self.request_id,
            conversation_id=self.conversation_id,
        )
        contract_payload.setdefault("schema_version", _EVENT_SCHEMA_VERSION)

        if normalized_name in _CONTRACT_EVENT_TYPES:
            contract_payload.setdefault("name", normalized_name)
            return normalized_name, contract_payload

        if normalized_name == "assistant_message_delta":
            contract_payload.setdefault("name", "assistant_message_delta")
            contract_payload["content"] = _coerce_content(contract_payload)
            return "token", contract_payload

        if normalized_name == "assistant_message":
            contract_payload.setdefault("name", "assistant_message")
            contract_payload["content"] = _coerce_content(contract_payload)
            return "token", contract_payload

        if normalized_name == "tool_call_completed":
            tool_name = str(contract_payload.get("tool_name") or "tool")
            contract_payload.setdefault("name", tool_name)
            contract_payload.setdefault("content", "")
            return "tool_end", contract_payload

        if normalized_name == "tool_call_started":
            contract_payload.setdefault("name", "tool_call_started")
            return "tool_start", contract_payload

        if normalized_name in {
            "run_start",
            "run_retry",
            "run_resume",
            "run_finish",
            "run_cancel",
            "run_interrupt",
            "attempt_start",
            "attempt_end",
            "message_finish",
        }:
            contract_payload.setdefault("name", normalized_name)
            contract_payload.setdefault("content", "")
            return "state", contract_payload

        if normalized_name == "run_error":
            contract_payload.setdefault("name", normalized_name)
            contract_payload.setdefault("content", str(contract_payload.get("error") or ""))
            return "error", contract_payload

        if normalized_name.startswith("execution_"):
            contract_payload.setdefault("name", normalized_name)
            if "content" not in contract_payload:
                details = contract_payload.get("error")
                if details is None:
                    contract_payload["content"] = normalized_name
                else:
                    contract_payload["content"] = f"{normalized_name}: {details}"
            else:
                contract_payload["content"] = _coerce_content(contract_payload)
            if normalized_name == "execution_failed":
                return "error", contract_payload
            if normalized_name in {"execution_completed", "execution_cancelled"}:
                return "done", contract_payload
            return "state", contract_payload

        if normalized_name == "agent_runtime_marker":
            contract_payload.setdefault("name", "agent_runtime_marker")
            if "content" not in contract_payload:
                marker_value = contract_payload.get("marker")
                contract_payload["content"] = str(marker_value or "")
            else:
                contract_payload["content"] = _coerce_content(contract_payload)
            return "state", contract_payload

        contract_payload.setdefault("name", normalized_name)
        if "content" not in contract_payload:
            contract_payload["content"] = ""
        else:
            contract_payload["content"] = _coerce_content(contract_payload)
        return "token", contract_payload


def _coerce_content(payload: EventPayload) -> str:
    if "content" in payload:
        return str(payload.get("content") or "")
    if "chunk" in payload:
        return str(payload.get("chunk") or "")
    return ""


def _sanitize_payload(
    payload: EventPayload,
    *,
    execution_id: str,
    trace_id: str | None = None,
    thread_id: str | None = None,
    request_id: str | None = None,
    conversation_id: str | None = None,
) -> EventPayload:
    payload.setdefault("execution_id", execution_id)
    if thread_id is not None:
        payload.setdefault("thread_id", thread_id)
    if request_id is not None:
        payload.setdefault("request_id", request_id)
    if conversation_id is not None:
        payload.setdefault("session_id", conversation_id)
    if trace_id is not None:
        payload.setdefault("trace_id", trace_id)
    payload.setdefault("schema_version", _EVENT_SCHEMA_VERSION)
    return payload


class PersistentAgentEventWriter(AgentEventWriter):
    def __init__(
        self,
        execution_id: str,
        session_bind: Any,
        settings: Any | None = None,
        *,
        trace_id: str | None = None,
        thread_id: str | None = None,
        request_id: str | None = None,
        conversation_id: str | None = None,
        stream_publisher: EventStreamPublisher | None = None,
    ) -> None:
        super().__init__(
            execution_id,
            settings=settings,
            trace_id=trace_id,
            thread_id=thread_id,
            request_id=request_id,
            conversation_id=conversation_id,
        )
        agent_config = settings if settings is not None else get_settings()
        self._stream_publisher = stream_publisher or RedisEventStream.from_settings(agent_config)
        self._session_bind = session_bind
        self._fallback_sequence = 0
        self._streaming_degraded = False
        self._attempt_id: str | None = None
        try:
            with Session(self._session_bind) as session:
                execution = session.get(AgentExecution, self.execution_id)
                if execution is not None:
                    self._streaming_degraded = execution.streaming_degraded
                    self._attempt_id = execution.current_attempt_id
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to load stream state for execution %s", self.execution_id, exc_info=True
            )

    def _write_event(self, event: str, data: EventPayload) -> None:
        timestamp = now_utc()
        data.setdefault("attempt_id", self._attempt_id)
        if self._streaming_degraded:
            return
        if isinstance(self._stream_publisher, RedisEventStream):
            try:
                self._stream_publisher.publish_event(
                    execution_id=self.execution_id,
                    event_type=event,
                    payload=_make_json_payload(data),
                    timestamp=timestamp,
                )
            except EventStreamUnavailable as exc:
                self._streaming_degraded = True
                self._mark_degraded("REDIS_PUBLISH_FAILED", timestamp)
                raise exc
            self._record_first_event(event, timestamp)
            return
        self._fallback_sequence += 1
        self._stream_publisher.publish(
            [
                StreamEvent(
                    sequence=self._fallback_sequence,
                    execution_id=self.execution_id,
                    event_type=event,
                    timestamp=timestamp,
                    payload=_make_json_payload(data),
                )
            ]
        )
        self._record_first_event(event, timestamp)

    def _record_first_event(self, event: str, timestamp: datetime) -> None:
        """Persist timing metadata independently of the Redis projection."""
        try:
            with Session(self._session_bind) as session:
                execution = session.get(AgentExecution, self.execution_id)
                if execution is None:
                    return
                changed = False
                if execution.first_event_at is None:
                    execution.first_event_at = timestamp
                    changed = True
                if event == "token" and execution.first_token_at is None:
                    execution.first_token_at = timestamp
                    changed = True
                if execution.current_attempt_id:
                    attempt = session.get(AgentExecutionAttempt, execution.current_attempt_id)
                    if attempt is not None:
                        if attempt.first_event_at is None:
                            attempt.first_event_at = timestamp
                            changed = True
                        if event == "token" and attempt.first_token_at is None:
                            attempt.first_token_at = timestamp
                            changed = True
                        session.add(attempt)
                if changed:
                    execution.touch_updated_at(timestamp)
                    session.add(execution)
                    session.commit()
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to persist execution event timings: %s", self.execution_id, exc_info=True
            )

    def _mark_degraded(self, reason: str, timestamp: datetime) -> None:
        try:
            with Session(self._session_bind) as session:
                execution = session.get(AgentExecution, self.execution_id)
                if execution is None or execution.streaming_degraded:
                    return
                execution.streaming_degraded = True
                execution.streaming_degraded_at = timestamp
                execution.streaming_degraded_reason = reason[:500]
                execution.touch_updated_at(timestamp)
                session.add(execution)
                session.commit()
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to persist sticky streaming degradation for %s", self.execution_id
            )


def emit_event(
    event: str,
    data: EventPayload | None = None,
    *,
    writer: AgentEventWriter | None = None,
) -> None:
    if writer is None:
        return
    payload = data.copy() if isinstance(data, dict) else {}
    try:
        writer.emit(event, payload)
    except Exception:  # noqa: BLE001
        logger.exception(
            "emit_event failed for execution=%s event=%s",
            getattr(writer, "execution_id", "unknown"),
            event,
        )


__all__ = [
    "AgentEventWriter",
    "PersistentAgentEventWriter",
    "emit_event",
    "now_utc",
    "ClosedWriterError",
]
