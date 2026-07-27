from __future__ import annotations

import threading
from dataclasses import dataclass
from importlib import import_module

import pytest
from api.app import create_app
from client import ApiClient
from core.config import get_settings
from core.model_config_crypto import ModelConfigurationSecretProtector
from core.security import AuthContext, authenticate_request
from db.session import get_engine
from fastapi import HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from model_config_helpers import TEST_MODEL_CONFIG_API_KEY
from models.model_configuration import ModelConfiguration
from models.user import AdminAuditLog
from services.model_config_network import (
    ModelAuthenticationFailed,
    ModelCapabilitiesUnsupported,
    ModelEndpointForbidden,
    ModelNotFound,
    ModelProbeFailed,
    ModelProbeResult,
    ModelProviderUnreachable,
)
from sqlalchemy import text
from sqlmodel import Session, select

RUNTIME_PAYLOAD = {
    "temperature": 0.35,
    "context_window_tokens": 200_000,
    "chat_max_tokens": 12_000,
    "structured_max_tokens": 6_000,
}


def _service_module():
    try:
        return import_module("services.model_configuration_service")
    except ModuleNotFoundError:
        pytest.fail("model configuration service module is missing")


def _auth(request: Request) -> AuthContext:
    return AuthContext(
        user_id="local-user",
        role=request.headers.get("X-Test-Role", "user"),
    )


def _admin_headers() -> dict[str, str]:
    return {"X-Test-Role": "admin"}


@dataclass
class FakeProbe:
    failure: Exception | None = None

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def probe(self, base_url: str, api_key: str, model_name: str | None = None):
        self.calls.append((base_url, api_key, model_name))
        if self.failure is not None:
            raise self.failure
        return ModelProbeResult(
            base_url=base_url.rstrip("/"),
            models=("a-model", "z-model"),
            models_truncated=False,
            model_validated=model_name is not None,
            latency_ms=12,
        )


@pytest.fixture
def admin_client() -> ApiClient:
    app = create_app()
    app.dependency_overrides[authenticate_request] = _auth
    with ApiClient(app) as client:
        yield client


def _set_probe(client: ApiClient, probe: FakeProbe) -> None:
    client.app.state.model_configuration_service._prober = probe


def _delete_all_configurations() -> None:
    with get_engine().begin() as connection:
        connection.execute(text("DELETE FROM modelconfiguration"))


def test_all_model_configuration_routes_require_admin(admin_client: ApiClient) -> None:
    _set_probe(admin_client, FakeProbe())

    responses = [
        admin_client.get("/api/admin/model-config"),
        admin_client.post(
            "/api/admin/model-config/probe",
            json={"base_url": "https://api.example.test/v1", "api_key": "new-secret"},
        ),
        admin_client.put(
            "/api/admin/model-config",
            json={
                "base_url": "https://api.example.test/v1",
                "api_key": "new-secret",
                "model_name": "a-model",
                "expected_version": 1,
                **RUNTIME_PAYLOAD,
            },
        ),
    ]

    assert [response.status_code for response in responses] == [403, 403, 403]
    assert all(response.headers["Cache-Control"] == "no-store" for response in responses)


def test_admin_get_returns_no_store_safe_active_metadata(admin_client: ApiClient) -> None:
    response = admin_client.get("/api/admin/model-config", headers=_admin_headers())

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "configured": True,
        "id": "default-model-config",
        "version": 1,
        "provider": "openai_compatible",
        "base_url": "https://models.test.invalid/v1",
        "model_name": "test-model",
        "temperature": 0.2,
        "context_window_tokens": 32_000,
        "chat_max_tokens": 8_000,
        "structured_max_tokens": 8_000,
        "api_key_hint": "...7890",
        "validated_at": response.json()["validated_at"],
        "created_at": response.json()["created_at"],
        "created_by_user_id": "local-user",
        "created_by_email": "local@test.invalid",
    }
    serialized = response.text
    assert TEST_MODEL_CONFIG_API_KEY not in serialized
    assert "ciphertext" not in serialized.lower()


def test_admin_get_reports_unconfigured_without_secret_fields(admin_client: ApiClient) -> None:
    _delete_all_configurations()

    response = admin_client.get("/api/admin/model-config", headers=_admin_headers())

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"configured": False}


def test_required_active_configuration_uses_stable_not_configured_error() -> None:
    service_module = _service_module()
    _delete_all_configurations()
    service = service_module.ModelConfigurationService(get_settings(), prober=FakeProbe())

    with Session(get_engine()) as session:
        with pytest.raises(service_module.ModelNotConfigured) as exc_info:
            service.get_required_active(session)

    assert exc_info.value.code == "MODEL_NOT_CONFIGURED"
    assert exc_info.value.status_code == 503


def test_probe_reuses_active_key_and_never_returns_it(admin_client: ApiClient) -> None:
    probe = FakeProbe()
    _set_probe(admin_client, probe)

    response = admin_client.post(
        "/api/admin/model-config/probe",
        headers=_admin_headers(),
        json={
            "base_url": "https://api.example.test/v1",
            "model_name": "custom-model",
        },
    )

    assert response.status_code == 200
    assert probe.calls == [
        ("https://api.example.test/v1", TEST_MODEL_CONFIG_API_KEY, "custom-model")
    ]
    assert response.json() == {
        "base_url": "https://api.example.test/v1",
        "models": ["a-model", "z-model"],
        "models_truncated": False,
        "model_validated": True,
        "latency_ms": 12,
    }
    assert TEST_MODEL_CONFIG_API_KEY not in response.text


def test_first_probe_and_save_require_api_key(admin_client: ApiClient) -> None:
    _delete_all_configurations()
    _set_probe(admin_client, FakeProbe())

    probe_response = admin_client.post(
        "/api/admin/model-config/probe",
        headers=_admin_headers(),
        json={"base_url": "https://api.example.test/v1", "model_name": "a-model"},
    )
    save_response = admin_client.put(
        "/api/admin/model-config",
        headers=_admin_headers(),
        json={
            "base_url": "https://api.example.test/v1",
            "model_name": "a-model",
            "expected_version": 0,
            **RUNTIME_PAYLOAD,
        },
    )

    for response in (probe_response, save_response):
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "MODEL_CREDENTIALS_REQUIRED"
        assert response.headers["Cache-Control"] == "no-store"


def test_save_creates_new_immutable_version_and_secret_free_audit(
    admin_client: ApiClient,
) -> None:
    probe = FakeProbe()
    _set_probe(admin_client, probe)
    new_key = "new-provider-key-never-return-4321"

    response = admin_client.put(
        "/api/admin/model-config",
        headers=_admin_headers(),
        json={
            "base_url": "https://new.example.test/v1/",
            "api_key": new_key,
            "model_name": "custom-model",
            "expected_version": 1,
            **RUNTIME_PAYLOAD,
        },
    )

    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.json()["base_url"] == "https://new.example.test/v1"
    assert RUNTIME_PAYLOAD.items() <= response.json().items()
    assert new_key not in response.text
    with Session(get_engine()) as session:
        versions = session.exec(
            select(ModelConfiguration).order_by(ModelConfiguration.version)
        ).all()
        audits = session.exec(
            select(AdminAuditLog).where(AdminAuditLog.action == "model_config.updated")
        ).all()
    assert [(item.version, item.is_active) for item in versions] == [(1, False), (2, True)]
    assert versions[0].superseded_at is not None
    protector = ModelConfigurationSecretProtector(get_settings().model_config_encryption_key)
    assert protector.decrypt(versions[1].api_key_ciphertext) == new_key
    assert all(getattr(versions[1], name) == value for name, value in RUNTIME_PAYLOAD.items())
    assert len(audits) == 1
    assert RUNTIME_PAYLOAD.items() <= audits[0].detail.items()
    audit_text = repr(audits[0].detail)
    assert "api_key" not in audit_text
    assert new_key not in audit_text
    assert versions[1].api_key_ciphertext not in audit_text
    assert "Authorization" not in audit_text


def test_save_accepts_auto_temperature_as_explicit_null(
    admin_client: ApiClient,
) -> None:
    _set_probe(admin_client, FakeProbe())

    response = admin_client.put(
        "/api/admin/model-config",
        headers=_admin_headers(),
        json={
            "base_url": "https://new.example.test/v1",
            "model_name": "custom-model",
            "expected_version": 1,
            **RUNTIME_PAYLOAD,
            "temperature": None,
        },
    )

    assert response.status_code == 200
    assert response.json()["temperature"] is None
    with Session(get_engine()) as session:
        active = session.exec(
            select(ModelConfiguration).where(ModelConfiguration.is_active.is_(True))
        ).one()
    assert active.temperature is None


def test_save_still_requires_temperature_field(
    admin_client: ApiClient,
) -> None:
    payload = {
        "base_url": "https://new.example.test/v1",
        "model_name": "custom-model",
        "expected_version": 1,
        **RUNTIME_PAYLOAD,
    }
    payload.pop("temperature")

    response = admin_client.put(
        "/api/admin/model-config",
        headers=_admin_headers(),
        json=payload,
    )

    assert response.status_code == 422


def test_blank_key_reuses_active_secret_for_probe_and_new_version(
    admin_client: ApiClient,
) -> None:
    probe = FakeProbe()
    _set_probe(admin_client, probe)

    response = admin_client.put(
        "/api/admin/model-config",
        headers=_admin_headers(),
        json={
            "base_url": "https://new.example.test/v1",
            "api_key": "   ",
            "model_name": "custom-model",
            "expected_version": 1,
            **RUNTIME_PAYLOAD,
        },
    )

    assert response.status_code == 200
    assert probe.calls[0][1] == TEST_MODEL_CONFIG_API_KEY
    with Session(get_engine()) as session:
        active = session.exec(
            select(ModelConfiguration).where(ModelConfiguration.is_active.is_(True))
        ).one()
    protector = ModelConfigurationSecretProtector(get_settings().model_config_encryption_key)
    assert protector.decrypt(active.api_key_ciphertext) == TEST_MODEL_CONFIG_API_KEY


def test_failed_save_probe_leaves_active_version_unchanged(admin_client: ApiClient) -> None:
    _set_probe(admin_client, FakeProbe(failure=ModelProbeFailed("remote-secret-body")))

    response = admin_client.put(
        "/api/admin/model-config",
        headers=_admin_headers(),
        json={
            "base_url": "https://new.example.test/v1",
            "api_key": "failed-new-key",
            "model_name": "custom-model",
            "expected_version": 1,
            **RUNTIME_PAYLOAD,
        },
    )

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "MODEL_PROBE_FAILED"
    assert "remote-secret-body" not in response.text
    assert "failed-new-key" not in response.text
    with Session(get_engine()) as session:
        versions = session.exec(select(ModelConfiguration)).all()
    assert [(item.version, item.is_active) for item in versions] == [(1, True)]


def test_optimistic_version_conflict_does_not_probe_or_switch(admin_client: ApiClient) -> None:
    probe = FakeProbe()
    _set_probe(admin_client, probe)

    response = admin_client.put(
        "/api/admin/model-config",
        headers=_admin_headers(),
        json={
            "base_url": "https://new.example.test/v1",
            "api_key": "new-key",
            "model_name": "custom-model",
            "expected_version": 0,
            **RUNTIME_PAYLOAD,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MODEL_CONFIG_CHANGED"
    assert probe.calls == []
    with Session(get_engine()) as session:
        versions = session.exec(select(ModelConfiguration)).all()
    assert [(item.version, item.is_active) for item in versions] == [(1, True)]


@pytest.mark.parametrize(
    ("failure", "status_code", "code"),
    [
        (ModelEndpointForbidden("remote-secret-message"), 422, "MODEL_ENDPOINT_FORBIDDEN"),
        (ModelAuthenticationFailed("remote-secret-message"), 422, "MODEL_AUTH_FAILED"),
        (ModelNotFound("remote-secret-message"), 422, "MODEL_NOT_FOUND"),
        (ModelProviderUnreachable("remote-secret-message"), 502, "MODEL_PROVIDER_UNREACHABLE"),
        (
            ModelCapabilitiesUnsupported("remote-secret-message"),
            422,
            "MODEL_CAPABILITIES_UNSUPPORTED",
        ),
        (ModelProbeFailed("remote-secret-message"), 502, "MODEL_PROBE_FAILED"),
    ],
)
def test_probe_maps_stable_errors_without_exception_or_key_disclosure(
    admin_client: ApiClient,
    failure: Exception,
    status_code: int,
    code: str,
) -> None:
    request_key = "request-secret-key-should-never-return"
    _set_probe(admin_client, FakeProbe(failure=failure))

    response = admin_client.post(
        "/api/admin/model-config/probe",
        headers=_admin_headers(),
        json={
            "base_url": "https://api.example.test/v1",
            "api_key": request_key,
            "model_name": "custom-model",
        },
    )

    assert response.status_code == status_code
    assert response.json()["detail"]["code"] == code
    assert response.headers["Cache-Control"] == "no-store"
    assert "remote-secret-message" not in response.text
    assert request_key not in response.text


def test_validation_errors_redact_malformed_api_key(admin_client: ApiClient) -> None:
    malformed_secret = "malformed-secret-must-be-redacted"

    response = admin_client.post(
        "/api/admin/model-config/probe",
        headers=_admin_headers(),
        json={
            "base_url": "https://api.example.test/v1",
            "api_key": {"unexpected": malformed_secret},
        },
    )

    assert response.status_code == 422
    assert malformed_secret not in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_api_key_length_is_bounded_without_echoing_oversized_secret(
    admin_client: ApiClient,
) -> None:
    oversized_secret = "k" * 4097

    response = admin_client.post(
        "/api/admin/model-config/probe",
        headers=_admin_headers(),
        json={
            "base_url": "https://api.example.test/v1",
            "api_key": oversized_secret,
        },
    )

    assert response.status_code == 422
    assert oversized_secret not in response.text
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "runtime_overrides",
    [
        {"temperature": -0.01},
        {"temperature": 2.01},
        {"context_window_tokens": 0},
        {"context_window_tokens": -101},
        {"chat_max_tokens": 0},
        {"chat_max_tokens": -103},
        {"structured_max_tokens": 0},
        {"structured_max_tokens": -107},
        {"chat_max_tokens": 200_000},
        {"chat_max_tokens": 200_001},
        {"structured_max_tokens": 200_000},
        {"structured_max_tokens": 200_003},
    ],
)
def test_model_config_update_rejects_invalid_runtime_parameters_without_echo(
    admin_client: ApiClient,
    runtime_overrides: dict[str, int | float],
) -> None:
    secret = "invalid-runtime-secret-must-not-escape"
    payload = {
        "base_url": "https://invalid-runtime.example.test/v1",
        "api_key": secret,
        "model_name": "invalid-runtime-model",
        "expected_version": 1,
        **RUNTIME_PAYLOAD,
        **runtime_overrides,
    }

    response = admin_client.put(
        "/api/admin/model-config",
        headers=_admin_headers(),
        json=payload,
    )

    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert all(error.get("input") == "[REDACTED]" for error in response.json()["detail"])
    assert all(
        error.get("ctx", "[REDACTED]") == "[REDACTED]"
        for error in response.json()["detail"]
    )
    assert secret not in response.text
    assert payload["base_url"] not in response.text
    assert payload["model_name"] not in response.text


def test_probe_rejects_runtime_parameters_as_non_connection_input(
    admin_client: ApiClient,
) -> None:
    response = admin_client.post(
        "/api/admin/model-config/probe",
        headers=_admin_headers(),
        json={
            "base_url": "https://api.example.test/v1",
            "api_key": "probe-runtime-secret",
            **RUNTIME_PAYLOAD,
        },
    )

    assert response.status_code == 422
    assert all(error.get("input") == "[REDACTED]" for error in response.json()["detail"])
    assert "probe-runtime-secret" not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"base_url": {"api_key": "nested-base-url-secret"}},
        {"base_url": "https://api.example.test/v1?token=" + "s" * 2050},
        {
            "base_url": "https://api.example.test/v1",
            "api_key": {"unexpected": "malformed-key-secret"},
        },
    ],
)
def test_model_config_validation_redacts_all_input_and_context(
    admin_client: ApiClient,
    payload: dict[str, object],
) -> None:
    response = admin_client.post(
        "/api/admin/model-config/probe",
        headers=_admin_headers(),
        json=payload,
    )

    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert all(error.get("input") == "[REDACTED]" for error in response.json()["detail"])
    assert all(
        error.get("ctx", "[REDACTED]") == "[REDACTED]"
        for error in response.json()["detail"]
    )
    assert not any(
        secret in response.text
        for secret in (
            "nested-base-url-secret",
            "token=",
            "malformed-key-secret",
        )
    )


def test_non_model_config_validation_uses_fastapi_default_handler(
    admin_client: ApiClient,
) -> None:
    response = admin_client.get(
        "/api/admin/usage?start=not-a-datetime",
        headers=_admin_headers(),
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["input"] == "not-a-datetime"
    assert "Cache-Control" not in response.headers


def test_probe_error_returns_response_without_http_exception_chain() -> None:
    app = create_app()
    app.dependency_overrides[authenticate_request] = _auth
    captured_http_exceptions: list[tuple[BaseException | None, BaseException | None]] = []

    async def capture_http_exception(request: Request, exc: Exception):
        assert isinstance(exc, HTTPException)
        captured_http_exceptions.append((exc.__cause__, exc.__context__))
        return await http_exception_handler(request, exc)

    app.add_exception_handler(HTTPException, capture_http_exception)
    with ApiClient(app) as client:
        _set_probe(client, FakeProbe(failure=ModelProbeFailed("remote-secret-message")))
        response = client.post(
            "/api/admin/model-config/probe",
            headers=_admin_headers(),
            json={
                "base_url": "https://api.example.test/v1",
                "api_key": "request-secret-key",
            },
        )

    assert response.status_code == 502
    assert response.headers["Cache-Control"] == "no-store"
    assert captured_http_exceptions == []
    assert "remote-secret-message" not in response.text
    assert "request-secret-key" not in response.text


class BarrierProbe(FakeProbe):
    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__()
        self.barrier = barrier

    def probe(self, base_url: str, api_key: str, model_name: str | None = None):
        self.barrier.wait(timeout=10)
        return super().probe(base_url, api_key, model_name)


def test_advisory_lock_allows_only_one_concurrent_expected_version_switch() -> None:
    service_module = _service_module()
    barrier = threading.Barrier(2)
    service = service_module.ModelConfigurationService(
        get_settings(), prober=BarrierProbe(barrier)
    )
    outcomes: list[tuple[str, object]] = []
    outcomes_lock = threading.Lock()

    def switch(model_name: str) -> None:
        try:
            with Session(get_engine()) as session:
                configuration = service.update(
                    session,
                    actor_user_id="local-user",
                    request_id=f"request-{model_name}",
                    base_url="https://new.example.test/v1",
                    api_key="new-concurrent-key",
                    model_name=model_name,
                    **RUNTIME_PAYLOAD,
                    expected_version=1,
                )
            outcome: tuple[str, object] = ("ok", configuration.version)
        except Exception as exc:  # The exact service exception is asserted below.
            outcome = ("error", exc)
        with outcomes_lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=switch, args=("model-a",)),
        threading.Thread(target=switch, args=("model-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    assert [kind for kind, _ in outcomes].count("ok") == 1
    errors = [value for kind, value in outcomes if kind == "error"]
    assert len(errors) == 1
    assert isinstance(errors[0], service_module.ModelConfigurationChanged)
    with Session(get_engine()) as session:
        versions = session.exec(
            select(ModelConfiguration).order_by(ModelConfiguration.version)
        ).all()
    assert [(item.version, item.is_active) for item in versions] == [(1, False), (2, True)]
