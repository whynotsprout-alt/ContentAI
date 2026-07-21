from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from db.session import get_engine
from models.chat import AgentExecution
from services.agent_service import AgentService
from services.execution_claim import claim_execution
from services.side_effects import execute_side_effect_job, read_side_effect_receipt
from sqlmodel import Session


@dataclass(frozen=True)
class _DirectPostExecutionDispatcher:
    service: AgentService

    def dispatch(self, execution_id: str, request_id: str | None = None) -> None:
        with Session(get_engine(self.service.settings)) as session:
            execution = session.get(AgentExecution, execution_id)
            if execution is None:
                return
            model_config_id = execution.model_config_id
        self.service.runner.post_service.process(
            execution_id=execution_id,
            model_config_id=model_config_id,
            request_id=request_id,
        )


class DirectDispatcher:
    """Synchronous dispatcher used only by the test application."""

    def __init__(self, service: AgentService) -> None:
        self.service = service
        self.service.runner.post_service.dispatcher = _DirectPostExecutionDispatcher(service)
        self.service.runtime.side_effect_dispatcher = execute_side_effect_job
        self.service.runtime.side_effect_receipt_poller = read_side_effect_receipt

    def dispatch(self, execution_id: str, request_id: str | None = None) -> None:
        claimed = claim_execution(
            self.service,
            execution_id,
            f"direct:{uuid4()}",
            create_attempt=False,
            use_lease=False,
        )
        if claimed is None:
            return
        with Session(get_engine(self.service.settings)) as session:
            self.service.runner.run(
                session,
                execution_id=execution_id,
                auth=claimed.auth,
                tool_permissions=claimed.auth.tool_permissions,
                turn_context=claimed.turn_context,
                resume_value=claimed.resume_value,
                resume_request_id=claimed.resume_request_id,
                continue_from_checkpoint=claimed.continue_from_checkpoint,
                request_id=request_id,
            )
