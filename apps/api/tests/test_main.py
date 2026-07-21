import pytest
from api.app import _normalize_origins, app, create_app
from client import ApiClient as TestClient
from core.config import Settings
from db.session import get_engine
from models.schemas import AgentVersionCreate
from pydantic import ValidationError
from services.service_heartbeat import REQUIRED_WORKER_QUEUES, upsert_service_heartbeat
from sqlalchemy import inspect
from sqlmodel import Session


def _seed_required_service_heartbeats() -> None:
    with Session(get_engine()) as session:
        upsert_service_heartbeat(
            session,
            service_name="dispatcher",
            instance_id="test:dispatcher",
        )
        for queue_name in REQUIRED_WORKER_QUEUES:
            upsert_service_heartbeat(
                session,
                service_name="worker",
                instance_id=f"test:{queue_name}",
                queue_name=queue_name,
            )
        session.commit()


def test_normalize_origins_splits_comma_separated_values_and_deduplicates():
    assert _normalize_origins(
        [
            "https://example.com, https://admin.example.com",
            "https://example.com",
            "",
        ]
    ) == ["https://example.com", "https://admin.example.com"]


def test_ready_endpoint_reflects_lifespan_state():
    _seed_required_service_heartbeats()

    with TestClient(app) as client:
        response = client.get("/api/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["checks"] == {
        "database": True,
        "alembic_version": "202607210001",
        "alembic_head": "202607210001",
        "database_revision_current": True,
        "checkpoint": True,
        "checkpoint_tables": {
            "checkpoint_migrations": True,
            "checkpoints": True,
            "checkpoint_blobs": True,
            "checkpoint_writes": True,
        },
        "redis": True,
        "queue": True,
        "services": {"dispatcher": True, "workers_missing": []},
        "outbox_pending": 0,
        "outbox_unclaimed_published": 0,
        "outbox_oldest_age_seconds": 0,
        "outbox_within_threshold": True,
    }
    assert app.state.ready is False


def test_startup_registers_only_runtime_services():
    _seed_required_service_heartbeats()
    created_app = create_app()

    with TestClient(created_app) as client:
        response = client.get("/api/ready")

    assert response.status_code == 200
    assert list(created_app.state._state) == [
        "settings",
        "runtime",
        "execution_dispatcher_factory",
        "ready",
        "agent_service",
        "catalog_service",
        "conversation_service",
        "auth_service",
        "admin_service",
        "rate_limiter",
    ]


def test_ready_response_includes_request_id_header():
    _seed_required_service_heartbeats()
    created_app = create_app()

    with TestClient(created_app) as client:
        response = client.get("/api/ready")

    assert response.status_code == 200
    assert response.headers["X-Request-ID"]


def test_lifespan_uses_app_settings_for_database_and_agent_service(monkeypatch):
    _seed_required_service_heartbeats()
    test_settings = Settings(
        env="test",
        database={"url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"},
    )
    created_app = create_app(test_settings)
    initialized_settings = []
    agent_service_settings = []
    agent_service_runtimes = []

    class DummyAgentService:
        def __init__(self, settings, runtime=None):
            self.settings = settings
            agent_service_settings.append(settings)
            agent_service_runtimes.append(runtime)
            self.runtime = runtime or object()
            self.runner = object()

        def close(self):
            pass

        def start(self):
            pass

    monkeypatch.setattr("api.app.AgentService", DummyAgentService)
    monkeypatch.setattr(
        "api.app.init_database",
        lambda settings: initialized_settings.append(settings),
    )
    monkeypatch.setattr("api.app.close_database", lambda: None)

    def fail_get_settings():
        raise AssertionError("lifespan should use app.state.settings")

    monkeypatch.setattr("api.app.get_settings", fail_get_settings)

    with TestClient(created_app) as client:
        response = client.get("/api/ready")

    assert response.status_code == 200
    assert initialized_settings == [test_settings]
    assert agent_service_settings == [test_settings]
    assert agent_service_runtimes == [None]
    assert isinstance(created_app.state.agent_service, DummyAgentService)


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
            _env_file=None,
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


def test_agent_version_validates_hotspot_sources():
    payload = AgentVersionCreate(
        content_prompt="Create concise content.",
        hotspot_sources=["Weibo", "weibo", "douyin"],
    )

    assert payload.hotspot_sources == ["weibo", "douyin"]

    with pytest.raises(ValidationError):
        AgentVersionCreate(
            content_prompt="Create concise content.",
            hotspot_sources=["weibo", "cb"],
        )


def test_chat_schema_uses_conversation_driven_contract():
    inspector = inspect(get_engine())
    table_names = set(inspector.get_table_names())

    assert "agentrun" not in table_names
    assert "agentrunevent" not in table_names
    assert {"agentinvocation", "agentexecution", "toolexecution"}.issubset(
        table_names
    )
    assert "agentevent" not in table_names

    session_columns = {column["name"] for column in inspector.get_columns("chatsession")}
    message_columns = {column["name"] for column in inspector.get_columns("chatmessage")}
    execution_columns = {column["name"] for column in inspector.get_columns("agentexecution")}

    assert {"agent_id", "agent_version_id", "langgraph_thread_id", "user_id"}.issubset(
        session_columns
    )
    assert {"invocation_id", "session_id", "role", "message_type", "content"}.issubset(
        message_columns
    )
    assert not {"parent_message_id", "payload", "model_name", "input_tokens"} & message_columns
    assert {
        "invocation_id",
        "trace_id",
        "latest_checkpoint_id",
        "status",
        "cancel_requested_at",
    }.issubset(execution_columns)
