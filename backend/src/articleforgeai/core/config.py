from __future__ import annotations

from functools import lru_cache

from articleforgeai.core.paths import PROJECT_ROOT, SQL_DIR
from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv(PROJECT_ROOT / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    app_name: str = "ContentAI"
    frontend_origin: str = Field("http://localhost:5173", alias="ARTICLEFORGE_FRONTEND_ORIGIN")
    database_url: str = Field(
        f"sqlite:///{(SQL_DIR / 'contentai.sqlite3').as_posix()}",
        alias="ARTICLEFORGE_DATABASE_URL",
    )

    traffic_relay_base_url: str = Field(
        "https://traffic-relay.onrender.com/v1", alias="TRAFFIC_RELAY_BASE_URL"
    )
    traffic_relay_api_key: str = Field("", alias="TRAFFIC_RELAY_API_KEY")
    agent_model: str = Field("gpt-5.5", alias="ARTICLEFORGE_AGENT_MODEL")
    content_model: str = Field("claude-opus-4-6", alias="ARTICLEFORGE_CONTENT_MODEL")
    content_max_tokens: int = Field(128000, alias="ARTICLEFORGE_CONTENT_MAX_TOKENS")
    model_mode: str = Field("demo", alias="ARTICLEFORGE_MODEL_MODE")

    tikhub_api_key: str = Field("", alias="TIKHUB_API_KEY")
    metaso_api_key: str = Field("", alias="METASO_API_KEY")
    anspire_api_key: str = Field("", alias="ANSPIRE_API_KEY")

    @property
    def frontend_origins(self) -> list[str]:
        configured = [
            item.strip()
            for item in self.frontend_origin.split(",")
            if item.strip()
        ]
        local_origins = [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
        return list(dict.fromkeys([*configured, *local_origins]))


@lru_cache
def get_settings() -> Settings:
    return Settings()
