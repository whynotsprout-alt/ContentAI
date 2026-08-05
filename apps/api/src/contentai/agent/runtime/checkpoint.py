from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator, MutableMapping, Sequence
from copy import deepcopy
from datetime import datetime
from typing import Any

from contentai.core.config import Settings, get_settings
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

_FENCE_WORKER_KEY = "__worker_id"
_FENCE_ATTEMPT_KEY = "__attempt_id"


class CheckpointWriteFenceError(RuntimeError):
    """The graph writer no longer owns the execution attempt or checkpoint head."""


class CheckpointOwnershipLostError(CheckpointWriteFenceError):
    """The worker, attempt, cancellation, status, or lease fence is no longer active."""


class CheckpointSupersededError(CheckpointWriteFenceError):
    """A checkpoint put was based on a business head that has already advanced."""


class CheckpointStorageIntegrityError(CheckpointWriteFenceError):
    """Checkpoint storage or its business revision violated an invariant."""


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
        for key in (_FENCE_WORKER_KEY, _FENCE_ATTEMPT_KEY):
            if key in requested_configurable:
                configurable[key] = requested_configurable[key]
        return mapped

    @staticmethod
    def _required_fence(config: dict[str, Any]) -> tuple[str, str]:
        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            raise CheckpointWriteFenceError("CHECKPOINT_WRITE_FENCE_INVALID")
        values: list[str] = []
        for key in (_FENCE_WORKER_KEY, _FENCE_ATTEMPT_KEY):
            value = configurable.get(key)
            if not isinstance(value, str) or not value or value != value.strip():
                raise CheckpointWriteFenceError("CHECKPOINT_WRITE_FENCE_INVALID")
            values.append(value)
        return values[0], values[1]

    @staticmethod
    def _requested_checkpoint_id(config: dict[str, Any]) -> str | None:
        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            raise CheckpointWriteFenceError("CHECKPOINT_WRITE_FENCE_INVALID")
        value = configurable.get("checkpoint_id")
        if value is None:
            return None
        if not isinstance(value, str) or not value or value != value.strip():
            raise CheckpointWriteFenceError("CHECKPOINT_WRITE_FENCE_INVALID")
        return value

    def _connection_pool(self) -> ConnectionPool | None:
        connection_source = getattr(self.delegate, "conn", None)
        return connection_source if isinstance(connection_source, ConnectionPool) else None

    def _fenced_write(
        self,
        config: dict[str, Any],
        *,
        next_checkpoint_id: str | None,
        advance_head: bool,
        write: Any,
    ) -> Any:
        pool = self._connection_pool()
        if pool is None:
            return write(self.delegate)

        execution_id = self._execution_id(config)
        worker_id, attempt_id = self._required_fence(config)
        requested_checkpoint_id = self._requested_checkpoint_id(config)
        physical = self._physical(config)
        configurable = physical["configurable"]
        thread_id = str(configurable.get("thread_id") or "").strip()
        if not thread_id:
            raise CheckpointWriteFenceError("CHECKPOINT_WRITE_FENCE_INVALID")

        with pool.connection() as connection, connection.transaction():
            row = connection.execute(
                """
                SELECT latest_checkpoint_id, checkpoint_revision, status,
                       worker_id, current_attempt_id, cancel_requested_at,
                       lease_expires_at
                FROM agentexecution
                WHERE id = %s
                FOR UPDATE
                """,
                (execution_id,),
            ).fetchone()
            if row is None:
                raise CheckpointOwnershipLostError("CHECKPOINT_WRITE_FENCE_REJECTED")
            authorization_time = connection.execute(
                "SELECT clock_timestamp() AS authorization_time"
            ).fetchone()["authorization_time"]
            if (
                row["status"] != "running"
                or row["worker_id"] != worker_id
                or row["current_attempt_id"] != attempt_id
                or row["cancel_requested_at"] is not None
                or row["lease_expires_at"] is None
                or row["lease_expires_at"] <= authorization_time
            ):
                raise CheckpointOwnershipLostError("CHECKPOINT_WRITE_FENCE_REJECTED")

            stored_head = row["latest_checkpoint_id"]
            revision = int(row["checkpoint_revision"])
            effective_head = stored_head
            writes_bootstrap_head: str | None = None
            if requested_checkpoint_id is not None:
                checkpoint_exists = connection.execute(
                    """
                    SELECT 1
                    FROM checkpoints
                    WHERE thread_id = %s
                      AND checkpoint_ns = %s
                      AND checkpoint_id = %s
                    FOR KEY SHARE
                    """,
                    (thread_id, execution_id, requested_checkpoint_id),
                ).fetchone()
                if advance_head and checkpoint_exists is None:
                    raise CheckpointStorageIntegrityError(
                        "CHECKPOINT_PARENT_FENCE_REJECTED"
                    )
                if advance_head and stored_head is None and revision == 0:
                    effective_head = requested_checkpoint_id
                if not advance_head:
                    if checkpoint_exists is None and requested_checkpoint_id == stored_head:
                        raise CheckpointStorageIntegrityError(
                            "CHECKPOINT_PARENT_FENCE_REJECTED"
                        )
                    if checkpoint_exists is not None and stored_head is None and revision == 0:
                        writes_bootstrap_head = requested_checkpoint_id

            if advance_head and requested_checkpoint_id != effective_head:
                raise CheckpointSupersededError("CHECKPOINT_PARENT_FENCE_REJECTED")
            if not advance_head and requested_checkpoint_id is None:
                raise CheckpointWriteFenceError("CHECKPOINT_WRITE_FENCE_INVALID")

            short_saver = PostgresSaver(connection, serde=self.serde)
            result = write(short_saver)
            durable_head = (
                next_checkpoint_id
                if advance_head
                else writes_bootstrap_head or stored_head
            )
            updated = connection.execute(
                """
                UPDATE agentexecution
                SET latest_checkpoint_id = %s,
                    checkpoint_revision = checkpoint_revision + 1
                WHERE id = %s
                  AND checkpoint_revision = %s
                  AND latest_checkpoint_id IS NOT DISTINCT FROM %s
                """,
                (durable_head, execution_id, revision, stored_head),
            )
            if updated.rowcount != 1:
                raise CheckpointStorageIntegrityError("CHECKPOINT_HEAD_CAS_REJECTED")
            return result

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
        checkpoint_id = checkpoint.get("id")
        if (
            not isinstance(checkpoint_id, str)
            or not checkpoint_id
            or checkpoint_id != checkpoint_id.strip()
        ):
            raise CheckpointWriteFenceError("CHECKPOINT_ID_INVALID")
        sanitized_metadata = {
            key: value
            for key, value in metadata.items()
            if key not in {_FENCE_WORKER_KEY, _FENCE_ATTEMPT_KEY}
        }
        stored = self._fenced_write(
            config,
            next_checkpoint_id=checkpoint_id,
            advance_head=True,
            write=lambda saver: saver.put(
                self._physical(config),
                checkpoint,
                sanitized_metadata,
                new_versions,
            ),
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
        self._fenced_write(
            config,
            next_checkpoint_id=None,
            advance_head=False,
            write=lambda saver: saver.put_writes(
                self._physical(config), writes, task_id, task_path
            ),
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
            pool_size = max(1, self.settings.database.checkpoint_pool_size)
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

    def purge_checkpoint_data(
        self,
        *,
        batch_size: int,
        dry_run: bool,
        retention_cutoff: datetime | None = None,
    ) -> dict[str, dict[str, int | bool]]:
        """Delete bounded checkpoint rows whose execution namespace is orphaned.

        Checkpoint rows have no business timestamp. ``retention_cutoff`` is therefore
        applied indirectly by the caller deleting eligible ``AgentExecution`` rows
        first; every execution row that survives that business purge protects its
        matching ``checkpoint_ns`` here. The table names and ordering are a fixed
        leaf-to-root administrative allowlist.
        """
        self.get_checkpointer()
        assert self._pool is not None
        summary: dict[str, dict[str, int | bool]] = {}
        tables = (
            (
                "checkpoint_writes",
                "candidate.thread_id, candidate.checkpoint_ns, "
                "candidate.checkpoint_id, candidate.task_id, candidate.idx",
            ),
            (
                "checkpoint_blobs",
                "candidate.thread_id, candidate.checkpoint_ns, "
                "candidate.channel, candidate.version",
            ),
            (
                "checkpoints",
                "candidate.thread_id, candidate.checkpoint_ns, "
                "candidate.checkpoint_id",
            ),
        )
        for table, order_by in tables:
            orphan_candidate = (
                "NOT EXISTS ("
                "SELECT 1 FROM agentexecution AS execution "
                "WHERE execution.id = candidate.checkpoint_ns"
                ")"
            )
            orphan_target = (
                "NOT EXISTS ("
                "SELECT 1 FROM agentexecution AS execution "
                "WHERE execution.id = target.checkpoint_ns"
                ")"
            )
            with self._pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT 1 FROM {table} AS candidate "
                        f"WHERE {orphan_candidate} "
                        f"ORDER BY {order_by} LIMIT %s",
                        (batch_size + 1,),
                    )
                    probed = cursor.fetchall()
                    candidate_count = min(len(probed), batch_size)
                    deleted = 0
                    if not dry_run and candidate_count:
                        cursor.execute(
                            f"DELETE FROM {table} AS target "
                            "WHERE target.ctid IN ("
                            f"SELECT candidate.ctid FROM {table} AS candidate "
                            f"WHERE {orphan_candidate} "
                            f"ORDER BY {order_by} LIMIT %s"
                            ") "
                            f"AND {orphan_target}",
                            (batch_size,),
                        )
                        deleted = cursor.rowcount
                    summary[table] = {
                        "candidate_count": candidate_count,
                        "has_more": len(probed) > batch_size,
                        "deleted": deleted,
                    }
        return summary

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
    "CheckpointOwnershipLostError",
    "CheckpointStorageIntegrityError",
    "CheckpointSupersededError",
    "CheckpointWriteFenceError",
    "checkpoint_connection_string",
    "checkpoint_interrupts",
    "checkpoint_messages",
    "clear_thread_persistence",
    "clear_execution_persistence",
    "execution_checkpoint_config",
]
