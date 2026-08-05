from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from contentai.models.base import new_id, utcnow


class AppUser(SQLModel, table=True):
    __tablename__ = "appuser"
    __table_args__ = (
        UniqueConstraint("email_normalized", name="ux_appuser_email_normalized"),
        Index("ix_appuser_role_status", "role", "status"),
        Index("ix_appuser_created_id", "created_at", "id"),
        Index("ix_appuser_status_created_id", "status", "created_at", "id"),
    )

    id: str = Field(default_factory=lambda: new_id("usr"), primary_key=True)
    email: str
    email_normalized: str = Field(index=True)
    password_hash: str
    role: str = Field(default="user", index=True)
    status: str = Field(default="active", index=True)
    email_verified_at: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    password_changed_at: datetime = Field(
        default_factory=utcnow, sa_type=DateTime(timezone=True)
    )
    must_change_password: bool = Field(default=False)
    temporary_password_expires_at: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    failed_login_count: int = 0
    locked_until: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    last_login_at: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    created_at: datetime = Field(
        default_factory=utcnow, index=True, sa_type=DateTime(timezone=True)
    )
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class AuthSession(SQLModel, table=True):
    __tablename__ = "authsession"
    __table_args__ = (
        UniqueConstraint("token_hash", name="ux_authsession_token_hash"),
        Index("ix_authsession_user_active", "user_id", "revoked_at", "expires_at"),
    )

    id: str = Field(default_factory=lambda: new_id("aus"), primary_key=True)
    user_id: str = Field(index=True, foreign_key="appuser.id")
    token_hash: str = Field(index=True)
    csrf_hash: str
    user_agent: str = ""
    ip_address: str = ""
    expires_at: datetime = Field(index=True, sa_type=DateTime(timezone=True))
    revoked_at: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    last_seen_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class ModelUsage(SQLModel, table=True):
    __tablename__ = "modelusage"
    __table_args__ = (
        UniqueConstraint("call_id", name="ux_modelusage_call_id"),
        Index("ix_modelusage_user_created", "user_id", "created_at"),
        Index("ix_modelusage_execution_category", "execution_id", "category"),
        CheckConstraint("input_tokens >= 0", name="ck_modelusage_input_tokens_nonnegative"),
        CheckConstraint("output_tokens >= 0", name="ck_modelusage_output_tokens_nonnegative"),
        CheckConstraint("total_tokens >= 0", name="ck_modelusage_total_tokens_nonnegative"),
        CheckConstraint(
            "total_tokens >= input_tokens + output_tokens",
            name="ck_modelusage_total_tokens_cover_parts",
        ),
        CheckConstraint("input_cost_usd >= 0", name="ck_modelusage_input_cost_nonnegative"),
        CheckConstraint("output_cost_usd >= 0", name="ck_modelusage_output_cost_nonnegative"),
        CheckConstraint("total_cost_usd >= 0", name="ck_modelusage_total_cost_nonnegative"),
        CheckConstraint(
            "total_cost_usd = input_cost_usd + output_cost_usd",
            name="ck_modelusage_total_cost_matches_parts",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("use"), primary_key=True)
    call_id: str = Field(index=True)
    user_id: str = Field(index=True, foreign_key="appuser.id")
    session_id: str | None = Field(default=None, index=True)
    execution_id: str | None = Field(default=None, index=True)
    category: str = Field(index=True)
    provider: str = Field(default="unknown", index=True)
    latency_ms: int | None = Field(default=None, index=True)
    status: str = Field(default="completed", index=True)
    model_name: str = Field(default="unknown", index=True)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    input_cost_usd: Decimal = Field(
        default=Decimal("0"),
        sa_column=Column(Numeric(20, 10), nullable=False),
    )
    output_cost_usd: Decimal = Field(
        default=Decimal("0"),
        sa_column=Column(Numeric(20, 10), nullable=False),
    )
    total_cost_usd: Decimal = Field(
        default=Decimal("0"),
        sa_column=Column(Numeric(20, 10), nullable=False),
    )
    usage_available: bool = Field(default=False, index=True)
    created_at: datetime = Field(
        default_factory=utcnow, index=True, sa_type=DateTime(timezone=True)
    )


class AdminAuditLog(SQLModel, table=True):
    __tablename__ = "adminauditlog"
    __table_args__ = (
        Index("ix_adminauditlog_actor_created", "actor_user_id", "created_at"),
        Index("ix_adminauditlog_target_created", "target_user_id", "created_at"),
    )

    id: str = Field(default_factory=lambda: new_id("aud"), primary_key=True)
    actor_user_id: str = Field(index=True, foreign_key="appuser.id")
    target_user_id: str | None = Field(default=None, index=True, foreign_key="appuser.id")
    action: str = Field(index=True)
    request_id: str = Field(default="", index=True)
    detail: dict[str, object] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    created_at: datetime = Field(
        default_factory=utcnow, index=True, sa_type=DateTime(timezone=True)
    )
