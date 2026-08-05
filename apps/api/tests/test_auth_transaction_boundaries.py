from __future__ import annotations

import pytest
from contentai.core.config import get_settings
from contentai.db.session import get_engine
from contentai.models.user import AppUser
from contentai.services.auth_service import AuthService
from sqlalchemy import event
from sqlmodel import Session, select


def test_non_integrity_flush_failure_rolls_back_and_session_remains_reusable() -> None:
    settings = get_settings()
    service = AuthService(settings)
    failed_once = False

    def fail_first_flush(_session: Session, _flush_context: object) -> None:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("forced non-integrity flush failure")

    with Session(get_engine(settings)) as session:
        event.listen(session, "after_flush_postexec", fail_first_flush)
        try:
            with pytest.raises(RuntimeError, match="forced non-integrity flush failure"):
                service.register(
                    session,
                    email="rollback-reuse@example.com",
                    password="rollback reuse password 123",
                )

            assert not session.in_transaction()
            assert (
                session.exec(
                    select(AppUser).where(
                        AppUser.email_normalized == "rollback-reuse@example.com"
                    )
                ).first()
                is None
            )

            created = service.register(
                session,
                email="rollback-reuse@example.com",
                password="rollback reuse password 123",
            )
        finally:
            event.remove(session, "after_flush_postexec", fail_first_flush)

    assert created.email_normalized == "rollback-reuse@example.com"
