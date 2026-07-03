import pytest
from core.config import Settings
from fastapi.testclient import TestClient
from main import _normalize_origins, app, create_app
from pydantic import ValidationError


def test_normalize_origins_splits_comma_separated_values_and_deduplicates():
    assert _normalize_origins(
        [
            "https://example.com, https://admin.example.com",
            "https://example.com",
            "",
        ]
    ) == ["https://example.com", "https://admin.example.com"]


def test_ready_endpoint_reflects_lifespan_state():
    with TestClient(app) as client:
        response = client.get("/api/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert app.state.ready is False


def test_startup_registers_only_runtime_services():
    created_app = create_app()

    with TestClient(created_app) as client:
        response = client.get("/api/ready")

    assert response.status_code == 200
    assert list(created_app.state._state) == ["catalog_service", "run_worker_task", "ready"]


def test_settings_reject_sqlite_database_url():
    with pytest.raises(ValidationError):
        Settings(
            CONTENTAI_ENV="development",
            CONTENTAI_DATABASE_URL="sqlite:///./data/test.db",
        )
