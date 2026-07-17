from __future__ import annotations

from api.dependencies import AuthServiceDep, CurrentUserDep, SessionDep
from core.config import Env
from fastapi import APIRouter, HTTPException, Request, Response
from models.schemas import (
    ChangePasswordRequest,
    CurrentUserResponse,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
)
from models.user import AppUser
from services.auth_service import AuthServiceError

router = APIRouter(prefix="/auth", tags=["auth"])


def _raise_auth_error(exc: AuthServiceError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _set_auth_cookies(
    response: Response,
    request: Request,
    *,
    session_token: str,
    csrf_token: str,
) -> None:
    settings = request.app.state.settings
    secure = settings.env == Env.production
    max_age = settings.auth.session_days * 24 * 60 * 60
    response.set_cookie(
        settings.auth.session_cookie_name,
        session_token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        settings.auth.csrf_cookie_name,
        csrf_token,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _clear_auth_cookies(response: Response, request: Request) -> None:
    settings = request.app.state.settings
    response.delete_cookie(settings.auth.session_cookie_name, path="/")
    response.delete_cookie(settings.auth.csrf_cookie_name, path="/")


@router.post("/register", response_model=MessageResponse, status_code=201)
def register(
    payload: RegisterRequest,
    service: AuthServiceDep,
    session: SessionDep,
) -> MessageResponse:
    try:
        service.register(session, email=str(payload.email), password=payload.password)
    except AuthServiceError as exc:
        _raise_auth_error(exc)
    return MessageResponse(message="Registration successful. You can sign in now.")


@router.post("/login", response_model=CurrentUserResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: AuthServiceDep,
    session: SessionDep,
) -> CurrentUserResponse:
    try:
        issued = service.login(
            session,
            email=str(payload.email),
            password=payload.password,
            user_agent=request.headers.get("user-agent", ""),
            ip_address=request.client.host if request.client else "",
        )
    except AuthServiceError as exc:
        _raise_auth_error(exc)
    _set_auth_cookies(
        response,
        request,
        session_token=issued.session_token,
        csrf_token=issued.csrf_token,
    )
    return service.to_response(issued.user)


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    auth: CurrentUserDep,
    service: AuthServiceDep,
    session: SessionDep,
) -> None:
    service.logout(session, auth.session_id)
    _clear_auth_cookies(response, request)


@router.get("/me", response_model=CurrentUserResponse)
def me(
    auth: CurrentUserDep,
    session: SessionDep,
    service: AuthServiceDep,
) -> CurrentUserResponse:
    user = session.get(AppUser, auth.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return service.to_response(user)


@router.post("/change-password", response_model=MessageResponse)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    auth: CurrentUserDep,
    service: AuthServiceDep,
    session: SessionDep,
) -> MessageResponse:
    try:
        issued = service.change_password(
            session,
            user_id=auth.user_id,
            current_password=payload.current_password,
            new_password=payload.new_password,
            user_agent=request.headers.get("user-agent", ""),
            ip_address=request.client.host if request.client else "",
        )
    except AuthServiceError as exc:
        _raise_auth_error(exc)
    _set_auth_cookies(
        response,
        request,
        session_token=issued.session_token,
        csrf_token=issued.csrf_token,
    )
    return MessageResponse(message="密码已修改")
