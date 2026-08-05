from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, update
from sqlmodel import Session, select

from contentai.models.base import utcnow
from contentai.models.chat import CheckpointDeletionOutbox

MAX_BATCH_SIZE = 100
DEFAULT_BATCH_SIZE = 25
LOCK_SECONDS = 60
MAX_BACKOFF_SECONDS = 300


@dataclass(frozen=True)
class ClaimedCheckpointDeletion:
    id: str
    thread_id: str
    checkpoint_ns: str
    owner: str


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
    owner = f"{worker_id or 'checkpoint-drain'}:{uuid4().hex}"
    completed = 0
    for _ in range(batch_size):
        claim = _claim_one(
            session,
            owner=owner,
            lock_seconds=lock_seconds,
        )
        if claim is None:
            break

        try:
            checkpointer.delete_namespace(claim.thread_id, claim.checkpoint_ns)
        except (KeyError, FileNotFoundError):
            completed += int(_complete(session, claim))
        except Exception as exc:
            _retry(session, claim, str(exc))
        else:
            completed += int(_complete(session, claim))
    return completed


def _claim_one(
    session: Session,
    *,
    owner: str,
    lock_seconds: int,
) -> ClaimedCheckpointDeletion | None:
    now = utcnow()
    row = session.exec(
        select(CheckpointDeletionOutbox)
        .where(
            or_(
                (CheckpointDeletionOutbox.status.in_(("pending", "failed")))
                & (CheckpointDeletionOutbox.available_at <= now),
                (CheckpointDeletionOutbox.status == "processing")
                & or_(
                    CheckpointDeletionOutbox.locked_until.is_(None),
                    CheckpointDeletionOutbox.locked_until <= now,
                ),
            )
        )
        .order_by(CheckpointDeletionOutbox.available_at, CheckpointDeletionOutbox.id)
        .with_for_update(skip_locked=True)
    ).first()
    if row is None:
        session.rollback()
        return None
    row.status = "processing"
    row.locked_by = owner
    row.locked_until = now + timedelta(seconds=lock_seconds)
    row.attempts += 1
    row.updated_at = now
    claim = ClaimedCheckpointDeletion(
        id=row.id,
        thread_id=row.thread_id,
        checkpoint_ns=row.checkpoint_ns,
        owner=owner,
    )
    session.add(row)
    session.commit()
    return claim


def _complete(session: Session, claim: ClaimedCheckpointDeletion) -> bool:
    result = session.execute(
        update(CheckpointDeletionOutbox)
        .where(
            CheckpointDeletionOutbox.id == claim.id,
            CheckpointDeletionOutbox.status == "processing",
            CheckpointDeletionOutbox.locked_by == claim.owner,
        )
        .values(
            status="completed",
            locked_by=None,
            locked_until=None,
            last_error="",
            updated_at=utcnow(),
        )
    )
    session.commit()
    return result.rowcount == 1


def _retry(session: Session, claim: ClaimedCheckpointDeletion, error: str) -> bool:
    attempts = session.exec(
        select(CheckpointDeletionOutbox.attempts).where(
            CheckpointDeletionOutbox.id == claim.id,
            CheckpointDeletionOutbox.status == "processing",
            CheckpointDeletionOutbox.locked_by == claim.owner,
        )
    ).one_or_none()
    if attempts is None:
        session.rollback()
        return False
    delay = min(MAX_BACKOFF_SECONDS, 2 ** min(max(int(attempts) - 1, 0), 8))
    now = utcnow()
    result = session.execute(
        update(CheckpointDeletionOutbox)
        .where(
            CheckpointDeletionOutbox.id == claim.id,
            CheckpointDeletionOutbox.status == "processing",
            CheckpointDeletionOutbox.locked_by == claim.owner,
        )
        .values(
            status="pending",
            locked_by=None,
            locked_until=None,
            available_at=now + timedelta(seconds=delay),
            last_error=error[:2000],
            updated_at=now,
        )
    )
    session.commit()
    return result.rowcount == 1


__all__ = [
    "ClaimedCheckpointDeletion",
    "DEFAULT_BATCH_SIZE",
    "MAX_BATCH_SIZE",
    "drain_checkpoint_deletion_outbox",
]
