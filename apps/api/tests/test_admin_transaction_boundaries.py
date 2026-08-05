from __future__ import annotations

import pytest
from contentai.core.config import get_settings
from contentai.db.session import get_engine
from contentai.models.user import AdminAuditLog, AppUser
from contentai.services.admin_service import AdminService
from contentai.services.auth_service import AuthService
from sqlalchemy import event
from sqlmodel import Session, select


def test_non_integrity_flush_failure_rolls_back_admin_write_and_reuses_session() -> None:
    settings = get_settings()
    service = AdminService(AuthService(settings))
    failed_once = False

    def fail_first_flush(_session: Session, _flush_context: object) -> None:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("forced admin non-integrity flush failure")

    with Session(get_engine(settings)) as session:
        target = service.auth_service.register(
            session,
            email="admin-boundary@example.com",
            password="admin boundary password 123",
        )
        target_id = target.id
        event.listen(session, "after_flush_postexec", fail_first_flush)
        try:
            with pytest.raises(RuntimeError, match="forced admin non-integrity flush failure"):
                service.update_user(
                    session,
                    actor_user_id="local-user",
                    user_id=target_id,
                    role="admin",
                    request_id="failed-update",
                )

            assert not session.in_transaction()
            user = session.get(AppUser, target_id)
            assert user is not None
            assert user.role == "user"
            assert session.exec(select(AdminAuditLog)).all() == []

            updated = service.update_user(
                session,
                actor_user_id="local-user",
                user_id=target_id,
                role="admin",
                request_id="successful-update",
            )
        finally:
            event.remove(session, "after_flush_postexec", fail_first_flush)

        audit_rows = session.exec(select(AdminAuditLog)).all()

    assert updated.role == "admin"
    assert len(audit_rows) == 1
    assert audit_rows[0].request_id == "successful-update"
