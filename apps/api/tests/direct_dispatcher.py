from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from db.session import get_engine
from services.agent_service import AgentService
from services.execution_claim import claim_execution
from sqlmodel import Session


@dataclass(frozen=True)
class _DirectPostExecutionDispatcher:
    service: AgentService

    def dispatch(self, execution_id: str, request_id: str | None = None) -> None:
        self.service.runner.post_service.process(
            execution_id=execution_id,
            request_id=request_id,
        )


class DirectDispatcher:
    """Synchronous dispatcher used only by the test application."""

    def __init__(self, service: AgentService) -> None:
        self.service = service
        self.service.runner.post_service.dispatcher = _DirectPostExecutionDispatcher(service)

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
                resume_value=claimed.resume_value,
                resume_request_id=claimed.resume_request_id,
                continue_from_checkpoint=claimed.continue_from_checkpoint,
                request_id=request_id,
            )
