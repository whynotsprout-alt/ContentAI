from datetime import datetime
from typing import Annotated

from api.dependencies import AdminServiceDep, CurrentAdminDep, RequestContextDep, SessionDep
from fastapi import APIRouter, HTTPException, Query
from models.schemas import (
    AdminSessionDetail,
    AdminSessionListResponse,
    AdminUsageResponse,
    AdminUserListResponse,
    AdminUserSummary,
    AdminUserUpdate,
    MessageResponse,
)
from services.auth_service import AuthServiceError

router = APIRouter(prefix="/admin", tags=["admin"])


def _raise_admin_error(exc: AuthServiceError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/users", response_model=AdminUserListResponse)
def list_users(
    service: AdminServiceDep,
    session: SessionDep,
    _auth: CurrentAdminDep,
    search: str = Query(default="", max_length=254),
    status: str | None = Query(default=None, pattern="^(pending_verification|active|disabled)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> AdminUserListResponse:
    return service.list_users(
        session,
        search=search,
        status=status,
        page=page,
        page_size=page_size,
    )


@router.get("/users/{user_id}", response_model=AdminUserSummary)
def get_user(
    user_id: str,
    service: AdminServiceDep,
    session: SessionDep,
    _auth: CurrentAdminDep,
) -> AdminUserSummary:
    try:
        return service.get_user(session, user_id)
    except AuthServiceError as exc:
        _raise_admin_error(exc)


@router.post("/users/{user_id}/disable", response_model=AdminUserSummary)
def disable_user(
    user_id: str,
    service: AdminServiceDep,
    session: SessionDep,
    auth: CurrentAdminDep,
    request_context: RequestContextDep,
) -> AdminUserSummary:
    try:
        return service.set_disabled(
            session,
            actor_user_id=auth.user_id,
            user_id=user_id,
            disabled=True,
            request_id=request_context.request_id or "",
        )
    except AuthServiceError as exc:
        _raise_admin_error(exc)


@router.post("/users/{user_id}/enable", response_model=AdminUserSummary)
def enable_user(
    user_id: str,
    service: AdminServiceDep,
    session: SessionDep,
    auth: CurrentAdminDep,
    request_context: RequestContextDep,
) -> AdminUserSummary:
    try:
        return service.set_disabled(
            session,
            actor_user_id=auth.user_id,
            user_id=user_id,
            disabled=False,
            request_id=request_context.request_id or "",
        )
    except AuthServiceError as exc:
        _raise_admin_error(exc)


@router.post("/users/{user_id}/password-reset", response_model=MessageResponse)
def password_reset(
    user_id: str,
) -> MessageResponse:
    _ = user_id
    raise HTTPException(status_code=410, detail="Password reset has been retired.")


@router.patch("/users/{user_id}", response_model=AdminUserSummary)
def update_user(
    user_id: str,
    payload: AdminUserUpdate,
    service: AdminServiceDep,
    session: SessionDep,
    auth: CurrentAdminDep,
    request_context: RequestContextDep,
) -> AdminUserSummary:
    try:
        return service.update_user(
            session,
            actor_user_id=auth.user_id,
            user_id=user_id,
            role=payload.role,
            request_id=request_context.request_id or "",
        )
    except AuthServiceError as exc:
        _raise_admin_error(exc)


@router.get("/users/{user_id}/sessions", response_model=AdminSessionListResponse)
def list_user_sessions(
    user_id: str,
    service: AdminServiceDep,
    session: SessionDep,
    _auth: CurrentAdminDep,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
) -> AdminSessionListResponse:
    try:
        return service.list_user_sessions(session, user_id, page=page, page_size=page_size)
    except AuthServiceError as exc:
        _raise_admin_error(exc)


@router.get("/sessions/{session_id}", response_model=AdminSessionDetail)
def get_session_detail(
    session_id: str,
    service: AdminServiceDep,
    session: SessionDep,
    auth: CurrentAdminDep,
    request_context: RequestContextDep,
) -> AdminSessionDetail:
    try:
        return service.get_session_detail(
            session,
            session_id,
            actor_user_id=auth.user_id,
            request_id=request_context.request_id or "",
        )
    except AuthServiceError as exc:
        _raise_admin_error(exc)


@router.get("/usage", response_model=AdminUsageResponse)
def usage(
    service: AdminServiceDep,
    session: SessionDep,
    _auth: CurrentAdminDep,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    user_id: str | None = Query(default=None),
    model: str | None = Query(default=None),
    category: str | None = Query(default=None),
    group_by: str = Query(default="day", pattern="^(day|model|category)$"),
) -> AdminUsageResponse:
    return service.usage(
        session,
        start=start,
        end=end,
        user_id=user_id,
        model=model,
        category=category,
        group_by=group_by,  # type: ignore[arg-type]
    )
