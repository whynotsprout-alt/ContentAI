from __future__ import annotations

from uuid import uuid4

from client import ApiClient as TestClient
from core.config import Settings
from db.session import get_engine
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from main import create_app
from models.chat import ChatMessage, ChatSession
from models.enums import MessageRole
from models.user import AppUser, ModelUsage
from services.usage_service import ModelUsageCallback, UsageContext
from sqlmodel import Session, select


def local_auth_app():
    return create_app(
        Settings(
            env="test",
            database={
                "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
            },
            auth={
                "mail_backend": "console",
                "bootstrap_admin_emails": ["admin@example.com"],
                "public_base_url": "http://testserver",
            },
        )
    )


def _register_and_verify(client: TestClient, app, email: str, password: str) -> None:
    response = client.post("/api/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201
    token = app.state.mailer.outbox[-1].text.split("token=", 1)[1].strip()
    verified = client.post("/api/auth/verify-email", json={"token": token})
    assert verified.status_code == 200


def _login(client: TestClient, email: str, password: str):
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    csrf = response.cookies.get("contentai_csrf")
    assert csrf
    return {"X-CSRF-Token": csrf}


def test_local_registration_verification_login_and_password_reset():
    app = local_auth_app()
    with TestClient(app) as client:
        _register_and_verify(client, app, "person@example.com", "correct horse battery")
        login_headers = _login(client, "person@example.com", "correct horse battery")
        assert client.get("/api/auth/me").json()["email"] == "person@example.com"

        forgot = client.post("/api/auth/forgot-password", json={"email": "person@example.com"})
        assert forgot.status_code == 200
        token = app.state.mailer.outbox[-1].text.split("token=", 1)[1].strip()
        reset = client.post(
            "/api/auth/reset-password",
            json={"token": token, "password": "a different secure password"},
        )
        assert reset.status_code == 200
        assert client.get("/api/auth/me").status_code == 401
        assert client.post("/api/auth/logout", headers=login_headers).status_code in {204, 401}
        _login(client, "person@example.com", "a different secure password")


def test_local_registration_can_skip_email_verification_for_development():
    app = create_app(
        Settings(
            env="test",
            database={
                "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
            },
            auth={
                "mail_backend": "console",
                "require_email_verification": False,
            },
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/register",
            json={"email": "local@example.com", "password": "correct horse battery"},
        )

        assert response.status_code == 201
        assert response.json()["message"] == "Registration successful. You can sign in now."
        assert app.state.mailer.outbox == []
        _login(client, "local@example.com", "correct horse battery")


def test_admin_user_listing_usage_and_disable():
    app = local_auth_app()
    with TestClient(app) as client:
        _register_and_verify(client, app, "member@example.com", "member password 123")
        _register_and_verify(client, app, "admin@example.com", "admin password 123")
        admin_headers = _login(client, "admin@example.com", "admin password 123")

        with Session(get_engine(app.state.settings)) as session:
            member = session.exec(
                select(AppUser).where(AppUser.email_normalized == "member@example.com")
            ).one()
            session.add(
                ModelUsage(
                    call_id="usage-test-call",
                    tenant_id=member.tenant_id,
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

        disabled = client.post(
            f"/api/admin/users/{member_id}/disable",
            headers=admin_headers,
        )
        assert disabled.status_code == 200
        assert disabled.json()["status"] == "disabled"


def test_disabled_pending_user_cannot_reactivate_with_old_verification_link():
    app = local_auth_app()
    with TestClient(app) as client:
        registered = client.post(
            "/api/auth/register",
            json={"email": "pending@example.com", "password": "pending password 123"},
        )
        assert registered.status_code == 201
        old_token = app.state.mailer.outbox[-1].text.split("token=", 1)[1].strip()

        _register_and_verify(client, app, "admin@example.com", "admin password 123")
        admin_headers = _login(client, "admin@example.com", "admin password 123")
        with Session(get_engine(app.state.settings)) as session:
            pending = session.exec(
                select(AppUser).where(AppUser.email_normalized == "pending@example.com")
            ).one()
            pending_id = pending.id

        disabled = client.post(
            f"/api/admin/users/{pending_id}/disable",
            headers=admin_headers,
        )
        assert disabled.status_code == 200

        verification = client.post(
            "/api/auth/verify-email",
            json={"token": old_token},
        )
        assert verification.status_code in {400, 403}
        with Session(get_engine(app.state.settings)) as session:
            pending = session.get(AppUser, pending_id)
            assert pending is not None
            assert pending.status == "disabled"
            assert pending.email_verified_at is None


def test_admin_can_view_session_messages_after_audit_is_recorded():
    app = local_auth_app()
    with TestClient(app) as client:
        _register_and_verify(client, app, "member@example.com", "member password 123")
        _register_and_verify(client, app, "admin@example.com", "admin password 123")
        admin_headers = _login(client, "admin@example.com", "admin password 123")

        with Session(get_engine(app.state.settings)) as session:
            member = session.exec(
                select(AppUser).where(AppUser.email_normalized == "member@example.com")
            ).one()
            chat = ChatSession(
                agent_id="default-agent",
                agent_version_id="default-agent-v1",
                tenant_id=member.tenant_id,
                owner_user_id=member.id,
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
                    role=MessageRole.tool,
                    content='{"internal": "tool result"}',
                )
            )
            session.commit()
            session_id = chat.id

        response = client.get(f"/api/admin/sessions/{session_id}", headers=admin_headers)

        assert response.status_code == 200
        messages = response.json()["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Please keep this message visible to administrators."
        assert "executions" not in response.json()
        assert "tools" not in response.json()
        assert "events" not in response.json()


def test_non_admin_cannot_access_admin_api_and_csrf_is_required():
    app = local_auth_app()
    with TestClient(app) as client:
        _register_and_verify(client, app, "member@example.com", "member password 123")
        _login(client, "member@example.com", "member password 123")
        assert client.get("/api/admin/users").status_code == 403
        assert client.post("/api/auth/logout").status_code == 403


def test_local_users_cannot_access_each_others_content_accounts():
    app = local_auth_app()
    with TestClient(app) as client:
        _register_and_verify(client, app, "first@example.com", "first password 123")
        first_headers = _login(client, "first@example.com", "first password 123")
        created = client.post(
            "/api/agents",
            headers=first_headers,
            json={
                "name": "Private content account",
                "description": "Only the first user can access this.",
                "agent_type": "content",
                "status": "active",
                "content_prompt": "Create private test content.",
                "graph_name": "default",
                "tools_config": {"hotspot_sources": ["weibo"]},
                "memory_config": {},
            },
        )
        assert created.status_code == 201
        account_id = created.json()["id"]

        _register_and_verify(client, app, "second@example.com", "second password 123")
        _login(client, "second@example.com", "second password 123")
        assert client.get("/api/agents").json() == []
        assert client.get(f"/api/agents/{account_id}").status_code == 404


def test_model_usage_callback_is_exact_and_idempotent():
    app = local_auth_app()
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
                tenant_id=user.tenant_id,
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
