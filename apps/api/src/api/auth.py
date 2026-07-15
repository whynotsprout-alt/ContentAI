from __future__ import annotations

from api.dependencies import AuthServiceDep, CurrentUserDep, SessionDep
from core.config import Env
from core.rate_limit import RateLimitRule, RateLimitUnavailable
from fastapi import APIRouter, HTTPException, Request, Response
from models.schemas import (
    ChangePasswordRequest,
    CurrentUserResponse,
    EmailRequest,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    ResetPasswordRequest,
    TokenRequest,
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
    request: Request,
    service: AuthServiceDep,
    session: SessionDep,
) -> MessageResponse:
    try:
        service.register(session, email=str(payload.email), password=payload.password)
    except AuthServiceError as exc:
        _raise_auth_error(exc)
    if not request.app.state.settings.auth.require_email_verification:
        return MessageResponse(message="Registration successful. You can sign in now.")
    return MessageResponse(message="注册成功，请查收验证邮件")


@router.post("/verify-email", response_model=CurrentUserResponse)
def verify_email(
    payload: TokenRequest,
    request: Request,
    service: AuthServiceDep,
    session: SessionDep,
) -> CurrentUserResponse:
    try:
        user = service.verify_email(session, token=payload.token)
    except AuthServiceError as exc:
        _raise_auth_error(exc)
    return service.to_response(user)


@router.post("/resend-verification", response_model=MessageResponse)
def resend_verification(
    payload: EmailRequest,
    request: Request,
    service: AuthServiceDep,
    session: SessionDep,
) -> MessageResponse:
    normalized_email = service.normalize_email(str(payload.email))
    user = service.get_user_by_email(session, normalized_email)
    identities = [
        ("verification_resend:email", normalized_email),
        (
            "verification_resend:ip",
            request.client.host if request.client else "unknown",
        ),
    ]
    if user is not None:
        identities.append(("verification_resend:user", user.id))
    settings = request.app.state.settings.auth
    rule = RateLimitRule(
        settings.resend_verification_limit,
        settings.resend_verification_window_seconds,
    )
    try:
        for scope, identity in identities:
            request.app.state.rate_limiter.check(scope, identity, rule)
    except PermissionError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except RateLimitUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    service.resend_verification(session, email=str(payload.email))
    return MessageResponse(message="如果该邮箱需要验证，我们已发送新邮件")


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


@router.post("/forgot-password", response_model=MessageResponse)
def forgot_password(
    payload: EmailRequest,
    request: Request,
    service: AuthServiceDep,
    session: SessionDep,
) -> MessageResponse:
    service.forgot_password(session, email=str(payload.email))
    return MessageResponse(message="如果该邮箱可用，我们已发送密码重置邮件")


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    service: AuthServiceDep,
    session: SessionDep,
) -> MessageResponse:
    try:
        service.reset_password(session, token=payload.token, password=payload.password)
    except AuthServiceError as exc:
        _raise_auth_error(exc)
    return MessageResponse(message="密码已重置，请重新登录")


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
        service.change_password(
            session,
            user_id=auth.user_id,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
    except AuthServiceError as exc:
        _raise_auth_error(exc)
    _clear_auth_cookies(response, request)
    return MessageResponse(message="密码已修改，请重新登录")
