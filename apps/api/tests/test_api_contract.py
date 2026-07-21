from api.app import app

EXPECTED_API_OPERATIONS = {
    ("GET", "/api/health"),
    ("GET", "/api/ready"),
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/change-password"),
    ("GET", "/api/agents"),
    ("GET", "/api/agents/{agent_id}"),
    ("POST", "/api/agents"),
    ("PATCH", "/api/agents/{agent_id}"),
    ("POST", "/api/agents/{agent_id}/versions"),
    ("DELETE", "/api/agents/{agent_id}"),
    ("GET", "/api/chat/sessions"),
    ("POST", "/api/chat/sessions"),
    ("GET", "/api/chat/sessions/{session_id}"),
    ("DELETE", "/api/chat/sessions/{session_id}"),
    ("POST", "/api/chat/sessions/{session_id}/messages"),
    ("GET", "/api/chat/runs/{execution_id}/events"),
    ("GET", "/api/chat/runs/{execution_id}/status"),
    ("POST", "/api/chat/runs/{execution_id}/cancel"),
    ("POST", "/api/chat/runs/{execution_id}/resume"),
    ("GET", "/api/admin/users"),
    ("GET", "/api/admin/users/{user_id}"),
    ("POST", "/api/admin/users/{user_id}/enable"),
    ("POST", "/api/admin/users/{user_id}/disable"),
    ("POST", "/api/admin/users/{user_id}/temporary-password"),
    ("PATCH", "/api/admin/users/{user_id}"),
    ("GET", "/api/admin/users/{user_id}/sessions"),
    ("GET", "/api/admin/sessions/{session_id}"),
    ("GET", "/api/admin/sessions/{session_id}/messages"),
    ("GET", "/api/admin/usage"),
    ("GET", "/api/admin/model-config"),
    ("POST", "/api/admin/model-config/probe"),
    ("PUT", "/api/admin/model-config"),
}


def test_openapi_matches_documented_business_api() -> None:
    schema = app.openapi()
    actual = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        if path.startswith("/api/")
        for method in operations
        if method in {"get", "post", "patch", "delete", "put"}
    }

    assert len(EXPECTED_API_OPERATIONS) == 35
    assert actual == EXPECTED_API_OPERATIONS
