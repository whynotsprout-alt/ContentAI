from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query

from contentai.api.dependencies import CatalogServiceDep, CurrentUserDep, SessionDep
from contentai.models.schemas import (
    AgentProfileCreate,
    AgentProfileDetail,
    AgentProfileListResponse,
    AgentProfileUpdate,
    AgentVersionCreate,
    AgentVersionSummary,
)
from contentai.services.errors import (
    AgentAlreadyExistsError,
    AgentInUseError,
    AgentNotFoundError,
    AgentPermissionError,
    AgentValidationError,
    AgentVersionConflictError,
    InvalidCursorError,
)

router = APIRouter(prefix="/agents", tags=["agents"])

AgentIdPath = Annotated[
    str,
    Path(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$"),
]


@router.get("", response_model=AgentProfileListResponse)
def list_agents(
    service: CatalogServiceDep,
    session: SessionDep,
    auth: CurrentUserDep,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> AgentProfileListResponse:
    try:
        return service.list_agents(session, auth, cursor=cursor, limit=limit)
    except InvalidCursorError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_CURSOR", "message": "Invalid cursor"},
        ) from exc


@router.get("/{agent_id}", response_model=AgentProfileDetail)
def get_agent(
    agent_id: AgentIdPath,
    service: CatalogServiceDep,
    session: SessionDep,
    auth: CurrentUserDep,
) -> AgentProfileDetail:
    try:
        return service.get_agent(session, agent_id, auth)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Agent not found") from exc
    except AgentPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("", response_model=AgentProfileDetail, status_code=201)
def create_agent(
    payload: AgentProfileCreate,
    service: CatalogServiceDep,
    session: SessionDep,
    auth: CurrentUserDep,
) -> AgentProfileDetail:
    try:
        return service.create_agent(session, payload, auth)
    except AgentAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AgentPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.patch(
    "/{agent_id}",
    response_model=AgentProfileDetail,
    summary="Update agent profile (partial fields)",
)
def patch_agent(
    agent_id: AgentIdPath,
    payload: AgentProfileUpdate,
    service: CatalogServiceDep,
    session: SessionDep,
    auth: CurrentUserDep,
) -> AgentProfileDetail:
    try:
        return service.update_agent(session, agent_id, payload, auth)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Agent not found") from exc
    except AgentAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AgentPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/{agent_id}/versions", response_model=AgentVersionSummary, status_code=201)
def create_agent_version(
    agent_id: AgentIdPath,
    payload: AgentVersionCreate,
    service: CatalogServiceDep,
    session: SessionDep,
    auth: CurrentUserDep,
) -> AgentVersionSummary:
    try:
        return service.create_agent_version(session, agent_id, payload, auth)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Agent not found") from exc
    except AgentPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except AgentVersionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/{agent_id}", status_code=204)
def delete_agent(
    agent_id: AgentIdPath,
    service: CatalogServiceDep,
    session: SessionDep,
    auth: CurrentUserDep,
) -> None:
    try:
        service.delete_agent(session, agent_id, auth)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Agent not found") from exc
    except AgentInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
