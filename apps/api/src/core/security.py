from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from core.config import Settings, get_settings
from db.session import get_engine
from fastapi import HTTPException, Request, status
from models.base import utcnow
from models.user import AppUser, AuthSession
from sqlmodel import Session, select

AGENT_WILDCARD = "*"
TOOL_WILDCARD = "*"


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    tenant_id: str
    role: str = "user"
    status: str = "active"
    session_id: str | None = None
    allowed_agent_ids: tuple[str, ...] = (AGENT_WILDCARD,)
    tool_permissions: tuple[str, ...] = (TOOL_WILDCARD,)

    def can_access_agent(self, agent_id: str) -> bool:
        return AGENT_WILDCARD in self.allowed_agent_ids or agent_id in self.allowed_agent_ids

    def can_use_tool(self, tool_name: str) -> bool:
        return TOOL_WILDCARD in self.tool_permissions or tool_name in self.tool_permissions


def authenticate_request(request: Request) -> AuthContext:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    return _authenticate_local_request(request, settings)


def _authenticate_local_request(request: Request, settings: Settings) -> AuthContext:
    raw_token = request.cookies.get(settings.auth.session_cookie_name, "").strip()
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    with Session(get_engine(settings)) as session:
        row = session.exec(
            select(AuthSession, AppUser)
            .join(AppUser, AuthSession.user_id == AppUser.id)
            .where(AuthSession.token_hash == token_hash)
        ).first()
        now = utcnow()
        if row is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")
        auth_session, user = row
        if user.status == "disabled":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "USER_DISABLED", "message": "Account is disabled"},
            )
        if (
            auth_session.revoked_at is not None
            or auth_session.expires_at <= now
            or user.status != "active"
            or user.email_verified_at is None
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            csrf_cookie = request.cookies.get(settings.auth.csrf_cookie_name, "")
            csrf_header = request.headers.get("x-csrf-token", "")
            csrf_hash = hashlib.sha256(csrf_header.encode("utf-8")).hexdigest()
            if (
                not csrf_cookie
                or not csrf_header
                or not secrets.compare_digest(csrf_cookie, csrf_header)
                or not secrets.compare_digest(auth_session.csrf_hash, csrf_hash)
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid CSRF token",
                )
        if now - auth_session.last_seen_at > timedelta(minutes=5):
            auth_session.last_seen_at = now
            session.add(auth_session)
            session.commit()
        return AuthContext(
            user_id=user.id,
            tenant_id=user.tenant_id,
            role=user.role,
            status=user.status,
            session_id=auth_session.id,
        )
