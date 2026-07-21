from typing import Annotated

from api.dependencies import (
    AdminServiceDep,
    CurrentAdminDep,
    ModelConfigurationServiceDep,
    RequestContextDep,
    SessionDep,
)
from fastapi import APIRouter, HTTPException, Query, Request, Response
from models.schemas import (
    AdminMessageListResponse,
    AdminSessionDetail,
    AdminSessionListResponse,
    AdminUsageResponse,
    AdminUserListResponse,
    AdminUserSummary,
    AdminUserUpdate,
    ModelConfigurationProbeRequest,
    ModelConfigurationProbeResponse,
    ModelConfigurationResponse,
    ModelConfigurationUpdateRequest,
    TemporaryPasswordResponse,
)
from models.schemas.base import AwareDatetime
from services.auth_service import AuthServiceError
from services.errors import InvalidCursorError, ResponseItemTooLargeError
from services.model_config_network import ModelProbeError
from services.model_configuration_repository import ModelConfigurationChanged
from services.model_configuration_service import ModelCredentialsRequired

router = APIRouter(prefix="/admin", tags=["admin"])

_MODEL_ERROR_STATUS = {
    "MODEL_NOT_CONFIGURED": 503,
    "MODEL_CREDENTIALS_REQUIRED": 422,
    "MODEL_ENDPOINT_FORBIDDEN": 422,
    "MODEL_CONFIG_CHANGED": 409,
    "MODEL_AUTH_FAILED": 422,
    "MODEL_NOT_FOUND": 422,
    "MODEL_PROVIDER_UNREACHABLE": 502,
    "MODEL_PROBE_FAILED": 502,
}

_MODEL_ERROR_MESSAGES = {
    "MODEL_NOT_CONFIGURED": "A model provider has not been configured.",
    "MODEL_CREDENTIALS_REQUIRED": "Model provider credentials are required.",
    "MODEL_ENDPOINT_FORBIDDEN": "The model endpoint is not permitted.",
    "MODEL_CONFIG_CHANGED": "The model configuration changed. Refresh and try again.",
    "MODEL_AUTH_FAILED": "The model provider rejected the credentials.",
    "MODEL_NOT_FOUND": "The requested model was not found.",
    "MODEL_PROVIDER_UNREACHABLE": "The model provider could not be reached.",
    "MODEL_PROBE_FAILED": "The model provider probe failed.",
}


def _raise_model_error(exc: Exception) -> None:
    if isinstance(exc, ModelConfigurationChanged):
        code = "MODEL_CONFIG_CHANGED"
    else:
        code = getattr(exc, "code", "MODEL_PROBE_FAILED")
    raise HTTPException(
        status_code=_MODEL_ERROR_STATUS[code],
        detail={"code": code, "message": _MODEL_ERROR_MESSAGES[code]},
    ) from exc


def _model_configuration_response(active) -> ModelConfigurationResponse:
    if active is None:
        return ModelConfigurationResponse(configured=False)
    configuration = active.configuration
    return ModelConfigurationResponse(
        configured=True,
        id=configuration.id,
        version=configuration.version,
        provider=configuration.provider,
        base_url=configuration.base_url,
        model_name=configuration.model_name,
        api_key_hint=configuration.api_key_hint,
        validated_at=configuration.validated_at,
        created_at=configuration.created_at,
        created_by_user_id=configuration.created_by_user_id,
        created_by_email=active.created_by_email,
    )


@router.get(
    "/model-config",
    response_model=ModelConfigurationResponse,
    response_model_exclude_none=True,
)
def get_model_configuration(
    response: Response,
    service: ModelConfigurationServiceDep,
    session: SessionDep,
    _auth: CurrentAdminDep,
) -> ModelConfigurationResponse:
    response.headers["Cache-Control"] = "no-store"
    return _model_configuration_response(service.get_active(session))


@router.post("/model-config/probe", response_model=ModelConfigurationProbeResponse)
def probe_model_configuration(
    payload: ModelConfigurationProbeRequest,
    response: Response,
    service: ModelConfigurationServiceDep,
    session: SessionDep,
    _auth: CurrentAdminDep,
) -> ModelConfigurationProbeResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        result = service.probe(
            session,
            base_url=payload.base_url,
            api_key=payload.api_key.get_secret_value() if payload.api_key is not None else None,
            model_name=payload.model_name,
        )
    except (ModelProbeError, ModelCredentialsRequired) as exc:
        _raise_model_error(exc)
    return ModelConfigurationProbeResponse(
        base_url=result.base_url,
        models=list(result.models),
        models_truncated=result.models_truncated,
        model_validated=result.model_validated,
        latency_ms=result.latency_ms,
    )


@router.put(
    "/model-config",
    response_model=ModelConfigurationResponse,
    response_model_exclude_none=True,
)
def update_model_configuration(
    payload: ModelConfigurationUpdateRequest,
    response: Response,
    service: ModelConfigurationServiceDep,
    session: SessionDep,
    auth: CurrentAdminDep,
    request_context: RequestContextDep,
) -> ModelConfigurationResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        service.update(
            session,
            actor_user_id=auth.user_id,
            request_id=request_context.request_id or "",
            base_url=payload.base_url,
            api_key=payload.api_key.get_secret_value() if payload.api_key is not None else None,
            model_name=payload.model_name,
            expected_version=payload.expected_version,
        )
    except (ModelProbeError, ModelCredentialsRequired, ModelConfigurationChanged) as exc:
        session.rollback()
        _raise_model_error(exc)
    return _model_configuration_response(service.get_active(session))


def _raise_admin_error(exc: AuthServiceError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _reject_legacy_query(request: Request) -> None:
    for name in ("page", "page_size", "before"):
        if name in request.query_params:
            raise HTTPException(status_code=422, detail=f"{name} is no longer supported")


@router.get("/users", response_model=AdminUserListResponse)
def list_users(
    service: AdminServiceDep,
    session: SessionDep,
    _auth: CurrentAdminDep,
    request: Request,
    search: str = Query(default="", max_length=254),
    status: str | None = Query(default=None, pattern="^(pending_verification|active|disabled)$"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> AdminUserListResponse:
    _reject_legacy_query(request)
    try:
        return service.list_users(
            session,
            search=search,
            status=status,
            cursor=cursor,
            limit=limit,
            scope=f"admin.users:{_auth.user_id}:{search}:{status or ''}",
        )
    except InvalidCursorError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_CURSOR", "message": "Invalid cursor"},
        ) from exc
    except ResponseItemTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail={"code": "RESPONSE_ITEM_TOO_LARGE", "message": str(exc)},
        ) from exc


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


@router.post(
    "/users/{user_id}/temporary-password",
    response_model=TemporaryPasswordResponse,
)
def temporary_password(
    user_id: str,
    response: Response,
    service: AdminServiceDep,
    session: SessionDep,
    auth: CurrentAdminDep,
    request_context: RequestContextDep,
) -> TemporaryPasswordResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.issue_temporary_password(
            session,
            actor_user_id=auth.user_id,
            user_id=user_id,
            request_id=request_context.request_id or "",
        )
    except AuthServiceError as exc:
        _raise_admin_error(exc)


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
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> AdminSessionListResponse:
    _reject_legacy_query(request)
    try:
        return service.list_user_sessions(
            session,
            user_id,
            cursor=cursor,
            limit=limit,
            scope=f"admin.user_sessions:{_auth.user_id}:{user_id}",
        )
    except InvalidCursorError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_CURSOR", "message": "Invalid cursor"},
        ) from exc
    except ResponseItemTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail={"code": "RESPONSE_ITEM_TOO_LARGE", "message": str(exc)},
        ) from exc
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


@router.get("/sessions/{session_id}/messages", response_model=AdminMessageListResponse)
def list_session_messages(
    session_id: str,
    service: AdminServiceDep,
    session: SessionDep,
    _auth: CurrentAdminDep,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> AdminMessageListResponse:
    _reject_legacy_query(request)
    try:
        return service.list_session_messages(
            session,
            session_id,
            cursor=cursor,
            limit=limit,
            scope=f"admin.messages:{_auth.user_id}:{session_id}",
        )
    except InvalidCursorError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_CURSOR", "message": "Invalid cursor"},
        ) from exc
    except ResponseItemTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail={"code": "RESPONSE_ITEM_TOO_LARGE", "message": str(exc)},
        ) from exc
    except AuthServiceError as exc:
        _raise_admin_error(exc)


@router.get("/usage", response_model=AdminUsageResponse)
def usage(
    service: AdminServiceDep,
    session: SessionDep,
    _auth: CurrentAdminDep,
    start: Annotated[AwareDatetime | None, Query()] = None,
    end: Annotated[AwareDatetime | None, Query()] = None,
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
