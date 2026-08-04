from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from contentai.db.session import get_engine
from contentai.models.research import ResearchPackage


def topic_digest(topic: str) -> str:
    normalized = " ".join(str(topic or "").split()).strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class ResearchPackageRepository:
    @staticmethod
    def for_execution(
        session: Session,
        *,
        package_id: str,
        execution_id: str,
        topic_hash: str,
    ) -> ResearchPackage | None:
        return session.exec(
            select(ResearchPackage).where(
                ResearchPackage.id == package_id,
                ResearchPackage.execution_id == execution_id,
                ResearchPackage.topic_hash == topic_hash,
            )
        ).first()

    @staticmethod
    def persist(
        *,
        session_id: str,
        execution_id: str,
        agent_version_id: str,
        topic: str,
        package_data: dict[str, Any],
        sources: list[dict[str, Any]],
        provider_diagnostics: dict[str, Any],
        rendered_content: str,
        valid_source_count: int,
        isolated_source_count: int,
        removed_unknown_reference_count: int,
    ) -> ResearchPackage:
        digest = topic_digest(topic)
        with Session(get_engine()) as session:
            row = session.exec(
                select(ResearchPackage).where(
                    ResearchPackage.execution_id == execution_id,
                    ResearchPackage.topic_hash == digest,
                )
            ).first()
            if row is None:
                row = ResearchPackage(
                    session_id=session_id,
                    execution_id=execution_id,
                    agent_version_id=agent_version_id,
                    topic=topic,
                    topic_hash=digest,
                    rendered_content=rendered_content,
                )
            row.package_data = package_data
            row.sources = sources
            row.provider_diagnostics = provider_diagnostics
            row.rendered_content = rendered_content
            row.valid_source_count = max(0, int(valid_source_count))
            row.isolated_source_count = max(0, int(isolated_source_count))
            row.removed_unknown_reference_count = max(
                0, int(removed_unknown_reference_count)
            )
            row.touch_updated_at()
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                row = session.exec(
                    select(ResearchPackage).where(
                        ResearchPackage.execution_id == execution_id,
                        ResearchPackage.topic_hash == digest,
                    )
                ).one()
            session.refresh(row)
            return row


__all__ = ["ResearchPackageRepository", "topic_digest"]
