from fastapi.testclient import TestClient
from main import _normalize_origins, app, create_app


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
    assert list(created_app.state._state) == ["catalog_service", "ready"]
