from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from core.security import AuthContext, authenticate_request
from db.session import get_engine
from fastapi import Depends, Request
from services.admin_service import AdminService
from services.agent_service import AgentService
from services.auth_service import AuthService
from services.catalog_service import CatalogService
from services.conversation_service import ConversationService
from sqlmodel import Session

AuthContextDep = Annotated[AuthContext, Depends(authenticate_request)]


@dataclass(frozen=True)
class RequestContext:
    request_id: str | None
    user_id: str
    conversation_id: str | None


def get_request_session(request: Request) -> Iterator[Session]:
    with Session(get_engine(request.app.state.settings)) as session:
        yield session


def get_current_user(request: Request, auth: AuthContextDep) -> AuthContext:
    request.state.user_id = auth.user_id
    request.state.conversation_id = _conversation_id_from_path(request)
    return auth


CurrentUserDep = Annotated[AuthContext, Depends(get_current_user)]


def get_current_admin(auth: CurrentUserDep) -> AuthContext:
    if auth.role != "admin":
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Administrator access required")
    return auth


CurrentAdminDep = Annotated[AuthContext, Depends(get_current_admin)]


def get_request_context(request: Request, auth: CurrentUserDep) -> RequestContext:
    return RequestContext(
        request_id=getattr(request.state, "request_id", None),
        user_id=auth.user_id,
        conversation_id=getattr(request.state, "conversation_id", None),
    )


def _conversation_id_from_path(request: Request) -> str | None:
    session_id = request.path_params.get("session_id")
    return str(session_id) if session_id is not None else None


def get_agent_service(request: Request) -> AgentService:
    return request.app.state.agent_service


def get_catalog_service(request: Request) -> CatalogService:
    return request.app.state.catalog_service


def get_conversation_service(request: Request) -> ConversationService:
    return request.app.state.conversation_service


def get_auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


def get_admin_service(request: Request) -> AdminService:
    return request.app.state.admin_service


SessionDep = Annotated[Session, Depends(get_request_session)]
RequestContextDep = Annotated[RequestContext, Depends(get_request_context)]
AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
CatalogServiceDep = Annotated[CatalogService, Depends(get_catalog_service)]
ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]
AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
AdminServiceDep = Annotated[AdminService, Depends(get_admin_service)]
