from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

from models.base import utcnow
from models.chat import CheckpointDeletionOutbox
from sqlalchemy import or_
from sqlmodel import Session, select

MAX_BATCH_SIZE = 100
DEFAULT_BATCH_SIZE = 25
LOCK_SECONDS = 60
MAX_BACKOFF_SECONDS = 300


def drain_checkpoint_deletion_outbox(
    session: Session,
    checkpointer: Any,
    *,
    worker_id: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    lock_seconds: int = LOCK_SECONDS,
) -> int:
    """Delete execution namespaces from the checkpoint store in bounded batches."""
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    worker_id = worker_id or f"checkpoint-drain-{uuid4().hex}"
    completed = 0
    for _ in range(batch_size):
        now = utcnow()
        row = session.exec(
            select(CheckpointDeletionOutbox)
            .where(
                or_(
                    (CheckpointDeletionOutbox.status.in_(("pending", "failed")))
                    & (CheckpointDeletionOutbox.available_at <= now),
                    (CheckpointDeletionOutbox.status == "processing")
                    & (CheckpointDeletionOutbox.locked_until <= now),
                )
            )
            .order_by(CheckpointDeletionOutbox.available_at, CheckpointDeletionOutbox.id)
            .with_for_update(skip_locked=True)
        ).first()
        if row is None:
            break

        row.status = "processing"
        row.locked_by = worker_id
        row.locked_until = now + timedelta(seconds=lock_seconds)
        row.attempts += 1
        row.updated_at = now
        session.add(row)
        session.commit()

        try:
            checkpointer.delete_namespace(row.thread_id, row.checkpoint_ns)
        except (KeyError, FileNotFoundError):
            _complete(session, row.id)
            completed += 1
        except Exception as exc:
            _retry(session, row.id, str(exc))
        else:
            _complete(session, row.id)
            completed += 1
    return completed


def _complete(session: Session, row_id: str) -> None:
    row = session.get(CheckpointDeletionOutbox, row_id)
    if row is None:
        return
    row.status = "completed"
    row.locked_by = None
    row.locked_until = None
    row.last_error = ""
    row.updated_at = utcnow()
    session.add(row)
    session.commit()


def _retry(session: Session, row_id: str, error: str) -> None:
    row = session.get(CheckpointDeletionOutbox, row_id)
    if row is None:
        return
    delay = min(MAX_BACKOFF_SECONDS, 2 ** min(max(row.attempts - 1, 0), 8))
    row.status = "pending"
    row.locked_by = None
    row.locked_until = None
    row.available_at = utcnow() + timedelta(seconds=delay)
    row.last_error = error[:2000]
    row.updated_at = utcnow()
    session.add(row)
    session.commit()


__all__ = ["DEFAULT_BATCH_SIZE", "MAX_BATCH_SIZE", "drain_checkpoint_deletion_outbox"]
