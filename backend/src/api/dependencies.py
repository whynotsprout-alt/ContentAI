from typing import Annotated

from agent.runtime.container import RuntimeContainer
from core.security import AuthContext, authenticate_request
from db.session import get_engine
from fastapi import Depends, Request
from services.catalog_service import CatalogService
from services.conversation_service import ConversationService
from sqlmodel import Session

AuthContextDep = Annotated[AuthContext, Depends(authenticate_request)]


def get_request_session(request: Request):
    with Session(get_engine(request.app.state.settings)) as session:
        yield session


def get_agent_runtime(request: Request) -> RuntimeContainer:
    return request.app.state.agent_runtime


def get_catalog_service(request: Request) -> CatalogService:
    return request.app.state.catalog_service


def get_conversation_service(request: Request) -> ConversationService:
    return request.app.state.conversation_service


SessionDep = Annotated[Session, Depends(get_request_session)]
AgentRuntimeDep = Annotated[RuntimeContainer, Depends(get_agent_runtime)]
CatalogServiceDep = Annotated[CatalogService, Depends(get_catalog_service)]
ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]
