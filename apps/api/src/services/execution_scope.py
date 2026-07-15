from __future__ import annotations

from dataclasses import dataclass

from core.security import AGENT_WILDCARD, AuthContext
from models.chat import AgentExecution, AgentInvocation, ChatSession
from models.enums import SessionStatus
from services.errors import ExecutionNotFoundError
from sqlmodel import Session, select


@dataclass(frozen=True)
class ScopedExecution:
    execution: AgentExecution
    invocation: AgentInvocation
    chat: ChatSession


class ExecutionScopeGuard:
    def require_execution(
        self,
        session: Session,
        execution_id: str,
        auth: AuthContext,
        *,
        for_update: bool = False,
    ) -> ScopedExecution:
        agent_filter = None
        if AGENT_WILDCARD not in auth.allowed_agent_ids:
            allowed_agents = [agent for agent in auth.allowed_agent_ids if agent]
            agent_filter = ChatSession.agent_id.in_(allowed_agents)

        statement = (
            select(AgentExecution, AgentInvocation, ChatSession)
            .join(AgentInvocation, AgentExecution.invocation_id == AgentInvocation.id)
            .join(ChatSession, AgentInvocation.session_id == ChatSession.id)
            .where(AgentExecution.id == execution_id)
            .where(ChatSession.tenant_id == auth.tenant_id)
            .where(ChatSession.owner_user_id == auth.user_id)
            .where(ChatSession.status != SessionStatus.deleted)
        )
        if agent_filter is not None:
            statement = statement.where(agent_filter)
        if for_update:
            statement = statement.with_for_update()

        row = session.exec(statement).first()
        if row is None:
            raise ExecutionNotFoundError(execution_id)

        execution, invocation, chat = row
        if not auth.can_access_agent(chat.agent_id):
            raise ExecutionNotFoundError(execution_id)
        if chat is None or execution is None or invocation is None:
            raise ExecutionNotFoundError(execution_id)
        return ScopedExecution(
            execution=execution,
            invocation=invocation,
            chat=chat,
        )
