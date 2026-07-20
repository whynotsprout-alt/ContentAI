from datetime import timedelta

from models.base import utcnow
from models.chat import CheckpointDeletionOutbox
from services.checkpoint_deletion import drain_checkpoint_deletion_outbox
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
