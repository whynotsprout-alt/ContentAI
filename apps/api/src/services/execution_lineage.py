from __future__ import annotations

from dataclasses import dataclass

from core.security import AuthContext
from models.chat import ChatSession
from services.errors import ChatSessionNotFoundError
from sqlmodel import Session, select


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
        chat = session.exec(
            select(ChatSession)
            .where(ChatSession.id == session_id)
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
