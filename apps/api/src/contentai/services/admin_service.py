from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, text
from sqlmodel import Session, select

from contentai.models.agent import AgentProfile
from contentai.models.base import utcnow
from contentai.models.chat import (
    AgentExecution,
    AgentInvocation,
    ChatMessage,
    ChatSession,
)
from contentai.models.enums import MessageRole, RunStatus
from contentai.models.schemas.admin import (
    AdminMessageListResponse,
    AdminSessionDetail,
    AdminSessionListResponse,
    AdminSessionSummary,
    AdminUsageBucket,
    AdminUsageResponse,
    TemporaryPasswordResponse,
)
from contentai.models.schemas.auth import AdminUserListResponse, AdminUserSummary
from contentai.models.schemas.chat import ChatMessageResponse
from contentai.models.user import AdminAuditLog, AppUser, ModelUsage
from contentai.services.auth_service import AuthService, AuthServiceError
from contentai.services.errors import ResponseItemTooLargeError
from contentai.services.execution_settlement import (
    current_database_time,
    settle_execution_cancellation,
)
from contentai.services.pagination import (
    MAX_RESPONSE_BYTES,
    apply_ascending_cursor,
    apply_descending_cursor,
    encode_cursor,
    fit_response_items,
    signer_from_settings,
)


@dataclass(frozen=True)
class _UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    input_cost_usd: float = 0
    output_cost_usd: float = 0
    total_cost_usd: float = 0
    chat_input_tokens: int = 0
    chat_output_tokens: int = 0
    chat_total_tokens: int = 0
    chat_input_cost_usd: float = 0
    chat_output_cost_usd: float = 0
    chat_total_cost_usd: float = 0
    call_count: int = 0
    completed_call_count: int = 0
    missing_usage_call_count: int = 0
    failed_call_count: int = 0

    @classmethod
    def from_row(cls, row: Any) -> _UsageTotals:
        values = list(row)
        if len(values) != 16:
            raise ValueError("Unexpected model usage aggregate shape")
        return cls(
            *(int(value or 0) for value in values[:3]),
            *(float(value or 0) for value in values[3:6]),
            *(int(value or 0) for value in values[6:9]),
            *(float(value or 0) for value in values[9:12]),
            *(int(value or 0) for value in values[12:]),
        )


class AdminService:
    def __init__(self, auth_service: AuthService) -> None:
        self.auth_service = auth_service
        self._cursor_signer = signer_from_settings(auth_service.settings)

    def list_users(
        self,
        session: Session,
        *,
        search: str = "",
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
        scope: str = "admin.users",
    ) -> AdminUserListResponse:
        statement = select(AppUser)
        if search.strip():
            statement = statement.where(
                AppUser.email_normalized.ilike(f"%{search.strip().lower()}%")
            )
        if status:
            statement = statement.where(AppUser.status == status)
        statement = apply_descending_cursor(
            statement,
            AppUser.created_at,
            AppUser.id,
            cursor,
            scope=scope,
            signer=self._cursor_signer,
        ).order_by(AppUser.created_at.desc(), AppUser.id.desc())
        users = list(session.exec(statement.limit(limit + 1)).all())
        has_more = len(users) > limit
        users = users[:limit]
        ids = [user.id for user in users]
        if not ids:
            return AdminUserListResponse(items=[], next_cursor=None)
        agent_rows = session.exec(
            select(AgentProfile.user_id, func.count(AgentProfile.id))
            .where(AgentProfile.user_id.in_(ids))
            .group_by(AgentProfile.user_id)
        ).all()
        session_rows = session.exec(
            select(ChatSession.user_id, func.count(ChatSession.id))
            .where(ChatSession.user_id.in_(ids))
            .group_by(ChatSession.user_id)
        ).all()
        usage_rows = session.exec(
            select(
                ModelUsage.user_id,
                *self._usage_aggregate_columns(),
            )
            .where(ModelUsage.user_id.in_(ids))
            .group_by(ModelUsage.user_id)
        ).all()
        agents = {row[0]: int(row[1] or 0) for row in agent_rows}
        conversations = {row[0]: int(row[1] or 0) for row in session_rows}
        usage = {row[0]: _UsageTotals.from_row(row[1:]) for row in usage_rows}
        items = [
            self._user_summary_from_values(
                user,
                agent_count=agents.get(user.id, 0),
                conversation_count=conversations.get(user.id, 0),
                usage_values=usage.get(user.id),
            )
            for user in users
        ]
        items, budget_more = fit_response_items(items)
        has_more = has_more or budget_more
        next_cursor = None
        if has_more and items:
            boundary = users[len(items) - 1]
            next_cursor = encode_cursor(
                boundary.created_at, boundary.id, scope=scope, signer=self._cursor_signer
            )
        return AdminUserListResponse(items=items, next_cursor=next_cursor)

    def get_user(self, session: Session, user_id: str) -> AdminUserSummary:
        user = session.get(AppUser, user_id)
        if user is None:
            raise AuthServiceError("用户不存在", status_code=404)
        return self.user_summary(session, user)

    def set_disabled(
        self,
        session: Session,
        *,
        actor_user_id: str,
        user_id: str,
        disabled: bool,
        request_id: str = "",
    ) -> AdminUserSummary:
        user = session.get(AppUser, user_id)
        if user is None:
            raise AuthServiceError("用户不存在", status_code=404)
        if disabled and user.id == actor_user_id:
            raise AuthServiceError("不能禁用当前管理员", status_code=409)
        if disabled and user.role == "admin":
            self._ensure_not_last_admin(session, user.id)
        user.status = "disabled" if disabled else "active"
        session.add(user)
        if disabled:
            self.auth_service.revoke_all_sessions(session, user.id, commit=False)
            self._disable_user_runtime(session, user.id)
        self._audit(
            session,
            actor_user_id=actor_user_id,
            target_user_id=user.id,
            action="user.disabled" if disabled else "user.enabled",
            request_id=request_id,
            detail={"status": user.status},
        )
        self._commit_or_rollback(session)
        session.refresh(user)
        return self.user_summary(session, user)

    def update_user(
        self,
        session: Session,
        *,
        actor_user_id: str,
        user_id: str,
        role: str,
        request_id: str = "",
    ) -> AdminUserSummary:
        user = session.get(AppUser, user_id)
        if user is None:
            raise AuthServiceError("用户不存在", status_code=404)
        if role == "user" and user.role == "admin":
            self._ensure_not_last_admin(session, user.id)
        if user.id == actor_user_id and role == "user":
            raise AuthServiceError("不能降级当前管理员账号", status_code=409)
        user.role = role
        self._audit(
            session,
            actor_user_id=actor_user_id,
            target_user_id=user.id,
            action="user.updated",
            request_id=request_id,
            detail={"role": user.role},
        )
        session.add(user)
        self._commit_or_rollback(session)
        session.refresh(user)
        return self.user_summary(session, user)

    def issue_temporary_password(
        self,
        session: Session,
        *,
        actor_user_id: str,
        user_id: str,
        request_id: str = "",
    ) -> TemporaryPasswordResponse:
        user = session.exec(
            select(AppUser)
            .where(AppUser.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if user is None:
            raise AuthServiceError("用户不存在", status_code=404)
        if user.id == actor_user_id:
            raise AuthServiceError("管理员不能为自己设置临时密码", status_code=409)
        if user.status == "disabled":
            raise AuthServiceError("不能为已禁用用户设置临时密码", status_code=409)

        temporary_password = secrets.token_urlsafe(18)
        now = utcnow()
        expires_at = now + timedelta(hours=24)
        user.password_hash = self.auth_service.password_hash.hash(temporary_password)
        user.must_change_password = True
        user.temporary_password_expires_at = expires_at
        user.password_changed_at = now
        user.updated_at = now
        session.add(user)
        self.auth_service.revoke_all_sessions(session, user.id, commit=False)
        self._audit(
            session,
            actor_user_id=actor_user_id,
            target_user_id=user.id,
            action="user.temporary_password_issued",
            request_id=request_id,
            detail={"expires_at": expires_at.isoformat().replace("+00:00", "Z")},
        )
        self._commit_or_rollback(session)
        return TemporaryPasswordResponse(
            temporary_password=temporary_password,
            expires_at=expires_at,
        )

    def list_user_sessions(
        self,
        session: Session,
        user_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        scope: str = "admin.user_sessions",
    ) -> AdminSessionListResponse:
        user = session.get(AppUser, user_id)
        if user is None:
            raise AuthServiceError("用户不存在", status_code=404)
        query = apply_descending_cursor(
            select(ChatSession).where(ChatSession.user_id == user_id),
            ChatSession.updated_at,
            ChatSession.id,
            cursor,
            scope=scope,
            signer=self._cursor_signer,
        ).order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        chats = list(session.exec(query.limit(limit + 1)).all())
        has_more = len(chats) > limit
        chats = chats[:limit]
        if not chats:
            return AdminSessionListResponse(items=[], next_cursor=None)
        ids = [chat.id for chat in chats]
        count_rows = session.exec(
            select(ChatMessage.session_id, func.count(ChatMessage.id))
            .where(ChatMessage.session_id.in_(ids))
            .group_by(ChatMessage.session_id)
        ).all()
        latest_rows = session.exec(
            select(AgentExecution.session_id, AgentExecution.status)
            .where(AgentExecution.session_id.in_(ids))
            .distinct(AgentExecution.session_id)
            .order_by(
                AgentExecution.session_id,
                AgentExecution.updated_at.desc(),
                AgentExecution.id.desc(),
            )
        ).all()
        counts = {row[0]: int(row[1] or 0) for row in count_rows}
        latest = {row[0]: row[1] for row in latest_rows}
        items = [
            AdminSessionSummary(
                session_id=chat.id,
                user_id=user.id,
                user_email=user.email,
                agent_id=chat.agent_id,
                title=chat.title,
                message_count=counts.get(chat.id, 0),
                latest_execution_status=str(latest[chat.id]) if chat.id in latest else None,
                updated_at=chat.updated_at,
            )
            for chat in chats
        ]
        items, budget_more = fit_response_items(items)
        has_more = has_more or budget_more
        next_cursor = None
        if has_more and items:
            boundary = chats[len(items) - 1]
            next_cursor = encode_cursor(
                boundary.updated_at, boundary.id, scope=scope, signer=self._cursor_signer
            )
        return AdminSessionListResponse(items=items, next_cursor=next_cursor)

    def get_session_detail(
        self,
        session: Session,
        session_id: str,
        *,
        actor_user_id: str,
        request_id: str = "",
    ) -> AdminSessionDetail:
        chat = session.get(ChatSession, session_id)
        if chat is None:
            raise AuthServiceError("会话不存在", status_code=404)
        user = session.get(AppUser, chat.user_id)
        if user is None:
            raise AuthServiceError("会话所属用户不存在", status_code=404)
        detail = AdminSessionDetail(
            **self._session_summary(session, chat, user).model_dump(),
        )
        self._audit(
            session,
            actor_user_id=actor_user_id,
            target_user_id=user.id,
            action="session.viewed",
            request_id=request_id,
            detail={"session_id": chat.id},
        )
        self._commit_or_rollback(session)
        return detail

    def list_session_messages(
        self,
        session: Session,
        session_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        scope: str = "admin.messages",
    ) -> AdminMessageListResponse:
        chat = session.get(ChatSession, session_id)
        if chat is None:
            raise AuthServiceError("会话不存在", status_code=404)
        statement = apply_ascending_cursor(
            select(ChatMessage)
            .where(ChatMessage.session_id == chat.id)
            .where(ChatMessage.role.in_([MessageRole.user, MessageRole.assistant])),
            ChatMessage.created_at,
            ChatMessage.id,
            cursor,
            scope=scope,
            signer=self._cursor_signer,
        ).order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        rows = list(session.exec(statement.limit(limit + 1)).all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        if rows and len(rows[0].content.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ResponseItemTooLargeError("A message exceeds the 1 MiB response budget")
        items = [
            ChatMessageResponse(
                id=item.id,
                role=item.role,
                message_type=item.message_type,
                content=item.content,
                created_at=item.created_at,
            )
            for item in rows
        ]
        items, budget_more = fit_response_items(items)
        if rows and not items:
            raise ResponseItemTooLargeError("A message exceeds the 1 MiB response budget")
        rows = rows[: len(items)]
        has_more = has_more or budget_more
        next_cursor = (
            encode_cursor(
                rows[-1].created_at,
                rows[-1].id,
                scope=scope,
                signer=self._cursor_signer,
            )
            if has_more and rows
            else None
        )
        return AdminMessageListResponse(items=items, next_cursor=next_cursor)

    def usage(
        self,
        session: Session,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        user_id: str | None = None,
        model: str | None = None,
        category: str | None = None,
        group_by: Literal["day", "model", "category"] = "day",
    ) -> AdminUsageResponse:
        bucket_column: Any
        if group_by == "model":
            bucket_column = ModelUsage.model_name
        elif group_by == "category":
            bucket_column = ModelUsage.category
        else:
            bucket_column = func.date_trunc("day", ModelUsage.created_at)
        query = (
            select(
                bucket_column.label("bucket"),
                func.coalesce(func.sum(ModelUsage.input_tokens), 0),
                func.coalesce(func.sum(ModelUsage.output_tokens), 0),
                func.coalesce(func.sum(ModelUsage.total_tokens), 0),
                func.coalesce(func.sum(ModelUsage.input_cost_usd), 0),
                func.coalesce(func.sum(ModelUsage.output_cost_usd), 0),
                func.coalesce(func.sum(ModelUsage.total_cost_usd), 0),
                func.count(ModelUsage.id),
                func.count(ModelUsage.id).filter(ModelUsage.status == "completed"),
                func.count(ModelUsage.id).filter(
                    ModelUsage.status == "completed",
                    ModelUsage.usage_available.is_(False),
                ),
                func.count(ModelUsage.id).filter(ModelUsage.status != "completed"),
                func.avg(ModelUsage.latency_ms),
            )
            .group_by(bucket_column)
            .order_by(bucket_column.desc())
        )
        if start:
            query = query.where(ModelUsage.created_at >= start)
        if end:
            query = query.where(ModelUsage.created_at < end)
        if user_id:
            query = query.where(ModelUsage.user_id == user_id)
        if model:
            query = query.where(ModelUsage.model_name == model)
        if category:
            query = query.where(ModelUsage.category == category)
        rows = session.exec(query).all()
        return AdminUsageResponse(
            items=[
                AdminUsageBucket(
                    bucket=str(row[0]),
                    input_tokens=int(row[1] or 0),
                    output_tokens=int(row[2] or 0),
                    total_tokens=int(row[3] or 0),
                    input_cost_usd=float(row[4] or 0),
                    output_cost_usd=float(row[5] or 0),
                    total_cost_usd=float(row[6] or 0),
                    call_count=int(row[7] or 0),
                    completed_call_count=int(row[8] or 0),
                    missing_usage_call_count=int(row[9] or 0),
                    failed_call_count=int(row[10] or 0),
                    average_latency_ms=float(row[11]) if row[11] is not None else None,
                )
                for row in rows
            ]
        )

    @staticmethod
    def _session_summary(session: Session, chat: ChatSession, user: AppUser) -> AdminSessionSummary:
        message_count = int(
            session.exec(select(func.count()).where(ChatMessage.session_id == chat.id)).one() or 0
        )
        latest = session.exec(
            select(AgentExecution.status)
            .join(AgentInvocation, AgentExecution.invocation_id == AgentInvocation.id)
            .where(AgentInvocation.session_id == chat.id)
            .order_by(AgentExecution.updated_at.desc(), AgentExecution.id.desc())
            .limit(1)
        ).first()
        return AdminSessionSummary(
            session_id=chat.id,
            user_id=user.id,
            user_email=user.email,
            agent_id=chat.agent_id,
            title=chat.title,
            message_count=message_count,
            latest_execution_status=str(latest) if latest is not None else None,
            updated_at=chat.updated_at,
        )

    @staticmethod
    def _audit(
        session: Session,
        *,
        actor_user_id: str,
        target_user_id: str | None,
        action: str,
        request_id: str,
        detail: dict[str, Any],
    ) -> None:
        session.add(
            AdminAuditLog(
                actor_user_id=actor_user_id,
                target_user_id=target_user_id,
                action=action,
                request_id=request_id,
                detail=detail,
            )
        )

    @staticmethod
    def _commit_or_rollback(session: Session) -> None:
        try:
            session.commit()
        except Exception:
            session.rollback()
            raise

    @staticmethod
    def _ensure_not_last_admin(session: Session, user_id: str) -> None:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": 0x434F4E54454E54},
        )
        active_admins = int(
            session.exec(
                select(func.count())
                .select_from(AppUser)
                .where(
                    AppUser.role == "admin",
                    AppUser.status == "active",
                )
            ).one()
            or 0
        )
        if active_admins <= 1:
            raise AuthServiceError("不能移除最后一个可用管理员", status_code=409)

    @staticmethod
    def _disable_user_runtime(session: Session, user_id: str) -> None:
        executions = session.exec(
            select(AgentExecution)
            .join(AgentInvocation, AgentExecution.invocation_id == AgentInvocation.id)
            .where(AgentInvocation.user_id == user_id)
            .where(
                AgentExecution.status.in_(
                    [RunStatus.pending, RunStatus.running, RunStatus.waiting_input]
                )
            )
            .with_for_update()
        ).all()
        now = current_database_time(session)
        for execution in executions:
            if execution.status == RunStatus.running:
                execution.cancel_requested_at = execution.cancel_requested_at or now
                execution.touch_updated_at(now)
                session.add(execution)
            else:
                settle_execution_cancellation(
                    session,
                    execution,
                    now=now,
                    error="USER_DISABLED",
                )

    @staticmethod
    def user_summary(session: Session, user: AppUser) -> AdminUserSummary:
        content_count = int(
            session.exec(
                select(func.count())
                .select_from(AgentProfile)
                .where(AgentProfile.user_id == user.id)
            ).one()
            or 0
        )
        conversation_count = int(
            session.exec(
                select(func.count())
                .select_from(ChatSession)
                .where(ChatSession.user_id == user.id)
            ).one()
            or 0
        )
        usage = session.exec(
            select(*AdminService._usage_aggregate_columns()).where(ModelUsage.user_id == user.id)
        ).one()
        return AdminService._user_summary_from_values(
            user,
            agent_count=content_count,
            conversation_count=conversation_count,
            usage_values=_UsageTotals.from_row(usage),
        )

    @staticmethod
    def _usage_aggregate_columns() -> tuple[Any, ...]:
        return (
            func.coalesce(func.sum(ModelUsage.input_tokens), 0),
            func.coalesce(func.sum(ModelUsage.output_tokens), 0),
            func.coalesce(func.sum(ModelUsage.total_tokens), 0),
            func.coalesce(func.sum(ModelUsage.input_cost_usd), 0),
            func.coalesce(func.sum(ModelUsage.output_cost_usd), 0),
            func.coalesce(func.sum(ModelUsage.total_cost_usd), 0),
            func.coalesce(
                func.sum(ModelUsage.input_tokens).filter(ModelUsage.category == "chat_agent"),
                0,
            ),
            func.coalesce(
                func.sum(ModelUsage.output_tokens).filter(ModelUsage.category == "chat_agent"),
                0,
            ),
            func.coalesce(
                func.sum(ModelUsage.total_tokens).filter(ModelUsage.category == "chat_agent"),
                0,
            ),
            func.coalesce(
                func.sum(ModelUsage.input_cost_usd).filter(
                    ModelUsage.category == "chat_agent"
                ),
                0,
            ),
            func.coalesce(
                func.sum(ModelUsage.output_cost_usd).filter(
                    ModelUsage.category == "chat_agent"
                ),
                0,
            ),
            func.coalesce(
                func.sum(ModelUsage.total_cost_usd).filter(
                    ModelUsage.category == "chat_agent"
                ),
                0,
            ),
            func.count(ModelUsage.id),
            func.count(ModelUsage.id).filter(ModelUsage.status == "completed"),
            func.count(ModelUsage.id).filter(
                ModelUsage.status == "completed",
                ModelUsage.usage_available.is_(False),
            ),
            func.count(ModelUsage.id).filter(ModelUsage.status != "completed"),
        )

    @staticmethod
    def _user_summary_from_values(
        user: AppUser,
        *,
        agent_count: int,
        conversation_count: int,
        usage_values: _UsageTotals | None,
    ) -> AdminUserSummary:
        totals = usage_values or _UsageTotals()
        coverage = (
            1.0
            if totals.completed_call_count == 0
            else (totals.completed_call_count - totals.missing_usage_call_count)
            / totals.completed_call_count
        )
        return AdminUserSummary(
            **AuthService.to_response(user).model_dump(),
            password_set=bool(user.password_hash),
            agent_count=agent_count,
            conversation_count=conversation_count,
            input_tokens=totals.input_tokens,
            output_tokens=totals.output_tokens,
            total_tokens=totals.total_tokens,
            input_cost_usd=totals.input_cost_usd,
            output_cost_usd=totals.output_cost_usd,
            total_cost_usd=totals.total_cost_usd,
            chat_input_tokens=totals.chat_input_tokens,
            chat_output_tokens=totals.chat_output_tokens,
            chat_total_tokens=totals.chat_total_tokens,
            chat_input_cost_usd=totals.chat_input_cost_usd,
            chat_output_cost_usd=totals.chat_output_cost_usd,
            chat_total_cost_usd=totals.chat_total_cost_usd,
            background_input_tokens=max(0, totals.input_tokens - totals.chat_input_tokens),
            background_output_tokens=max(0, totals.output_tokens - totals.chat_output_tokens),
            background_total_tokens=max(0, totals.total_tokens - totals.chat_total_tokens),
            background_input_cost_usd=max(
                0, totals.input_cost_usd - totals.chat_input_cost_usd
            ),
            background_output_cost_usd=max(
                0, totals.output_cost_usd - totals.chat_output_cost_usd
            ),
            background_total_cost_usd=max(
                0, totals.total_cost_usd - totals.chat_total_cost_usd
            ),
            usage_call_count=totals.call_count,
            completed_usage_call_count=totals.completed_call_count,
            missing_usage_call_count=totals.missing_usage_call_count,
            failed_usage_call_count=totals.failed_call_count,
            usage_coverage=coverage,
        )
