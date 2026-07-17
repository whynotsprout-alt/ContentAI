from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from models.agent import AgentProfile
from models.chat import (
    AgentExecution,
    AgentInvocation,
    ChatMessage,
    ChatSession,
    ExecutionOutbox,
)
from models.enums import MessageRole, RunStatus
from models.schemas.admin import (
    AdminSessionDetail,
    AdminSessionListResponse,
    AdminSessionSummary,
    AdminUsageBucket,
    AdminUsageResponse,
)
from models.schemas.auth import AdminUserListResponse, AdminUserSummary
from models.user import AdminAuditLog, AppUser, ModelUsage, UserActionToken
from services.auth_service import AuthService, AuthServiceError
from sqlalchemy import func, text
from sqlmodel import Session, select


class AdminService:
    def __init__(self, auth_service: AuthService) -> None:
        self.auth_service = auth_service

    def list_users(
        self,
        session: Session,
        *,
        search: str = "",
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> AdminUserListResponse:
        filters = []
        if search.strip():
            filters.append(AppUser.email_normalized.ilike(f"%{search.strip().lower()}%"))
        if status:
            filters.append(AppUser.status == status)
        count_query = select(func.count()).select_from(AppUser)
        users_query = select(AppUser)
        for condition in filters:
            count_query = count_query.where(condition)
            users_query = users_query.where(condition)
        total = int(session.exec(count_query).one() or 0)
        users = session.exec(
            users_query.order_by(AppUser.created_at.desc(), AppUser.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return AdminUserListResponse(
            items=[self.user_summary(session, user) for user in users],
            page=page,
            page_size=page_size,
            total=total,
        )

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
        session.commit()
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
        session.commit()
        session.refresh(user)
        return self.user_summary(session, user)

    def list_user_sessions(
        self,
        session: Session,
        user_id: str,
        *,
        page: int = 1,
        page_size: int = 30,
    ) -> AdminSessionListResponse:
        user = session.get(AppUser, user_id)
        if user is None:
            raise AuthServiceError("用户不存在", status_code=404)
        query = select(ChatSession).where(ChatSession.user_id == user_id)
        total = int(session.exec(select(func.count()).select_from(query.subquery())).one() or 0)
        chats = session.exec(
            query.order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return AdminSessionListResponse(
            items=[self._session_summary(session, chat, user) for chat in chats],
            page=page,
            page_size=page_size,
            total=total,
        )

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
        messages = list(
            session.exec(
                select(ChatMessage)
                .where(ChatMessage.session_id == chat.id)
                .where(ChatMessage.role.in_([MessageRole.user, MessageRole.assistant]))
                .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            ).all()
        )
        detail = AdminSessionDetail(
            **self._session_summary(session, chat, user).model_dump(),
            messages=[self._row(item) for item in messages],
        )
        self._audit(
            session,
            actor_user_id=actor_user_id,
            target_user_id=user.id,
            action="session.viewed",
            request_id=request_id,
            detail={"session_id": chat.id},
        )
        session.commit()
        return detail

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
                func.count(ModelUsage.id),
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
                    call_count=int(row[4] or 0),
                    failed_call_count=int(row[5] or 0),
                    average_latency_ms=float(row[6]) if row[6] is not None else None,
                )
                for row in rows
            ]
        )

    @staticmethod
    def _row(value: Any) -> dict[str, Any]:
        dump = getattr(value, "model_dump", None)
        raw = dump() if callable(dump) else dict(value)
        return {
            key: item.isoformat() if isinstance(item, datetime) else item
            for key, item in raw.items()
        }

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
        from models.base import utcnow

        now = utcnow()
        tokens = session.exec(
            select(UserActionToken).where(
                UserActionToken.user_id == user_id,
                UserActionToken.used_at.is_(None),
            )
        ).all()
        for token in tokens:
            token.used_at = now
            session.add(token)

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
        for execution in executions:
            if execution.status == RunStatus.running:
                execution.cancel_requested_at = execution.cancel_requested_at or now
            else:
                execution.status = RunStatus.cancelled
                execution.finished_at = now
                execution.interrupt_payload = {}
                execution.error = "USER_DISABLED"
                outbox = session.exec(
                    select(ExecutionOutbox).where(
                        ExecutionOutbox.execution_id == execution.id,
                        ExecutionOutbox.kind == "execute",
                    )
                ).first()
                if outbox is not None:
                    outbox.status = "cancelled"
                    outbox.updated_at = now
                    session.add(outbox)
            execution.touch_updated_at(now)
            session.add(execution)

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
            select(
                func.coalesce(func.sum(ModelUsage.input_tokens), 0),
                func.coalesce(func.sum(ModelUsage.output_tokens), 0),
                func.coalesce(func.sum(ModelUsage.total_tokens), 0),
                func.count(ModelUsage.id),
                func.count(ModelUsage.id).filter(ModelUsage.usage_available.is_(False)),
            ).where(ModelUsage.user_id == user.id)
        ).one()
        input_tokens, output_tokens, total_tokens, call_count, missing_count = map(int, usage)
        coverage = 1.0 if call_count == 0 else (call_count - missing_count) / call_count
        return AdminUserSummary(
            **AuthService.to_response(user).model_dump(),
            password_set=bool(user.password_hash),
            agent_count=content_count,
            conversation_count=conversation_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            usage_call_count=call_count,
            missing_usage_call_count=missing_count,
            usage_coverage=coverage,
        )
