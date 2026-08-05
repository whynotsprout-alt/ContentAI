from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from enum import Enum
from threading import RLock, Timer
from time import monotonic
from typing import Any

from contentai.core.config import get_settings
from contentai.models.chat import AgentExecution, AgentExecutionAttempt
from contentai.models.enums import RunStatus
from contentai.services.event_stream import (
    EventStreamClosed,
    EventStreamPayloadInvalid,
    EventStreamPublisher,
    EventStreamPublishExpired,
    EventStreamPublishGap,
    EventStreamUnavailable,
    RedisEventStream,
    StreamEvent,
)
from contentai.services.execution_settlement import (
    apply_streaming_degradation_first_wins,
    current_database_time,
    mark_streaming_degraded_first_wins,
)
from sqlmodel import Session, select

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
_COMPLETION_DRAIN_MARKERS = frozenset(
    {"persist_messages_ms", "persist_assistant_fallback_ms"}
)
_TERMINAL_PHASE_EVENTS: dict[RunStatus, frozenset[tuple[str, str]]] = {
    RunStatus.waiting_input: frozenset(
        {
            ("state", "run_interrupt"),
        }
    ),
    RunStatus.completed: frozenset(
        {
            ("state", "message_finish"),
            ("state", "run_finish"),
        }
    ),
    RunStatus.failed: frozenset(
        {
            ("error", "run_error"),
        }
    ),
    RunStatus.cancelled: frozenset(
        {
            ("state", "run_cancel"),
        }
    ),
}
_TERMINAL_CLOSE_EVENTS: dict[RunStatus, tuple[str, str]] = {
    RunStatus.waiting_input: ("state", "run_interrupt"),
    RunStatus.completed: ("state", "run_finish"),
    RunStatus.failed: ("error", "run_error"),
    RunStatus.cancelled: ("state", "run_cancel"),
}


class ClosedWriterError(RuntimeError):
    """Raised when operations are attempted on a closed event writer."""


class _EventPublishAuthorization(Enum):
    allowed = "allowed"
    reject_event = "reject_event"
    reject_fence = "reject_fence"
    reject_degraded = "reject_degraded"


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
        self._completion_prepared = False
        self._completion_drain_open = False
        self._completion_drained = False
        self._terminal_tokens_closed = False

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
            if self._completion_prepared or self._terminal_tokens_closed:
                return
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

    def _flush_pending_delta_locked(
        self,
        *,
        settlement_drain: bool = False,
        db_session: Session | None = None,
    ) -> None:
        if self._pending_delta is None:
            return
        if (
            self._completion_prepared or self._terminal_tokens_closed
        ) and not settlement_drain:
            return
        payload = self._pending_delta
        self._pending_delta = None
        self._pending_delta_started_at = 0.0
        self._cancel_pending_delta_timer_locked()
        if settlement_drain:
            self._emit_now_strict(
                "assistant_message_delta",
                payload,
                close_stream=False,
                db_session=db_session,
            )
        else:
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
            if (
                self._closed
                or self._closing
                or self._completion_prepared
                or self._terminal_tokens_closed
            ):
                return
            self._flush_pending_delta_locked()

    def flush(self) -> None:
        with self._batch_lock:
            if self._closed and not self._closing:
                raise ClosedWriterError(f"Event writer is closed: execution_id={self.execution_id}")
            if self._completion_prepared or self._terminal_tokens_closed:
                return
            self._flush_pending_delta_locked()

    def prepare_settlement(self) -> None:
        """Atomically close ordinary publishing and freeze the final assistant tail."""
        with self._batch_lock:
            if self._closed or self._closing:
                raise ClosedWriterError(
                    f"Event writer is closed: execution_id={self.execution_id}"
                )
            if self._completion_prepared or self._completion_drained:
                return
            self._completion_prepared = True
            self._cancel_pending_delta_timer_locked()

    def prepare_completion(self) -> None:
        """Backward-compatible alias for callers using the original completion name."""
        self.prepare_settlement()

    def drain_settlement(
        self,
        events: list[tuple[str, EventPayload]],
        *,
        db_session: Session | None = None,
    ) -> None:
        """Publish a frozen tail and strict terminal event set exactly once."""
        with self._batch_lock:
            if self._closed or self._closing:
                raise ClosedWriterError(
                    f"Event writer is closed: execution_id={self.execution_id}"
                )
            if not self._completion_prepared:
                raise RuntimeError("Settlement drain was not prepared.")
            if self._completion_drained:
                return
            if not events:
                raise ValueError("Settlement drain requires terminal events.")
            self._completion_drain_open = True
            try:
                self._flush_pending_delta_locked(
                    settlement_drain=True,
                    db_session=db_session,
                )
                self._active_delta_message_id = None
                last_index = len(events) - 1
                for index, (event_name, raw_payload) in enumerate(events):
                    if not isinstance(event_name, str) or not event_name.strip():
                        raise ValueError("event must be a non-empty string")
                    if not isinstance(raw_payload, dict):
                        raise TypeError("event data must be a JSON object")
                    payload = _sanitize_payload(
                        dict(raw_payload),
                        execution_id=self.execution_id,
                        trace_id=self.trace_id,
                        thread_id=self.thread_id,
                        request_id=self.request_id,
                        conversation_id=self.conversation_id,
                    )
                    self._emit_now_strict(
                        event_name.strip(),
                        payload,
                        close_stream=index == last_index,
                        db_session=db_session,
                    )
                self._completion_drained = True
            finally:
                self._completion_drain_open = False
                self._terminal_tokens_closed = True
                self._completion_prepared = not self._completion_drained

    def drain_completion(
        self,
        events: list[tuple[str, EventPayload]],
        *,
        db_session: Session | None = None,
    ) -> None:
        """Backward-compatible alias for the settlement drain protocol."""
        self.drain_settlement(events, db_session=db_session)

    def close(self) -> None:
        with self._batch_lock:
            if self._closed:
                return
            self._closing = True
            try:
                if self._completion_prepared:
                    self._pending_delta = None
                    self._pending_delta_started_at = 0.0
                    self._cancel_pending_delta_timer_locked()
                else:
                    self._flush_pending_delta_locked()
            finally:
                try:
                    self._close_writer()
                finally:
                    self._closing = False
                    self._closed = True

    def _write_event(self, event: str, payload: EventPayload) -> None:
        raise NotImplementedError

    def _write_event_strict(
        self,
        event: str,
        payload: EventPayload,
        *,
        close_stream: bool,
    ) -> None:
        _ = close_stream
        self._write_event(event, payload)

    def _write_event_strict_in_session(
        self,
        db_session: Session,
        event: str,
        payload: EventPayload,
        *,
        close_stream: bool,
    ) -> None:
        _ = db_session
        self._write_event_strict(event, payload, close_stream=close_stream)

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

    def _emit_now_strict(
        self,
        event_name: str,
        payload: EventPayload,
        *,
        close_stream: bool,
        db_session: Session | None = None,
    ) -> None:
        contract_event, contract_payload = self._normalize_contract_event(
            event_name,
            payload,
        )
        if db_session is None:
            self._write_event_strict(
                contract_event,
                contract_payload,
                close_stream=close_stream,
            )
        else:
            self._write_event_strict_in_session(
                db_session,
                contract_event,
                contract_payload,
                close_stream=close_stream,
            )
        self._emit_realtime(contract_event, contract_payload)

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
            "memory_extracted",
            "memory_extraction_failed",
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
        expected_worker_id: str | None = None,
        expected_attempt_id: str | None = None,
        postprocess: bool = False,
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
        publisher_factory = getattr(
            RedisEventStream,
            "publisher_from_settings",
            RedisEventStream.from_settings,
        )
        self._stream_publisher = stream_publisher or publisher_factory(agent_config)
        self._session_bind = session_bind
        self._fallback_sequence = 0
        self._streaming_degraded = False
        self._fence_rejected = False
        self._postprocess = bool(postprocess)
        if (expected_worker_id is None) != (expected_attempt_id is None):
            raise ValueError("Event writer worker and attempt fences must be provided together.")
        self._expected_worker_id = expected_worker_id
        self._expected_attempt_id = expected_attempt_id
        self._attempt_id: str | None = expected_attempt_id
        try:
            with Session(self._session_bind) as session:
                execution = session.get(AgentExecution, self.execution_id)
                if execution is not None:
                    self._streaming_degraded = execution.streaming_degraded
                    if self._attempt_id is None:
                        self._attempt_id = execution.current_attempt_id
                    if (
                        not self._postprocess
                        and self._expected_worker_id is None
                        and getattr(execution, "worker_id", None)
                        and getattr(execution, "current_attempt_id", None)
                    ):
                        self._expected_worker_id = str(execution.worker_id)
                        self._expected_attempt_id = str(execution.current_attempt_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to load stream state for execution %s", self.execution_id, exc_info=True
            )

    def _write_event(self, event: str, data: EventPayload) -> None:
        self._write_event_internal(event, data, close_stream=False, strict=False)

    def _write_event_strict(
        self,
        event: str,
        payload: EventPayload,
        *,
        close_stream: bool,
    ) -> None:
        self._write_event_internal(event, payload, close_stream=close_stream, strict=True)

    def _write_event_strict_in_session(
        self,
        db_session: Session,
        event: str,
        payload: EventPayload,
        *,
        close_stream: bool,
    ) -> None:
        self._write_event_internal(
            event,
            payload,
            close_stream=close_stream,
            strict=True,
            db_session=db_session,
        )

    def _write_event_internal(
        self,
        event: str,
        data: EventPayload,
        *,
        close_stream: bool,
        strict: bool,
        db_session: Session | None = None,
    ) -> None:
        timestamp = now_utc()
        if self._expected_attempt_id is not None:
            data["attempt_id"] = self._expected_attempt_id
        else:
            data.setdefault("attempt_id", self._attempt_id)
        if self._streaming_degraded:
            if strict:
                raise EventStreamUnavailable("Execution streaming is degraded")
            return
        if self._fence_rejected:
            if strict:
                raise EventStreamUnavailable("Execution event fence was rejected")
            return
        if self._postprocess:
            if strict:
                raise EventStreamClosed(
                    "Postprocess events cannot publish to the closed execution stream"
                )
            return
        if self._expected_worker_id is not None and self._expected_attempt_id is not None:
            self._write_fenced_event(
                event,
                data,
                timestamp,
                close_stream=close_stream,
                strict=strict,
                db_session=db_session,
            )
            return
        if db_session is not None:
            raise EventStreamUnavailable(
                "Unfenced writers cannot publish inside a settlement transaction"
            )
        self._write_unfenced_event(
            event,
            data,
            timestamp,
            close_stream=close_stream,
            strict=strict,
        )

    def _publish_event(
        self,
        event: str,
        data: EventPayload,
        timestamp: datetime,
        *,
        expected_sequence: int,
        allow_initialize: bool,
        close_stream: bool,
        terminal_attempt_id: str | None,
        terminal_event_name: str | None,
        terminal_status: str | None,
        allow_reopen: bool,
        reopen_attempt_id: str | None,
        reopen_status: str | None,
    ) -> int:
        try:
            json_payload = _make_json_payload(data)
        except (RecursionError, TypeError, ValueError) as exc:
            raise EventStreamPayloadInvalid("Invalid execution event payload") from exc
        if isinstance(self._stream_publisher, RedisEventStream):
            return self._stream_publisher.publish_event(
                execution_id=self.execution_id,
                event_type=event,
                payload=json_payload,
                timestamp=timestamp,
                expected_sequence=expected_sequence,
                allow_initialize=allow_initialize,
                close_stream=close_stream,
                terminal_attempt_id=terminal_attempt_id,
                terminal_event_name=terminal_event_name,
                terminal_status=terminal_status,
                allow_reopen=allow_reopen,
                reopen_attempt_id=reopen_attempt_id,
                reopen_status=reopen_status,
            )
        self._fallback_sequence = expected_sequence + 1
        self._stream_publisher.publish(
            [
                StreamEvent(
                    sequence=self._fallback_sequence,
                    execution_id=self.execution_id,
                    event_type=event,
                    timestamp=timestamp,
                    payload=json_payload,
                )
            ]
        )
        return self._fallback_sequence

    def _write_fenced_event(
        self,
        event: str,
        data: EventPayload,
        timestamp: datetime,
        *,
        close_stream: bool,
        strict: bool,
        db_session: Session | None = None,
    ) -> None:
        assert self._expected_worker_id is not None
        assert self._expected_attempt_id is not None
        published_sequence: int | None = None
        owns_session = db_session is None
        try:
            session_context = (
                Session(self._session_bind)
                if db_session is None
                else nullcontext(db_session)
            )
            with session_context as session:
                execution = session.exec(
                    select(AgentExecution)
                    .where(AgentExecution.id == self.execution_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).one_or_none()
                if execution is None or execution.current_attempt_id != self._expected_attempt_id:
                    if owns_session:
                        session.rollback()
                    self._fence_rejected = True
                    if strict:
                        raise EventStreamUnavailable("Execution event identity fence rejected")
                    return
                if execution.streaming_degraded:
                    if owns_session:
                        session.rollback()
                    self._streaming_degraded = True
                    if strict:
                        raise EventStreamUnavailable(
                            execution.streaming_degraded_reason or "Execution streaming is degraded"
                        )
                    return
                attempt = session.exec(
                    select(AgentExecutionAttempt)
                    .where(AgentExecutionAttempt.id == self._expected_attempt_id)
                    .where(AgentExecutionAttempt.execution_id == self.execution_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).one_or_none()
                if attempt is None or attempt.worker_id != self._expected_worker_id:
                    if owns_session:
                        session.rollback()
                    self._fence_rejected = True
                    if strict:
                        raise EventStreamUnavailable("Execution attempt event fence rejected")
                    return
                settled_waiting_drain = bool(
                    self._completion_drain_open
                    and execution.worker_id is None
                    and execution.status in {RunStatus.waiting_input, RunStatus.pending}
                    and attempt.status.value == "waiting_input"
                    and attempt.finished_at is not None
                )
                if (
                    execution.worker_id != self._expected_worker_id
                    and not settled_waiting_drain
                ):
                    if owns_session:
                        session.rollback()
                    self._fence_rejected = True
                    if strict:
                        raise EventStreamUnavailable("Execution event identity fence rejected")
                    return
                if (
                    self._completion_drain_open
                    and execution.status in _TERMINAL_PHASE_EVENTS
                    and (
                        attempt.status.value != execution.status.value
                        or attempt.finished_at is None
                    )
                ):
                    if owns_session:
                        session.rollback()
                    self._fence_rejected = True
                    if strict:
                        raise EventStreamUnavailable(
                            "Execution terminal attempt fence rejected"
                        )
                    return
                authorization_time = current_database_time(session)
                settlement_status = (
                    RunStatus.waiting_input if settled_waiting_drain else execution.status
                )
                expected_sequence = int(execution.stream_committed_sequence)
                event_name = str(data.get("name") or "")
                allow_reopen = bool(
                    not close_stream
                    and execution.status in {RunStatus.pending, RunStatus.running}
                    and event == "state"
                    and event_name in {"run_resume", "run_retry"}
                    and execution.terminal_stream_sequence == expected_sequence
                    and execution.terminal_stream_status == "waiting_input"
                    and execution.terminal_stream_attempt_id
                    and execution.terminal_stream_attempt_id != self._expected_attempt_id
                )
                authorization = self._event_phase_allows_publish(
                    execution,
                    event=event,
                    data=data,
                    authorization_time=authorization_time,
                    completion_drain_open=self._completion_drain_open,
                    close_stream=close_stream,
                    settlement_status=settlement_status,
                    allow_prior_waiting_reopen=allow_reopen,
                )
                if authorization is not _EventPublishAuthorization.allowed:
                    if owns_session:
                        session.rollback()
                    if authorization is _EventPublishAuthorization.reject_fence:
                        self._fence_rejected = True
                    elif authorization is _EventPublishAuthorization.reject_degraded:
                        self._streaming_degraded = True
                    if strict:
                        raise EventStreamUnavailable(
                            f"Execution event publication rejected: {authorization.value}"
                        )
                    return
                try:
                    if close_stream:
                        data["terminal_status"] = settlement_status.value
                    published_sequence = self._publish_event(
                        event,
                        data,
                        timestamp,
                        expected_sequence=expected_sequence,
                        allow_initialize=(
                            expected_sequence == 0 and execution.first_event_at is None
                        ),
                        close_stream=close_stream,
                        terminal_attempt_id=self._expected_attempt_id,
                        terminal_event_name=event_name if close_stream else None,
                        terminal_status=settlement_status.value if close_stream else None,
                        allow_reopen=allow_reopen,
                        reopen_attempt_id=execution.terminal_stream_attempt_id,
                        reopen_status=execution.terminal_stream_status,
                    )
                except EventStreamPublishExpired:
                    self._streaming_degraded = True
                    apply_streaming_degradation_first_wins(
                        execution,
                        reason="STREAM_REPLAY_EXPIRED",
                        now=authorization_time,
                    )
                    session.add(execution)
                    if owns_session:
                        session.commit()
                    raise
                except (EventStreamPublishGap, EventStreamClosed):
                    self._streaming_degraded = True
                    apply_streaming_degradation_first_wins(
                        execution,
                        reason="STREAM_REPLAY_GAP",
                        now=authorization_time,
                    )
                    session.add(execution)
                    if owns_session:
                        session.commit()
                    raise
                except EventStreamPayloadInvalid:
                    self._streaming_degraded = True
                    apply_streaming_degradation_first_wins(
                        execution,
                        reason="EVENT_PAYLOAD_INVALID",
                        now=authorization_time,
                    )
                    session.add(execution)
                    if owns_session:
                        session.commit()
                    raise
                except EventStreamUnavailable:
                    self._streaming_degraded = True
                    apply_streaming_degradation_first_wins(
                        execution,
                        reason="REDIS_PUBLISH_FAILED",
                        now=authorization_time,
                    )
                    session.add(execution)
                    if owns_session:
                        session.commit()
                    raise

                if published_sequence not in {
                    expected_sequence,
                    expected_sequence + 1,
                } or (
                    published_sequence == expected_sequence
                    and (
                        not close_stream
                        or execution.terminal_stream_sequence != expected_sequence
                    )
                ):
                    self._streaming_degraded = True
                    apply_streaming_degradation_first_wins(
                        execution,
                        reason="STREAM_REPLAY_GAP",
                        now=authorization_time,
                    )
                    session.add(execution)
                    if owns_session:
                        session.commit()
                    raise EventStreamPublishGap(
                        "Redis publication sequence did not advance from the database watermark"
                    )
                if published_sequence == expected_sequence:
                    if owns_session:
                        session.rollback()
                    return

                changed = False
                execution.stream_committed_sequence = published_sequence
                if close_stream:
                    execution.terminal_stream_sequence = published_sequence
                    execution.terminal_stream_attempt_id = self._expected_attempt_id
                    execution.terminal_stream_status = settlement_status.value
                elif allow_reopen:
                    execution.terminal_stream_sequence = None
                    execution.terminal_stream_attempt_id = None
                    execution.terminal_stream_status = None
                changed = True
                if execution.first_event_at is None:
                    execution.first_event_at = timestamp
                    changed = True
                if event == "token" and execution.first_token_at is None:
                    execution.first_token_at = timestamp
                    changed = True
                if attempt.first_event_at is None:
                    attempt.first_event_at = timestamp
                    changed = True
                if event == "token" and attempt.first_token_at is None:
                    attempt.first_token_at = timestamp
                    changed = True
                if changed:
                    execution.touch_updated_at(authorization_time)
                    session.add(execution)
                    session.add(attempt)
                if owns_session:
                    session.commit()
        except (EventStreamUnavailable, EventStreamPayloadInvalid):
            raise
        except Exception:  # noqa: BLE001
            self._streaming_degraded = True
            logger.warning(
                "Failed to persist fenced execution event: %s",
                self.execution_id,
                exc_info=True,
            )
            reason = (
                "STREAM_COMMIT_WATERMARK_MISMATCH"
                if published_sequence is not None
                else "EVENT_TIMING_PERSIST_FAILED"
            )
            if db_session is None:
                self._mark_degraded(reason)
            else:
                try:
                    execution = db_session.exec(
                        select(AgentExecution)
                        .where(AgentExecution.id == self.execution_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    ).one_or_none()
                    if execution is not None:
                        apply_streaming_degradation_first_wins(
                            execution,
                            reason=reason,
                            now=current_database_time(db_session),
                        )
                        db_session.add(execution)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Failed to mark in-transaction event degradation for %s",
                        self.execution_id,
                    )
            if strict:
                raise EventStreamUnavailable(reason) from None

    def _write_unfenced_event(
        self,
        event: str,
        data: EventPayload,
        timestamp: datetime,
        *,
        close_stream: bool,
        strict: bool,
    ) -> None:
        if close_stream:
            raise EventStreamUnavailable("Unfenced writers cannot close execution streams")
        published_sequence: int | None = None
        try:
            with Session(self._session_bind) as session:
                execution = session.exec(
                    select(AgentExecution)
                    .where(AgentExecution.id == self.execution_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).one_or_none()
                if execution is None:
                    session.rollback()
                    return
                if execution.streaming_degraded:
                    session.rollback()
                    self._streaming_degraded = True
                    if strict:
                        raise EventStreamUnavailable(
                            execution.streaming_degraded_reason or "Execution streaming is degraded"
                        )
                    return
                if execution.terminal_stream_sequence is not None:
                    session.rollback()
                    if strict:
                        raise EventStreamClosed("Execution stream is closed")
                    return
                authorization_time = current_database_time(session)
                expected_sequence = int(execution.stream_committed_sequence)
                try:
                    published_sequence = self._publish_event(
                        event,
                        data,
                        timestamp,
                        expected_sequence=expected_sequence,
                        allow_initialize=(
                            expected_sequence == 0 and execution.first_event_at is None
                        ),
                        close_stream=False,
                        terminal_attempt_id=None,
                        terminal_event_name=None,
                        terminal_status=None,
                        allow_reopen=False,
                        reopen_attempt_id=None,
                        reopen_status=None,
                    )
                except EventStreamPublishExpired:
                    reason = "STREAM_REPLAY_EXPIRED"
                    apply_streaming_degradation_first_wins(
                        execution, reason=reason, now=authorization_time
                    )
                    session.add(execution)
                    session.commit()
                    self._streaming_degraded = True
                    raise
                except (EventStreamPublishGap, EventStreamClosed):
                    reason = "STREAM_REPLAY_GAP"
                    apply_streaming_degradation_first_wins(
                        execution, reason=reason, now=authorization_time
                    )
                    session.add(execution)
                    session.commit()
                    self._streaming_degraded = True
                    raise
                except EventStreamPayloadInvalid:
                    reason = "EVENT_PAYLOAD_INVALID"
                    apply_streaming_degradation_first_wins(
                        execution, reason=reason, now=authorization_time
                    )
                    session.add(execution)
                    session.commit()
                    self._streaming_degraded = True
                    raise
                except EventStreamUnavailable:
                    reason = "REDIS_PUBLISH_FAILED"
                    apply_streaming_degradation_first_wins(
                        execution, reason=reason, now=authorization_time
                    )
                    session.add(execution)
                    session.commit()
                    self._streaming_degraded = True
                    raise
                if published_sequence != expected_sequence + 1:
                    self._streaming_degraded = True
                    apply_streaming_degradation_first_wins(
                        execution,
                        reason="STREAM_REPLAY_GAP",
                        now=authorization_time,
                    )
                    session.add(execution)
                    session.commit()
                    raise EventStreamPublishGap(
                        "Redis publication sequence did not advance from the database watermark"
                    )
                execution.stream_committed_sequence = published_sequence
                if execution.first_event_at is None:
                    execution.first_event_at = timestamp
                execution.touch_updated_at(authorization_time)
                session.add(execution)
                session.commit()
        except (EventStreamUnavailable, EventStreamPayloadInvalid):
            if strict:
                raise
        except Exception:  # noqa: BLE001
            self._streaming_degraded = True
            reason = (
                "STREAM_COMMIT_WATERMARK_MISMATCH"
                if published_sequence is not None
                else "EVENT_TIMING_PERSIST_FAILED"
            )
            self._mark_degraded(reason)
            if strict:
                raise EventStreamUnavailable(reason) from None

    @staticmethod
    def _event_phase_allows_publish(
        execution: AgentExecution,
        *,
        event: str,
        data: EventPayload,
        authorization_time: datetime,
        completion_drain_open: bool = False,
        close_stream: bool = False,
        settlement_status: RunStatus | None = None,
        allow_prior_waiting_reopen: bool = False,
    ) -> _EventPublishAuthorization:
        phase_status = execution.status if settlement_status is None else settlement_status
        if phase_status in {RunStatus.pending, RunStatus.running}:
            if execution.cancel_requested_at is not None:
                return _EventPublishAuthorization.reject_event
            if (
                execution.lease_expires_at is None
                or execution.lease_expires_at <= authorization_time
            ):
                return _EventPublishAuthorization.reject_fence
            return _EventPublishAuthorization.allowed
        event_name = str(data.get("name") or "")
        if not completion_drain_open:
            return _EventPublishAuthorization.reject_event
        if (
            execution.terminal_stream_sequence is not None
            and not allow_prior_waiting_reopen
        ):
            return _EventPublishAuthorization.reject_event
        if phase_status in _TERMINAL_PHASE_EVENTS:
            if event == "token" and event_name in {
                "assistant_message_delta",
                "assistant_message",
            }:
                if close_stream:
                    return _EventPublishAuthorization.reject_event
                return _EventPublishAuthorization.allowed
            if (
                phase_status == RunStatus.completed
                and
                event == "state"
                and event_name == "agent_runtime_marker"
                and data.get("marker") in _COMPLETION_DRAIN_MARKERS
            ):
                if close_stream:
                    return _EventPublishAuthorization.reject_event
                return _EventPublishAuthorization.allowed
        if event_name == "attempt_end":
            allowed = (
                not close_stream
                and event == "state"
                and data.get("status") == phase_status.value
            )
            return (
                _EventPublishAuthorization.allowed
                if allowed
                else _EventPublishAuthorization.reject_event
            )
        if phase_status not in _TERMINAL_PHASE_EVENTS:
            return _EventPublishAuthorization.reject_fence
        event_identity = (event, event_name)
        allowed = event_identity in _TERMINAL_PHASE_EVENTS.get(
            phase_status,
            frozenset(),
        ) and close_stream == (
            event_identity == _TERMINAL_CLOSE_EVENTS.get(phase_status)
        )
        return (
            _EventPublishAuthorization.allowed
            if allowed
            else _EventPublishAuthorization.reject_event
        )

    def _mark_degraded(self, reason: str) -> None:
        try:
            mark_streaming_degraded_first_wins(
                self._session_bind,
                self.execution_id,
                reason,
                expected_worker_id=self._expected_worker_id,
                expected_attempt_id=self._expected_attempt_id,
            )
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
