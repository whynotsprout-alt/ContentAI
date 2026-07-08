import pytest
from client import ApiClient as TestClient
from core.config import Settings
from db.session import get_engine
from main import _normalize_origins, app, create_app
from pydantic import ValidationError
from sqlalchemy import inspect, text


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
    assert list(created_app.state._state) == [
        "settings",
        "ready",
        "agent_runtime",
        "catalog_service",
        "conversation_service",
    ]


def test_lifespan_uses_app_settings_for_database_and_runtime(monkeypatch):
    test_settings = Settings(
        env="development",
        database={"url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai"},
    )
    created_app = create_app(test_settings)
    validated_settings = []
    runtime_settings = []

    class DummyRuntime:
        pass

    dummy_runtime = DummyRuntime()
    monkeypatch.setattr(
        "main.get_runtime_container",
        lambda settings: runtime_settings.append(settings) or dummy_runtime,
    )
    monkeypatch.setattr(
        "main.validate_database",
        lambda settings: validated_settings.append(settings),
    )
    monkeypatch.setattr("main.close_database", lambda: None)
    monkeypatch.setattr("main._shutdown_agent_runtime", lambda: None)

    def fail_get_settings():
        raise AssertionError("lifespan should use app.state.settings")

    monkeypatch.setattr("main.get_settings", fail_get_settings)

    with TestClient(created_app) as client:
        response = client.get("/api/ready")

    assert response.status_code == 200
    assert validated_settings == [test_settings]
    assert runtime_settings == [test_settings]
    assert created_app.state.agent_runtime is dummy_runtime


def test_settings_reject_sqlite_database_url():
    with pytest.raises(ValidationError):
        Settings(
            env="development",
            database={"url": "sqlite:///./data/test.db"},
        )


def test_settings_reads_dotenv_file(tmp_path, monkeypatch):
    for name in (
        "CONTENTAI_ENV",
        "CONTENTAI_DATABASE__URL",
        "CONTENTAI_SEARCH__TRAFFIC_RELAY_BASE_URL",
        "CONTENTAI_SEARCH__TRAFFIC_RELAY_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "CONTENTAI_DATABASE__URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai",
                "CONTENTAI_SEARCH__TRAFFIC_RELAY_BASE_URL=https://dotenv.example/v1",
                "CONTENTAI_SEARCH__TRAFFIC_RELAY_API_KEY=dotenv-key",
            ]
        ),
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_file)

    assert settings.search.traffic_relay_base_url == "https://dotenv.example/v1"
    assert settings.search.traffic_relay_api_key.get_secret_value() == "dotenv-key"


def test_settings_reads_process_environment(monkeypatch):
    monkeypatch.setenv("CONTENTAI_ENV", "development")
    monkeypatch.setenv(
        "CONTENTAI_DATABASE__URL",
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_env",
    )
    monkeypatch.setenv("CONTENTAI_SEARCH__TRAFFIC_RELAY_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("CONTENTAI_SEARCH__TRAFFIC_RELAY_API_KEY", "env-key")

    settings = Settings(_env_file=None)

    assert settings.database.url.endswith("/contentai_env")
    assert settings.search.traffic_relay_base_url == "https://env.example/v1"
    assert settings.search.traffic_relay_api_key.get_secret_value() == "env-key"


def test_settings_requires_database_url(monkeypatch):
    monkeypatch.delenv("CONTENTAI_DATABASE__URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_non_test_env_rejects_test_database():
    with pytest.raises(ValidationError):
        Settings(
            env="development",
            database={
                "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test",
            },
        )


def test_test_env_requires_test_database():
    with pytest.raises(ValidationError):
        Settings(
            env="test",
            database={
                "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai",
            },
        )


def test_production_requires_auth_frontend_origins_and_traffic_relay_key():
    with pytest.raises(ValidationError):
        Settings(
            env="production",
            database={
                "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai",
            },
        )


def test_development_frontend_origins_validate_final_list():
    settings = Settings(
        env="development",
        database={"url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai"},
    )

    assert "http://localhost:5173" in settings.server.frontend_origins
    assert "http://127.0.0.1:5173" in settings.server.frontend_origins


def test_chat_schema_uses_conversation_driven_contract():
    inspector = inspect(get_engine())
    table_names = set(inspector.get_table_names())

    assert "agentrun" not in table_names
    assert "agentrunevent" not in table_names
    assert {"agentinvocation", "agentexecution", "toolexecution"}.issubset(table_names)

    session_columns = {column["name"] for column in inspector.get_columns("chatsession")}
    message_columns = {column["name"] for column in inspector.get_columns("chatmessage")}
    execution_columns = {column["name"] for column in inspector.get_columns("agentexecution")}

    assert {"account_id", "langgraph_thread_id"}.issubset(session_columns)
    assert {"invocation_id", "model_name", "input_tokens", "output_tokens", "latency_ms"}.issubset(
        message_columns
    )
    assert {"invocation_id", "checkpoint_id", "status", "cancel_requested_at"}.issubset(
        execution_columns
    )
