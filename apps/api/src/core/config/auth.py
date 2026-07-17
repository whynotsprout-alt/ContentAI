from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AuthSettings(BaseModel):
    # Ignore retired email settings left in existing deployments' .env files.
    model_config = ConfigDict(extra="ignore")

    session_cookie_name: str = "contentai_session"
    csrf_cookie_name: str = "contentai_csrf"
    session_days: int = 30
    login_max_failures: int = 5
    login_lock_minutes: int = 15
    bootstrap_admin_emails: list[str] = Field(default_factory=list)
