from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.config import Env, Settings, get_settings
from fastapi import HTTPException, Request, status

ACCOUNT_WILDCARD = "*"
TOOL_WILDCARD = "*"


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    tenant_id: str
    allowed_account_ids: tuple[str, ...] = (ACCOUNT_WILDCARD,)
    tool_permissions: tuple[str, ...] = (TOOL_WILDCARD,)

    def can_access_account(self, account_id: str) -> bool:
        return (
            ACCOUNT_WILDCARD in self.allowed_account_ids
            or account_id in self.allowed_account_ids
        )

    def can_use_tool(self, tool_name: str) -> bool:
        return TOOL_WILDCARD in self.tool_permissions or tool_name in self.tool_permissions


def authenticate_request(request: Request) -> AuthContext:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    if not settings.auth.enabled:
        return AuthContext(user_id="local-user", tenant_id="local")

    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    claims = decode_jwt_claims(token.strip(), settings)
    return auth_context_from_claims(claims, settings)


def decode_jwt_claims(token: str, settings: Settings) -> dict[str, Any]:
    if settings.auth.allow_unsigned_test_tokens and settings.env != Env.production:
        return _decode_unverified_claims(token)

    try:
        import jwt
        from jwt import PyJWKClient
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT verification dependencies are not installed",
        ) from exc

    try:
        signing_key = PyJWKClient(settings.auth.oidc_jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=settings.auth.oidc_audience,
            issuer=settings.auth.oidc_issuer,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    if not isinstance(claims, dict):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token claims")
    return claims


def auth_context_from_claims(claims: dict[str, Any], settings: Settings) -> AuthContext:
    user_id = _claim_as_string(claims, settings.auth.user_claim)
    tenant_id = _claim_as_string(claims, settings.auth.tenant_claim)
    if not user_id or not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token is missing required identity claims",
        )

    return AuthContext(
        user_id=user_id,
        tenant_id=tenant_id,
        allowed_account_ids=_claim_as_tuple(claims, settings.auth.accounts_claim),
        tool_permissions=_claim_as_tuple(claims, settings.auth.tools_claim),
    )


def _decode_unverified_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        ) from exc
    if not isinstance(claims, dict):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token claims")

    exp = claims.get("exp")
    if isinstance(exp, int | float) and datetime.fromtimestamp(exp, UTC) < datetime.now(UTC):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    return claims


def _claim_as_string(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if value is None:
        return ""
    return str(value).strip()


def _claim_as_tuple(claims: dict[str, Any], name: str) -> tuple[str, ...]:
    value = claims.get(name, [ACCOUNT_WILDCARD])
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, list | tuple | set):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        items = []
    return tuple(dict.fromkeys(items)) or (ACCOUNT_WILDCARD,)
