from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlmodel import Session

from contentai.core.security import AuthContext, authenticate_request
from contentai.db.session import get_engine
from contentai.services.admin_service import AdminService
from contentai.services.agent_service import AgentService
from contentai.services.auth_service import AuthService
from contentai.services.catalog_service import CatalogService
from contentai.services.conversation_service import ConversationService
from contentai.services.model_configuration_service import ModelConfigurationService

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
    if auth.must_change_password and request.url.path not in {
        "/api/auth/me",
        "/api/auth/logout",
        "/api/auth/change-password",
    }:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "PASSWORD_CHANGE_REQUIRED",
                "message": "Change the temporary password before continuing.",
            },
        )
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


def get_model_configuration_service(request: Request) -> ModelConfigurationService:
    return request.app.state.model_configuration_service


SessionDep = Annotated[Session, Depends(get_request_session)]
FunctionSessionDep = Annotated[
    Session,
    Depends(get_request_session, scope="function"),
]
RequestContextDep = Annotated[RequestContext, Depends(get_request_context)]
AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
CatalogServiceDep = Annotated[CatalogService, Depends(get_catalog_service)]
ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]
AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
AdminServiceDep = Annotated[AdminService, Depends(get_admin_service)]
ModelConfigurationServiceDep = Annotated[
    ModelConfigurationService, Depends(get_model_configuration_service)
]
