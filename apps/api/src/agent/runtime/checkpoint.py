from __future__ import annotations

import logging
from typing import Any

from core.config import Settings, get_settings
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)


def checkpoint_connection_string(database_url: str) -> str:
    """Convert a SQLAlchemy PostgreSQL URL into a psycopg connection string."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1).replace(
        "postgresql+psycopg2://", "postgresql://", 1
    )


class RuntimePersistence:
    """Process-safe LangGraph persistence backed by the application PostgreSQL."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._pool: ConnectionPool | None = None
        self._checkpointer: PostgresSaver | None = None

    def start(self) -> None:
        self.get_checkpointer()

    def get_checkpointer(self) -> PostgresSaver:
        if self._checkpointer is None:
            database_url = self.settings.database.url
            if not database_url:
                raise RuntimeError("Database URL is required for checkpoint persistence.")
            pool_size = max(1, int(self.settings.database.pool_size))
            self._pool = ConnectionPool(
                checkpoint_connection_string(database_url),
                min_size=1,
                max_size=pool_size,
                timeout=float(self.settings.database.pool_timeout),
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
                open=True,
                name="contentai-checkpoints",
            )
            self._checkpointer = PostgresSaver(self._pool)
        return self._checkpointer

    def setup(self) -> None:
        """Create/upgrade LangGraph-owned tables from the migration job only."""
        self.get_checkpointer().setup()

    def close(self) -> None:
        self._checkpointer = None
        if self._pool is not None:
            self._pool.close()
            self._pool = None


def checkpoint_messages(checkpointer: Any, *, thread_id: str) -> list[BaseMessage]:
    checkpoint = checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
    if checkpoint is None:
        return []
    values = checkpoint.checkpoint.get("channel_values", {})
    messages = values.get("messages", [])
    return [message for message in messages if isinstance(message, BaseMessage)]


def checkpoint_interrupts(checkpointer: Any, *, thread_id: str) -> list[Any]:
    """Return the pending interrupt tasks from the latest durable checkpoint."""
    checkpoint = checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
    if checkpoint is None:
        return []
    pending: list[Any] = []
    for task in checkpoint.pending_writes or ():
        if len(task) >= 3 and task[1] == "__interrupt__":
            value = task[2]
            pending.extend(value if isinstance(value, list | tuple) else [value])
    if pending:
        return pending
    for task in getattr(checkpoint, "tasks", ()) or ():
        pending.extend(list(getattr(task, "interrupts", ()) or ()))
    return pending


def clear_thread_persistence(*, thread_id: str, checkpointer: Any) -> None:
    delete_thread = getattr(checkpointer, "delete_thread", None)
    if not callable(delete_thread):
        raise RuntimeError("LangGraph checkpointer does not support thread deletion.")
    delete_thread(thread_id)


__all__ = [
    "RuntimePersistence",
    "checkpoint_connection_string",
    "checkpoint_interrupts",
    "checkpoint_messages",
    "clear_thread_persistence",
]
