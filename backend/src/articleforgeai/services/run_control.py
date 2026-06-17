from __future__ import annotations

import json
from typing import Any

from articleforgeai.models.db import PipelineRun, RunStatus
from sqlmodel import Session

HOTSPOT_CONFIRMATION_MODE = "collect_hotspots_confirmation"


class RunControlService:
    def resume(self, session: Session, run: PipelineRun, next_stage: str) -> None:
        run.next_stage = next_stage
        run.status = RunStatus.running
        run.error = ""
        run.pending_payload = ""
        session.add(run)
        session.commit()

    def parse_pending_payload(self, run: PipelineRun) -> Any:
        try:
            return json.loads(run.pending_payload or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("No valid pending payload found") from exc

    @staticmethod
    def is_hotspot_confirmation(payload: Any) -> bool:
        return (
            isinstance(payload, dict)
            and payload.get("mode") == HOTSPOT_CONFIRMATION_MODE
        )


run_control_service = RunControlService()
