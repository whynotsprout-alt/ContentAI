from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self
from urllib.parse import urlparse

from core.config.agent import AgentSettings
from core.config.auth import AuthSettings
from core.config.database import DatabaseSettings
from core.config.llm import LLMSettings
from core.config.search import SearchSettings
from core.config.server import ServerSettings
from core.paths import PROJECT_ROOT
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

LOCAL_FRONTEND_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
POSTGRES_SCHEMES = {"postgresql", "postgresql+psycopg", "postgresql+psycopg2"}
_settings_env_file = PROJECT_ROOT / ".env"


class Env(StrEnum):
    development = "development"
    production = "production"
    test = "test"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="CONTENTAI_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    def __init__(self, **values) -> None:
        values.setdefault("_env_file", _settings_env_file)
        super().__init__(**values)

    env: Env = Env.development
    server: ServerSettings = Field(default_factory=ServerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @model_validator(mode="after")
    def _validate_env_settings(self) -> Self:
        self.server.frontend_origins = self._final_frontend_origins()

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
            if not self.auth.enabled:
                raise ValueError("CONTENTAI_AUTH__ENABLED is required in production.")
            if not self.server.frontend_origins:
                raise ValueError(
                    "CONTENTAI_SERVER__FRONTEND_ORIGINS is required in production."
                )

        self._validate_database_pool()
        self._validate_llm()
        self._validate_agent()
        self._validate_search()
        self._validate_auth()
        self._validate_frontend_origins()
        return self

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
        if self.database.pool_size < 1:
            raise ValueError("CONTENTAI_DATABASE__POOL_SIZE must be at least 1.")
        if self.database.max_overflow < 0:
            raise ValueError("CONTENTAI_DATABASE__MAX_OVERFLOW cannot be negative.")
        if self.database.pool_timeout <= 0:
            raise ValueError("CONTENTAI_DATABASE__POOL_TIMEOUT must be greater than 0.")
        if self.database.pool_recycle_seconds < 1:
            raise ValueError("CONTENTAI_DATABASE__POOL_RECYCLE_SECONDS must be greater than 0.")

    def _validate_llm(self) -> None:
        if not self.llm.chat_model.strip():
            raise ValueError("CONTENTAI_LLM__CHAT_MODEL cannot be empty.")
        if not self.llm.planning_model.strip():
            raise ValueError("CONTENTAI_LLM__PLANNING_MODEL cannot be empty.")
        if not self.llm.summary_model.strip():
            raise ValueError("CONTENTAI_LLM__SUMMARY_MODEL cannot be empty.")
        if self.llm.chat_max_tokens < 1:
            raise ValueError("CONTENTAI_LLM__CHAT_MAX_TOKENS must be greater than 0.")
        if self.llm.structured_max_tokens < 1:
            raise ValueError("CONTENTAI_LLM__STRUCTURED_MAX_TOKENS must be greater than 0.")

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
        if self.agent.tool_timeout_seconds <= 0:
            raise ValueError("CONTENTAI_AGENT__TOOL_TIMEOUT_SECONDS must be greater than 0.")
        if self.agent.checkpoint_backend not in {"postgres"}:
            raise ValueError("CONTENTAI_AGENT__CHECKPOINT_BACKEND must be postgres.")
        if self.agent.memory_backend not in {"postgres"}:
            raise ValueError("CONTENTAI_AGENT__MEMORY_BACKEND must be postgres.")

    def _validate_search(self) -> None:
        if self.search.search_cache_ttl_seconds < 0:
            raise ValueError("CONTENTAI_SEARCH__SEARCH_CACHE_TTL_SECONDS cannot be negative.")
        if self.search.hotspot_cache_ttl_seconds < 0:
            raise ValueError("CONTENTAI_SEARCH__HOTSPOT_CACHE_TTL_SECONDS cannot be negative.")
        if self.env == Env.production:
            if not self.search.traffic_relay_api_key.get_secret_value().strip():
                raise ValueError(
                    "CONTENTAI_SEARCH__TRAFFIC_RELAY_API_KEY is required in production."
                )

    def _validate_auth(self) -> None:
        if not self.auth.enabled:
            return
        if not self.auth.user_claim.strip():
            raise ValueError("CONTENTAI_AUTH__USER_CLAIM cannot be empty.")
        if not self.auth.tenant_claim.strip():
            raise ValueError("CONTENTAI_AUTH__TENANT_CLAIM cannot be empty.")
        if self.env == Env.production or not self.auth.allow_unsigned_test_tokens:
            if not self.auth.oidc_issuer.strip():
                raise ValueError("CONTENTAI_AUTH__OIDC_ISSUER is required when auth is enabled.")
            if not self.auth.oidc_audience.strip():
                raise ValueError("CONTENTAI_AUTH__OIDC_AUDIENCE is required when auth is enabled.")
            if not self.auth.oidc_jwks_url.strip():
                raise ValueError("CONTENTAI_AUTH__OIDC_JWKS_URL is required when auth is enabled.")
            parsed_jwks = urlparse(self.auth.oidc_jwks_url)
            if not (parsed_jwks.scheme in {"http", "https"} and parsed_jwks.netloc):
                raise ValueError("CONTENTAI_AUTH__OIDC_JWKS_URL must be an HTTP(S) URL.")

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
