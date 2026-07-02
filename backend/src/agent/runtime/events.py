from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from models.base import json_dumps
from models.db import AgentRunEvent
from sqlmodel import Session


def now_utc() -> datetime:
    return datetime.now(UTC)


class AgentEventWriter:
    def __init__(self, run_id: str, session: Session) -> None:
        self.run_id = run_id
        self.session = session

    def emit(self, event: str, data: dict[str, Any]) -> None:
        if not isinstance(event, str) or not event.strip():
            raise ValueError("event must be a non-empty string")
        if not isinstance(data, dict):
            raise TypeError("event data must be a JSON object")

        self.session.add(
            AgentRunEvent(
                run_id=self.run_id,
                event=event.strip(),
                payload=json_dumps(data),
            )
        )
        self.session.commit()


_event_writer: ContextVar[AgentEventWriter | None] = ContextVar(
    "agent_event_writer",
    default=None,
)


def emit_event(event: str, data: dict[str, Any] | None = None) -> None:
    writer = _event_writer.get()
    if writer is None:
        return
    payload = data.copy() if isinstance(data, dict) else {}
    payload.setdefault("run_id", writer.run_id)
    writer.emit(event, payload)


@contextmanager
def event_writer_scope(writer: AgentEventWriter):
    token = _event_writer.set(writer)
    try:
        yield
    finally:
        _event_writer.reset(token)


__all__ = ["AgentEventWriter", "emit_event", "event_writer_scope", "now_utc"]
