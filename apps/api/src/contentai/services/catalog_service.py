from __future__ import annotations

import hashlib

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from contentai.core.config import Settings, get_settings
from contentai.core.security import AGENT_WILDCARD, AuthContext
from contentai.models.agent import AgentProfile, AgentVersion
from contentai.models.chat import AgentExecution, AgentInvocation, ChatSession
from contentai.models.memory import MemoryRecord
from contentai.models.schemas import (
    AgentProfileCreate,
    AgentProfileDetail,
    AgentProfileListResponse,
    AgentProfileSummary,
    AgentProfileUpdate,
    AgentVersionCreate,
    AgentVersionReference,
    AgentVersionSummary,
)
from contentai.services.errors import (
    AgentAlreadyExistsError,
    AgentInUseError,
    AgentNotFoundError,
    AgentPermissionError,
    AgentValidationError,
    AgentVersionConflictError,
)
from contentai.services.pagination import (
    apply_descending_cursor,
    encode_cursor,
    fit_response_items,
    signer_from_settings,
)


class CatalogService:
    def __init__(self, settings: Settings | None = None) -> None:
        self._cursor_signer = signer_from_settings(settings or get_settings())

    def list_agents(
        self,
        session: Session,
        auth: AuthContext,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> AgentProfileListResponse:
        scope = self._list_scope(auth)
        statement = select(AgentProfile).where(AgentProfile.user_id == auth.user_id)
        if AGENT_WILDCARD not in auth.allowed_agent_ids:
            statement = statement.where(AgentProfile.id.in_(auth.allowed_agent_ids))
        statement = apply_descending_cursor(
            statement,
            AgentProfile.created_at,
            AgentProfile.id,
            cursor,
            scope=scope,
            signer=self._cursor_signer,
        ).order_by(AgentProfile.created_at.desc(), AgentProfile.id.desc())
        profiles = list(session.exec(statement.limit(limit + 1)).all())
        has_more = len(profiles) > limit
        profiles = profiles[:limit]
        versions = self._latest_version_refs_by_agent(
            session, [profile.id for profile in profiles]
        )
        items = [self._to_summary(profile, versions.get(profile.id)) for profile in profiles]
        items, budget_more = fit_response_items(items)
        has_more = has_more or budget_more
        next_cursor = None
        if has_more and items:
            boundary = profiles[len(items) - 1]
            next_cursor = encode_cursor(
                boundary.created_at,
                boundary.id,
                scope=scope,
                signer=self._cursor_signer,
            )
        return AgentProfileListResponse(items=items, next_cursor=next_cursor)

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
        return self._to_detail(profile, current)

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
        try:
            # The profile unique key can race between the preflight check and
            # flush; keep both writes inside the same integrity-error boundary.
            session.flush()
            version = AgentVersion(
                agent_id=profile.id,
                version=1,
                topic_scoring_prompt=payload.topic_scoring_prompt,
                content_prompt=payload.content_prompt,
                hotspot_sources=payload.hotspot_sources,
            )
            session.add(version)
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            self._raise_integrity_error(exc)
        except Exception:
            session.rollback()
            raise
        session.refresh(profile)
        session.refresh(version)
        return self._to_detail(profile, version)

    def update_agent(
        self,
        session: Session,
        agent_id: str,
        payload: AgentProfileUpdate,
        auth: AuthContext,
    ) -> AgentProfileDetail:
        # Deletion takes this same lock. Serializing both paths prevents an
        # updater from retaining a stale ORM instance while a concurrent
        # delete removes its row.
        profile = self._get_profile_for_auth(session, agent_id, auth, for_update=True)
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
            session.flush()
            versions = self._versions_for_agent(session, profile.id)
            current = versions[-1] if versions else None
            result = self._to_detail(profile, current)
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            self._raise_integrity_error(exc)
        except Exception:
            session.rollback()
            raise
        # Return the immutable response captured while the profile lock was
        # still held. A post-commit refresh could itself race with deletion.
        return result

    def create_agent_version(
        self,
        session: Session,
        agent_id: str,
        payload: AgentVersionCreate,
        auth: AuthContext,
    ) -> AgentVersionSummary:
        # Serialize version allocation per agent. The unique constraint remains
        # the final guard, but locking the profile prevents max(version)+1 races.
        profile = self._get_profile_for_auth(session, agent_id, auth, for_update=True)
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
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            self._raise_integrity_error(exc)
        except Exception:
            session.rollback()
            raise
        session.refresh(version)
        return self._to_version_summary(version)

    def delete_agent(self, session: Session, agent_id: str, auth: AuthContext) -> None:
        # Updates lock the same profile row, so deletion and the final
        # reference check cannot pass an in-flight update.
        profile = self._get_profile_for_auth(session, agent_id, auth, for_update=True)
        if profile is None:
            raise AgentNotFoundError(agent_id)
        self._ensure_agent_allowed(agent_id, auth)
        if self._agent_has_references(session, agent_id):
            raise AgentInUseError("Agent has chat history or memories and cannot be deleted")
        try:
            for version in self._versions_for_agent(session, agent_id):
                session.delete(version)
            session.flush()
            session.delete(profile)
            session.commit()
        except Exception:
            session.rollback()
            raise

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
        *,
        for_update: bool = False,
    ) -> AgentProfile | None:
        statement = select(AgentProfile).where(
            AgentProfile.id == agent_id,
            AgentProfile.user_id == auth.user_id,
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return session.exec(statement).first()

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
    def _latest_version_refs_by_agent(
        session: Session,
        agent_ids: list[str],
    ) -> dict[str, AgentVersionReference]:
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
            select(AgentVersion.id, AgentVersion.agent_id, AgentVersion.version).join(
                latest,
                (AgentVersion.agent_id == latest.c.agent_id)
                & (AgentVersion.version == latest.c.version),
            )
        ).all()
        return {
            agent_id: AgentVersionReference(
                id=version_id,
                agent_id=agent_id,
                version=version,
            )
            for version_id, agent_id, version in rows
        }

    @staticmethod
    def _next_version_number(session: Session, agent_id: str) -> int:
        latest = session.exec(
            select(func.max(AgentVersion.version)).where(AgentVersion.agent_id == agent_id)
        ).one()
        return int(latest or 0) + 1

    @staticmethod
    def _to_summary(
        profile: AgentProfile,
        current_version: AgentVersionReference | None,
    ) -> AgentProfileSummary:
        return AgentProfileSummary(
            id=profile.id,
            name=profile.name,
            description=profile.description,
            current_version=current_version,
        )

    @staticmethod
    def _to_detail(
        profile: AgentProfile,
        current_version: AgentVersion | None,
    ) -> AgentProfileDetail:
        return AgentProfileDetail(
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
    def _list_scope(auth: AuthContext) -> str:
        permission_scope = (
            "*"
            if AGENT_WILDCARD in auth.allowed_agent_ids
            else "\x00".join(sorted(auth.allowed_agent_ids))
        )
        permission_digest = hashlib.sha256(permission_scope.encode("utf-8")).hexdigest()
        return f"catalog.agents:{auth.user_id}:{permission_digest}"

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
        if constraint_name == "ux_agentversion_agent_version":
            raise AgentVersionConflictError(
                "Agent version allocation conflicted; please retry"
            ) from exc
        raise exc

    @staticmethod
    def _extract_constraint_name(exc: IntegrityError) -> str | None:
        orig = getattr(exc, "orig", None)
        diag_constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
        if isinstance(diag_constraint, str) and diag_constraint:
            return diag_constraint
        if "ux_agentprofile_user_name" in str(exc):
            return "ux_agentprofile_user_name"
        if "ux_agentversion_agent_version" in str(exc):
            return "ux_agentversion_agent_version"
        return None
