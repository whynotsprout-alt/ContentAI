from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from core.config import Settings, get_settings
from core.logging import configure_logging
from db.session import get_engine
from models.base import utcnow
from models.chat import ExecutionOutbox
from services.celery_app import celery_app
from services.service_heartbeat import (
    HEARTBEAT_INTERVAL_SECONDS,
    service_instance_id,
    upsert_service_heartbeat,
)
from sqlalchemy import and_, or_
from sqlmodel import Session, select

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutboxDelivery:
    outbox_id: str
    execution_id: str
    request_id: str | None
    kind: str


class OutboxDispatcher:
    def __init__(
        self, settings: Settings | None = None, *, dispatcher_id: str | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self.dispatcher_id = dispatcher_id or service_instance_id()
        self._next_heartbeat_at = 0.0

    def dispatch_once(self, *, batch_size: int = 50) -> int:
        self._write_heartbeat_if_due()
        now = utcnow()
        with Session(get_engine(self.settings)) as session:
            rows = list(
                session.exec(
                    select(ExecutionOutbox)
                    .where(
                        or_(
                            ExecutionOutbox.status == "pending",
                            and_(
                                ExecutionOutbox.status == "publishing",
                                ExecutionOutbox.locked_until < now,
                            ),
                        )
                    )
                    .where(ExecutionOutbox.available_at <= now)
                    .where(
                        (ExecutionOutbox.locked_until.is_(None))
                        | (ExecutionOutbox.locked_until < now)
                    )
                    .order_by(ExecutionOutbox.created_at.asc())
                    .with_for_update(skip_locked=True)
                    .limit(batch_size)
                ).all()
            )
            for row in rows:
                row.status = "publishing"
                row.locked_by = self.dispatcher_id
                row.locked_until = now + timedelta(seconds=30)
                row.updated_at = now
                session.add(row)
            deliveries = [
                OutboxDelivery(
                    outbox_id=row.id,
                    execution_id=row.execution_id,
                    request_id=row.request_id or None,
                    kind=row.kind,
                )
                for row in rows
            ]
            session.commit()

        published = 0
        for delivery in deliveries:
            try:
                task_name = (
                    "contentai.process_agent_post_execution"
                    if delivery.kind == "postprocess"
                    else "contentai.execute_agent"
                )
                queue = (
                    self.settings.agent.celery_background_queue
                    if delivery.kind == "postprocess"
                    else self.settings.agent.celery_queue
                )
                celery_app.send_task(
                    task_name,
                    kwargs={
                        "execution_id": delivery.execution_id,
                        "request_id": delivery.request_id,
                    },
                    queue=queue,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to publish execution %s",
                    delivery.execution_id,
                    exc_info=True,
                )
                self._mark_retry(delivery.outbox_id, str(exc))
            else:
                self._mark_published(delivery.outbox_id)
                published += 1
        return published

    def _write_heartbeat_if_due(self) -> None:
        now = time.monotonic()
        if now < self._next_heartbeat_at:
            return
        with Session(get_engine(self.settings)) as session:
            upsert_service_heartbeat(
                session,
                service_name="dispatcher",
                instance_id=self.dispatcher_id,
            )
            session.commit()
        self._next_heartbeat_at = now + HEARTBEAT_INTERVAL_SECONDS

    def run_forever(self, *, poll_seconds: float = 0.25) -> None:
        while True:
            dispatched = self.dispatch_once()
            if dispatched == 0:
                time.sleep(max(0.05, poll_seconds))

    def _mark_published(self, row_id: str) -> None:
        with Session(get_engine(self.settings)) as session:
            row = session.exec(
                select(ExecutionOutbox).where(ExecutionOutbox.id == row_id).with_for_update()
            ).first()
            if row is None or row.status != "publishing" or row.locked_by != self.dispatcher_id:
                return
            now = utcnow()
            row.status = "published"
            row.published_at = now
            row.locked_by = None
            row.locked_until = None
            row.last_error = ""
            row.updated_at = now
            session.add(row)
            session.commit()

    def _mark_retry(self, row_id: str, error: str) -> None:
        with Session(get_engine(self.settings)) as session:
            row = session.exec(
                select(ExecutionOutbox).where(ExecutionOutbox.id == row_id).with_for_update()
            ).first()
            if row is None or row.status != "publishing" or row.locked_by != self.dispatcher_id:
                return
            now = utcnow()
            row.attempts += 1
            row.status = "pending"
            row.available_at = now + timedelta(seconds=min(60, 2 ** min(row.attempts, 6)))
            row.locked_by = None
            row.locked_until = None
            row.last_error = error[:2000]
            row.updated_at = now
            session.add(row)
            session.commit()


def main() -> None:
    configure_logging("dispatcher")
    OutboxDispatcher().run_forever()


if __name__ == "__main__":
    main()
