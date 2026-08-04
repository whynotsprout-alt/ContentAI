from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from contentai.core.security import AGENT_WILDCARD, AuthContext
from contentai.models.agent import AgentProfile, AgentVersion
from contentai.models.chat import AgentExecution, AgentInvocation, ChatSession
from contentai.models.memory import MemoryRecord
from contentai.models.schemas import (
    AgentProfileCreate,
    AgentProfileDetail,
    AgentProfileSummary,
    AgentProfileUpdate,
    AgentVersionCreate,
    AgentVersionSummary,
)
from contentai.services.errors import (
    AgentAlreadyExistsError,
    AgentInUseError,
    AgentNotFoundError,
    AgentPermissionError,
    AgentValidationError,
)


class CatalogService:
    def list_agents(self, session: Session, auth: AuthContext) -> list[AgentProfileSummary]:
        query = (
            select(AgentProfile)
            .where(AgentProfile.user_id == auth.user_id)
            .order_by(AgentProfile.updated_at.desc(), AgentProfile.id)
        )
        if AGENT_WILDCARD not in auth.allowed_agent_ids:
            if not auth.allowed_agent_ids:
                return []
            query = query.where(AgentProfile.id.in_(auth.allowed_agent_ids))
        profiles = session.exec(query).all()
        versions = self._latest_versions_by_agent(session, [profile.id for profile in profiles])
        return [self._to_summary(profile, versions.get(profile.id)) for profile in profiles]

    def get_agent(
        self,
        session: Session,
        agent_id: str,
        auth: AuthContext,
    ) -> AgentProfileDetail:
        profile = self._get_profile_for_auth(session, agent_id, auth)
        if profile is None:
            raise AgentNotFoundError(agent_id)
        self._ensure_agent_allowed(agent_id, auth)
        versions = self._versions_for_agent(session, profile.id)
        current = versions[-1] if versions else None
        return AgentProfileDetail(**self._to_summary(profile, current).model_dump())

    def create_agent(
        self,
        session: Session,
        payload: AgentProfileCreate,
        auth: AuthContext,
    ) -> AgentProfileDetail:
        self._ensure_name_available(
            session,
            user_id=auth.user_id,
            name=payload.name,
        )
        profile = AgentProfile(
            user_id=auth.user_id,
            name=payload.name,
            description=payload.description,
        )
        session.add(profile)
        session.flush()
        version = AgentVersion(
            agent_id=profile.id,
            version=1,
            topic_scoring_prompt=payload.topic_scoring_prompt,
            content_prompt=payload.content_prompt,
            hotspot_sources=payload.hotspot_sources,
        )
        session.add(version)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            self._raise_integrity_error(exc)
        session.refresh(profile)
        session.refresh(version)
        return AgentProfileDetail(**self._to_summary(profile, version).model_dump())

    def update_agent(
        self,
        session: Session,
        agent_id: str,
        payload: AgentProfileUpdate,
        auth: AuthContext,
    ) -> AgentProfileDetail:
        profile = self._get_profile_for_auth(session, agent_id, auth)
        if profile is None:
            raise AgentNotFoundError(agent_id)
        self._ensure_agent_allowed(agent_id, auth)

        updates = payload.model_dump(exclude_unset=True)
        if "name" in updates:
            name = updates["name"]
            if name is None:
                raise AgentValidationError("Agent name cannot be empty")
            self._ensure_name_available(
                session,
                user_id=profile.user_id,
                name=name,
                exclude_agent_id=profile.id,
            )
            profile.name = name
        if "description" in updates and updates["description"] is not None:
            profile.description = updates["description"]
        session.add(profile)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            self._raise_integrity_error(exc)
        session.refresh(profile)
        return self.get_agent(session, profile.id, auth)

    def create_agent_version(
        self,
        session: Session,
        agent_id: str,
        payload: AgentVersionCreate,
        auth: AuthContext,
    ) -> AgentVersionSummary:
        profile = self._get_profile_for_auth(session, agent_id, auth)
        if profile is None:
            raise AgentNotFoundError(agent_id)
        self._ensure_agent_allowed(agent_id, auth)
        next_version = self._next_version_number(session, agent_id)
        version = AgentVersion(
            agent_id=agent_id,
            version=next_version,
            topic_scoring_prompt=payload.topic_scoring_prompt,
            content_prompt=payload.content_prompt,
            hotspot_sources=payload.hotspot_sources,
        )
        session.add(profile)
        session.add(version)
        session.commit()
        session.refresh(version)
        return self._to_version_summary(version)

    def delete_agent(self, session: Session, agent_id: str, auth: AuthContext) -> None:
        profile = self._get_profile_for_auth(session, agent_id, auth)
        if profile is None:
            raise AgentNotFoundError(agent_id)
        self._ensure_agent_allowed(agent_id, auth)
        if self._agent_has_references(session, agent_id):
            raise AgentInUseError("Agent has chat history or memories and cannot be deleted")
        for version in self._versions_for_agent(session, agent_id):
            session.delete(version)
        session.flush()
        session.delete(profile)
        session.commit()

    @staticmethod
    def _agent_has_references(session: Session, agent_id: str) -> bool:
        if session.exec(
            select(ChatSession.id).where(ChatSession.agent_id == agent_id).limit(1)
        ).first():
            return True
        if session.exec(
            select(AgentInvocation.id).where(AgentInvocation.agent_id == agent_id).limit(1)
        ).first():
            return True
        if session.exec(
            select(MemoryRecord.id).where(MemoryRecord.agent_id == agent_id).limit(1)
        ).first():
            return True
        if session.exec(
            select(AgentExecution.id)
            .join(AgentVersion, AgentExecution.agent_version_id == AgentVersion.id)
            .where(AgentVersion.agent_id == agent_id)
            .limit(1)
        ).first():
            return True
        return False

    @staticmethod
    def _get_profile_for_auth(
        session: Session,
        agent_id: str,
        auth: AuthContext,
    ) -> AgentProfile | None:
        return session.exec(
            select(AgentProfile).where(
                AgentProfile.id == agent_id,
                AgentProfile.user_id == auth.user_id,
            )
        ).first()

    @staticmethod
    def _ensure_name_available(
        session: Session,
        *,
        user_id: str,
        name: str,
        exclude_agent_id: str | None = None,
    ) -> None:
        query = select(AgentProfile.id).where(
            AgentProfile.user_id == user_id,
            AgentProfile.name == name,
        )
        if exclude_agent_id is not None:
            query = query.where(AgentProfile.id != exclude_agent_id)
        if session.exec(query.limit(1)).first() is not None:
            raise AgentAlreadyExistsError("Agent name already exists for this user")

    @staticmethod
    def _ensure_agent_allowed(agent_id: str, auth: AuthContext) -> None:
        if not auth.can_access_agent(agent_id):
            raise AgentPermissionError("Agent is not allowed for this user")

    @staticmethod
    def _versions_for_agent(session: Session, agent_id: str) -> list[AgentVersion]:
        return list(
            session.exec(
                select(AgentVersion)
                .where(AgentVersion.agent_id == agent_id)
                .order_by(AgentVersion.version.asc())
            ).all()
        )

    @staticmethod
    def _latest_versions_by_agent(
        session: Session,
        agent_ids: list[str],
    ) -> dict[str, AgentVersion]:
        if not agent_ids:
            return {}
        latest = (
            select(
                AgentVersion.agent_id.label("agent_id"),
                func.max(AgentVersion.version).label("version"),
            )
            .where(AgentVersion.agent_id.in_(agent_ids))
            .group_by(AgentVersion.agent_id)
            .subquery()
        )
        rows = session.exec(
            select(AgentVersion).join(
                latest,
                (AgentVersion.agent_id == latest.c.agent_id)
                & (AgentVersion.version == latest.c.version),
            )
        ).all()
        return {row.agent_id: row for row in rows}

    @staticmethod
    def _next_version_number(session: Session, agent_id: str) -> int:
        latest = session.exec(
            select(func.max(AgentVersion.version)).where(AgentVersion.agent_id == agent_id)
        ).one()
        return int(latest or 0) + 1

    @staticmethod
    def _to_summary(
        profile: AgentProfile,
        current_version: AgentVersion | None,
    ) -> AgentProfileSummary:
        return AgentProfileSummary(
            id=profile.id,
            name=profile.name,
            description=profile.description,
            current_version=(
                CatalogService._to_version_summary(current_version)
                if current_version is not None
                else None
            ),
        )

    @staticmethod
    def _to_version_summary(version: AgentVersion) -> AgentVersionSummary:
        return AgentVersionSummary(
            id=version.id,
            agent_id=version.agent_id,
            version=version.version,
            topic_scoring_prompt=version.topic_scoring_prompt,
            content_prompt=version.content_prompt,
            hotspot_sources=version.hotspot_sources,
        )

    @staticmethod
    def _raise_integrity_error(exc: IntegrityError) -> None:
        constraint_name = CatalogService._extract_constraint_name(exc)
        if constraint_name == "ux_agentprofile_user_name":
            raise AgentAlreadyExistsError("Agent name already exists for this user") from exc
        raise exc

    @staticmethod
    def _extract_constraint_name(exc: IntegrityError) -> str | None:
        orig = getattr(exc, "orig", None)
        diag_constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
        if isinstance(diag_constraint, str) and diag_constraint:
            return diag_constraint
        if "ux_agentprofile_user_name" in str(exc):
            return "ux_agentprofile_user_name"
        return None
