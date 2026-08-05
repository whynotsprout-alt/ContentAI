from contentai.api.app import (
    CSRF_HEADER_SECURITY_SCHEME,
    SESSION_COOKIE_SECURITY_SCHEME,
    app,
)

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


def test_openapi_documents_cookie_and_csrf_security_requirements() -> None:
    schema = app.openapi()
    security_schemes = schema["components"]["securitySchemes"]

    assert security_schemes[SESSION_COOKIE_SECURITY_SCHEME] == {
        "type": "apiKey",
        "in": "cookie",
        "name": app.state.settings.auth.session_cookie_name,
        "description": "Opaque ContentAI login session cookie.",
    }
    assert security_schemes[CSRF_HEADER_SECURITY_SCHEME] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-CSRF-Token",
        "description": "Double-submit CSRF token required for state-changing requests.",
    }

    public_operations = {
        ("GET", "/api/health"),
        ("GET", "/api/ready"),
        ("POST", "/api/auth/register"),
        ("POST", "/api/auth/login"),
    }
    state_changing_methods = {"POST", "PUT", "PATCH", "DELETE"}
    for method, path in EXPECTED_API_OPERATIONS:
        operation = schema["paths"][path][method.lower()]
        if (method, path) in public_operations:
            assert "security" not in operation
        elif method in state_changing_methods:
            assert operation["security"] == [
                {
                    SESSION_COOKIE_SECURITY_SCHEME: [],
                    CSRF_HEADER_SECURITY_SCHEME: [],
                }
            ]
        else:
            assert operation["security"] == [{SESSION_COOKIE_SECURITY_SCHEME: []}]


def test_openapi_documents_stream_event_response() -> None:
    schema = app.openapi()
    responses = schema["paths"]["/api/chat/runs/{execution_id}/events"]["get"][
        "responses"
    ]
    parameters = schema["paths"]["/api/chat/runs/{execution_id}/events"]["get"][
        "parameters"
    ]
    after_sequence_parameter = next(
        parameter for parameter in parameters if parameter["name"] == "after_sequence"
    )
    after_sequence_variants = after_sequence_parameter["schema"].get(
        "anyOf", [after_sequence_parameter["schema"]]
    )
    assert {"type": "string"} in after_sequence_variants
    assert "minimum" not in after_sequence_parameter["schema"]
    assert "ignored when Last-Event-ID is non-empty" in after_sequence_parameter[
        "description"
    ]
    response = responses["200"]

    assert set(response["content"]) == {"text/event-stream"}
    event_content = response["content"]["text/event-stream"]
    assert event_content["schema"] == {"type": "string"}
    assert set(event_content["examples"]) == {"data", "heartbeat", "control"}

    data_example = event_content["examples"]["data"]["value"]
    heartbeat_example = event_content["examples"]["heartbeat"]["value"]
    control_example = event_content["examples"]["control"]["value"]
    assert data_example.startswith("id: exe_example:1\nevent: lifecycle\ndata: {")
    assert data_example.endswith("\n\n")
    assert heartbeat_example == "event: heartbeat\ndata: {\"schema_version\":3}\n\n"
    assert "event: errors\n" in control_example
    assert '"code":"STREAM_EXCEPTION_ERROR"' in control_example
    assert all(
        not example.startswith("id:") and "\nid:" not in example
        for example in (heartbeat_example, control_example)
    )

    event_schema = schema["components"]["schemas"]["StreamEventV3"]
    assert event_schema["discriminator"]["propertyName"] == "channel"
    assert {variant["$ref"] for variant in event_schema["oneOf"]} == {
        "#/components/schemas/StreamPublicEventV3",
        "#/components/schemas/StreamErrorEventV3",
    }
    assert [example["channel"] for example in event_schema["examples"]] == [
        "lifecycle",
        "errors",
    ]

    public_event_schema = schema["components"]["schemas"]["StreamPublicEventV3"]
    error_event_schema = schema["components"]["schemas"]["StreamErrorEventV3"]
    error_data_schema = schema["components"]["schemas"]["StreamErrorDataV3"]
    assert "errors" not in public_event_schema["properties"]["channel"]["enum"]
    assert error_event_schema["properties"]["channel"]["const"] == "errors"
    assert error_event_schema["properties"]["sequence"]["minimum"] == 1
    assert public_event_schema["properties"]["sequence"]["maximum"] == (
        9_007_199_254_740_991
    )
    assert error_event_schema["properties"]["sequence"]["maximum"] == (
        9_007_199_254_740_991
    )
    assert error_event_schema["properties"]["data"] == {
        "$ref": "#/components/schemas/StreamErrorDataV3"
    }
    assert {"code", "message"} <= set(error_data_schema["required"])
    assert error_data_schema["properties"]["code"]["minLength"] == 1
    assert error_data_schema["properties"]["message"]["minLength"] == 1
    assert error_data_schema["additionalProperties"] is True

    assert {"401", "403", "404", "409", "422"} <= set(responses)
    conflict_content = responses["409"]["content"]["application/json"]
    conflict_codes = {
        example["value"]["detail"]["code"]
        for example in conflict_content["examples"].values()
    }
    assert conflict_codes == {
        "INVALID_STREAM_CURSOR",
        "STREAM_REPLAY_GAP",
        "STREAM_REPLAY_EXPIRED",
        "STREAMING_DEGRADED",
    }
    documented_codes = conflict_content["schema"]["properties"]["detail"]["properties"][
        "code"
    ]["enum"]
    assert set(documented_codes) == conflict_codes

    validation_content = responses["422"]["content"]["application/json"]
    assert {"$ref": "#/components/schemas/HTTPValidationError"} in validation_content[
        "schema"
    ]["oneOf"]
    assert {
        "type": "object",
        "required": ["detail"],
        "properties": {"detail": {"type": "string"}},
    } in validation_content["schema"]["oneOf"]
    assert set(validation_content["examples"]) == {
        "framework_validation",
        "after_sequence",
        "last_event_id",
    }
    assert isinstance(
        validation_content["examples"]["framework_validation"]["value"]["detail"],
        list,
    )
    for example_name in ("after_sequence", "last_event_id"):
        assert isinstance(
            validation_content["examples"][example_name]["value"]["detail"],
            str,
        )
