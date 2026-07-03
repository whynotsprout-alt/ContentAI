from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

from agent.runtime.events import now_utc
from agent.runtime.executor import agent_executor
from core.config import get_settings
from db.session import engine
from sqlalchemy import text
from sqlmodel import Session

logger = logging.getLogger(__name__)


@dataclass
class RunWorker:
    worker_id: str = field(
        default_factory=lambda: f"{socket.gethostname()}-{uuid4().hex[:8]}"
    )
    _stop: asyncio.Event | None = field(default=None, init=False)

    async def serve(self) -> None:
        settings = get_settings()
        self._stop = asyncio.Event()
        while not self._stop.is_set():
            try:
                ran = await asyncio.to_thread(self.run_once)
            except Exception:  # noqa: BLE001
                logger.exception("Run worker loop failed.")
                ran = False
            if ran:
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.run_poll_seconds)
            except TimeoutError:
                continue

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    def run_once(self) -> bool:
        self._fail_exhausted_stale_runs()
        run_id = self._claim_next_run()
        if run_id is None:
            return False
        with Session(engine) as session:
            agent_executor.run(session, run_id=run_id)
        return True

    def _claim_next_run(self) -> str | None:
        settings = get_settings()
        now = now_utc()
        lease_expires_at = now + timedelta(seconds=settings.run_lease_seconds)
        with Session(engine) as session:
            row = session.execute(
                text(
                    """
                    WITH candidate AS (
                        SELECT id
                        FROM agentrun
                        WHERE (
                            status = 'queued'
                            OR (
                                status = 'running'
                                AND lease_expires_at IS NOT NULL
                                AND lease_expires_at < :now
                            )
                        )
                        AND cancel_requested_at IS NULL
                        AND attempt_count < :max_attempts
                        ORDER BY created_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE agentrun
                    SET status = 'running',
                        lease_owner = :lease_owner,
                        lease_expires_at = :lease_expires_at,
                        attempt_count = attempt_count + 1,
                        started_at = COALESCE(started_at, :now),
                        last_heartbeat_at = :now,
                        updated_at = :now,
                        error = ''
                    WHERE id = (SELECT id FROM candidate)
                    RETURNING id
                    """
                ),
                {
                    "now": now,
                    "lease_owner": self.worker_id,
                    "lease_expires_at": lease_expires_at,
                    "max_attempts": settings.run_max_attempts,
                },
            ).first()
            session.commit()
        return str(row[0]) if row else None

    def _fail_exhausted_stale_runs(self) -> None:
        settings = get_settings()
        now = now_utc()
        with Session(engine) as session:
            session.execute(
                text(
                    """
                    UPDATE agentrun
                    SET status = 'failed',
                        error = 'Run exceeded maximum retry attempts.',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        finished_at = COALESCE(finished_at, :now),
                        updated_at = :now
                    WHERE status = 'running'
                    AND lease_expires_at IS NOT NULL
                    AND lease_expires_at < :now
                    AND attempt_count >= :max_attempts
                    """
                ),
                {"now": now, "max_attempts": settings.run_max_attempts},
            )
            session.commit()


run_worker = RunWorker()
