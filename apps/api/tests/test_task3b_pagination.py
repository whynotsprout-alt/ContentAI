from __future__ import annotations

from datetime import UTC, datetime, timedelta
from time import perf_counter

from api.app import create_app
from client import ApiClient as TestClient
from core.config import Settings
from db.session import get_engine
from models.agent import AgentProfile, AgentVersion
from models.chat import ChatMessage, ChatSession
from models.enums import MessageRole
from models.user import AppUser
from services.admin_service import AdminService
from services.auth_service import AuthService
from services.pagination import apply_descending_cursor
from sqlalchemy import event, text
from sqlalchemy import select as sa_select
from sqlalchemy.dialects import postgresql
from sqlmodel import Session


def _settings() -> Settings:
    return Settings(
        env="test",
        database={
            "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
        },
        auth={
            "bootstrap_admin_email": "admin@example.com",
            "bootstrap_admin_password": "admin password 123",
        },
    )


def _login(client: TestClient, email: str, password: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    return {"X-CSRF-Token": response.cookies["contentai_csrf"]}


def _seed_sessions(settings: Settings, *, count: int = 51) -> tuple[str, str]:
    at = datetime(2026, 7, 20, 10, tzinfo=UTC)
    with Session(get_engine(settings)) as session:
        user = session.get(AppUser, "local-user")
        assert user is not None
        user.email = "local@example.com"
        user.email_normalized = "local@example.com"
        user.password_hash = AuthService(settings).password_hash.hash("local password 123")
        session.add(user)
        other_agent = AgentProfile(id="other-agent", user_id=user.id, name="Other")
        session.add(other_agent)
        session.flush()
        other_version = AgentVersion(
            id="other-agent-v1", agent_id=other_agent.id, version=1, content_prompt="test"
        )
        session.add(other_version)
        for index in range(count):
            session.add(
                ChatSession(
                    id=f"ses_page_{index:03d}",
                    agent_id="default-agent",
                    agent_version_id="default-agent-v1",
                    user_id=user.id,
                    updated_at=at,
                )
            )
        # Newer sessions for another agent must not consume the selected-agent page.
        for index in range(60):
            session.add(
                ChatSession(
                    id=f"ses_other_{index:03d}",
                    agent_id=other_agent.id,
                    agent_version_id=other_version.id,
                    user_id=user.id,
                    updated_at=at + timedelta(days=1),
                )
            )
        session.commit()
    return "ses_page_050", "ses_page_000"


def test_chat_session_keyset_contract_and_agent_filter() -> None:
    settings = _settings()
    app = create_app(settings)
    newest, oldest = _seed_sessions(settings)

    with TestClient(app) as client:
        headers = _login(client, "local@example.com", "local password 123")
        first = client.get("/api/chat/sessions?agent_id=default-agent", headers=headers)
        assert first.status_code == 200
        payload = first.json()
        assert set(payload) == {"items", "next_cursor"}
        assert len(payload["items"]) == 50
        assert payload["items"][0]["session_id"] == newest
        assert payload["next_cursor"].endswith("Z|ses_page_001")

        second = client.get(
            "/api/chat/sessions",
            params={"agent_id": "default-agent", "cursor": payload["next_cursor"]},
            headers=headers,
        )
        assert second.status_code == 200
        assert [item["session_id"] for item in second.json()["items"]] == [oldest]

        assert client.get("/api/chat/sessions?limit=200", headers=headers).status_code == 200
        assert client.get("/api/chat/sessions?limit=201", headers=headers).status_code == 422
        assert client.get("/api/chat/sessions?cursor=bad", headers=headers).json()["detail"][
            "code"
        ] == (
            "INVALID_CURSOR"
        )


def test_admin_lists_are_keyset_pages_and_old_parameters_are_removed() -> None:
    settings = _settings()
    app = create_app(settings)
    at = datetime(2026, 7, 20, 9, tzinfo=UTC)
    with Session(get_engine(settings)) as session:
        local = session.get(AppUser, "local-user")
        assert local is not None
        local.email = "local@example.com"
        local.email_normalized = "local@example.com"
        session.add(local)
        for index in range(51):
            session.add(
                AppUser(
                    id=f"usr_page_{index:03d}",
                    email=f"page-{index}@example.com",
                    email_normalized=f"page-{index}@example.com",
                    password_hash="test-only-password-hash",
                    created_at=at,
                )
            )
        session.commit()

    with TestClient(app) as client:
        headers = _login(client, "admin@example.com", "admin password 123")
        first = client.get("/api/admin/users", headers=headers)
        assert first.status_code == 200
        payload = first.json()
        assert set(payload) == {"items", "next_cursor"}
        assert len(payload["items"]) == 50
        second = client.get(
            "/api/admin/users", headers=headers, params={"cursor": payload["next_cursor"]}
        )
        ids = [item["id"] for item in payload["items"] + second.json()["items"]]
        assert len(ids) == len(set(ids)) == 53  # seeded users, local user, and bootstrap admin
        assert client.get("/api/admin/users?limit=201", headers=headers).status_code == 422
        invalid = client.get("/api/admin/users?cursor=2026-07-20T10:00:00|usr_bad", headers=headers)
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "INVALID_CURSOR"
        assert client.get("/api/admin/users?page=2", headers=headers).status_code == 422
        assert client.get("/api/admin/users?page_size=2", headers=headers).status_code == 422


def test_admin_session_detail_excludes_messages_and_messages_page_independently() -> None:
    settings = _settings()
    app = create_app(settings)
    with Session(get_engine(settings)) as session:
        chat = ChatSession(
            id="ses_admin_page",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )
        session.add(chat)
        session.flush()
        at = datetime(2026, 7, 20, 8, tzinfo=UTC)
        session.add_all(
            ChatMessage(
                id=f"msg_page_{index:03d}",
                session_id=chat.id,
                role=MessageRole.user,
                content=f"message {index}",
                created_at=at,
            )
            for index in range(51)
        )
        session.commit()

    with TestClient(app) as client:
        headers = _login(client, "admin@example.com", "admin password 123")
        detail = client.get("/api/admin/sessions/ses_admin_page", headers=headers)
        assert detail.status_code == 200
        assert "messages" not in detail.json()

        first = client.get("/api/admin/sessions/ses_admin_page/messages", headers=headers)
        assert first.status_code == 200
        payload = first.json()
        assert set(payload) == {"items", "next_cursor"}
        assert len(payload["items"]) == 50
        second = client.get(
            "/api/admin/sessions/ses_admin_page/messages",
            headers=headers,
            params={"cursor": payload["next_cursor"]},
        )
        ids = [item["id"] for item in payload["items"] + second.json()["items"]]
        assert len(ids) == len(set(ids)) == 51
        assert client.get(
            "/api/admin/sessions/ses_admin_page/messages?before=anything", headers=headers
        ).status_code == 422
        assert client.get(
            "/api/admin/sessions/ses_admin_page/messages?limit=201", headers=headers
        ).status_code == 422

        session_page = client.get("/api/admin/users/local-user/sessions", headers=headers)
        assert session_page.status_code == 200
        assert set(session_page.json()) == {"items", "next_cursor"}
        assert client.get(
            "/api/admin/users/local-user/sessions?limit=201", headers=headers
        ).status_code == 422


def test_deep_cursor_compiles_to_postgres_tuple_comparison() -> None:
    statement = apply_descending_cursor(
        sa_select(ChatSession), ChatSession.updated_at, ChatSession.id,
        "2026-07-20T10:00:00Z|ses_001",
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "updated_at, chatsession.id" in sql
    assert "<" in sql
    assert " OR " not in sql.upper()


def test_admin_user_page_uses_constant_batched_query_count() -> None:
    settings = _settings()
    auth_service = AuthService(settings)
    with Session(get_engine(settings)) as session:
        for index in range(200):
            session.add(
                AppUser(
                    id=f"usr_capacity_{index:03d}",
                    email=f"capacity-{index}@example.com",
                    email_normalized=f"capacity-{index}@example.com",
                    password_hash="test-only-password-hash",
                )
            )
        session.commit()
        service = AdminService(auth_service)
        statements: list[str] = []

        def capture(*args: object, **kwargs: object) -> None:
            statement = args[2] if len(args) > 2 else ""
            if isinstance(statement, str) and statement.strip().upper().startswith("SELECT"):
                statements.append(statement)

        engine = get_engine(settings)
        event.listen(engine, "before_cursor_execute", capture)
        try:
            response = service.list_users(session, search="capacity", limit=200)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert len(response.items) == 200
        assert len(statements) <= 4


def test_large_admin_message_page_stays_below_one_mib_and_resumes() -> None:
    settings = _settings()
    app = create_app(settings)
    with Session(get_engine(settings)) as session:
        chat = ChatSession(
            id="ses_large_page",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )
        session.add(chat)
        session.flush()
        at = datetime(2026, 7, 20, 7, tzinfo=UTC)
        session.add_all(
            ChatMessage(
                id=f"msg_large_{index:03d}",
                session_id=chat.id,
                role=MessageRole.user,
                content="x" * 200_000,
                created_at=at + timedelta(seconds=index),
            )
            for index in range(6)
        )
        session.commit()

    with TestClient(app) as client:
        headers = _login(client, "admin@example.com", "admin password 123")
        first = client.get("/api/admin/sessions/ses_large_page/messages", headers=headers)
        assert len(first.content) <= 1024 * 1024
        payload = first.json()
        assert payload["next_cursor"] is not None
        second = client.get(
            "/api/admin/sessions/ses_large_page/messages",
            headers=headers,
            params={"cursor": payload["next_cursor"]},
        )
        ids = [item["id"] for item in payload["items"] + second.json()["items"]]
        assert len(ids) == len(set(ids)) == 6


def test_100k_deep_cursor_capacity_uses_bounded_index_scan() -> None:
    settings = _settings()
    engine = get_engine(settings)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO chatsession (
                    id, title, agent_id, agent_version_id, langgraph_thread_id,
                    user_id, created_at, updated_at
                )
                SELECT
                    'ses_capacity_' || lpad(value::text, 6, '0'),
                    'Capacity', 'default-agent', 'default-agent-v1',
                    'thr_capacity_' || lpad(value::text, 6, '0'),
                    'local-user',
                    timestamptz '2026-01-01 00:00:00+00' + value * interval '1 second',
                    timestamptz '2026-01-01 00:00:00+00' + value * interval '1 second'
                FROM generate_series(1, 100000) AS value
                """
            )
        )
        connection.execute(text("ANALYZE chatsession"))
        query = text(
            """
            SELECT id, updated_at
            FROM chatsession
            WHERE user_id = :user_id
              AND (updated_at, id) < (:cursor_at, :cursor_id)
            ORDER BY updated_at DESC, id DESC
            LIMIT 51
            """
        )
        params = {
            "user_id": "local-user",
            "cursor_at": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=50_000),
            "cursor_id": "ses_capacity_050000",
        }
        plan = connection.execute(
            text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query.text), params
        ).scalar_one()[0]

        def total(key: str, node: dict[str, object]) -> int:
            nested = node.get("Plans", [])
            children = nested if isinstance(nested, list) else []
            return int(node.get(key, 0) or 0) + sum(
                total(key, child) for child in children if isinstance(child, dict)
            )

        assert total("Rows Removed by Filter", plan["Plan"]) <= 50
        assert total("Shared Hit Blocks", plan["Plan"]) <= 100
        assert "Index" in str(plan["Plan"])

        timings: list[float] = []
        for _ in range(20):
            started = perf_counter()
            assert len(connection.execute(query, params).all()) == 51
            timings.append((perf_counter() - started) * 1000)
        p95 = sorted(timings)[18]
        assert p95 <= 100
        assert p95 <= 500
