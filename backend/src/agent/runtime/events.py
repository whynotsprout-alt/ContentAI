from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from core.config import get_settings


def now_utc() -> datetime:
    return datetime.now(UTC)


class AgentEventWriter:
    def __init__(self, execution_id: str) -> None:
        self.execution_id = execution_id
        settings = get_settings()
        self.flush_interval_seconds = max(settings.agent.event_flush_interval_ms, 0) / 1000
        self.flush_max_chars = max(settings.agent.event_flush_max_chars, 1)
        self._pending_delta: dict[str, Any] | None = None
        self._pending_delta_chars = 0
        self._last_delta_flush_at = time.monotonic()
        self._closed = False

    def emit(self, event: str, data: dict[str, Any]) -> None:
        if not isinstance(event, str) or not event.strip():
            raise ValueError("event must be a non-empty string")
        if not isinstance(data, dict):
            raise TypeError("event data must be a JSON object")
        if self._closed:
            raise RuntimeError("event writer is closed")

        event_name = event.strip()
        payload = data.copy()
        payload.setdefault("execution_id", self.execution_id)
        if self._should_buffer_assistant_delta(event_name, payload):
            self._buffer_assistant_delta(payload)
            return

        self.flush()
        contract_event, contract_payload = self._normalize_contract_event(event_name, payload)
        self._write_event(contract_event, contract_payload)

    def flush(self) -> None:
        if self._closed or self._pending_delta is None:
            return
        payload = self._pending_delta
        self._pending_delta = None
        self._pending_delta_chars = 0
        self._last_delta_flush_at = time.monotonic()
        contract_event, contract_payload = self._normalize_contract_event(
            "assistant_message_delta",
            payload,
        )
        self._write_event(contract_event, contract_payload)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.flush()
        finally:
            self._closed = True

    def _write_event(self, _event: str, _data: dict[str, Any]) -> None:
        return None

    @staticmethod
    def _should_buffer_assistant_delta(event: str, data: dict[str, Any]) -> bool:
        if event != "assistant_message_delta":
            return False
        if bool(data.get("done")):
            return False
        return bool(data.get("chunk"))

    @staticmethod
    def _coerce_content(payload: dict[str, Any]) -> str:
        if "content" in payload:
            value = payload.get("content")
            if value is None:
                return ""
            return str(value)
        if "chunk" in payload:
            value = payload.get("chunk")
            if value is None:
                return ""
            return str(value)
        return ""

    def _normalize_contract_event(
        self,
        event_name: str,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        normalized_name = event_name.strip()
        contract_payload = payload.copy()
        contract_payload.setdefault("execution_id", self.execution_id)

        if normalized_name == "assistant_message_delta":
            contract_payload.setdefault("name", "assistant_message_delta")
            contract_payload["content"] = self._coerce_content(contract_payload)
            return "token", contract_payload

        if normalized_name == "assistant_message":
            contract_payload.setdefault("name", "assistant_message")
            contract_payload["content"] = self._coerce_content(contract_payload)
            return "token", contract_payload

        if normalized_name == "tool_call_completed":
            tool_name = str(contract_payload.get("tool_name") or "tool")
            contract_payload.setdefault("name", tool_name)
            contract_payload.setdefault("content", "")
            return "tool", contract_payload

        if normalized_name.startswith("execution_"):
            contract_payload.setdefault("name", normalized_name)
            if "content" not in contract_payload:
                details = contract_payload.get("error")
                if details is None:
                    contract_payload["content"] = normalized_name
                else:
                    contract_payload["content"] = f"{normalized_name}: {details}"
            return "token", contract_payload

        if normalized_name == "agent_runtime_marker":
            contract_payload.setdefault("name", "agent_runtime_marker")
            if "content" not in contract_payload:
                marker_value = contract_payload.get("marker")
                contract_payload["content"] = str(marker_value or "")
            return "token", contract_payload

        contract_payload.setdefault("name", normalized_name)
        if "content" not in contract_payload:
            contract_payload["content"] = ""
        else:
            contract_payload["content"] = self._coerce_content(contract_payload)
        return "token", contract_payload

    def _buffer_assistant_delta(self, data: dict[str, Any]) -> None:
        chunk = str(data.get("chunk") or "")
        if not chunk:
            return
        if self._pending_delta is None:
            self._pending_delta = {**data, "chunk": chunk, "done": False}
            self._pending_delta_chars = len(chunk)
        else:
            pending_type = self._pending_delta.get("message_type")
            next_type = data.get("message_type")
            if pending_type != next_type:
                self.flush()
                self._pending_delta = {**data, "chunk": chunk, "done": False}
                self._pending_delta_chars = len(chunk)
            else:
                self._pending_delta["chunk"] = str(self._pending_delta.get("chunk") or "") + chunk
                self._pending_delta_chars += len(chunk)

        elapsed = time.monotonic() - self._last_delta_flush_at
        if (
            self._pending_delta_chars >= self.flush_max_chars
            or self.flush_interval_seconds == 0
            or elapsed >= self.flush_interval_seconds
        ):
            self.flush()


_event_writer: ContextVar[AgentEventWriter | None] = ContextVar(
    "agent_event_writer",
    default=None,
)


def emit_event(event: str, data: dict[str, Any] | None = None) -> None:
    writer = _event_writer.get()
    if writer is None:
        return
    payload = data.copy() if isinstance(data, dict) else {}
    execution_id = getattr(writer, "execution_id", None)
    if execution_id is not None:
        payload.setdefault("execution_id", execution_id)
    writer.emit(event, payload)


@contextmanager
def event_writer_scope(writer: AgentEventWriter):
    token = _event_writer.set(writer)
    try:
        yield
    finally:
        flush = getattr(writer, "flush", None)
        if callable(flush):
            flush()
        _event_writer.reset(token)


__all__ = ["AgentEventWriter", "emit_event", "event_writer_scope", "now_utc"]
