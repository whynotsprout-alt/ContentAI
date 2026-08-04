from __future__ import annotations

import ipaddress
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self
from urllib.parse import urlparse

from contentai.core.config.agent import AgentSettings
from contentai.core.config.auth import AuthSettings
from contentai.core.config.database import DatabaseSettings, validate_connection_budget
from contentai.core.config.llm import LLMSettings
from contentai.core.config.logging import LoggingSettings
from contentai.core.config.redis import RedisSettings
from contentai.core.config.search import SearchSettings
from contentai.core.config.server import ServerSettings
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

LOCAL_FRONTEND_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
POSTGRES_SCHEMES = {"postgresql", "postgresql+psycopg", "postgresql+psycopg2"}
_settings_env_file = Path(".env")


class Env(StrEnum):
    development = "development"
    production = "production"
    test = "test"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="CONTENTAI_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    def __init__(self, **values) -> None:
        super().__init__(**values)

    env: Env = Env.development
    server: ServerSettings = Field(default_factory=ServerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    model_config_encryption_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY",
        exclude=True,
        repr=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        _ = settings_cls
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @model_validator(mode="after")
    def _validate_env_settings(self) -> Self:
        self.server.frontend_origins = self._final_frontend_origins()
        self.server.trusted_proxy_cidrs = self._final_trusted_proxy_cidrs()

        database_url = (self.database.url or "").strip()
        if not database_url:
            raise ValueError("CONTENTAI_DATABASE__URL is required.")
        self.database.url = database_url

        parsed_database = urlparse(database_url)
        if parsed_database.scheme not in POSTGRES_SCHEMES:
            raise ValueError(
                "CONTENTAI_DATABASE__URL must use PostgreSQL. "
                "Supported schemes: postgresql, postgresql+psycopg, postgresql+psycopg2."
            )
        self._validate_database_env(parsed_database.path.lstrip("/"))

        if self.env == Env.production:
            if not self.server.frontend_origins:
                raise ValueError("CONTENTAI_SERVER__FRONTEND_ORIGINS is required in production.")
        if self.server.outbox_readiness_threshold < 1:
            raise ValueError("CONTENTAI_SERVER__OUTBOX_READINESS_THRESHOLD must be at least 1.")
        if self.server.outbox_max_age_seconds < 1:
            raise ValueError("CONTENTAI_SERVER__OUTBOX_MAX_AGE_SECONDS must be at least 1.")

        self._validate_database_pool()
        self._validate_llm()
        self._validate_logging()
        self._validate_agent()
        self._validate_redis()
        self._validate_search()
        self._validate_auth()
        self._validate_model_configuration()
        self._validate_frontend_origins()
        return self

    def _final_trusted_proxy_cidrs(self) -> list[str]:
        raw = self.server.trusted_proxy_cidrs
        values = raw.split(",") if isinstance(raw, str) else raw
        normalized: list[str] = []
        for value in values:
            value = value.strip()
            if not value:
                continue
            try:
                network = ipaddress.ip_network(value, strict=False)
            except ValueError as exc:
                raise ValueError(
                    "CONTENTAI_SERVER__TRUSTED_PROXY_CIDRS must contain valid IP/CIDR values."
                ) from exc
            normalized.append(str(network))
        return list(dict.fromkeys(normalized))

    def _final_frontend_origins(self) -> list[str]:
        raw_origins = self.server.frontend_origins
        if isinstance(raw_origins, str):
            origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
        else:
            origins = [
                origin.strip()
                for origin in raw_origins
                if isinstance(origin, str) and origin.strip()
            ]
        if self.env == Env.development:
            origins.extend(LOCAL_FRONTEND_ORIGINS)
        return list(dict.fromkeys(origins))

    def _validate_database_env(self, database_name: str) -> None:
        is_test_database = "test" in database_name.lower()
        if self.env == Env.test and not is_test_database:
            raise ValueError("CONTENTAI_ENV=test must use a dedicated test database.")
        if self.env != Env.test and is_test_database:
            raise ValueError("Test databases can only be used when CONTENTAI_ENV=test.")

    def _validate_database_pool(self) -> None:
        if self.database.pool_timeout <= 0:
            raise ValueError("CONTENTAI_DATABASE__POOL_TIMEOUT must be greater than 0.")
        if self.database.pool_recycle_seconds < 1:
            raise ValueError("CONTENTAI_DATABASE__POOL_RECYCLE_SECONDS must be greater than 0.")
        if self.database.agent_worker_concurrency != self.agent.worker_concurrency:
            raise ValueError(
                "CONTENTAI_DATABASE__AGENT_WORKER_CONCURRENCY must match "
                "CONTENTAI_AGENT__WORKER_CONCURRENCY."
            )
        validate_connection_budget(self.database)

    def _validate_llm(self) -> None:
        if self.llm.context_window_tokens < 1:
            raise ValueError("CONTENTAI_LLM__CONTEXT_WINDOW_TOKENS must be greater than 0.")
        if self.llm.chat_max_tokens < 1:
            raise ValueError("CONTENTAI_LLM__CHAT_MAX_TOKENS must be greater than 0.")
        if self.llm.structured_max_tokens < 1:
            raise ValueError("CONTENTAI_LLM__STRUCTURED_MAX_TOKENS must be greater than 0.")

    def _validate_logging(self) -> None:
        import logging

        if not isinstance(logging.getLevelName(self.logging.level.upper()), int):
            raise ValueError("CONTENTAI_LOGGING__LEVEL must be a standard logging level.")

    def _validate_agent(self) -> None:
        if self.agent.context_max_messages < 1:
            raise ValueError("CONTENTAI_AGENT__CONTEXT_MAX_MESSAGES must be at least 1.")
        if self.agent.context_min_focused_messages < 1:
            raise ValueError("CONTENTAI_AGENT__CONTEXT_MIN_FOCUSED_MESSAGES must be at least 1.")
        if self.agent.max_iterations < 1:
            raise ValueError("CONTENTAI_AGENT__MAX_ITERATIONS must be at least 1.")
        if self.agent.recursion_limit < 1:
            raise ValueError("CONTENTAI_AGENT__RECURSION_LIMIT must be at least 1.")
        if self.agent.event_flush_interval_ms < 0:
            raise ValueError("CONTENTAI_AGENT__EVENT_FLUSH_INTERVAL_MS cannot be negative.")
        if self.agent.event_flush_max_chars < 1:
            raise ValueError("CONTENTAI_AGENT__EVENT_FLUSH_MAX_CHARS must be at least 1.")
        if self.agent.runtime_cache_capacity < 1:
            raise ValueError("CONTENTAI_AGENT__RUNTIME_CACHE_CAPACITY must be at least 1.")
        if self.agent.tool_timeout_seconds <= 0:
            raise ValueError("CONTENTAI_AGENT__TOOL_TIMEOUT_SECONDS must be greater than 0.")
        if self.agent.checkpoint_backend not in {"postgres"}:
            raise ValueError("CONTENTAI_AGENT__CHECKPOINT_BACKEND must be postgres.")
        if self.agent.memory_backend not in {"postgres"}:
            raise ValueError("CONTENTAI_AGENT__MEMORY_BACKEND must be postgres.")
        if not self.agent.celery_queue.strip():
            raise ValueError("CONTENTAI_AGENT__CELERY_QUEUE cannot be empty.")
        if not self.agent.celery_background_queue.strip():
            raise ValueError("CONTENTAI_AGENT__CELERY_BACKGROUND_QUEUE cannot be empty.")
        if not self.agent.celery_side_effect_queue.strip():
            raise ValueError("CONTENTAI_AGENT__CELERY_SIDE_EFFECT_QUEUE cannot be empty.")
        if self.agent.worker_lease_seconds < 30:
            raise ValueError("CONTENTAI_AGENT__WORKER_LEASE_SECONDS must be at least 30.")
        if self.agent.worker_concurrency < 1:
            raise ValueError("CONTENTAI_AGENT__WORKER_CONCURRENCY must be at least 1.")
        if self.agent.user_daily_run_limit < 1:
            raise ValueError("CONTENTAI_AGENT__USER_DAILY_RUN_LIMIT must be at least 1.")
        if self.agent.postprocess_max_attempts < 1:
            raise ValueError("CONTENTAI_AGENT__POSTPROCESS_MAX_ATTEMPTS must be at least 1.")
        if self.agent.postprocess_retry_base_seconds < 1:
            raise ValueError(
                "CONTENTAI_AGENT__POSTPROCESS_RETRY_BASE_SECONDS must be at least 1."
            )
        if self.agent.celery_visibility_timeout_seconds <= self.agent.worker_time_limit_seconds:
            raise ValueError(
                "CONTENTAI_AGENT__CELERY_VISIBILITY_TIMEOUT_SECONDS must exceed the hard worker "
                "time limit."
            )
        if self.agent.worker_soft_time_limit_seconds < 1:
            raise ValueError("CONTENTAI_AGENT__WORKER_SOFT_TIME_LIMIT_SECONDS must be positive.")
        if self.agent.worker_time_limit_seconds <= self.agent.worker_soft_time_limit_seconds:
            raise ValueError(
                "CONTENTAI_AGENT__WORKER_TIME_LIMIT_SECONDS must exceed the soft time limit."
            )

    def _validate_redis(self) -> None:
        parsed = urlparse(self.redis.url)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError("CONTENTAI_REDIS__URL must be a redis:// or rediss:// URL.")
        if self.redis.event_ttl_seconds < 60:
            raise ValueError("CONTENTAI_REDIS__EVENT_TTL_SECONDS must be at least 60.")
        if self.redis.event_max_length < 100:
            raise ValueError("CONTENTAI_REDIS__EVENT_MAX_LENGTH must be at least 100.")
        if self.redis.event_block_ms < 100:
            raise ValueError("CONTENTAI_REDIS__EVENT_BLOCK_MS must be at least 100.")

    def _validate_search(self) -> None:
        if self.search.search_cache_ttl_seconds < 0:
            raise ValueError("CONTENTAI_SEARCH__SEARCH_CACHE_TTL_SECONDS cannot be negative.")
        if self.search.hotspot_cache_ttl_seconds < 0:
            raise ValueError("CONTENTAI_SEARCH__HOTSPOT_CACHE_TTL_SECONDS cannot be negative.")
        bounded_limits = {
            "HOTSPOT_PER_SOURCE_LIMIT": (self.search.hotspot_per_source_limit, 10),
            "HOTSPOT_RAW_CANDIDATE_LIMIT": (self.search.hotspot_raw_candidate_limit, 200),
        }
        for name, (value, maximum) in bounded_limits.items():
            if value < 1 or value > maximum:
                raise ValueError(
                    f"CONTENTAI_SEARCH__{name} must be between 1 and {maximum}."
                )
        if self.env == Env.production:
            if not self.search.traffic_relay_api_key.get_secret_value().strip():
                raise ValueError(
                    "CONTENTAI_SEARCH__TRAFFIC_RELAY_API_KEY is required in production."
                )

    def _validate_auth(self) -> None:
        if self.auth.session_days < 1:
            raise ValueError("CONTENTAI_AUTH__SESSION_DAYS must be at least 1.")
        if self.auth.login_max_failures < 1 or self.auth.login_lock_minutes < 1:
            raise ValueError("Auth login lock settings must be positive.")
        self.auth.bootstrap_admin_email = self.auth.bootstrap_admin_email.strip().lower()
        bootstrap_password = self.auth.bootstrap_admin_password.get_secret_value().strip()
        if bool(self.auth.bootstrap_admin_email) != bool(bootstrap_password):
            missing = (
                "CONTENTAI_AUTH__BOOTSTRAP_ADMIN_PASSWORD"
                if self.auth.bootstrap_admin_email
                else "CONTENTAI_AUTH__BOOTSTRAP_ADMIN_EMAIL"
            )
            raise ValueError(f"{missing} is required when configuring a bootstrap administrator.")
        if self.env == Env.production:
            if not self.auth.bootstrap_admin_email:
                raise ValueError(
                    "CONTENTAI_AUTH__BOOTSTRAP_ADMIN_EMAIL is required in production."
                )

    def _validate_model_configuration(self) -> None:
        from contentai.core.model_config_crypto import (
            ModelConfigurationSecretError,
            ModelConfigurationSecretProtector,
        )

        encryption_key = self.model_config_encryption_key.get_secret_value().strip()
        if not encryption_key:
            raise ValueError("CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY is required.")
        try:
            ModelConfigurationSecretProtector(encryption_key)
        except ModelConfigurationSecretError as exc:
            raise ValueError(
                "CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY must be a valid Fernet key."
            ) from exc
        self.model_config_encryption_key = SecretStr(encryption_key)

    def _validate_frontend_origins(self) -> None:
        for origin in self.server.frontend_origins:
            parsed = urlparse(origin)
            if not (parsed.scheme in {"http", "https"} and parsed.netloc):
                raise ValueError(f"Invalid frontend origin URL: {origin}")


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=_settings_env_file)


def set_settings_env_file(path: str | Path) -> None:
    global _settings_env_file
    _settings_env_file = Path(path)
    get_settings.cache_clear()
