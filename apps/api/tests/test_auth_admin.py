from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, local
from types import SimpleNamespace
from uuid import uuid4

import pytest
from api.app import create_app
from client import ApiClient as TestClient
from core.config import Settings
from database_helpers import get_test_database_url
from db.session import get_engine
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from models.agent import AgentProfile, AgentVersion
from models.chat import ChatMessage, ChatSession
from models.enums import MessageRole
from models.user import AdminAuditLog, AppUser, AuthSession, ModelUsage
from pydantic import ValidationError
from services.auth_service import AuthService, AuthServiceError
from services.usage_service import ModelUsageCallback, UsageContext
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

DEV_DATABASE = {
    "url": "postgresql+psycopg://postgres:postgres@db/contentai",
}
TEST_DATABASE = {
    "url": get_test_database_url(),
}
BOOTSTRAP_ADMIN_ENV_VARS = (
    "CONTENTAI_AUTH__BOOTSTRAP_ADMIN_EMAIL",
    "CONTENTAI_AUTH__BOOTSTRAP_ADMIN_PASSWORD",
)


def _clear_bootstrap_admin_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in BOOTSTRAP_ADMIN_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def auth_app():
    return create_app(
        Settings(
            env="test",
            database=TEST_DATABASE,
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


def test_legacy_pending_user_is_rejected_after_valid_login():
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
        response = client.post(
            "/api/auth/login",
            json={"email": "legacy@example.com", "password": "correct horse battery"},
        )
        assert response.status_code == 403
        with Session(get_engine(app.state.settings)) as session:
            user = session.exec(
                select(AppUser).where(AppUser.email_normalized == "legacy@example.com")
            ).one()
            assert user.status == "pending_verification"
            assert user.email_verified_at is None


def _production_settings(**auth_overrides: object) -> Settings:
    return Settings(
        env="production",
        server={"frontend_origins": "https://content.example.com"},
        database={"url": "postgresql+psycopg://postgres:postgres@db/contentai"},
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
        database=TEST_DATABASE,
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
        assert users[0].role == "admin"
        assert users[0].email_verified_at is not None
        assert users[0].must_change_password is False


@pytest.mark.parametrize(
    ("role", "status", "is_verified"),
    [
        ("user", "active", True),
        ("admin", "disabled", True),
        ("admin", "active", False),
    ],
)
def test_bootstrap_conflict_fails_without_changing_existing_account(
    role: str,
    status: str,
    is_verified: bool,
    caplog: pytest.LogCaptureFixture,
):
    bootstrap_password = "new bootstrap password"
    settings = Settings(
        env="test",
        database=TEST_DATABASE,
        auth={
            "bootstrap_admin_email": "existing@example.com",
            "bootstrap_admin_password": bootstrap_password,
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
                role=role,
                status=status,
                email_verified_at=datetime.now(UTC) if is_verified else None,
                must_change_password=True,
            )
        )
        session.commit()

    with pytest.raises(
        AuthServiceError,
        match=r"^Bootstrap administrator conflicts with an existing account\.$",
    ) as caught:
        with TestClient(create_app(settings)):
            pass

    assert type(caught.value).__name__ == "BootstrapAdminConflictError"
    assert bootstrap_password not in str(caught.value)
    assert bootstrap_password not in caplog.text

    with Session(get_engine(settings)) as session:
        user = session.exec(
            select(AppUser).where(AppUser.email_normalized == "existing@example.com")
        ).one()
        assert user.role == role
        assert user.status == status
        assert (user.email_verified_at is not None) is is_verified
        assert user.must_change_password is True
        assert auth_service.password_hash.verify("existing password", user.password_hash)


def test_bootstrap_keeps_an_existing_valid_administrator_unchanged():
    settings = Settings(
        env="test",
        database=TEST_DATABASE,
        auth={
            "bootstrap_admin_email": "existing-admin@example.com",
            "bootstrap_admin_password": "new bootstrap password",
        },
    )
    auth_service = AuthService(settings)
    password_hash = auth_service.password_hash.hash("existing password")
    verified_at = datetime.now(UTC)
    with Session(get_engine(settings)) as session:
        session.add(
            AppUser(
                email="existing-admin@example.com",
                email_normalized="existing-admin@example.com",
                password_hash=password_hash,
                role="admin",
                status="active",
                email_verified_at=verified_at,
                must_change_password=True,
            )
        )
        session.commit()

        assert auth_service.bootstrap_default_admin(session) is None

    with Session(get_engine(settings)) as session:
        user = session.exec(
            select(AppUser).where(AppUser.email_normalized == "existing-admin@example.com")
        ).one()
        assert user.role == "admin"
        assert user.status == "active"
        assert user.email_verified_at == verified_at
        assert user.must_change_password is True
        assert auth_service.password_hash.verify("existing password", user.password_hash)


def test_concurrent_bootstrap_admin_calls_converge_without_secret_leakage(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    bootstrap_password = "concurrent bootstrap password"
    settings = Settings(
        env="test",
        database=TEST_DATABASE,
        auth={
            "bootstrap_admin_email": "concurrent-admin@example.com",
            "bootstrap_admin_password": bootstrap_password,
        },
    )
    initial_read_barrier = Barrier(2)
    lookup_state = local()
    original_lookup = AuthService.get_user_by_email

    def synchronized_lookup(
        service: AuthService,
        session: Session,
        email: str,
        *,
        for_update: bool = False,
    ) -> AppUser | None:
        result = original_lookup(service, session, email, for_update=for_update)
        lookup_count = getattr(lookup_state, "count", 0)
        lookup_state.count = lookup_count + 1
        if lookup_count == 0:
            initial_read_barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(AuthService, "get_user_by_email", synchronized_lookup)

    def bootstrap() -> AppUser | None | BaseException:
        try:
            with Session(get_engine(settings)) as session:
                return AuthService(settings).bootstrap_default_admin(session)
        except BaseException as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(bootstrap) for _ in range(2)]
        outcomes = [future.result(timeout=30) for future in futures]

    assert not any(isinstance(outcome, IntegrityError) for outcome in outcomes)
    assert not any(isinstance(outcome, BaseException) for outcome in outcomes)
    assert all(bootstrap_password not in repr(outcome) for outcome in outcomes)
    assert bootstrap_password not in caplog.text

    with Session(get_engine(settings)) as session:
        users = session.exec(
            select(AppUser).where(
                AppUser.email_normalized == "concurrent-admin@example.com"
            )
        ).all()
        assert len(users) == 1
        user = users[0]
        assert user.role == "admin"
        assert user.status == "active"
        assert user.email_verified_at is not None
        assert user.must_change_password is False


@pytest.mark.parametrize("env", ["development", "production"])
def test_runtime_requires_bootstrap_admin_credentials(
    env: str,
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_bootstrap_admin_environment(monkeypatch)
    settings_kwargs: dict[str, object] = {
        "_env_file": None,
        "env": env,
        "database": DEV_DATABASE,
        "auth": {},
    }
    if env == "production":
        settings_kwargs["server"] = {"frontend_origins": "https://content.example.com"}

    with pytest.raises(ValidationError, match="BOOTSTRAP_ADMIN"):
        Settings(**settings_kwargs)


@pytest.mark.parametrize(
    "auth",
    [
        {
            "bootstrap_admin_email": "not-an-email",
            "bootstrap_admin_password": "valid password 123",
        },
        {
            "bootstrap_admin_email": "admin@example.com",
            "bootstrap_admin_password": "short",
        },
        {
            "bootstrap_admin_email": "admin@example.com",
            "bootstrap_admin_password": "x" * 129,
        },
        {
            "bootstrap_admin_email": "admin@example.com",
            "bootstrap_admin_password": " " * 10,
        },
    ],
)
def test_runtime_rejects_invalid_bootstrap_admin_credentials(auth: dict[str, object]):
    bootstrap_password = str(auth["bootstrap_admin_password"])

    with pytest.raises(ValidationError) as caught:
        Settings(
            _env_file=None,
            env="development",
            database=DEV_DATABASE,
            auth=auth,
        )

    assert bootstrap_password not in str(caught.value)


def test_runtime_validation_error_redacts_bootstrap_password_from_all_representations(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_bootstrap_admin_environment(monkeypatch)
    secret_marker = "BOOTSTRAP_PASSWORD_REVIEW_SECRET_MARKER_"
    invalid_password = secret_marker + ("x" * 128)

    with pytest.raises(ValidationError) as caught:
        Settings(
            _env_file=None,
            env="development",
            database=DEV_DATABASE,
            auth={
                "bootstrap_admin_email": "admin@example.com",
                "bootstrap_admin_password": invalid_password,
            },
        )

    errors = caught.value.errors()
    assert errors[0]["loc"] == ()
    assert errors[0]["type"] == "value_error"
    assert "CONTENTAI_AUTH__BOOTSTRAP_ADMIN_PASSWORD" in errors[0]["msg"]
    assert errors[0]["input"] is None
    assert "ctx" not in errors[0]
    assert secret_marker not in str(caught.value)
    assert secret_marker not in repr(errors)
    assert secret_marker not in caught.value.json()
    assert caught.value.__context__ is None


def test_runtime_normalizes_bootstrap_email_without_trimming_password():
    bootstrap_password = "  valid password 123  "

    settings = Settings(
        _env_file=None,
        env="development",
        database=DEV_DATABASE,
        auth={
            "bootstrap_admin_email": "  Bootstrap.Admin@EXAMPLE.COM  ",
            "bootstrap_admin_password": bootstrap_password,
        },
    )

    assert settings.auth.bootstrap_admin_email == "bootstrap.admin@example.com"
    assert (
        settings.auth.bootstrap_admin_password.get_secret_value()
        == bootstrap_password
    )


def test_test_environment_allows_omitting_bootstrap_admin_credentials(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_bootstrap_admin_environment(monkeypatch)
    settings = Settings(
        _env_file=None,
        env="test",
        database=TEST_DATABASE,
        auth={},
    )

    assert settings.auth.bootstrap_admin_email == ""
    assert settings.auth.bootstrap_admin_password.get_secret_value() == ""


def test_bootstrap_admin_requires_email_and_password_together(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_bootstrap_admin_environment(monkeypatch)
    with pytest.raises(ValidationError, match="BOOTSTRAP_ADMIN_PASSWORD"):
        Settings(
            _env_file=None,
            env="test",
            database=TEST_DATABASE,
            auth={"bootstrap_admin_email": "bootstrap-admin@example.com"},
        )


@pytest.mark.parametrize(
    ("sqlstate", "constraint_name"),
    [
        ("23514", "ck_appuser_unrelated"),
        ("23505", "ux_appuser_other_unique"),
    ],
)
def test_bootstrap_propagates_non_email_unique_integrity_errors_after_rollback(
    sqlstate: str,
    constraint_name: str,
    monkeypatch: pytest.MonkeyPatch,
):
    settings = Settings(
        env="test",
        database=TEST_DATABASE,
        auth={
            "bootstrap_admin_email": "integrity-error@example.com",
            "bootstrap_admin_password": "integrity error password",
        },
    )
    auth_service = AuthService(settings)

    class InjectedDatabaseError(Exception):
        def __init__(self) -> None:
            super().__init__("injected database failure")
            self.sqlstate = sqlstate
            self.pgcode = sqlstate
            self.diag = SimpleNamespace(constraint_name=constraint_name)

    injected = IntegrityError(
        "INSERT INTO appuser",
        {},
        InjectedDatabaseError(),
    )
    with Session(get_engine(settings)) as session:
        original_commit = session.commit

        def raise_injected_integrity_error() -> None:
            raise injected

        monkeypatch.setattr(session, "commit", raise_injected_integrity_error)

        with pytest.raises(IntegrityError) as caught:
            auth_service.bootstrap_default_admin(session)

        assert caught.value is injected
        monkeypatch.setattr(session, "commit", original_commit)
        users = session.exec(
            select(AppUser).where(
                AppUser.email_normalized == "integrity-error@example.com"
            )
        ).all()
        assert users == []


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


def test_retired_bootstrap_admin_allowlist_is_rejected_explicitly():
    with pytest.raises(ValidationError, match="bootstrap_admin_emails"):
        Settings(
            env="test",
            database={"url": get_test_database_url()},
            auth={"bootstrap_admin_emails": ["allowlisted@example.com"]},
        )


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


def test_restricted_session_cannot_change_password_after_temporary_password_expiry():
    app = auth_app()
    with TestClient(app) as client:
        _register(client, "expired@example.com", "member password 123")
        admin_headers = _login(client, "admin@example.com", "admin password 123")
        with Session(get_engine(app.state.settings)) as session:
            member = session.exec(
                select(AppUser).where(AppUser.email_normalized == "expired@example.com")
            ).one()
            member_id = member.id

        issued = client.post(
            f"/api/admin/users/{member_id}/temporary-password",
            headers=admin_headers,
        ).json()
        temporary_password = issued["temporary_password"]
        temporary_headers = _login(client, "expired@example.com", temporary_password)

        with Session(get_engine(app.state.settings)) as session:
            member = session.get(AppUser, member_id)
            assert member is not None
            member.temporary_password_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.add(member)
            session.commit()

        response = client.post(
            "/api/auth/change-password",
            headers=temporary_headers,
            json={
                "current_password": temporary_password,
                "new_password": "member permanent password 456",
            },
        )
        assert response.status_code == 401
        assert temporary_password not in response.text
        with Session(get_engine(app.state.settings)) as session:
            active = session.exec(
                select(AuthSession).where(
                    AuthSession.user_id == member_id,
                    AuthSession.revoked_at.is_(None),
                )
            ).all()
            assert active == []


def test_admin_usage_rejects_naive_datetimes_and_accepts_offsets():
    app = auth_app()
    with TestClient(app) as client:
        admin_headers = _login(client, "admin@example.com", "admin password 123")
        naive = client.get(
            "/api/admin/usage?start=2026-07-17T10:00:00",
            headers=admin_headers,
        )
        assert naive.status_code == 422

        aware = client.get(
            "/api/admin/usage?start=2026-07-17T10:00:00%2B08:00",
            headers=admin_headers,
        )
        assert aware.status_code == 200

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
        assert "messages" not in response.json()
        messages_response = client.get(
            f"/api/admin/sessions/{session_id}/messages", headers=admin_headers
        )
        assert messages_response.status_code == 200
        messages = messages_response.json()["items"]
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
