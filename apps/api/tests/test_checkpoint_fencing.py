from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import timedelta

import pytest
from contentai.agent.runtime.checkpoint import (
    CheckpointOwnershipLostError,
    CheckpointStorageIntegrityError,
    CheckpointWriteFenceError,
    RuntimePersistence,
    execution_checkpoint_config,
)
from contentai.core.config import get_settings
from contentai.db.session import get_engine
from contentai.models.chat import (
    AgentExecution,
    AgentExecutionAttempt,
    AgentInvocation,
    ChatSession,
)
from contentai.models.enums import RunStatus
from contentai.services.execution_settlement import current_database_time
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from model_config_helpers import DEFAULT_MODEL_CONFIG_ID
from sqlalchemy import text
from sqlmodel import Session, select


def _seed_running_execution(
    execution_id: str,
    *,
    worker_id: str,
    attempt_id: str,
) -> tuple[str, str]:
    session_id = f"session-{execution_id}"
    invocation_id = f"invocation-{execution_id}"
    thread_id = f"thread-{execution_id}"
    with Session(get_engine()) as session:
        now = current_database_time(session)
        session.add(
            ChatSession(
                id=session_id,
                agent_id="default-agent",
                agent_version_id="default-agent-v1",
                langgraph_thread_id=thread_id,
                user_id="local-user",
            )
        )
        session.flush()
        session.add(
            AgentInvocation(
                id=invocation_id,
                session_id=session_id,
                agent_id="default-agent",
                user_id="local-user",
            )
        )
        session.flush()
        execution = AgentExecution(
            id=execution_id,
            invocation_id=invocation_id,
            session_id=session_id,
            agent_version_id="default-agent-v1",
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
            status=RunStatus.running,
            worker_id=worker_id,
            attempt_count=1,
            lease_expires_at=now + timedelta(minutes=5),
        )
        session.add(execution)
        session.flush()
        session.add(
            AgentExecutionAttempt(
                id=attempt_id,
                execution_id=execution_id,
                ordinal=1,
                worker_id=worker_id,
            )
        )
        session.flush()
        execution.current_attempt_id = attempt_id
        session.add(execution)
        session.commit()
    return session_id, thread_id


def _fenced_config(
    *,
    thread_id: str,
    execution_id: str,
    worker_id: str,
    attempt_id: str,
) -> dict[str, object]:
    config: dict[str, object] = execution_checkpoint_config(
        thread_id=thread_id,
        execution_id=execution_id,
    )
    configurable = config["configurable"]
    assert isinstance(configurable, dict)
    configurable["__worker_id"] = worker_id
    configurable["__attempt_id"] = attempt_id
    return config


def _namespace_counts(execution_id: str) -> tuple[int, int, int]:
    with get_engine().connect() as connection:
        return tuple(
            int(value)
            for value in connection.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM checkpoints WHERE checkpoint_ns = :execution_id),
                      (SELECT count(*) FROM checkpoint_blobs WHERE checkpoint_ns = :execution_id),
                      (SELECT count(*) FROM checkpoint_writes WHERE checkpoint_ns = :execution_id)
                    """
                ),
                {"execution_id": execution_id},
            ).one()
        )


def _business_checkpoint_fence(execution_id: str) -> tuple[str | None, int]:
    with get_engine().connect() as connection:
        row = connection.execute(
            text(
                "SELECT latest_checkpoint_id, checkpoint_revision "
                "FROM agentexecution WHERE id = :execution_id"
            ),
            {"execution_id": execution_id},
        ).one()
    return row[0], int(row[1])


def test_checkpoint_writes_preserve_attempt_fence_and_advance_one_durable_head() -> None:
    execution_id = "execution-checkpoint-head-fence"
    worker_id = "worker-checkpoint-head-fence"
    attempt_id = "attempt-checkpoint-head-fence"
    _session_id, thread_id = _seed_running_execution(
        execution_id,
        worker_id=worker_id,
        attempt_id=attempt_id,
    )
    persistence = RuntimePersistence(get_settings())
    checkpointer = persistence.get_checkpointer()
    try:
        config = _fenced_config(
            thread_id=thread_id,
            execution_id=execution_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
        )
        first_checkpoint = empty_checkpoint()
        next_config = checkpointer.put(
            config,
            first_checkpoint,
            {"source": "input", "step": 0},
            {},
        )
        configurable = next_config["configurable"]
        assert configurable["checkpoint_id"] == first_checkpoint["id"]
        assert configurable["__worker_id"] == worker_id
        assert configurable["__attempt_id"] == attempt_id

        checkpointer.put_writes(
            next_config,
            [("custom", {"value": 1})],
            "task-head-fence",
        )
        second_checkpoint = empty_checkpoint()
        final_config = checkpointer.put(
            next_config,
            second_checkpoint,
            {
                "source": "loop",
                "step": 1,
                "__worker_id": "must-not-persist",
                "__attempt_id": "must-not-persist",
            },
            {},
        )

        with get_engine().connect() as connection:
            execution_row = connection.execute(
                text(
                    "SELECT latest_checkpoint_id, checkpoint_revision "
                    "FROM agentexecution WHERE id = :execution_id"
                ),
                {"execution_id": execution_id},
            ).one()
            metadata = connection.execute(
                text(
                    "SELECT metadata FROM checkpoints "
                    "WHERE checkpoint_ns = :execution_id "
                    "AND checkpoint_id = :checkpoint_id"
                ),
                {
                    "execution_id": execution_id,
                    "checkpoint_id": second_checkpoint["id"],
                },
            ).scalar_one()
        assert execution_row == (second_checkpoint["id"], 3)
        assert final_config["configurable"]["__worker_id"] == worker_id
        assert final_config["configurable"]["__attempt_id"] == attempt_id
        assert "__worker_id" not in metadata
        assert "__attempt_id" not in metadata
    finally:
        persistence.close()


def test_checkpoint_transaction_blocks_takeover_until_real_put_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = "execution-checkpoint-lock-fence"
    worker_id = "worker-checkpoint-lock-fence"
    attempt_id = "attempt-checkpoint-lock-fence"
    _session_id, thread_id = _seed_running_execution(
        execution_id,
        worker_id=worker_id,
        attempt_id=attempt_id,
    )
    persistence = RuntimePersistence(get_settings())
    checkpointer = persistence.get_checkpointer()
    config = _fenced_config(
        thread_id=thread_id,
        execution_id=execution_id,
        worker_id=worker_id,
        attempt_id=attempt_id,
    )
    checkpoint = empty_checkpoint()
    checkpoint_written = threading.Event()
    release_checkpoint_transaction = threading.Event()
    takeover_started = threading.Event()
    original_put = PostgresSaver.put

    def blocking_put(self, *args, **kwargs):
        result = original_put(self, *args, **kwargs)
        checkpoint_written.set()
        assert release_checkpoint_transaction.wait(timeout=3)
        return result

    def takeover() -> str:
        with Session(get_engine()) as session:
            session.exec(text("SET LOCAL lock_timeout = '3s'"))
            takeover_started.set()
            execution = session.exec(
                select(AgentExecution)
                .where(AgentExecution.id == execution_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            execution.worker_id = "worker-checkpoint-takeover"
            session.add(execution)
            session.commit()
            return execution.worker_id or ""

    monkeypatch.setattr(PostgresSaver, "put", blocking_put)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            writer_future = executor.submit(
                checkpointer.put,
                config,
                checkpoint,
                {"source": "input", "step": 0},
                {},
            )
            assert checkpoint_written.wait(timeout=3)
            takeover_future = executor.submit(takeover)
            assert takeover_started.wait(timeout=3)
            with pytest.raises(FutureTimeoutError):
                takeover_future.result(timeout=0.2)
            release_checkpoint_transaction.set()
            writer_future.result(timeout=3)
            assert takeover_future.result(timeout=3) == "worker-checkpoint-takeover"
    finally:
        release_checkpoint_transaction.set()
        persistence.close()


def test_put_writes_transaction_blocks_takeover_until_pending_writes_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = "execution-checkpoint-writes-lock-fence"
    worker_id = "worker-checkpoint-writes-lock-fence"
    attempt_id = "attempt-checkpoint-writes-lock-fence"
    _session_id, thread_id = _seed_running_execution(
        execution_id,
        worker_id=worker_id,
        attempt_id=attempt_id,
    )
    persistence = RuntimePersistence(get_settings())
    checkpointer = persistence.get_checkpointer()
    parent_config = checkpointer.put(
        _fenced_config(
            thread_id=thread_id,
            execution_id=execution_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
        ),
        empty_checkpoint(),
        {"source": "input", "step": 0},
        {},
    )
    writes_stored = threading.Event()
    release_writes_transaction = threading.Event()
    takeover_started = threading.Event()
    original_put_writes = PostgresSaver.put_writes

    def blocking_put_writes(self, *args, **kwargs):
        result = original_put_writes(self, *args, **kwargs)
        writes_stored.set()
        assert release_writes_transaction.wait(timeout=3)
        return result

    def takeover() -> None:
        with Session(get_engine()) as session:
            session.exec(text("SET LOCAL lock_timeout = '3s'"))
            takeover_started.set()
            execution = session.exec(
                select(AgentExecution)
                .where(AgentExecution.id == execution_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            execution.worker_id = "replacement-worker"
            session.add(execution)
            session.commit()

    monkeypatch.setattr(PostgresSaver, "put_writes", blocking_put_writes)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            writer_future = executor.submit(
                checkpointer.put_writes,
                parent_config,
                [("custom", {"pending": True})],
                "task-writes-lock-fence",
            )
            assert writes_stored.wait(timeout=3)
            takeover_future = executor.submit(takeover)
            assert takeover_started.wait(timeout=3)
            with pytest.raises(FutureTimeoutError):
                takeover_future.result(timeout=0.2)
            release_writes_transaction.set()
            writer_future.result(timeout=3)
            takeover_future.result(timeout=3)
    finally:
        release_writes_transaction.set()
        persistence.close()

    with get_engine().connect() as connection:
        stored = connection.execute(
            text(
                "SELECT count(*) FROM checkpoint_writes "
                "WHERE checkpoint_ns = :execution_id "
                "AND task_id = 'task-writes-lock-fence'"
            ),
            {"execution_id": execution_id},
        ).scalar_one()
    assert stored == 1


def test_old_attempt_and_stale_parent_fail_without_orphan_checkpoint_rows() -> None:
    execution_id = "execution-checkpoint-stale-fence"
    worker_id = "worker-checkpoint-stale-fence"
    attempt_id = "attempt-checkpoint-stale-fence"
    _session_id, thread_id = _seed_running_execution(
        execution_id,
        worker_id=worker_id,
        attempt_id=attempt_id,
    )
    persistence = RuntimePersistence(get_settings())
    checkpointer = persistence.get_checkpointer()
    try:
        config = _fenced_config(
            thread_id=thread_id,
            execution_id=execution_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
        )
        first_checkpoint = empty_checkpoint()
        next_config = checkpointer.put(
            config,
            first_checkpoint,
            {"source": "input", "step": 0},
            {},
        )
        counts_before = _namespace_counts(execution_id)

        stale_parent = _fenced_config(
            thread_id=thread_id,
            execution_id=execution_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
        )
        stale_parent["configurable"]["checkpoint_id"] = "checkpoint-stale-parent"
        with pytest.raises(CheckpointWriteFenceError, match="PARENT_FENCE"):
            checkpointer.put(
                stale_parent,
                empty_checkpoint(),
                {"source": "loop", "step": 1},
                {},
            )
        assert _namespace_counts(execution_id) == counts_before

        replacement_attempt_id = "attempt-checkpoint-stale-fence-replacement"
        with Session(get_engine()) as session:
            execution = session.exec(
                select(AgentExecution)
                .where(AgentExecution.id == execution_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            session.add(
                AgentExecutionAttempt(
                    id=replacement_attempt_id,
                    execution_id=execution_id,
                    ordinal=2,
                    worker_id=worker_id,
                )
            )
            session.flush()
            execution.current_attempt_id = replacement_attempt_id
            execution.attempt_count = 2
            session.add(execution)
            session.commit()

        with pytest.raises(CheckpointWriteFenceError, match="FENCE_REJECTED"):
            checkpointer.put_writes(
                next_config,
                [("custom", {"stale": True})],
                "task-stale-attempt",
            )
        with pytest.raises(CheckpointWriteFenceError, match="FENCE_REJECTED"):
            checkpointer.put(
                next_config,
                empty_checkpoint(),
                {"source": "loop", "step": 2},
                {},
            )
        assert _namespace_counts(execution_id) == counts_before
    finally:
        persistence.close()


def test_checkpoint_owner_clock_is_rechecked_after_row_lock_wait() -> None:
    execution_id = "execution-checkpoint-lock-wait-expiry"
    worker_id = "worker-checkpoint-lock-wait-expiry"
    attempt_id = "attempt-checkpoint-lock-wait-expiry"
    _session_id, thread_id = _seed_running_execution(
        execution_id,
        worker_id=worker_id,
        attempt_id=attempt_id,
    )
    engine = get_engine()
    with Session(engine) as session:
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        lease_expires_at = current_database_time(session) + timedelta(milliseconds=250)
        execution.lease_expires_at = lease_expires_at
        session.add(execution)
        session.commit()

    persistence = RuntimePersistence(get_settings())
    checkpointer = persistence.get_checkpointer()
    config = _fenced_config(
        thread_id=thread_id,
        execution_id=execution_id,
        worker_id=worker_id,
        attempt_id=attempt_id,
    )
    holder = Session(engine)
    holder.exec(
        select(AgentExecution)
        .where(AgentExecution.id == execution_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    writer_started = threading.Event()
    writer_errors: list[BaseException] = []

    def write_checkpoint() -> None:
        writer_started.set()
        try:
            checkpointer.put(
                config,
                empty_checkpoint(),
                {"source": "input", "step": 0},
                {},
            )
        except BaseException as exc:  # noqa: BLE001
            writer_errors.append(exc)

    writer_thread = threading.Thread(target=write_checkpoint)
    writer_thread.start()
    try:
        assert writer_started.wait(timeout=1)
        writer_thread.join(timeout=0.1)
        assert writer_thread.is_alive()
        with Session(engine) as clock_session:
            while current_database_time(clock_session) <= lease_expires_at:
                pass
    finally:
        holder.rollback()
        holder.close()
        writer_thread.join(timeout=3)
        persistence.close()

    assert not writer_thread.is_alive()
    assert len(writer_errors) == 1
    assert isinstance(writer_errors[0], CheckpointOwnershipLostError)
    assert _namespace_counts(execution_id) == (0, 0, 0)
    assert _business_checkpoint_fence(execution_id) == (None, 0)


def test_delayed_and_future_anchor_writes_never_rewind_the_business_head() -> None:
    execution_id = "execution-checkpoint-delayed-writes"
    worker_id = "worker-checkpoint-delayed-writes"
    attempt_id = "attempt-checkpoint-delayed-writes"
    _session_id, thread_id = _seed_running_execution(
        execution_id,
        worker_id=worker_id,
        attempt_id=attempt_id,
    )
    persistence = RuntimePersistence(get_settings())
    checkpointer = persistence.get_checkpointer()
    try:
        config = _fenced_config(
            thread_id=thread_id,
            execution_id=execution_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
        )
        parent = empty_checkpoint()
        parent_config = checkpointer.put(
            config,
            parent,
            {"source": "input", "step": 0},
            {},
        )
        child = empty_checkpoint()
        future_anchor = _fenced_config(
            thread_id=thread_id,
            execution_id=execution_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
        )
        future_anchor["configurable"]["checkpoint_id"] = child["id"]

        checkpointer.put_writes(
            future_anchor,
            [("custom", {"arrived": "before-checkpoint"})],
            "task-future-anchor",
        )
        assert _business_checkpoint_fence(execution_id) == (parent["id"], 2)

        child_config = checkpointer.put(
            parent_config,
            child,
            {"source": "loop", "step": 1},
            {},
        )
        assert _business_checkpoint_fence(execution_id) == (child["id"], 3)
        stored_child = checkpointer.get_tuple(child_config)
        assert stored_child is not None
        assert any(
            write[1] == "custom" and write[2] == {"arrived": "before-checkpoint"}
            for write in stored_child.pending_writes
        )

        checkpointer.put_writes(
            parent_config,
            [("custom", {"arrived": "after-child"})],
            "task-delayed-parent",
        )
        assert _business_checkpoint_fence(execution_id) == (child["id"], 4)
        with get_engine().connect() as connection:
            delayed_count = connection.execute(
                text(
                    "SELECT count(*) FROM checkpoint_writes "
                    "WHERE checkpoint_ns = :execution_id "
                    "AND checkpoint_id = :checkpoint_id "
                    "AND task_id = 'task-delayed-parent'"
                ),
                {
                    "execution_id": execution_id,
                    "checkpoint_id": parent["id"],
                },
            ).scalar_one()
        assert delayed_count == 1
    finally:
        persistence.close()


def test_legacy_parent_bootstraps_through_put_writes_then_put() -> None:
    execution_id = "execution-checkpoint-legacy-bootstrap"
    worker_id = "worker-checkpoint-legacy-bootstrap"
    attempt_id = "attempt-checkpoint-legacy-bootstrap"
    _session_id, thread_id = _seed_running_execution(
        execution_id,
        worker_id=worker_id,
        attempt_id=attempt_id,
    )
    persistence = RuntimePersistence(get_settings())
    checkpointer = persistence.get_checkpointer()
    try:
        config = _fenced_config(
            thread_id=thread_id,
            execution_id=execution_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
        )
        parent = empty_checkpoint()
        stored_parent = checkpointer.delegate.put(
            checkpointer._physical(config),
            parent,
            {"source": "legacy", "step": 0},
            {},
        )
        anchored = checkpointer._logical(stored_parent, config)
        assert anchored is not None
        assert _business_checkpoint_fence(execution_id) == (None, 0)

        checkpointer.put_writes(
            anchored,
            [("__resume__", {"decision": "approve"})],
            "task-legacy-resume",
        )
        assert _business_checkpoint_fence(execution_id) == (parent["id"], 1)

        child = empty_checkpoint()
        checkpointer.put(
            anchored,
            child,
            {"source": "loop", "step": 1},
            {},
        )
        assert _business_checkpoint_fence(execution_id) == (child["id"], 2)
    finally:
        persistence.close()


def test_missing_stored_head_rejects_put_and_put_writes_without_side_effects() -> None:
    execution_id = "execution-checkpoint-missing-stored-head"
    worker_id = "worker-checkpoint-missing-stored-head"
    attempt_id = "attempt-checkpoint-missing-stored-head"
    _session_id, thread_id = _seed_running_execution(
        execution_id,
        worker_id=worker_id,
        attempt_id=attempt_id,
    )
    persistence = RuntimePersistence(get_settings())
    checkpointer = persistence.get_checkpointer()
    try:
        config = _fenced_config(
            thread_id=thread_id,
            execution_id=execution_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
        )
        parent = empty_checkpoint()
        parent_config = checkpointer.put(
            config,
            parent,
            {"source": "input", "step": 0},
            {},
        )
        with get_engine().begin() as connection:
            deleted = connection.execute(
                text(
                    "DELETE FROM checkpoints WHERE thread_id = :thread_id "
                    "AND checkpoint_ns = :execution_id "
                    "AND checkpoint_id = :checkpoint_id"
                ),
                {
                    "thread_id": thread_id,
                    "execution_id": execution_id,
                    "checkpoint_id": parent["id"],
                },
            )
        assert deleted.rowcount == 1
        counts_before = _namespace_counts(execution_id)
        fence_before = _business_checkpoint_fence(execution_id)

        with pytest.raises(CheckpointStorageIntegrityError, match="PARENT_FENCE"):
            checkpointer.put_writes(
                parent_config,
                [("custom", {"must": "not-write"})],
                "task-missing-stored-head",
            )
        with pytest.raises(CheckpointStorageIntegrityError, match="PARENT_FENCE"):
            checkpointer.put(
                parent_config,
                empty_checkpoint(),
                {"source": "loop", "step": 1},
                {},
            )

        assert _namespace_counts(execution_id) == counts_before
        assert _business_checkpoint_fence(execution_id) == fence_before
    finally:
        persistence.close()


@pytest.mark.parametrize("operation", ["put", "put_writes"])
def test_parent_delete_waits_for_the_fenced_checkpoint_transaction(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = f"execution-checkpoint-parent-lock-{operation}"
    worker_id = f"worker-checkpoint-parent-lock-{operation}"
    attempt_id = f"attempt-checkpoint-parent-lock-{operation}"
    _session_id, thread_id = _seed_running_execution(
        execution_id,
        worker_id=worker_id,
        attempt_id=attempt_id,
    )
    persistence = RuntimePersistence(get_settings())
    checkpointer = persistence.get_checkpointer()
    parent = empty_checkpoint()
    config = _fenced_config(
        thread_id=thread_id,
        execution_id=execution_id,
        worker_id=worker_id,
        attempt_id=attempt_id,
    )
    parent_config = checkpointer.put(
        config,
        parent,
        {"source": "input", "step": 0},
        {},
    )
    delegate_written = threading.Event()
    release_writer = threading.Event()
    delete_started = threading.Event()
    method_name = "put" if operation == "put" else "put_writes"
    original = getattr(PostgresSaver, method_name)

    def blocking_write(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        delegate_written.set()
        assert release_writer.wait(timeout=3)
        return result

    def delete_parent() -> int:
        with get_engine().begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout = '3s'"))
            delete_started.set()
            deleted = connection.execute(
                text(
                    "DELETE FROM checkpoints WHERE thread_id = :thread_id "
                    "AND checkpoint_ns = :execution_id "
                    "AND checkpoint_id = :checkpoint_id"
                ),
                {
                    "thread_id": thread_id,
                    "execution_id": execution_id,
                    "checkpoint_id": parent["id"],
                },
            )
            return int(deleted.rowcount or 0)

    monkeypatch.setattr(PostgresSaver, method_name, blocking_write)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            if operation == "put":
                writer_future = executor.submit(
                    checkpointer.put,
                    parent_config,
                    empty_checkpoint(),
                    {"source": "loop", "step": 1},
                    {},
                )
            else:
                writer_future = executor.submit(
                    checkpointer.put_writes,
                    parent_config,
                    [("custom", {"locked": True})],
                    "task-parent-lock",
                )
            assert delegate_written.wait(timeout=3)
            delete_future = executor.submit(delete_parent)
            assert delete_started.wait(timeout=3)
            with pytest.raises(FutureTimeoutError):
                delete_future.result(timeout=0.2)
            release_writer.set()
            writer_future.result(timeout=3)
            assert delete_future.result(timeout=3) == 1
    finally:
        release_writer.set()
        persistence.close()


def test_async_checkpoint_wrappers_preserve_the_attempt_fence() -> None:
    execution_id = "execution-checkpoint-async-fence"
    worker_id = "worker-checkpoint-async-fence"
    attempt_id = "attempt-checkpoint-async-fence"
    _session_id, thread_id = _seed_running_execution(
        execution_id,
        worker_id=worker_id,
        attempt_id=attempt_id,
    )
    persistence = RuntimePersistence(get_settings())
    checkpointer = persistence.get_checkpointer()

    async def write() -> dict[str, object]:
        config = _fenced_config(
            thread_id=thread_id,
            execution_id=execution_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
        )
        checkpoint = empty_checkpoint()
        next_config = await checkpointer.aput(
            config,
            checkpoint,
            {
                "source": "input",
                "step": 0,
                "__worker_id": "must-not-persist",
                "__attempt_id": "must-not-persist",
            },
            {},
        )
        await checkpointer.aput_writes(
            next_config,
            [("custom", {"async": True})],
            "task-async-fence",
        )
        return next_config

    try:
        next_config = asyncio.run(write())
        configurable = next_config["configurable"]
        assert isinstance(configurable, dict)
        assert configurable["__worker_id"] == worker_id
        assert configurable["__attempt_id"] == attempt_id
        assert _business_checkpoint_fence(execution_id)[1] == 2
        with get_engine().connect() as connection:
            metadata = connection.execute(
                text(
                    "SELECT metadata FROM checkpoints "
                    "WHERE checkpoint_ns = :execution_id"
                ),
                {"execution_id": execution_id},
            ).scalar_one()
        assert "__worker_id" not in metadata
        assert "__attempt_id" not in metadata
    finally:
        persistence.close()
