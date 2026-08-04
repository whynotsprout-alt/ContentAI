from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from contentai.core.config import Settings
from contentai.core.model_config_crypto import ModelConfigurationSecretProtector
from contentai.models.base import utcnow
from contentai.models.model_configuration import ModelConfiguration
from contentai.services.model_config_network import ModelProbeResult, OpenAICompatibleProbe
from contentai.services.model_configuration_repository import (
    ActiveModelConfiguration,
    ModelConfigurationChanged,
    ModelConfigurationRepository,
)


class ModelCredentialsRequired(RuntimeError):
    code = "MODEL_CREDENTIALS_REQUIRED"
    status_code = 422


class ModelNotConfigured(RuntimeError):
    code = "MODEL_NOT_CONFIGURED"
    status_code = 503


class ModelConfigurationPersistenceFailed(RuntimeError):
    code = "MODEL_CONFIG_PERSISTENCE_FAILED"
    status_code = 503


DEFAULT_INPUT_PRICE_PER_MILLION_USD = Decimal("5.000000")
DEFAULT_OUTPUT_PRICE_PER_MILLION_USD = Decimal("25.000000")


@dataclass(frozen=True)
class RuntimeModelConfiguration:
    id: str
    base_url: str
    model_name: str
    api_key: SecretStr
    temperature: float | None
    context_window_tokens: int
    chat_max_tokens: int
    structured_max_tokens: int


class ModelConfigurationService:
    def __init__(
        self,
        settings: Settings,
        *,
        repository: ModelConfigurationRepository | None = None,
        prober: OpenAICompatibleProbe | None = None,
    ) -> None:
        self._repository = repository or ModelConfigurationRepository()
        self._prober = prober or OpenAICompatibleProbe()
        self._protector = ModelConfigurationSecretProtector(
            settings.model_config_encryption_key
        )

    def get_active(self, session: Session) -> ActiveModelConfiguration | None:
        return self._repository.get_active_metadata(session)

    def get_required_active(self, session: Session) -> ModelConfiguration:
        active = self._repository.get_active(session)
        if active is None:
            raise ModelNotConfigured("A model provider has not been configured.")
        return active

    def get_required_active_runtime(self, session: Session) -> RuntimeModelConfiguration:
        return self._runtime_configuration(self.get_required_active(session))

    def get_runtime_by_id(
        self,
        session: Session,
        model_config_id: str,
    ) -> RuntimeModelConfiguration:
        configuration = session.get(ModelConfiguration, model_config_id)
        if configuration is None:
            raise ModelNotConfigured("The execution model configuration is unavailable.")
        return self._runtime_configuration(configuration)

    def probe(
        self,
        session: Session,
        *,
        base_url: str,
        api_key: str | None,
        model_name: str | None,
    ) -> ModelProbeResult:
        active = self._repository.get_active(session)
        plaintext_key = self._resolve_plaintext_key(active, api_key)
        return self._prober.probe(base_url, plaintext_key, model_name)

    def update(
        self,
        session: Session,
        *,
        actor_user_id: str,
        request_id: str,
        base_url: str,
        api_key: str | None,
        model_name: str,
        temperature: float | None,
        context_window_tokens: int,
        chat_max_tokens: int,
        structured_max_tokens: int,
        input_price_per_million_usd: float | None = None,
        output_price_per_million_usd: float | None = None,
        expected_version: int,
    ) -> ModelConfiguration:
        active = self._repository.get_active(session)
        actual_version = active.version if active is not None else 0
        if actual_version != expected_version:
            raise ModelConfigurationChanged("The model configuration changed.")

        plaintext_key = self._resolve_plaintext_key(active, api_key)
        input_price = self._resolve_price(
            active.input_price_per_million_usd if active is not None else None,
            input_price_per_million_usd,
            DEFAULT_INPUT_PRICE_PER_MILLION_USD,
        )
        output_price = self._resolve_price(
            active.output_price_per_million_usd if active is not None else None,
            output_price_per_million_usd,
            DEFAULT_OUTPUT_PRICE_PER_MILLION_USD,
        )
        result = self._prober.probe(base_url, plaintext_key, model_name)
        if not result.model_validated:
            from contentai.services.model_config_network import ModelProbeFailed

            raise ModelProbeFailed("The model provider probe failed.")

        if api_key is not None and api_key.strip():
            ciphertext = self._protector.encrypt(plaintext_key)
            fingerprint = self._protector.fingerprint(plaintext_key)
            hint = self._protector.hint(plaintext_key)
        else:
            if active is None:
                raise ModelCredentialsRequired("Model provider credentials are required.")
            ciphertext = active.api_key_ciphertext
            fingerprint = active.api_key_fingerprint
            hint = active.api_key_hint

        try:
            return self._repository.replace_active(
                session,
                expected_version=expected_version,
                actor_user_id=actor_user_id,
                request_id=request_id,
                base_url=result.base_url,
                model_name=model_name,
                temperature=temperature,
                context_window_tokens=context_window_tokens,
                chat_max_tokens=chat_max_tokens,
                structured_max_tokens=structured_max_tokens,
                input_price_per_million_usd=input_price,
                output_price_per_million_usd=output_price,
                api_key_ciphertext=ciphertext,
                api_key_fingerprint=fingerprint,
                api_key_hint=hint,
                validated_at=utcnow(),
            )
        except SQLAlchemyError:
            session.rollback()
            raise ModelConfigurationPersistenceFailed(
                "The model configuration could not be saved."
            ) from None

    def _resolve_plaintext_key(
        self,
        active: ModelConfiguration | None,
        api_key: str | None,
    ) -> str:
        if api_key is not None and api_key.strip():
            return api_key
        if active is None:
            raise ModelCredentialsRequired("Model provider credentials are required.")
        return self._protector.decrypt(active.api_key_ciphertext)

    @staticmethod
    def _resolve_price(
        active_price: Decimal | None,
        requested_price: float | None,
        default_price: Decimal,
    ) -> Decimal:
        if requested_price is not None:
            return Decimal(str(requested_price)).quantize(Decimal("0.000001"))
        return active_price if active_price is not None else default_price

    def _runtime_configuration(
        self,
        configuration: ModelConfiguration,
    ) -> RuntimeModelConfiguration:
        return RuntimeModelConfiguration(
            id=configuration.id,
            base_url=configuration.base_url,
            model_name=configuration.model_name,
            api_key=SecretStr(self._protector.decrypt(configuration.api_key_ciphertext)),
            temperature=configuration.temperature,
            context_window_tokens=configuration.context_window_tokens,
            chat_max_tokens=configuration.chat_max_tokens,
            structured_max_tokens=configuration.structured_max_tokens,
        )
