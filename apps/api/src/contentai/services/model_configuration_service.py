from __future__ import annotations

import inspect
import ipaddress
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit

from pydantic import SecretStr
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from contentai.core.config import Settings
from contentai.core.model_config_crypto import ModelConfigurationSecretProtector
from contentai.models.base import utcnow
from contentai.models.model_configuration import (
    MODEL_PRICE_MAX_USD,
    MODEL_PRICE_QUANTUM_USD,
    ModelConfiguration,
)
from contentai.services.model_config_network import (
    ModelProbeFailed,
    ModelProbeResult,
    OpenAICompatibleProbe,
)
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
DEFAULT_API_MODE = "chat_completions"
SUPPORTED_API_MODES = frozenset({"chat_completions", "responses"})


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
    api_mode: str = "chat_completions"


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
        api_mode: str = DEFAULT_API_MODE,
    ) -> ModelProbeResult:
        with self._preflight_session(session) as preflight_session:
            active = self._repository.get_active(preflight_session)
            plaintext_key = self._resolve_plaintext_key(active, api_key, base_url)
        return self._probe_provider(
            base_url,
            plaintext_key,
            model_name,
            api_mode=api_mode,
        )

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
        api_mode: str | None = None,
        input_price_per_million_usd: Decimal | None = None,
        output_price_per_million_usd: Decimal | None = None,
        expected_version: int,
    ) -> ModelConfiguration:
        with self._preflight_session(session) as preflight_session:
            active = self._repository.get_active(preflight_session)
            actual_version = active.version if active is not None else 0
            if actual_version != expected_version:
                raise ModelConfigurationChanged("The model configuration changed.")

            resolved_api_mode = self._resolve_api_mode(active, api_mode)

            plaintext_key = self._resolve_plaintext_key(active, api_key, base_url)
            active_api_key_ciphertext = (
                active.api_key_ciphertext if active is not None else None
            )
            active_api_key_fingerprint = (
                active.api_key_fingerprint if active is not None else None
            )
            active_api_key_hint = active.api_key_hint if active is not None else None
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

        result = self._probe_provider(
            base_url,
            plaintext_key,
            model_name,
            api_mode=resolved_api_mode,
        )
        if not result.model_validated:
            raise ModelProbeFailed("The model provider probe failed.")

        if api_key is not None and api_key.strip():
            ciphertext = self._protector.encrypt(plaintext_key)
            fingerprint = self._protector.fingerprint(plaintext_key)
            hint = self._protector.hint(plaintext_key)
        else:
            if (
                active_api_key_ciphertext is None
                or active_api_key_fingerprint is None
                or active_api_key_hint is None
            ):
                raise ModelCredentialsRequired("Model provider credentials are required.")
            ciphertext = active_api_key_ciphertext
            fingerprint = active_api_key_fingerprint
            hint = active_api_key_hint

        try:
            return self._repository.replace_active(
                session,
                expected_version=expected_version,
                actor_user_id=actor_user_id,
                request_id=request_id,
                base_url=result.base_url,
                model_name=model_name,
                api_mode=resolved_api_mode,
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
        target_base_url: str,
    ) -> str:
        if api_key is not None and api_key.strip():
            return api_key
        if active is None:
            raise ModelCredentialsRequired("Model provider credentials are required.")
        active_origin = self._endpoint_origin(active.base_url)
        target_origin = self._endpoint_origin(target_base_url)
        if target_origin is None or active_origin != target_origin:
            raise ModelCredentialsRequired(
                "Explicit model provider credentials are required when the endpoint "
                "origin changes."
            )
        return self._protector.decrypt(active.api_key_ciphertext)

    @staticmethod
    def _endpoint_origin(base_url: str) -> tuple[str, str, int] | None:
        """Return an RFC-origin-like key without resolving or contacting the host."""
        try:
            parsed = urlsplit(base_url.strip())
            port = parsed.port
        except (AttributeError, ValueError):
            return None
        scheme = parsed.scheme.lower()
        if (
            scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None

        host = parsed.hostname.lower().rstrip(".")
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            try:
                host = host.encode("idna").decode("ascii")
            except UnicodeError:
                return None
        if not host:
            return None
        effective_port = port if port is not None else (443 if scheme == "https" else 80)
        if effective_port == 0:
            return None
        return scheme, host, effective_port

    @staticmethod
    def _resolve_price(
        active_price: Decimal | None,
        requested_price: Decimal | None,
        default_price: Decimal,
    ) -> Decimal:
        if requested_price is not None:
            price = Decimal(str(requested_price))
            if (
                not price.is_finite()
                or price < 0
                or price > MODEL_PRICE_MAX_USD
            ):
                raise ValueError(
                    f"model price must be between 0 and {MODEL_PRICE_MAX_USD}"
                )
            quantized = price.quantize(MODEL_PRICE_QUANTUM_USD)
            if quantized != price:
                raise ValueError("model price must have at most 6 decimal places")
            return quantized
        return active_price if active_price is not None else default_price

    def _runtime_configuration(
        self,
        configuration: ModelConfiguration,
    ) -> RuntimeModelConfiguration:
        return RuntimeModelConfiguration(
            id=configuration.id,
            base_url=configuration.base_url,
            model_name=configuration.model_name,
            api_mode=configuration.api_mode,
            api_key=SecretStr(self._protector.decrypt(configuration.api_key_ciphertext)),
            temperature=configuration.temperature,
            context_window_tokens=configuration.context_window_tokens,
            chat_max_tokens=configuration.chat_max_tokens,
            structured_max_tokens=configuration.structured_max_tokens,
        )

    @staticmethod
    def _resolve_api_mode(
        active: ModelConfiguration | None,
        requested: str | None,
    ) -> str:
        mode = requested
        if mode is None:
            mode = active.api_mode if active is not None else DEFAULT_API_MODE
        if mode not in SUPPORTED_API_MODES:
            raise ValueError("api_mode must be 'chat_completions' or 'responses'.")
        return mode

    @staticmethod
    @contextmanager
    def _preflight_session(session: Session) -> Iterator[Session]:
        """Read provider state without taking ownership of the caller's transaction."""
        get_bind = getattr(session, "get_bind", None)
        if not callable(get_bind):
            # Lightweight test doubles do not own database resources.
            yield session
            return

        bind = get_bind()
        preflight_bind = bind.engine if isinstance(bind, Connection) else bind
        with Session(bind=preflight_bind) as preflight_session:
            yield preflight_session

    def _probe_provider(
        self,
        base_url: str,
        api_key: str,
        model_name: str | None,
        *,
        api_mode: str,
    ) -> ModelProbeResult:
        """Call older injected probers only when they can validate the selected mode.

        The production prober accepts ``api_mode``.  A small compatibility
        shim keeps three-argument Chat Completions probers working, but never
        lets one incorrectly validate a Responses configuration.
        """
        probe = self._prober.probe
        try:
            api_mode_parameter = inspect.signature(probe).parameters.get("api_mode")
        except (TypeError, ValueError):
            api_mode_parameter = None
        supports_keyword = api_mode_parameter is not None and api_mode_parameter.kind in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
        if supports_keyword:
            return probe(base_url, api_key, model_name, api_mode=api_mode)
        if api_mode != DEFAULT_API_MODE:
            raise ModelProbeFailed(
                "The model provider probe cannot validate the selected API mode."
            )
        return probe(base_url, api_key, model_name)
