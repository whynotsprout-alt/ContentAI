from enum import StrEnum
from functools import lru_cache
from typing import Self
from urllib.parse import urlparse

from core.paths import PROJECT_ROOT
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_FRONTEND_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai"
POSTGRES_SCHEMES = {"postgresql", "postgresql+psycopg", "postgresql+psycopg2"}


class Env(StrEnum):
    development = "development"
    production = "production"
    test = "test"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    app_name: str = "ContentAI"
    env: Env = Field(Env.development, alias="CONTENTAI_ENV")
    frontend_origins_raw: str = Field("", alias="CONTENTAI_FRONTEND_ORIGINS")
    database_url: str = Field(DEFAULT_DATABASE_URL, alias="CONTENTAI_DATABASE_URL")
    test_database_url: str = Field("", alias="CONTENTAI_TEST_DATABASE_URL")
    traffic_relay_base_url: str = Field(
        "https://traffic-relay.onrender.com/v1", alias="TRAFFIC_RELAY_BASE_URL"
    )
    traffic_relay_api_key: SecretStr = Field("", alias="TRAFFIC_RELAY_API_KEY")
    tikhub_api_key: SecretStr = Field("", alias="TIKHUB_API_KEY")
    metaso_api_key: SecretStr = Field("", alias="METASO_API_KEY")
    metaso_search_api_key: SecretStr = Field("", alias="METASO_SEARCH_API_KEY")
    metaso_key: SecretStr = Field("", alias="METASO_KEY")
    anspire_api_key: SecretStr = Field("", alias="ANSPIRE_API_KEY")
    llm_model: str = Field("claude-opus-4-6", alias="CONTENTAI_LLM_MODEL")
    llm_max_tokens: int = Field(4096, alias="CONTENTAI_LLM_MAX_TOKENS")
    llm_temperature: float = Field(0.2, alias="CONTENTAI_LLM_TEMPERATURE")
    run_worker_enabled: bool = Field(True, alias="CONTENTAI_RUN_WORKER_ENABLED")
    run_lease_seconds: int = Field(600, alias="CONTENTAI_RUN_LEASE_SECONDS")
    run_max_attempts: int = Field(3, alias="CONTENTAI_RUN_MAX_ATTEMPTS")
    run_poll_seconds: float = Field(2.0, alias="CONTENTAI_RUN_POLL_SECONDS")

    @property
    def frontend_origins(self) -> list[str]:
        configured = [
            item.strip()
            for item in self.frontend_origins_raw.split(",")
            if item.strip()
        ]
        if self.env == Env.development:
            configured.extend(LOCAL_FRONTEND_ORIGINS)
        return list(dict.fromkeys(configured))

    @model_validator(mode="after")
    def _validate_env_settings(self) -> Self:
        if self.env == Env.test:
            if not self.test_database_url.strip():
                raise ValueError(
                    "CONTENTAI_TEST_DATABASE_URL is required in test environment."
                )
            self.database_url = self.test_database_url.strip()

        parsed_database = urlparse(self.database_url)
        if parsed_database.scheme not in POSTGRES_SCHEMES:
            raise ValueError(
                "CONTENTAI_DATABASE_URL must use PostgreSQL. "
                "Supported schemes: postgresql, postgresql+psycopg, postgresql+psycopg2."
            )

        if self.env == Env.production:
            if self.database_url == DEFAULT_DATABASE_URL:
                raise ValueError(
                    "CONTENTAI_DATABASE_URL must be explicitly configured in production."
                )
            if not self.frontend_origins_raw.strip():
                raise ValueError(
                    "CONTENTAI_FRONTEND_ORIGINS is required in production environment."
                )

        if not self.database_url:
            raise ValueError("CONTENTAI_DATABASE_URL cannot be empty.")

        if self.run_lease_seconds < 30:
            raise ValueError("CONTENTAI_RUN_LEASE_SECONDS must be at least 30.")
        if self.run_max_attempts < 1:
            raise ValueError("CONTENTAI_RUN_MAX_ATTEMPTS must be at least 1.")
        if self.run_poll_seconds <= 0:
            raise ValueError("CONTENTAI_RUN_POLL_SECONDS must be greater than 0.")

        if self.env == Env.production and not self.frontend_origins:
            raise ValueError(
                "CONTENTAI_FRONTEND_ORIGINS must be set for production environment."
            )

        for raw_origin in self.frontend_origins_raw.split(","):
            origin = raw_origin.strip()
            if not origin:
                continue
            parsed = urlparse(origin)
            if not (parsed.scheme in {"http", "https"} and parsed.netloc):
                raise ValueError(f"Invalid frontend origin URL: {origin}")

        if self.env == Env.production:
            if not self.traffic_relay_api_key.get_secret_value().strip():
                raise ValueError(
                    "TRAFFIC_RELAY_API_KEY is required in production environment."
                )

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
