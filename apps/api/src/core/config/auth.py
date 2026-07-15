from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class AuthSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_cookie_name: str = "contentai_session"
    csrf_cookie_name: str = "contentai_csrf"
    session_days: int = 30
    require_email_verification: bool = True
    verification_hours: int = 24
    reset_minutes: int = 30
    login_max_failures: int = 5
    login_lock_minutes: int = 15
    resend_verification_limit: int = 3
    resend_verification_window_seconds: int = 3600
    public_base_url: str = "http://127.0.0.1:5180"
    bootstrap_admin_emails: list[str] = Field(default_factory=list)
    mail_backend: str = "console"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from_email: str = ""
    smtp_use_tls: bool = True
