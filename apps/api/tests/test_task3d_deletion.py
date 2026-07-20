from datetime import timedelta
from types import SimpleNamespace

import pytest
from core.config import get_settings
from core.security import AuthContext
from models.base import utcnow
from models.chat import (
    AgentExecution,
    AgentInvocation,
    ChatSession,
    CheckpointDeletionOutbox,
)
from models.enums import RunStatus
from services.checkpoint_deletion import drain_checkpoint_deletion_outbox
from services.conversation_service import ConversationService
from sqlmodel import Session, select


class _Checkpointer:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_once = fail_once

    def delete_namespace(self, thread_id: str, checkpoint_ns: str) -> None:
        self.calls.append((thread_id, checkpoint_ns))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("checkpoint unavailable")


def test_drain_retries_failed_row_and_completes_after_checkpoint_success() -> None:
    from db.session import get_engine

    now = utcnow() - timedelta(seconds=1)
    with Session(get_engine()) as session:
        session.add(
            CheckpointDeletionOutbox(
                id="cdo-task3d",
                user_id="local-user",
                session_id="session-task3d",
                thread_id="thread-task3d",
                checkpoint_ns="execution-task3d",
                available_at=now,
            )
        )
        session.commit()

    checkpointer = _Checkpointer(fail_once=True)
    with Session(get_engine()) as session:
        assert drain_checkpoint_deletion_outbox(
            session,
            checkpointer,
            worker_id="task3d",
            batch_size=1,
        ) == 0

    with Session(get_engine()) as session:
        pending = session.exec(select(CheckpointDeletionOutbox)).one()
        assert pending.status == "pending"
        assert pending.last_error == "checkpoint unavailable"

    with Session(get_engine()) as session:
        pending = session.exec(select(CheckpointDeletionOutbox)).one()
        pending.available_at = utcnow() - timedelta(seconds=1)
        session.add(pending)
        session.commit()
        assert drain_checkpoint_deletion_outbox(
            session,
            checkpointer,
            worker_id="task3d",
            batch_size=1,
        ) == 1

    with Session(get_engine()) as session:
        completed = session.exec(select(CheckpointDeletionOutbox)).one()
        assert completed.status == "completed"
    assert checkpointer.calls == [
        ("thread-task3d", "execution-task3d"),
        ("thread-task3d", "execution-task3d"),
    ]


def test_drain_missing_namespace_is_success() -> None:
    from db.session import get_engine

    with Session(get_engine()) as session:
        session.add(
            CheckpointDeletionOutbox(
                id="cdo-task3d-missing",
                user_id="local-user",
                session_id="session-task3d",
                thread_id="thread-task3d-missing",
                checkpoint_ns="execution-task3d-missing",
                available_at=utcnow() - timedelta(seconds=1),
            )
        )
        session.commit()

        class Missing:
            def delete_namespace(self, thread_id: str, checkpoint_ns: str) -> None:
                raise KeyError(checkpoint_ns)

        assert drain_checkpoint_deletion_outbox(
            session,
            Missing(),
            worker_id="task3d",
            batch_size=1,
        ) == 1

        row = session.exec(select(CheckpointDeletionOutbox)).one()
        assert row.status == "completed"


def test_drain_reclaims_available_failed_rows_with_a_bounded_claim() -> None:
    from db.session import get_engine

    available_at = utcnow() - timedelta(seconds=2)
    with Session(get_engine()) as session:
        for suffix in ("first", "second"):
            session.add(
                CheckpointDeletionOutbox(
                    id=f"cdo-task3d-{suffix}",
                    user_id="local-user",
                    session_id="session-task3d",
                    thread_id=f"thread-task3d-{suffix}",
                    checkpoint_ns=f"execution-task3d-{suffix}",
                    status="failed",
                    available_at=available_at,
                )
            )
        session.commit()

        checkpointer = _Checkpointer()
        assert drain_checkpoint_deletion_outbox(
            session,
            checkpointer,
            worker_id="task3d",
            batch_size=1,
        ) == 1
        rows = session.exec(
            select(CheckpointDeletionOutbox).order_by(CheckpointDeletionOutbox.id)
        ).all()
        assert [row.status for row in rows] == ["completed", "failed"]
        assert len(checkpointer.calls) == 1


def test_stale_worker_cannot_overwrite_new_owner_completion() -> None:
    from db.session import get_engine

    with Session(get_engine()) as session:
        session.add(
            CheckpointDeletionOutbox(
                id="cdo-task3d-fenced",
                user_id="local-user",
                session_id="session-task3d",
                thread_id="thread-task3d-fenced",
                checkpoint_ns="execution-task3d-fenced",
                available_at=utcnow() - timedelta(seconds=1),
            )
        )
        session.commit()

    class StaleWorker:
        def delete_namespace(self, thread_id: str, checkpoint_ns: str) -> None:
            with Session(get_engine()) as nested:
                row = nested.get(CheckpointDeletionOutbox, "cdo-task3d-fenced")
                assert row is not None
                row.locked_until = utcnow() - timedelta(seconds=1)
                nested.add(row)
                nested.commit()
                assert drain_checkpoint_deletion_outbox(
                    nested,
                    _Checkpointer(),
                    worker_id="new-worker",
                    batch_size=1,
                ) == 1
            raise RuntimeError("old worker failed after lease loss")

    with Session(get_engine()) as session:
        assert drain_checkpoint_deletion_outbox(
            session,
            StaleWorker(),
            worker_id="old-worker",
            batch_size=1,
        ) == 0

    with Session(get_engine()) as session:
        row = session.get(CheckpointDeletionOutbox, "cdo-task3d-fenced")
        assert row is not None
        assert row.status == "completed"
        assert row.last_error == ""


def test_session_delete_rollback_does_not_touch_checkpoint_or_business_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from db.session import get_engine

    with Session(get_engine()) as seed:
        chat = ChatSession(
            id="session-task3d-rollback",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )
        seed.add(chat)
        seed.flush()
        invocation = AgentInvocation(
            id="invocation-task3d-rollback",
            session_id=chat.id,
            agent_id=chat.agent_id,
            user_id=chat.user_id,
        )
        seed.add(invocation)
        seed.flush()
        seed.add(
            AgentExecution(
                id="execution-task3d-rollback",
                invocation_id=invocation.id,
                session_id=chat.id,
                agent_version_id=chat.agent_version_id,
                status=RunStatus.completed,
            )
        )
        seed.commit()

    checkpoint_calls: list[tuple[str, str]] = []
    checkpointer = SimpleNamespace(
        delete_namespace=lambda thread_id, checkpoint_ns: checkpoint_calls.append(
            (thread_id, checkpoint_ns)
        )
    )
    service = ConversationService(
        SimpleNamespace(
            runtime=SimpleNamespace(get_checkpointer=lambda: checkpointer),
            settings=get_settings(),
        )
    )
    with Session(get_engine()) as session:
        monkeypatch.setattr(session, "commit", lambda: (_ for _ in ()).throw(RuntimeError("db")))
        with pytest.raises(RuntimeError, match="db"):
            service.delete_session(
                session,
                "session-task3d-rollback",
                AuthContext(user_id="local-user"),
            )

    with Session(get_engine()) as session:
        assert session.get(ChatSession, "session-task3d-rollback") is not None
        assert session.exec(select(CheckpointDeletionOutbox)).all() == []
    assert checkpoint_calls == []


def test_session_delete_enqueues_one_namespace_per_execution_and_drain_is_idempotent() -> None:
    from db.session import get_engine

    with Session(get_engine()) as session:
        chat = ChatSession(
            id="session-task3d-multiple",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )
        session.add(chat)
        session.flush()
        for ordinal in range(2):
            invocation = AgentInvocation(
                id=f"invocation-task3d-multiple-{ordinal}",
                session_id=chat.id,
                agent_id=chat.agent_id,
                user_id=chat.user_id,
            )
            session.add(invocation)
            session.flush()
            session.add(
                AgentExecution(
                    id=f"execution-task3d-multiple-{ordinal}",
                    invocation_id=invocation.id,
                    session_id=chat.id,
                    agent_version_id=chat.agent_version_id,
                    status=RunStatus.completed,
                )
            )
        session.commit()

        service = ConversationService(
            SimpleNamespace(settings=get_settings(), runtime=SimpleNamespace())
        )
        service.delete_session(
            session,
            chat.id,
            AuthContext(user_id="local-user"),
        )

    checkpointer = _Checkpointer()
    with Session(get_engine()) as session:
        rows = session.exec(
            select(CheckpointDeletionOutbox).order_by(
                CheckpointDeletionOutbox.checkpoint_ns
            )
        ).all()
        assert [row.checkpoint_ns for row in rows] == [
            "execution-task3d-multiple-0",
            "execution-task3d-multiple-1",
        ]
        assert drain_checkpoint_deletion_outbox(
            session,
            checkpointer,
            worker_id="multiple",
            batch_size=10,
        ) == 2
        assert drain_checkpoint_deletion_outbox(
            session,
            checkpointer,
            worker_id="multiple",
            batch_size=10,
        ) == 0
    assert len(checkpointer.calls) == 2
