from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator, MutableMapping, Sequence
from copy import deepcopy
from typing import Any

from core.config import Settings, get_settings
from core.config.database import pool_profile_for_role
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
    def _execution_id(config: dict[str, Any] | None) -> str:
        if not isinstance(config, dict):
            raise ValueError("Checkpoint access requires an execution_id.")
        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            raise ValueError("Checkpoint access requires an execution_id.")
        execution_id = str(configurable.get("execution_id") or "").strip()
        checkpoint_ns = str(configurable.get("checkpoint_ns") or "").strip()
        if not execution_id:
            raise ValueError("Checkpoint access requires an execution_id.")
        if execution_id and checkpoint_ns and checkpoint_ns != execution_id:
            raise ValueError("checkpoint_ns must match execution_id when both are provided.")
        return execution_id

    @classmethod
    def _physical(
        cls,
        config: dict[str, Any],
        *,
        expected_execution_id: str | None = None,
    ) -> dict[str, Any]:
        execution_id = cls._execution_id(config)
        if expected_execution_id is not None and execution_id != expected_execution_id:
            raise ValueError("Checkpoint config crosses execution_id boundaries.")
        mapped = deepcopy(config)
        configurable = mapped.setdefault("configurable", {})
        configurable["execution_id"] = execution_id
        configurable["checkpoint_ns"] = execution_id
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
        execution_id = ExecutionScopedCheckpointer._execution_id(requested)
        configurable["execution_id"] = execution_id
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
        execution_id = self._execution_id(config)
        assert config is not None
        requested = config
        values = self.delegate.list(
            self._physical(requested),
            filter=filter,
            before=(
                self._physical(before, expected_execution_id=execution_id)
                if before is not None
                else None
            ),
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
        raise ValueError(
            "Thread-wide deletion is forbidden on an execution-scoped checkpointer."
        )

    def delete_thread_maintenance(self, thread_id: str) -> None:
        """Explicit maintenance operation used only when deleting a whole session."""
        self.delegate.delete_thread(thread_id)

    def delete_namespace(self, thread_id: str, checkpoint_ns: str) -> None:
        thread_id = str(thread_id).strip()
        checkpoint_ns = str(checkpoint_ns).strip()
        if not thread_id or not checkpoint_ns:
            raise ValueError("Namespace deletion requires thread_id and execution_id.")
        delegate_delete_namespace = getattr(self.delegate, "delete_namespace", None)
        if callable(delegate_delete_namespace):
            delegate_delete_namespace(thread_id, checkpoint_ns)
            return
        cursor_factory = getattr(self.delegate, "_cursor", None)
        if callable(cursor_factory):
            with cursor_factory(pipeline=True) as cursor:
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    cursor.execute(
                        f"DELETE FROM {table} WHERE thread_id = %s AND checkpoint_ns = %s",
                        (thread_id, checkpoint_ns),
                    )
            return
        storage = getattr(self.delegate, "storage", None)
        writes = getattr(self.delegate, "writes", None)
        blobs = getattr(self.delegate, "blobs", None)
        if not all(isinstance(value, MutableMapping) for value in (storage, writes, blobs)):
            raise RuntimeError("Checkpoint saver does not support namespace deletion.")
        thread_storage = storage.get(thread_id)
        if isinstance(thread_storage, MutableMapping):
            thread_storage.pop(checkpoint_ns, None)
            if not thread_storage:
                storage.pop(thread_id, None)
        for collection in (writes, blobs):
            for key in list(collection):
                if len(key) >= 2 and key[0] == thread_id and key[1] == checkpoint_ns:
                    del collection[key]

    async def aget_tuple(self, config: dict[str, Any]) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: dict[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        values = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for value in values:
            yield value

    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: dict[str, Any],
        metadata: dict[str, Any],
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.put,
            config,
            checkpoint,
            metadata,
            new_versions,
        )

    async def aput_writes(
        self,
        config: dict[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        raise ValueError(
            "Thread-wide deletion is forbidden on an execution-scoped checkpointer."
        )

    def get_next_version(self, current: Any, channel: Any) -> Any:
        return self.delegate.get_next_version(current, channel)


def execution_checkpoint_config(
    *,
    thread_id: str,
    execution_id: str,
    checkpoint_id: str | None = None,
) -> dict[str, dict[str, str]]:
    configurable = {
        "thread_id": thread_id,
        "checkpoint_ns": "",
        "execution_id": execution_id,
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
        if self.settings.database.runtime_role == "agent-worker":
            self.get_checkpointer()

    def get_checkpointer(self) -> ExecutionScopedCheckpointer:
        if self._checkpointer is None:
            database_url = self.settings.database.url
            if not database_url:
                raise RuntimeError("Database URL is required for checkpoint persistence.")
            profile = pool_profile_for_role(self.settings.database.runtime_role)
            pool_size = max(1, profile.checkpoint_pool_size)
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
    execution_id: str,
) -> list[BaseMessage]:
    checkpoint = checkpointer.get_tuple(
        execution_checkpoint_config(thread_id=thread_id, execution_id=execution_id)
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
    execution_id: str,
) -> list[Any]:
    """Return the pending interrupt tasks from the latest durable checkpoint."""
    checkpoint = checkpointer.get_tuple(
        execution_checkpoint_config(thread_id=thread_id, execution_id=execution_id)
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
    delete_thread = getattr(checkpointer, "delete_thread_maintenance", None)
    if not callable(delete_thread):
        delete_thread = getattr(checkpointer, "delete_thread", None)
    if not callable(delete_thread):
        raise RuntimeError("LangGraph checkpointer does not support thread deletion.")
    delete_thread(thread_id)


def clear_execution_persistence(
    *,
    thread_id: str,
    execution_id: str,
    checkpointer: Any,
) -> None:
    delete_namespace = getattr(checkpointer, "delete_namespace", None)
    if callable(delete_namespace):
        delete_namespace(thread_id, execution_id)
        return
    cursor_factory = getattr(checkpointer, "_cursor", None)
    if not callable(cursor_factory):
        raise RuntimeError("LangGraph checkpointer does not support namespace deletion.")
    with cursor_factory(pipeline=True) as cursor:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            cursor.execute(
                f"DELETE FROM {table} WHERE thread_id = %s AND checkpoint_ns = %s",
                (str(thread_id), str(execution_id)),
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
