from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from api.app import create_app
from client import ApiClient as TestClient
from core.config import Settings
from db.session import get_engine
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from models.agent import AgentProfile, AgentVersion
from models.chat import ChatMessage, ChatSession
from models.enums import MessageRole
from models.user import AdminAuditLog, AppUser, AuthSession, ModelUsage
from pydantic import ValidationError
from services.auth_service import AuthService
from services.usage_service import ModelUsageCallback, UsageContext
from sqlmodel import Session, select


def auth_app():
    return create_app(
        Settings(
            env="test",
            database={
                "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
            },
            auth={
                "bootstrap_admin_email": "admin@example.com",
                "bootstrap_admin_password": "admin password 123",
            },
        )
    )


def _register(client: TestClient, email: str, password: str) -> None:
    response = client.post("/api/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201
    assert response.json()["message"] == "Registration successful. You can sign in now."


def _login(client: TestClient, email: str, password: str):
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    csrf = response.cookies.get("contentai_csrf")
    assert csrf
    return {"X-CSRF-Token": csrf}


def test_local_registration_login_and_removed_password_reset_routes():
    app = auth_app()
    with TestClient(app) as client:
        _register(client, "person@example.com", "correct horse battery")
        login_headers = _login(client, "person@example.com", "correct horse battery")
        assert client.get("/api/auth/me").json()["email"] == "person@example.com"

        forgot = client.post("/api/auth/forgot-password", json={"email": "person@example.com"})
        assert forgot.status_code == 404
        reset = client.post(
            "/api/auth/reset-password",
            json={"token": "retired-token", "password": "a different secure password"},
        )
        assert reset.status_code == 404
        assert client.post("/api/auth/logout", headers=login_headers).status_code == 204


def test_legacy_pending_user_is_activated_after_valid_login():
    app = auth_app()
    with TestClient(app) as client:
        _register(client, "legacy@example.com", "correct horse battery")
        with Session(get_engine(app.state.settings)) as session:
            user = session.exec(
                select(AppUser).where(AppUser.email_normalized == "legacy@example.com")
            ).one()
            user.status = "pending_verification"
            user.email_verified_at = None
            session.add(user)
            session.commit()
        _login(client, "legacy@example.com", "correct horse battery")
        with Session(get_engine(app.state.settings)) as session:
            user = session.exec(
                select(AppUser).where(AppUser.email_normalized == "legacy@example.com")
            ).one()
            assert user.status == "active"
            assert user.email_verified_at is not None


def _production_settings(**auth_overrides: object) -> Settings:
    return Settings(
        env="production",
        server={"frontend_origins": "https://content.example.com"},
        database={"url": "postgresql+psycopg://postgres:postgres@db/contentai"},
        search={"traffic_relay_api_key": "test-relay-key"},
        auth={
            "bootstrap_admin_email": "admin@example.com",
            "bootstrap_admin_password": "bootstrap password 123",
            **auth_overrides,
        },
    )


def test_production_ignores_retired_email_settings():
    settings = _production_settings(
        public_base_url="http://testserver",
        require_email_verification=True,
        mail_backend="smtp",
    )

    assert not hasattr(settings.auth, "public_base_url")
    assert not hasattr(settings.auth, "require_email_verification")
    assert not hasattr(settings.auth, "mail_backend")


def test_startup_bootstraps_an_active_admin_once():
    settings = Settings(
        env="test",
        database={
            "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
        },
        auth={
            "bootstrap_admin_email": "bootstrap-admin@example.com",
            "bootstrap_admin_password": "bootstrap password 123",
        },
    )
    app = create_app(settings)

    with TestClient(app) as client:
        login_headers = _login(client, "bootstrap-admin@example.com", "bootstrap password 123")
        assert client.get("/api/auth/me", headers=login_headers).json()["role"] == "admin"

    with Session(get_engine(settings)) as session:
        users = session.exec(
            select(AppUser).where(AppUser.email_normalized == "bootstrap-admin@example.com")
        ).all()
        assert len(users) == 1
        assert users[0].status == "active"
        assert users[0].email_verified_at is not None


def test_bootstrap_does_not_change_an_existing_user():
    settings = Settings(
        env="test",
        database={
            "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
        },
        auth={
            "bootstrap_admin_email": "existing@example.com",
            "bootstrap_admin_password": "new bootstrap password",
        },
    )
    auth_service = AuthService(settings)
    password_hash = auth_service.password_hash.hash("existing password")
    with Session(get_engine(settings)) as session:
        session.add(
            AppUser(
                email="existing@example.com",
                email_normalized="existing@example.com",
                password_hash=password_hash,
                role="user",
                status="disabled",
            )
        )
        session.commit()

    with TestClient(create_app(settings)):
        pass

    with Session(get_engine(settings)) as session:
        user = session.exec(
            select(AppUser).where(AppUser.email_normalized == "existing@example.com")
        ).one()
        assert user.role == "user"
        assert user.status == "disabled"
        assert auth_service.password_hash.verify("existing password", user.password_hash)


def test_bootstrap_admin_requires_email_and_password_together():
    with pytest.raises(ValidationError, match="BOOTSTRAP_ADMIN_PASSWORD"):
        Settings(
            _env_file=None,
            env="test",
            database={
                "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
            },
            auth={"bootstrap_admin_email": "bootstrap-admin@example.com"},
        )


def test_admin_user_listing_usage_and_disable():
    app = auth_app()
    with TestClient(app) as client:
        _register(client, "member@example.com", "member password 123")
        admin_headers = _login(client, "admin@example.com", "admin password 123")

        with Session(get_engine(app.state.settings)) as session:
            member = session.exec(
                select(AppUser).where(AppUser.email_normalized == "member@example.com")
            ).one()
            session.add(
                ModelUsage(
                    call_id="usage-test-call",
                    user_id=member.id,
                    session_id="session-test",
                    execution_id="execution-test",
                    category="chat_agent",
                    model_name="test-model",
                    input_tokens=120,
                    output_tokens=30,
                    total_tokens=150,
                    usage_available=True,
                )
            )
            session.commit()
            member_id = member.id

        listing = client.get("/api/admin/users?search=member")
        assert listing.status_code == 200
        item = listing.json()["items"][0]
        assert item["total_tokens"] == 150
        assert "password_hash" not in item

        assert (
            client.post(
                f"/api/admin/users/{member_id}/password-reset",
                headers=admin_headers,
            ).status_code
            == 404
        )
        disabled = client.post(
            f"/api/admin/users/{member_id}/disable",
            headers=admin_headers,
        )
        assert disabled.status_code == 200
        assert disabled.json()["status"] == "disabled"


def test_public_registration_never_consumes_a_legacy_admin_allowlist():
    settings = Settings(
        env="test",
        database={
            "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
        },
        auth={"bootstrap_admin_emails": ["allowlisted@example.com"]},
    )
    with TestClient(create_app(settings)) as client:
        _register(client, "allowlisted@example.com", "allowlisted password 123")

    with Session(get_engine(settings)) as session:
        user = session.exec(
            select(AppUser).where(AppUser.email_normalized == "allowlisted@example.com")
        ).one()
        assert user.role == "user"
    assert not hasattr(settings.auth, "bootstrap_admin_emails")


def test_temporary_password_is_one_time_restricts_session_and_rotates_on_change():
    app = auth_app()
    with TestClient(app) as client:
        _register(client, "member@example.com", "member password 123")
        _login(client, "member@example.com", "member password 123")
        with Session(get_engine(app.state.settings)) as session:
            member = session.exec(
                select(AppUser).where(AppUser.email_normalized == "member@example.com")
            ).one()
            member_id = member.id
            assert len(
                session.exec(
                    select(AuthSession).where(
                        AuthSession.user_id == member_id,
                        AuthSession.revoked_at.is_(None),
                    )
                ).all()
            ) == 1

        admin_headers = _login(client, "admin@example.com", "admin password 123")
        issued_at = datetime.now(UTC)
        response = client.post(
            f"/api/admin/users/{member_id}/temporary-password",
            headers=admin_headers,
        )

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        payload = response.json()
        temporary_password = payload["temporary_password"]
        expires_at = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
        assert len(temporary_password) >= 24
        assert issued_at + timedelta(hours=23, minutes=59) <= expires_at
        assert expires_at <= issued_at + timedelta(hours=24, minutes=1)

        with Session(get_engine(app.state.settings)) as session:
            member = session.get(AppUser, member_id)
            assert member is not None
            assert member.must_change_password is True
            assert member.temporary_password_expires_at == expires_at
            assert all(
                item.revoked_at is not None
                for item in session.exec(
                    select(AuthSession).where(AuthSession.user_id == member_id)
                ).all()
            )
            audit = session.exec(
                select(AdminAuditLog)
                .where(AdminAuditLog.target_user_id == member_id)
                .where(AdminAuditLog.action == "user.temporary_password_issued")
            ).one()
            assert temporary_password not in repr(audit.detail)

        temporary_headers = _login(client, "member@example.com", temporary_password)
        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["must_change_password"] is True
        assert me.json()["temporary_password_expires_at"] == payload["expires_at"]
        restricted = client.get("/api/agents")
        assert restricted.status_code == 403
        assert restricted.json()["detail"]["code"] == "PASSWORD_CHANGE_REQUIRED"

        changed = client.post(
            "/api/auth/change-password",
            headers=temporary_headers,
            json={
                "current_password": temporary_password,
                "new_password": "member permanent password 456",
            },
        )
        assert changed.status_code == 200
        assert client.get("/api/auth/me").json()["must_change_password"] is False
        assert client.get("/api/agents").status_code == 200
        with Session(get_engine(app.state.settings)) as session:
            member = session.get(AppUser, member_id)
            assert member is not None
            assert member.temporary_password_expires_at is None
            active_sessions = session.exec(
                select(AuthSession).where(
                    AuthSession.user_id == member_id,
                    AuthSession.revoked_at.is_(None),
                )
            ).all()
            assert len(active_sessions) == 1

        assert client.post(
            "/api/auth/login",
            json={"email": "member@example.com", "password": temporary_password},
        ).status_code == 401


def test_temporary_password_rejects_disabled_target_and_admin_self_reset():
    app = auth_app()
    with TestClient(app) as client:
        _register(client, "disabled@example.com", "disabled password 123")
        admin_headers = _login(client, "admin@example.com", "admin password 123")
        with Session(get_engine(app.state.settings)) as session:
            disabled = session.exec(
                select(AppUser).where(AppUser.email_normalized == "disabled@example.com")
            ).one()
            disabled.status = "disabled"
            admin = session.exec(
                select(AppUser).where(AppUser.email_normalized == "admin@example.com")
            ).one()
            session.add(disabled)
            session.commit()
            disabled_id = disabled.id
            admin_id = admin.id

        assert client.post(
            f"/api/admin/users/{disabled_id}/temporary-password",
            headers=admin_headers,
        ).status_code == 409
        assert client.post(
            f"/api/admin/users/{admin_id}/temporary-password",
            headers=admin_headers,
        ).status_code == 409

def test_verification_endpoints_are_removed():
    app = auth_app()
    with TestClient(app) as client:
        _register(client, "person@example.com", "correct horse battery")
        assert client.post("/api/auth/verify-email", json={"token": "old-token"}).status_code == 404
        assert client.post(
            "/api/auth/resend-verification", json={"email": "person@example.com"}
        ).status_code == 404


def test_admin_can_view_session_messages_after_audit_is_recorded():
    app = auth_app()
    with TestClient(app) as client:
        _register(client, "member@example.com", "member password 123")
        admin_headers = _login(client, "admin@example.com", "admin password 123")

        with Session(get_engine(app.state.settings)) as session:
            member = session.exec(
                select(AppUser).where(AppUser.email_normalized == "member@example.com")
            ).one()
            agent = AgentProfile(user_id=member.id, name="Member agent")
            session.add(agent)
            session.flush()
            version = AgentVersion(
                agent_id=agent.id,
                version=1,
                content_prompt="Create member content.",
            )
            session.add(version)
            session.flush()
            chat = ChatSession(
                agent_id=agent.id,
                agent_version_id=version.id,
                user_id=member.id,
                title="Session for audit",
            )
            session.add(chat)
            session.flush()
            session.add(
                ChatMessage(
                    session_id=chat.id,
                    role=MessageRole.user,
                    content="Please keep this message visible to administrators.",
                )
            )
            session.add(
                ChatMessage(
                    session_id=chat.id,
                    role=MessageRole.assistant,
                    content="This is the visible assistant response.",
                )
            )
            session.commit()
            session_id = chat.id

        response = client.get(f"/api/admin/sessions/{session_id}", headers=admin_headers)

        assert response.status_code == 200
        messages = response.json()["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Please keep this message visible to administrators."
        assert "executions" not in response.json()
        assert "tools" not in response.json()
        assert "events" not in response.json()


def test_non_admin_cannot_access_admin_api_and_csrf_is_required():
    app = auth_app()
    with TestClient(app) as client:
        _register(client, "member@example.com", "member password 123")
        _login(client, "member@example.com", "member password 123")
        assert client.get("/api/admin/users").status_code == 403
        assert client.post("/api/auth/logout").status_code == 403


def test_local_users_cannot_access_each_others_content_accounts():
    app = auth_app()
    with TestClient(app) as client:
        _register(client, "first@example.com", "first password 123")
        first_headers = _login(client, "first@example.com", "first password 123")
        created = client.post(
            "/api/agents",
            headers=first_headers,
            json={
                "name": "Private content account",
                "description": "Only the first user can access this.",
                "topic_scoring_prompt": "Score private topics.",
                "content_prompt": "Create private test content.",
                "hotspot_sources": ["weibo"],
            },
        )
        assert created.status_code == 201
        account_id = created.json()["id"]

        _register(client, "second@example.com", "second password 123")
        _login(client, "second@example.com", "second password 123")
        assert client.get("/api/agents").json() == []
        assert client.get(f"/api/agents/{account_id}").status_code == 404


def test_model_usage_callback_is_exact_and_idempotent():
    app = auth_app()
    with TestClient(app):
        with Session(get_engine(app.state.settings)) as session:
            user = AppUser(
                email="usage@example.com",
                email_normalized="usage@example.com",
                password_hash="test-hash",
                status="active",
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            context = UsageContext(
                user_id=user.id,
                session_id="session-usage",
                execution_id="execution-usage",
                category="chat_agent",
            )
            user_id = user.id

        callback = ModelUsageCallback(app.state.settings, context)
        run_id = uuid4()
        response = LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content="done",
                            usage_metadata={
                                "input_tokens": 40,
                                "output_tokens": 10,
                                "total_tokens": 50,
                            },
                            response_metadata={"model_name": "usage-model"},
                        )
                    )
                ]
            ]
        )
        callback.on_llm_error(RuntimeError("temporary provider failure"), run_id=run_id)
        callback.on_llm_end(response, run_id=run_id)
        callback.on_llm_end(response, run_id=run_id)

        with Session(get_engine(app.state.settings)) as session:
            rows = session.exec(select(ModelUsage).where(ModelUsage.user_id == user_id)).all()
            assert len(rows) == 1
            assert rows[0].total_tokens == 50
            assert rows[0].usage_available is True
