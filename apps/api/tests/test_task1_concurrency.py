from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic

import pytest
from contentai.core.config import get_settings
from contentai.db.session import get_engine
from contentai.models.user import AppUser, AuthSession
from contentai.services.admin_service import AdminService
from contentai.services.auth_service import AuthService, AuthServiceError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select


def _create_user_and_session() -> tuple[AuthService, str, str]:
    service = AuthService(get_settings())
    with Session(get_engine()) as session:
        user = service.register(
            session,
            email="lock-test@example.com",
            password="original password 123",
        )
        issued = service.login(
            session,
            email=user.email,
            password="original password 123",
        )
        return service, user.id, issued.user.email


def _assert_for_update_conflicts_with_key_share(
    *,
    table: str,
    row_id: str,
    operation: Callable[[], None],
) -> None:
    engine = get_engine()
    with engine.connect() as blocker:
        transaction = blocker.begin()
        blocker.execute(
            text(f'SELECT id FROM "{table}" WHERE id = :row_id FOR KEY SHARE'),
            {"row_id": row_id},
        )
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(operation)
                with pytest.raises(OperationalError, match="lock timeout"):
                    future.result(timeout=3)
        finally:
            transaction.rollback()


@pytest.mark.parametrize("operation_name", ["login", "change_password", "temporary_password"])
def test_password_write_paths_lock_user_before_verification_or_write(operation_name: str):
    service, user_id, email = _create_user_and_session()

    def operation() -> None:
        with Session(get_engine()) as session:
            session.exec(text("SET LOCAL lock_timeout = '200ms'"))
            if operation_name == "login":
                service.login(
                    session,
                    email=email,
                    password="original password 123",
                )
            elif operation_name == "change_password":
                service.change_password(
                    session,
                    user_id=user_id,
                    current_password="original password 123",
                    new_password="replacement password 456",
                )
            else:
                AdminService(service).issue_temporary_password(
                    session,
                    actor_user_id="local-user",
                    user_id=user_id,
                )

    _assert_for_update_conflicts_with_key_share(
        table=AppUser.__tablename__,
        row_id=user_id,
        operation=operation,
    )


def test_revoke_all_sessions_locks_active_rows_before_revocation():
    service, user_id, _email = _create_user_and_session()
    with Session(get_engine()) as session:
        active = session.exec(
            text(
                "SELECT id FROM authsession "
                "WHERE user_id = :user_id AND revoked_at IS NULL LIMIT 1"
            ),
            params={"user_id": user_id},
        ).one()
        session_id = str(active[0])

    def operation() -> None:
        with Session(get_engine()) as session:
            session.exec(text("SET LOCAL lock_timeout = '200ms'"))
            service.revoke_all_sessions(session, user_id)

    _assert_for_update_conflicts_with_key_share(
        table=AuthSession.__tablename__,
        row_id=session_id,
        operation=operation,
    )


def _wait_until_postgres_reports_lock_wait(application_name: str) -> None:
    deadline = monotonic() + 5
    with get_engine().connect() as connection:
        while monotonic() < deadline:
            waiting = connection.execute(
                text(
                    "SELECT 1 FROM pg_stat_activity "
                    "WHERE application_name = :application_name "
                    "AND wait_event_type = 'Lock'"
                ),
                {"application_name": application_name},
            ).first()
            if waiting is not None:
                return
    raise AssertionError(f"{application_name} never reached a PostgreSQL lock wait")


def test_concurrent_password_changes_recheck_hash_and_leave_one_session(monkeypatch):
    service, user_id, _email = _create_user_and_session()
    first_verifying = Event()
    release_first = Event()
    original_verify = service.password_hash.verify

    def controlled_verify(password: str, password_hash: str) -> bool:
        if password == "original password 123" and not first_verifying.is_set():
            first_verifying.set()
            assert release_first.wait(timeout=5)
        return original_verify(password, password_hash)

    monkeypatch.setattr(service.password_hash, "verify", controlled_verify)

    def change(application_name: str, new_password: str) -> str:
        with Session(get_engine()) as session:
            session.exec(text(f"SET LOCAL application_name = '{application_name}'"))
            try:
                service.change_password(
                    session,
                    user_id=user_id,
                    current_password="original password 123",
                    new_password=new_password,
                )
            except AuthServiceError:
                return "denied"
            return "changed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(change, "task1-first-change", "first replacement password")
        assert first_verifying.wait(timeout=5)
        second = pool.submit(change, "task1-second-change", "second replacement password")
        _wait_until_postgres_reports_lock_wait("task1-second-change")
        release_first.set()
        assert [first.result(timeout=10), second.result(timeout=10)] == ["changed", "denied"]

    with Session(get_engine()) as session:
        active_sessions = session.exec(
            select(AuthSession).where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
            )
        ).all()
        assert len(active_sessions) == 1


@pytest.mark.parametrize("competitor", ["login", "change_password"])
def test_temporary_password_reset_serializes_against_password_consumers(
    competitor: str,
    monkeypatch,
):
    service, user_id, email = _create_user_and_session()
    reset_hashing = Event()
    release_reset = Event()
    original_hash = service.password_hash.hash

    def controlled_hash(password: str) -> str:
        reset_hashing.set()
        assert release_reset.wait(timeout=5)
        return original_hash(password)

    monkeypatch.setattr(service.password_hash, "hash", controlled_hash)

    def reset() -> None:
        with Session(get_engine()) as session:
            session.exec(text("SET LOCAL application_name = 'task1-reset'"))
            AdminService(service).issue_temporary_password(
                session,
                actor_user_id="local-user",
                user_id=user_id,
            )

    def consume_old_password() -> str:
        with Session(get_engine()) as session:
            session.exec(text("SET LOCAL application_name = 'task1-password-consumer'"))
            try:
                if competitor == "login":
                    service.login(
                        session,
                        email=email,
                        password="original password 123",
                    )
                else:
                    service.change_password(
                        session,
                        user_id=user_id,
                        current_password="original password 123",
                        new_password="consumer replacement password",
                    )
            except AuthServiceError:
                return "denied"
            return "accepted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        reset_future = pool.submit(reset)
        assert reset_hashing.wait(timeout=5)
        consumer_future = pool.submit(consume_old_password)
        _wait_until_postgres_reports_lock_wait("task1-password-consumer")
        release_reset.set()
        reset_future.result(timeout=10)
        assert consumer_future.result(timeout=10) == "denied"

    with Session(get_engine()) as session:
        active_sessions = session.exec(
            select(AuthSession).where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
            )
        ).all()
        assert active_sessions == []


def test_locked_login_refreshes_a_preloaded_user_before_password_verification():
    service, user_id, email = _create_user_and_session()
    with Session(get_engine()) as stale_session:
        preloaded = stale_session.get(AppUser, user_id)
        assert preloaded is not None

        with Session(get_engine()) as competing_session:
            current = competing_session.get(AppUser, user_id)
            assert current is not None
            current.password_hash = service.password_hash.hash("competing password 456")
            competing_session.add(current)
            competing_session.commit()

        with pytest.raises(AuthServiceError):
            service.login(
                stale_session,
                email=email,
                password="original password 123",
            )


def test_login_rechecks_temporary_password_expiry_after_hash_verification(monkeypatch):
    service, user_id, email = _create_user_and_session()
    before_expiry = datetime(2026, 7, 17, 10, tzinfo=UTC)
    crossed_expiry = Event()
    original_verify = service.password_hash.verify

    with Session(get_engine()) as session:
        user = session.get(AppUser, user_id)
        assert user is not None
        user.must_change_password = True
        user.temporary_password_expires_at = before_expiry + timedelta(seconds=1)
        session.add(user)
        session.commit()

    def controlled_verify(password: str, password_hash: str) -> bool:
        verified = original_verify(password, password_hash)
        crossed_expiry.set()
        return verified

    def controlled_now() -> datetime:
        if crossed_expiry.is_set():
            return before_expiry + timedelta(seconds=2)
        return before_expiry

    monkeypatch.setattr(service.password_hash, "verify", controlled_verify)
    monkeypatch.setattr("contentai.services.auth_service.utcnow", controlled_now)

    with Session(get_engine()) as session:
        with pytest.raises(AuthServiceError, match="expired") as caught:
            service.login(
                session,
                email=email,
                password="original password 123",
            )
        assert caught.value.status_code == 401

    with Session(get_engine()) as session:
        active_sessions = session.exec(
            select(AuthSession).where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
            )
        ).all()
        assert active_sessions == []


def test_change_password_rechecks_expiry_after_waiting_for_user_lock(monkeypatch):
    service, user_id, _email = _create_user_and_session()
    before_expiry = datetime(2026, 7, 17, 10, tzinfo=UTC)
    crossed_expiry = Event()

    with Session(get_engine()) as session:
        user = session.get(AppUser, user_id)
        assert user is not None
        original_hash = user.password_hash
        user.must_change_password = True
        user.temporary_password_expires_at = before_expiry + timedelta(seconds=1)
        session.add(user)
        session.commit()

    monkeypatch.setattr(
        "contentai.services.auth_service.utcnow",
        lambda: before_expiry + timedelta(seconds=2)
        if crossed_expiry.is_set()
        else before_expiry,
    )

    def change_after_authentication() -> str:
        with Session(get_engine()) as session:
            session.exec(text("SET LOCAL application_name = 'task1-expiry-change'"))
            try:
                service.change_password(
                    session,
                    user_id=user_id,
                    current_password="original password 123",
                    new_password="must not be persisted 456",
                )
            except AuthServiceError as exc:
                assert exc.status_code == 401
                return "expired"
            return "changed"

    with get_engine().connect() as blocker:
        transaction = blocker.begin()
        blocker.execute(
            text("SELECT id FROM appuser WHERE id = :user_id FOR UPDATE"),
            {"user_id": user_id},
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(change_after_authentication)
            _wait_until_postgres_reports_lock_wait("task1-expiry-change")
            crossed_expiry.set()
            transaction.rollback()
            assert future.result(timeout=10) == "expired"

    with Session(get_engine()) as session:
        user = session.get(AppUser, user_id)
        assert user is not None
        assert user.password_hash == original_hash
        active_sessions = session.exec(
            select(AuthSession).where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
            )
        ).all()
        assert active_sessions == []
