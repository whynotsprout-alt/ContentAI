from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, text
from sqlmodel import Session, select

from contentai.models.base import utcnow
from contentai.models.model_configuration import ModelConfiguration
from contentai.models.user import AdminAuditLog, AppUser

MODEL_CONFIGURATION_LOCK_KEY = 0x4D4F44454C434647


class ModelConfigurationChanged(RuntimeError):
    """Raised when an optimistic model-configuration version is stale."""

    code = "MODEL_CONFIG_CHANGED"
    status_code = 409


@dataclass(frozen=True)
class ActiveModelConfiguration:
    configuration: ModelConfiguration
    created_by_email: str


class ModelConfigurationRepository:
    @staticmethod
    def get_active(session: Session, *, for_update: bool = False) -> ModelConfiguration | None:
        statement = select(ModelConfiguration).where(ModelConfiguration.is_active.is_(True))
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return session.exec(statement).first()

    @staticmethod
    def get_active_metadata(session: Session) -> ActiveModelConfiguration | None:
        row = session.exec(
            select(ModelConfiguration, AppUser.email)
            .join(AppUser, ModelConfiguration.created_by_user_id == AppUser.id)
            .where(ModelConfiguration.is_active.is_(True))
        ).first()
        if row is None:
            return None
        configuration, email = row
        return ActiveModelConfiguration(configuration=configuration, created_by_email=email)

    def replace_active(
        self,
        session: Session,
        *,
        expected_version: int,
        actor_user_id: str,
        request_id: str,
        base_url: str,
        model_name: str,
        temperature: float | None,
        context_window_tokens: int,
        chat_max_tokens: int,
        structured_max_tokens: int,
        input_price_per_million_usd: Decimal,
        output_price_per_million_usd: Decimal,
        api_key_ciphertext: str,
        api_key_fingerprint: str,
        api_key_hint: str,
        validated_at: datetime,
    ) -> ModelConfiguration:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": MODEL_CONFIGURATION_LOCK_KEY},
        )
        session.expire_all()
        active = self.get_active(session, for_update=True)
        actual_version = active.version if active is not None else 0
        if actual_version != expected_version:
            raise ModelConfigurationChanged("The model configuration changed.")

        next_version = int(
            session.exec(select(func.coalesce(func.max(ModelConfiguration.version), 0))).one()
        ) + 1
        now = utcnow()
        if active is not None:
            active.is_active = False
            active.superseded_at = now
            session.add(active)
            session.flush()

        configuration = ModelConfiguration(
            version=next_version,
            base_url=base_url,
            model_name=model_name,
            temperature=temperature,
            context_window_tokens=context_window_tokens,
            chat_max_tokens=chat_max_tokens,
            structured_max_tokens=structured_max_tokens,
            input_price_per_million_usd=input_price_per_million_usd,
            output_price_per_million_usd=output_price_per_million_usd,
            api_key_ciphertext=api_key_ciphertext,
            api_key_fingerprint=api_key_fingerprint,
            api_key_hint=api_key_hint,
            is_active=True,
            validated_at=validated_at,
            created_at=now,
            created_by_user_id=actor_user_id,
        )
        session.add(configuration)
        session.flush()
        session.add(
            AdminAuditLog(
                actor_user_id=actor_user_id,
                action="model_config.updated",
                request_id=request_id,
                detail={
                    "previous_configuration_id": active.id if active is not None else None,
                    "previous_version": actual_version,
                    "configuration_id": configuration.id,
                    "version": configuration.version,
                    "provider": configuration.provider,
                    "base_url": configuration.base_url,
                    "model_name": configuration.model_name,
                    "temperature": configuration.temperature,
                    "context_window_tokens": configuration.context_window_tokens,
                    "chat_max_tokens": configuration.chat_max_tokens,
                    "structured_max_tokens": configuration.structured_max_tokens,
                    "input_price_per_million_usd": str(
                        configuration.input_price_per_million_usd
                    ),
                    "output_price_per_million_usd": str(
                        configuration.output_price_per_million_usd
                    ),
                },
            )
        )
        session.commit()
        session.refresh(configuration)
        return configuration
