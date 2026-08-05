from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlmodel import Field, Relationship, SQLModel

from contentai.models.base import new_id, utcnow

if TYPE_CHECKING:
    from contentai.models.chat import AgentExecution


MODEL_PRICE_PRECISION = 12
MODEL_PRICE_SCALE = 6
MODEL_PRICE_QUANTUM_USD = Decimal("0.000001")
MODEL_PRICE_MAX_USD = Decimal("999999.999999")


class ModelConfiguration(SQLModel, table=True):
    __tablename__ = "modelconfiguration"
    __table_args__ = (
        UniqueConstraint("version", name="ux_modelconfiguration_version"),
        CheckConstraint("version > 0", name="ck_modelconfiguration_version_positive"),
        CheckConstraint(
            "input_price_per_million_usd >= 0",
            name="ck_modelconfiguration_input_price_nonnegative",
        ),
        CheckConstraint(
            "output_price_per_million_usd >= 0",
            name="ck_modelconfiguration_output_price_nonnegative",
        ),
        CheckConstraint(
            "temperature IS NULL OR (temperature >= 0 AND temperature <= 2)",
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
        CheckConstraint(
            "provider = 'openai_compatible'",
            name="ck_modelconfiguration_provider",
        ),
        CheckConstraint(
            "api_mode IN ('chat_completions', 'responses')",
            name="ck_modelconfiguration_api_mode",
        ),
        CheckConstraint(
            "char_length(api_key_fingerprint) = 64",
            name="ck_modelconfiguration_fingerprint_length",
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
    # Keep the endpoint selection explicit.  ``chat_completions`` is the
    # compatibility default and prevents LangChain from inferring an endpoint
    # from the model name.
    api_mode: str = Field(default="chat_completions", sa_type=String(24))
    base_url: str
    model_name: str
    input_price_per_million_usd: Decimal = Field(
        default=Decimal("5.000000"),
        sa_column=Column(
            Numeric(MODEL_PRICE_PRECISION, MODEL_PRICE_SCALE),
            nullable=False,
        ),
    )
    output_price_per_million_usd: Decimal = Field(
        default=Decimal("25.000000"),
        sa_column=Column(
            Numeric(MODEL_PRICE_PRECISION, MODEL_PRICE_SCALE),
            nullable=False,
        ),
    )
    temperature: float | None = Field(default=None, sa_type=Float)
    context_window_tokens: int = Field(default=32_000, sa_type=Integer)
    chat_max_tokens: int = Field(default=8_000, sa_type=Integer)
    structured_max_tokens: int = Field(default=8_000, sa_type=Integer)
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
