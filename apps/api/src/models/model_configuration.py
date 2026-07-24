from datetime import datetime
from typing import TYPE_CHECKING

from models.base import new_id, utcnow
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from models.chat import AgentExecution


class ModelConfiguration(SQLModel, table=True):
    __tablename__ = "modelconfiguration"
    __table_args__ = (
        UniqueConstraint("version", name="ux_modelconfiguration_version"),
        CheckConstraint("version > 0", name="ck_modelconfiguration_version_positive"),
        CheckConstraint(
            "provider = 'openai_compatible'",
            name="ck_modelconfiguration_provider",
        ),
        CheckConstraint(
            "char_length(api_key_fingerprint) = 64",
            name="ck_modelconfiguration_fingerprint_length",
        ),
        CheckConstraint(
            "temperature >= 0 AND temperature <= 2",
            name="ck_modelconfiguration_temperature_range",
        ),
        CheckConstraint(
            "context_window_tokens > 0",
            name="ck_modelconfiguration_context_window_positive",
        ),
        CheckConstraint(
            "chat_max_tokens > 0 AND chat_max_tokens < context_window_tokens",
            name="ck_modelconfiguration_chat_output_fits_context",
        ),
        CheckConstraint(
            "structured_max_tokens > 0 AND structured_max_tokens < context_window_tokens",
            name="ck_modelconfiguration_structured_output_fits_context",
        ),
        Index(
            "ux_modelconfiguration_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: str = Field(default_factory=lambda: new_id("mcf"), primary_key=True)
    version: int
    provider: str = Field(default="openai_compatible", sa_type=String(32))
    base_url: str
    model_name: str
    temperature: float = Field(sa_type=Float)
    context_window_tokens: int = Field(sa_type=Integer)
    chat_max_tokens: int = Field(sa_type=Integer)
    structured_max_tokens: int = Field(sa_type=Integer)
    api_key_ciphertext: str = Field(
        sa_column=Column(Text, nullable=False),
        exclude=True,
        repr=False,
    )
    api_key_fingerprint: str = Field(sa_type=String(64))
    api_key_hint: str
    is_active: bool = Field(default=True)
    validated_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    superseded_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_by_user_id: str = Field(
        index=True,
        foreign_key="appuser.id",
        ondelete="RESTRICT",
    )

    executions: list["AgentExecution"] = Relationship(back_populates="model_configuration")
