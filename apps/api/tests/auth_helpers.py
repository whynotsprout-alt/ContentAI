from __future__ import annotations

from contentai.core.security import AGENT_WILDCARD, TOOL_WILDCARD, AuthContext
from fastapi import HTTPException, Request


def resolve_test_auth_context(request: Request) -> AuthContext:
    """Resolve synthetic identity only through an explicit test dependency override."""
    user_id = request.headers.get("x-test-user-id", "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="Test identity required")
    return AuthContext(
        user_id=user_id,
        allowed_agent_ids=_csv_header(request, "x-test-agent-ids", AGENT_WILDCARD),
        tool_permissions=_csv_header(request, "x-test-tool-permissions", TOOL_WILDCARD),
    )


def default_test_auth_context(request: Request) -> AuthContext:
    if "x-test-user-id" not in request.headers:
        return AuthContext(user_id="local-user")
    return resolve_test_auth_context(request)


def auth_headers(
    *,
    user_id: str = "local-user",
    agents: list[str] | None = None,
    tools: list[str] | None = None,
) -> dict[str, str]:
    return {
        "X-Test-User-ID": user_id,
        "X-Test-Agent-IDs": ",".join(agents or [AGENT_WILDCARD]),
        "X-Test-Tool-Permissions": ",".join(tools or [TOOL_WILDCARD]),
    }


def _csv_header(request: Request, name: str, default: str) -> tuple[str, ...]:
    values = tuple(
        value.strip() for value in request.headers.get(name, default).split(",") if value.strip()
    )
    return values or (default,)
