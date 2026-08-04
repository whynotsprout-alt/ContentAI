from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, select

from contentai.core.security import AGENT_WILDCARD, AuthContext
from contentai.models.chat import ChatSession
from contentai.services.errors import ChatSessionNotFoundError


@dataclass(frozen=True)
class ExecutionLineage:
    chat: ChatSession
    session_id: str
    user_id: str
    agent_id: str
    agent_version_id: str

    @classmethod
    def resolve_for_update(
        cls,
        session: Session,
        session_id: str,
        auth: AuthContext,
    ) -> ExecutionLineage:
        statement = select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.user_id == auth.user_id,
        )
        if AGENT_WILDCARD not in auth.allowed_agent_ids:
            allowed_agent_ids = tuple(
                agent_id for agent_id in auth.allowed_agent_ids if agent_id
            )
            if not allowed_agent_ids:
                raise ChatSessionNotFoundError(session_id)
            statement = statement.where(ChatSession.agent_id.in_(allowed_agent_ids))
        chat = session.exec(
            statement
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if (
            chat is None
            or chat.user_id != auth.user_id
            or not auth.can_access_agent(chat.agent_id)
        ):
            raise ChatSessionNotFoundError(session_id)
        return cls(
            chat=chat,
            session_id=chat.id,
            user_id=chat.user_id,
            agent_id=chat.agent_id,
            agent_version_id=chat.agent_version_id,
        )


__all__ = ["ExecutionLineage"]
