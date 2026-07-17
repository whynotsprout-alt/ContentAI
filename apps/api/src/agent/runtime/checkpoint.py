from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from copy import deepcopy
from typing import Any

from core.config import Settings, get_settings
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)


class ExecutionScopedCheckpointer(BaseCheckpointSaver):
    """Persist root graph state in a physical execution namespace."""

    def __init__(self, delegate: Any) -> None:
        super().__init__(serde=delegate.serde)
        self.delegate = delegate

    @staticmethod
    def _physical(config: dict[str, Any]) -> dict[str, Any]:
        mapped = deepcopy(config)
        configurable = mapped.setdefault("configurable", {})
        execution_id = configurable.get("execution_id") or configurable.get("checkpoint_ns")
        if execution_id:
            configurable["execution_id"] = str(execution_id)
            configurable["checkpoint_ns"] = str(execution_id)
        return mapped

    @staticmethod
    def _logical(
        config: dict[str, Any] | None,
        requested: dict[str, Any],
    ) -> dict[str, Any] | None:
        if config is None:
            return None
        mapped = deepcopy(config)
        configurable = mapped.setdefault("configurable", {})
        requested_configurable = requested.get("configurable", {})
        execution_id = requested_configurable.get("execution_id") or requested_configurable.get(
            "checkpoint_ns"
        )
        if execution_id:
            configurable["execution_id"] = str(execution_id)
        configurable["checkpoint_ns"] = str(requested_configurable.get("checkpoint_ns") or "")
        return mapped

    def setup(self) -> None:
        self.delegate.setup()

    def get_tuple(self, config: dict[str, Any]) -> CheckpointTuple | None:
        value = self.delegate.get_tuple(self._physical(config))
        if value is None:
            return None
        return CheckpointTuple(
            self._logical(value.config, config),
            value.checkpoint,
            value.metadata,
            self._logical(value.parent_config, config),
            value.pending_writes,
        )

    def list(
        self,
        config: dict[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        requested = config or {"configurable": {}}
        values = self.delegate.list(
            self._physical(requested) if config is not None else None,
            filter=filter,
            before=self._physical(before) if before is not None else None,
            limit=limit,
        )
        for value in values:
            yield CheckpointTuple(
                self._logical(value.config, requested),
                value.checkpoint,
                value.metadata,
                self._logical(value.parent_config, requested),
                value.pending_writes,
            )

    def put(
        self,
        config: dict[str, Any],
        checkpoint: dict[str, Any],
        metadata: dict[str, Any],
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        stored = self.delegate.put(
            self._physical(config), checkpoint, metadata, new_versions
        )
        logical = self._logical(stored, config)
        if logical is None:
            raise RuntimeError("Checkpoint saver returned no config.")
        return logical

    def put_writes(
        self,
        config: dict[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self.delegate.put_writes(
            self._physical(config), writes, task_id, task_path
        )

    def delete_thread(self, thread_id: str) -> None:
        self.delegate.delete_thread(thread_id)

    def delete_namespace(self, thread_id: str, checkpoint_ns: str) -> None:
        cursor_factory = getattr(self.delegate, "_cursor", None)
        if not callable(cursor_factory):
            raise RuntimeError("Checkpoint saver does not support namespace deletion.")
        with cursor_factory(pipeline=True) as cursor:
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                cursor.execute(
                    f"DELETE FROM {table} WHERE thread_id = %s AND checkpoint_ns = %s",
                    (str(thread_id), str(checkpoint_ns)),
                )

    def get_next_version(self, current: Any, channel: Any) -> Any:
        return self.delegate.get_next_version(current, channel)


def execution_checkpoint_config(
    *,
    thread_id: str,
    checkpoint_ns: str,
    checkpoint_id: str | None = None,
) -> dict[str, dict[str, str]]:
    configurable = {
        "thread_id": thread_id,
        "checkpoint_ns": checkpoint_ns,
        "execution_id": checkpoint_ns,
    }
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


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
        self._checkpointer: ExecutionScopedCheckpointer | None = None

    def start(self) -> None:
        self.get_checkpointer()

    def get_checkpointer(self) -> ExecutionScopedCheckpointer:
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
            self._checkpointer = ExecutionScopedCheckpointer(PostgresSaver(self._pool))
        return self._checkpointer

    def setup(self) -> None:
        """Create/upgrade LangGraph-owned tables from the migration job only."""
        self.get_checkpointer().setup()

    def close(self) -> None:
        self._checkpointer = None
        if self._pool is not None:
            self._pool.close()
            self._pool = None


def checkpoint_messages(
    checkpointer: Any,
    *,
    thread_id: str,
    checkpoint_ns: str,
) -> list[BaseMessage]:
    checkpoint = checkpointer.get_tuple(
        execution_checkpoint_config(thread_id=thread_id, checkpoint_ns=checkpoint_ns)
    )
    if checkpoint is None:
        return []
    values = checkpoint.checkpoint.get("channel_values", {})
    messages = values.get("messages", [])
    return [message for message in messages if isinstance(message, BaseMessage)]


def checkpoint_interrupts(
    checkpointer: Any,
    *,
    thread_id: str,
    checkpoint_ns: str,
) -> list[Any]:
    """Return the pending interrupt tasks from the latest durable checkpoint."""
    checkpoint = checkpointer.get_tuple(
        execution_checkpoint_config(thread_id=thread_id, checkpoint_ns=checkpoint_ns)
    )
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


def clear_execution_persistence(
    *,
    thread_id: str,
    checkpoint_ns: str,
    checkpointer: Any,
) -> None:
    delete_namespace = getattr(checkpointer, "delete_namespace", None)
    if callable(delete_namespace):
        delete_namespace(thread_id, checkpoint_ns)
        return
    cursor_factory = getattr(checkpointer, "_cursor", None)
    if not callable(cursor_factory):
        raise RuntimeError("LangGraph checkpointer does not support namespace deletion.")
    with cursor_factory(pipeline=True) as cursor:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            cursor.execute(
                f"DELETE FROM {table} WHERE thread_id = %s AND checkpoint_ns = %s",
                (str(thread_id), str(checkpoint_ns)),
            )


__all__ = [
    "RuntimePersistence",
    "ExecutionScopedCheckpointer",
    "checkpoint_connection_string",
    "checkpoint_interrupts",
    "checkpoint_messages",
    "clear_thread_persistence",
    "clear_execution_persistence",
    "execution_checkpoint_config",
]
