from __future__ import annotations

import pytest
from contentai.core.security import AuthContext
from contentai.db.session import get_engine
from contentai.models.agent import AgentProfile, AgentVersion
from contentai.models.schemas import AgentProfileCreate, AgentVersionCreate
from contentai.services.catalog_service import CatalogService
from sqlalchemy import event
from sqlmodel import Session, select


def test_create_agent_rolls_back_non_integrity_flush_failure_and_reuses_session() -> None:
    service = CatalogService()
    auth = AuthContext(user_id="local-user")
    payload = AgentProfileCreate(
        name="Catalog Boundary Agent",
        description="Tests transaction recovery.",
        content_prompt="Create concise content.",
        hotspot_sources=["weibo"],
    )
    failed_once = False

    def fail_first_flush(_session: Session, _flush_context: object) -> None:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("forced catalog profile flush failure")

    with Session(get_engine()) as session:
        event.listen(session, "after_flush_postexec", fail_first_flush)
        try:
            with pytest.raises(RuntimeError, match="forced catalog profile flush failure"):
                service.create_agent(session, payload, auth)

            assert not session.in_transaction()
            assert (
                session.exec(
                    select(AgentProfile).where(AgentProfile.name == payload.name)
                ).all()
                == []
            )

            created = service.create_agent(session, payload, auth)
        finally:
            event.remove(session, "after_flush_postexec", fail_first_flush)

        versions = session.exec(
            select(AgentVersion).where(AgentVersion.agent_id == created.id)
        ).all()

    assert created.name == payload.name
    assert len(versions) == 1
    assert versions[0].version == 1


def test_create_version_rolls_back_non_integrity_flush_failure_and_reuses_session() -> None:
    service = CatalogService()
    auth = AuthContext(user_id="local-user")
    payload = AgentVersionCreate(
        topic_scoring_prompt="Prefer timely and well-supported topics.",
        content_prompt="Create evidence-backed content.",
        hotspot_sources=["douyin", "weibo"],
    )
    failed_once = False

    def fail_first_flush(_session: Session, _flush_context: object) -> None:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("forced catalog version flush failure")

    with Session(get_engine()) as session:
        event.listen(session, "after_flush_postexec", fail_first_flush)
        try:
            with pytest.raises(RuntimeError, match="forced catalog version flush failure"):
                service.create_agent_version(session, "default-agent", payload, auth)

            assert not session.in_transaction()
            assert session.exec(
                select(AgentVersion.version)
                .where(AgentVersion.agent_id == "default-agent")
                .order_by(AgentVersion.version)
            ).all() == [1]

            created = service.create_agent_version(
                session,
                "default-agent",
                payload,
                auth,
            )
        finally:
            event.remove(session, "after_flush_postexec", fail_first_flush)

        versions = session.exec(
            select(AgentVersion.version)
            .where(AgentVersion.agent_id == "default-agent")
            .order_by(AgentVersion.version)
        ).all()

    assert created.version == 2
    assert versions == [1, 2]
