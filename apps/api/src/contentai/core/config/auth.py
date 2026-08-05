from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, SecretStr, model_validator


class AuthSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def reject_retired_admin_allowlist(cls, value: Any) -> Any:
        if isinstance(value, dict) and "bootstrap_admin_emails" in value:
            raise ValueError(
                "bootstrap_admin_emails is retired; configure one bootstrap_admin_email"
            )
        return value

    session_cookie_name: str = "contentai_session"
    csrf_cookie_name: str = "contentai_csrf"
    session_days: int = 30
    login_max_failures: int = 5
    login_lock_minutes: int = 15
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: SecretStr = SecretStr("")
