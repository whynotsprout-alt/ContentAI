from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from time import perf_counter

from auth_helpers import auth_headers, resolve_test_auth_context
from client import ApiClient as TestClient
from contentai.api.app import create_app
from contentai.core.config import Settings
from contentai.core.security import AuthContext, authenticate_request
from contentai.db.session import get_engine
from contentai.models.agent import AgentProfile, AgentVersion
from contentai.models.chat import ChatMessage, ChatSession
from contentai.models.enums import MessageRole
from contentai.models.user import AppUser
from contentai.services.admin_service import AdminService
from contentai.services.auth_service import AuthService
from contentai.services.catalog_service import CatalogService
from contentai.services.pagination import CursorSigner, apply_descending_cursor, encode_cursor
from model_config_helpers import resolve_test_database_url
from sqlalchemy import event, text
from sqlalchemy import select as sa_select
from sqlalchemy.dialects import postgresql
from sqlmodel import Session


def _settings() -> Settings:
    return Settings(
        env="test",
        database={"url": resolve_test_database_url()},
        auth={
            "bootstrap_admin_email": "admin@example.com",
            "bootstrap_admin_password": "admin password 123",
        },
    )


def _login(client: TestClient, email: str, password: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    return {"X-CSRF-Token": response.cookies["contentai_csrf"]}


def _tamper_cursor_payload(cursor: str, **changes: str) -> str:
    version, encoded, signature = cursor.split(".")
    payload = json.loads(
        base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
    )
    payload.update(changes)
    tampered = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{version}.{tampered}.{signature}"


def _catalog_app(settings: Settings):
    app = create_app(settings)
    app.dependency_overrides[authenticate_request] = resolve_test_auth_context
    return app


def _seed_agent_catalog(
    settings: Settings,
    *,
    user_id: str,
    count: int,
    prefix: str,
) -> list[str]:
    created_at = datetime(2026, 7, 21, 10, tzinfo=UTC)
    agent_ids = [f"{prefix}-agent-{index:04d}" for index in range(count)]
    with Session(get_engine(settings)) as session:
        if session.get(AppUser, user_id) is None:
            email = f"{user_id.replace('_', '-')}@example.test"
            session.add(
                AppUser(
                    id=user_id,
                    email=email,
                    email_normalized=email,
                    password_hash="test-only-password-hash",
                )
            )
            session.flush()
        for index, agent_id in enumerate(agent_ids):
            session.add(
                AgentProfile(
                    id=agent_id,
                    user_id=user_id,
                    name=f"Catalog Agent {index:04d}",
                    description=f"Catalog summary {index:04d}",
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
            session.add(
                AgentVersion(
                    id=f"{agent_id}-v1",
                    agent_id=agent_id,
                    version=1,
                    topic_scoring_prompt=f"Topic prompt {index:04d}",
                    content_prompt=f"Content prompt {index:04d}",
                    hotspot_sources=["weibo", "douyin"],
                    created_at=created_at,
                )
            )
        session.commit()
    return sorted(agent_ids, reverse=True)


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


def test_agent_catalog_keyset_pages_scope_and_summary_contract() -> None:
    settings = _settings()
    expected_ids = _seed_agent_catalog(
        settings,
        user_id="catalog-user",
        count=5,
        prefix="catalog",
    )
    _seed_agent_catalog(
        settings,
        user_id="catalog-other-user",
        count=2,
        prefix="catalog-other",
    )
    headers = auth_headers(user_id="catalog-user")

    with TestClient(_catalog_app(settings)) as client:
        first = client.get("/api/agents", params={"limit": 2}, headers=headers)
        assert first.status_code == 200
        first_payload = first.json()
        assert set(first_payload) == {"items", "next_cursor"}
        assert [item["id"] for item in first_payload["items"]] == expected_ids[:2]
        assert first_payload["next_cursor"] is not None

        all_items = list(first_payload["items"])
        cursor = first_payload["next_cursor"]
        while cursor:
            page = client.get(
                "/api/agents",
                params={"cursor": cursor, "limit": 2},
                headers=headers,
            )
            assert page.status_code == 200
            payload = page.json()
            all_items.extend(payload["items"])
            cursor = payload["next_cursor"]

        ids = [item["id"] for item in all_items]
        assert ids == expected_ids
        assert len(ids) == len(set(ids)) == 5

        summary = first_payload["items"][0]
        assert set(summary) == {"id", "name", "description", "current_version"}
        assert set(summary["current_version"]) == {"id", "agent_id", "version"}
        assert "topic_scoring_prompt" not in summary["current_version"]
        assert "content_prompt" not in summary["current_version"]
        assert "hotspot_sources" not in summary["current_version"]

        detail = client.get(f"/api/agents/{summary['id']}", headers=headers)
        assert detail.status_code == 200
        detail_version = detail.json()["current_version"]
        assert detail_version["topic_scoring_prompt"].startswith("Topic prompt")
        assert detail_version["content_prompt"].startswith("Content prompt")
        assert detail_version["hotspot_sources"] == ["weibo", "douyin"]

        assert client.get("/api/agents?limit=200", headers=headers).status_code == 200
        assert client.get("/api/agents?limit=201", headers=headers).status_code == 422

        tampered = _tamper_cursor_payload(
            first_payload["next_cursor"], id="catalog-agent-tampered"
        )
        malformed = client.get("/api/agents?cursor=bad", headers=headers)
        assert malformed.status_code == 422
        assert malformed.json()["detail"]["code"] == "INVALID_CURSOR"

        invalid = client.get("/api/agents", params={"cursor": tampered}, headers=headers)
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "INVALID_CURSOR"

        cross_user = client.get(
            "/api/agents",
            params={"cursor": first_payload["next_cursor"]},
            headers=auth_headers(user_id="catalog-other-user"),
        )
        assert cross_user.status_code == 422
        assert cross_user.json()["detail"]["code"] == "INVALID_CURSOR"

        scoped_ids = expected_ids[:3]
        scoped = client.get(
            "/api/agents",
            params={"limit": 1},
            headers=auth_headers(user_id="catalog-user", agents=scoped_ids),
        )
        assert scoped.status_code == 200
        scoped_cursor = scoped.json()["next_cursor"]
        assert scoped_cursor is not None
        scoped_next = client.get(
            "/api/agents",
            params={"cursor": scoped_cursor, "limit": 1},
            headers=auth_headers(user_id="catalog-user", agents=scoped_ids),
        )
        assert scoped_next.status_code == 200
        assert {
            item["id"] for item in scoped.json()["items"] + scoped_next.json()["items"]
        } <= set(scoped_ids)

        cross_permission = client.get(
            "/api/agents",
            params={"cursor": scoped_cursor},
            headers=auth_headers(user_id="catalog-user", agents=expected_ids[1:4]),
        )
        assert cross_permission.status_code == 422
        assert cross_permission.json()["detail"]["code"] == "INVALID_CURSOR"


def test_agent_catalog_page_uses_two_queries_at_small_and_max_limits() -> None:
    settings = _settings()
    _seed_agent_catalog(
        settings,
        user_id="catalog-query-user",
        count=201,
        prefix="catalog-query",
    )
    service = CatalogService(settings)
    auth = AuthContext(user_id="catalog-query-user")
    engine = get_engine(settings)

    with Session(engine) as session:
        query_counts: list[int] = []
        for limit in (1, 200):
            statements: list[str] = []

            def capture(
                *args: object,
                captured: list[str] = statements,
                **kwargs: object,
            ) -> None:
                statement = args[2] if len(args) > 2 else ""
                if isinstance(statement, str) and statement.strip().upper().startswith("SELECT"):
                    captured.append(statement)

            event.listen(engine, "before_cursor_execute", capture)
            try:
                response = service.list_agents(session, auth, limit=limit)
            finally:
                event.remove(engine, "before_cursor_execute", capture)
            assert len(response.items) == limit
            assert response.next_cursor is not None
            query_counts.append(len(statements))

    assert query_counts == [2, 2]


def test_agent_catalog_deep_cursor_uses_bounded_composite_index_scan() -> None:
    settings = _settings()
    engine = get_engine(settings)
    with Session(engine) as session:
        session.add(
            AppUser(
                id="catalog-capacity-user",
                email="catalog-capacity@example.test",
                email_normalized="catalog-capacity@example.test",
                password_hash="test-only-password-hash",
            )
        )
        session.commit()

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO agentprofile (
                    id, user_id, name, description, created_at, updated_at
                )
                SELECT
                    'catalog-capacity-agent-' || lpad(value::text, 6, '0'),
                    'catalog-capacity-user',
                    'Capacity Agent ' || lpad(value::text, 6, '0'),
                    '',
                    timestamptz '2026-01-01 00:00:00+00' + value * interval '1 second',
                    timestamptz '2026-01-01 00:00:00+00' + value * interval '1 second'
                FROM generate_series(1, 100000) AS value
                """
            )
        )
        connection.execute(text("ANALYZE agentprofile"))
        query = text(
            """
            SELECT id, created_at
            FROM agentprofile
            WHERE user_id = :user_id
              AND (created_at, id) < (:cursor_at, :cursor_id)
            ORDER BY created_at DESC, id DESC
            LIMIT 201
            """
        )
        params = {
            "user_id": "catalog-capacity-user",
            "cursor_at": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=50_000),
            "cursor_id": "catalog-capacity-agent-050000",
        }
        plan = connection.execute(
            text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query.text), params
        ).scalar_one()[0]

        def find_index_node(node: dict[str, object]) -> dict[str, object] | None:
            if node.get("Index Name") == "ix_agentprofile_user_created_id":
                return node
            children = node.get("Plans", [])
            if not isinstance(children, list):
                return None
            for child in children:
                if isinstance(child, dict) and (match := find_index_node(child)) is not None:
                    return match
            return None

        root = plan["Plan"]
        assert isinstance(root, dict)
        index_node = find_index_node(root)
        assert index_node is not None
        assert int(index_node.get("Actual Rows", 0) or 0) <= 201
        assert int(index_node.get("Rows Removed by Filter", 0) or 0) <= 1
        shared_blocks = int(index_node.get("Shared Hit Blocks", 0) or 0) + int(
            index_node.get("Shared Read Blocks", 0) or 0
        )
        assert shared_blocks <= 256


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
        assert payload["next_cursor"].count(".") == 2

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


def test_chat_message_cursor_round_trips_through_session_detail() -> None:
    settings = _settings()
    app = create_app(settings)
    _seed_sessions(settings, count=1)
    at = datetime(2026, 7, 20, 9, tzinfo=UTC)
    with Session(get_engine(settings)) as session:
        session.add_all(
            ChatMessage(
                id=f"msg_chat_page_{index:03d}",
                session_id="ses_page_000",
                role=MessageRole.user,
                content=f"message {index}",
                created_at=at,
            )
            for index in range(51)
        )
        session.commit()

    with TestClient(app) as client:
        headers = _login(client, "local@example.com", "local password 123")
        first = client.get("/api/chat/sessions/ses_page_000", headers=headers)
        assert first.status_code == 200
        payload = first.json()
        assert len(payload["messages"]) == 50
        assert payload["next_cursor"].startswith("v1.")

        second = client.get(
            "/api/chat/sessions/ses_page_000",
            params={"cursor": payload["next_cursor"]},
            headers=headers,
        )
        assert second.status_code == 200
        ids = [item["id"] for item in payload["messages"] + second.json()["messages"]]
        assert len(ids) == len(set(ids)) == 51


def test_cursor_is_signed_and_bound_to_timestamp_row_and_scope() -> None:
    settings = _settings()
    app = create_app(settings)
    newest, _ = _seed_sessions(settings)

    with TestClient(app) as client:
        headers = _login(client, "local@example.com", "local password 123")
        first = client.get("/api/chat/sessions?agent_id=default-agent", headers=headers).json()
        cursor = first["next_cursor"]
        assert cursor.count(".") == 2

        tampered_timestamp = _tamper_cursor_payload(cursor, at="2026-07-20T11:00:00Z")
        assert client.get(
            "/api/chat/sessions",
            params={"agent_id": "default-agent", "cursor": tampered_timestamp},
            headers=headers,
        ).status_code == 422

        tampered_row = _tamper_cursor_payload(cursor, id="ses_page_002")
        assert client.get(
            "/api/chat/sessions",
            params={"agent_id": "default-agent", "cursor": tampered_row},
            headers=headers,
        ).status_code == 422

        missing_signature = ".".join(cursor.split(".")[:2])
        assert client.get(
            "/api/chat/sessions",
            params={"agent_id": "default-agent", "cursor": missing_signature},
            headers=headers,
        ).status_code == 422

        wrong_signature = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
        assert client.get(
            "/api/chat/sessions",
            params={"agent_id": "default-agent", "cursor": wrong_signature},
            headers=headers,
        ).status_code == 422

        wrong_scope = client.get(
            "/api/chat/sessions",
            params={"agent_id": "other-agent", "cursor": cursor},
            headers=headers,
        )
        assert wrong_scope.status_code == 422

        # A cursor issued to the user endpoint cannot be replayed on admin endpoints.
        admin_headers = _login(client, "admin@example.com", "admin password 123")
        replay = client.get(
            "/api/admin/users",
            params={"cursor": cursor},
            headers=admin_headers,
        )
        assert replay.status_code == 422
        user_replay = client.get(
            "/api/chat/sessions",
            params={"agent_id": "default-agent", "cursor": cursor},
            headers=admin_headers,
        )
        assert user_replay.status_code == 422
        assert newest == "ses_page_050"


def test_non_ascii_cursor_payload_is_a_stable_invalid_cursor() -> None:
    settings = _settings()
    app = create_app(settings)
    _seed_sessions(settings)
    with TestClient(app) as client:
        headers = _login(client, "local@example.com", "local password 123")
        response = client.get(
            "/api/chat/sessions",
            params={"agent_id": "default-agent", "cursor": "v1.é.sig"},
            headers=headers,
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "INVALID_CURSOR"


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
        encode_cursor(
            datetime(2026, 7, 20, 10, tzinfo=UTC),
            "ses_001",
            signer=CursorSigner("test-secret"),
        ),
        signer=CursorSigner("test-secret"),
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


def test_single_oversized_message_returns_stable_budget_error() -> None:
    settings = _settings()
    app = create_app(settings)
    with Session(get_engine(settings)) as session:
        chat = ChatSession(
            id="ses_oversized_page",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )
        session.add(chat)
        session.flush()
        session.add(
            ChatMessage(
                id="msg_oversized_page",
                session_id=chat.id,
                role=MessageRole.user,
                content="x" * (1024 * 1024 + 1),
            )
        )
        session.commit()

    with TestClient(app) as client:
        headers = _login(client, "admin@example.com", "admin password 123")
        response = client.get(
            "/api/admin/sessions/ses_oversized_page/messages", headers=headers
        )
        assert response.status_code == 413
        assert response.json()["detail"]["code"] == "RESPONSE_ITEM_TOO_LARGE"


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
