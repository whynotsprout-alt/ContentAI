from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from core.config import Settings
from models.base import utcnow
from models.schemas.auth import CurrentUserResponse
from models.user import AppUser, AuthSession, UserActionToken
from pwdlib import PasswordHash
from services.mailer import Mailer
from sqlmodel import Session, select

VERIFY_PURPOSE = "verify_email"
RESET_PURPOSE = "reset_password"


class AuthServiceError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class IssuedSession:
    user: AppUser
    session_token: str
    csrf_token: str


class AuthService:
    def __init__(self, settings: Settings, mailer: Mailer) -> None:
        self.settings = settings
        self.mailer = mailer
        self.password_hash = PasswordHash.recommended()
        self.dummy_hash = self.password_hash.hash("contentai-dummy-password")

    @staticmethod
    def normalize_email(email: str) -> str:
        return email.strip().lower()

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def register(self, session: Session, *, email: str, password: str) -> AppUser:
        normalized = self.normalize_email(email)
        existing = session.exec(
            select(AppUser).where(AppUser.email_normalized == normalized)
        ).first()
        if existing is not None:
            raise AuthServiceError("该邮箱已注册", status_code=409)
        role = "admin" if normalized in self.settings.auth.bootstrap_admin_emails else "user"
        now = utcnow()
        requires_verification = self.settings.auth.require_email_verification
        user = AppUser(
            email=normalized,
            email_normalized=normalized,
            password_hash=self.password_hash.hash(password),
            role=role,
            status="pending_verification" if requires_verification else "active",
            email_verified_at=None if requires_verification else now,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        if requires_verification:
            raw = self.issue_action_token(session, user=user, purpose=VERIFY_PURPOSE)
            self.send_verification(user, raw)
        return user

    def verify_email(self, session: Session, *, token: str) -> AppUser:
        record, user = self.consume_action_token(
            session,
            token=token,
            purpose=VERIFY_PURPOSE,
        )
        if user.status == "disabled":
            raise AuthServiceError("Account is disabled", status_code=403)
        now = utcnow()
        user.email_verified_at = user.email_verified_at or now
        user.status = "active"
        user.updated_at = now
        record.used_at = now
        session.add(record)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user

    def resend_verification(self, session: Session, *, email: str) -> None:
        user = self.get_user_by_email(session, email)
        if user is None or user.email_verified_at is not None or user.status == "disabled":
            return
        raw = self.issue_action_token(session, user=user, purpose=VERIFY_PURPOSE)
        self.send_verification(user, raw)

    def login(
        self,
        session: Session,
        *,
        email: str,
        password: str,
        user_agent: str = "",
        ip_address: str = "",
    ) -> IssuedSession:
        user = self.get_user_by_email(session, email)
        now = utcnow()
        if user is None:
            self.password_hash.verify(password, self.dummy_hash)
            raise AuthServiceError("邮箱或密码错误", status_code=401)
        if user.locked_until is not None and user.locked_until > now:
            raise AuthServiceError("登录尝试过多，请稍后再试", status_code=429)
        if not self.password_hash.verify(password, user.password_hash):
            user.failed_login_count += 1
            if user.failed_login_count >= self.settings.auth.login_max_failures:
                user.locked_until = now + timedelta(minutes=self.settings.auth.login_lock_minutes)
                user.failed_login_count = 0
            user.updated_at = now
            session.add(user)
            session.commit()
            raise AuthServiceError("邮箱或密码错误", status_code=401)
        if user.status == "disabled":
            raise AuthServiceError("账号已被禁用", status_code=403)
        if not self.settings.auth.require_email_verification and user.email_verified_at is None:
            user.email_verified_at = now
            user.status = "active"
        if user.email_verified_at is None or user.status != "active":
            raise AuthServiceError("请先完成邮箱验证", status_code=403)

        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now
        user.updated_at = now
        session.add(user)
        session_token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        auth_session = AuthSession(
            user_id=user.id,
            token_hash=self.token_hash(session_token),
            csrf_hash=self.token_hash(csrf_token),
            user_agent=user_agent[:1000],
            ip_address=ip_address[:120],
            expires_at=now + timedelta(days=self.settings.auth.session_days),
        )
        session.add(auth_session)
        session.commit()
        return IssuedSession(user=user, session_token=session_token, csrf_token=csrf_token)

    def logout(self, session: Session, session_id: str | None) -> None:
        if not session_id:
            return
        auth_session = session.get(AuthSession, session_id)
        if auth_session is not None and auth_session.revoked_at is None:
            auth_session.revoked_at = utcnow()
            session.add(auth_session)
            session.commit()

    def forgot_password(self, session: Session, *, email: str) -> None:
        user = self.get_user_by_email(session, email)
        if user is None or user.status == "disabled":
            return
        if self.settings.auth.require_email_verification and user.email_verified_at is None:
            return
        if user.email_verified_at is None:
            user.email_verified_at = utcnow()
            user.status = "active"
            session.add(user)
        raw = self.issue_action_token(session, user=user, purpose=RESET_PURPOSE)
        self.send_password_reset(user, raw)

    def reset_password(self, session: Session, *, token: str, password: str) -> AppUser:
        record, user = self.consume_action_token(
            session,
            token=token,
            purpose=RESET_PURPOSE,
        )
        now = utcnow()
        user.password_hash = self.password_hash.hash(password)
        user.password_changed_at = now
        user.failed_login_count = 0
        user.locked_until = None
        user.updated_at = now
        record.used_at = now
        session.add(record)
        session.add(user)
        self.revoke_all_sessions(session, user.id, commit=False)
        session.commit()
        return user

    def change_password(
        self,
        session: Session,
        *,
        user_id: str,
        current_password: str,
        new_password: str,
    ) -> None:
        user = session.get(AppUser, user_id)
        if user is None or not self.password_hash.verify(current_password, user.password_hash):
            raise AuthServiceError("当前密码错误", status_code=400)
        now = utcnow()
        user.password_hash = self.password_hash.hash(new_password)
        user.password_changed_at = now
        user.updated_at = now
        session.add(user)
        self.revoke_all_sessions(session, user.id, commit=False)
        session.commit()

    def issue_action_token(self, session: Session, *, user: AppUser, purpose: str) -> str:
        now = utcnow()
        for item in session.exec(
            select(UserActionToken).where(
                UserActionToken.user_id == user.id,
                UserActionToken.purpose == purpose,
                UserActionToken.used_at.is_(None),
            )
        ).all():
            item.used_at = now
            session.add(item)
        raw = secrets.token_urlsafe(48)
        lifetime = (
            timedelta(hours=self.settings.auth.verification_hours)
            if purpose == VERIFY_PURPOSE
            else timedelta(minutes=self.settings.auth.reset_minutes)
        )
        session.add(
            UserActionToken(
                user_id=user.id,
                purpose=purpose,
                token_hash=self.token_hash(raw),
                expires_at=now + lifetime,
            )
        )
        session.commit()
        return raw

    def consume_action_token(
        self,
        session: Session,
        *,
        token: str,
        purpose: str,
    ) -> tuple[UserActionToken, AppUser]:
        record = session.exec(
            select(UserActionToken).where(
                UserActionToken.token_hash == self.token_hash(token),
                UserActionToken.purpose == purpose,
            )
        ).first()
        now = utcnow()
        if record is None or record.used_at is not None or record.expires_at <= now:
            raise AuthServiceError("链接无效或已过期", status_code=400)
        user = session.get(AppUser, record.user_id)
        if user is None:
            raise AuthServiceError("链接无效或已过期", status_code=400)
        if user.status == "disabled":
            raise AuthServiceError("Account is disabled", status_code=403)
        return record, user

    def revoke_all_sessions(self, session: Session, user_id: str, *, commit: bool = True) -> None:
        now = utcnow()
        rows = session.exec(
            select(AuthSession).where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
            )
        ).all()
        for item in rows:
            item.revoked_at = now
            session.add(item)
        if commit:
            session.commit()

    def get_user_by_email(self, session: Session, email: str) -> AppUser | None:
        return session.exec(
            select(AppUser).where(AppUser.email_normalized == self.normalize_email(email))
        ).first()

    def send_verification(self, user: AppUser, token: str) -> None:
        link = f"{self.settings.auth.public_base_url.rstrip('/')}/verify-email?token={token}"
        self.mailer.send(
            recipient=user.email,
            subject="验证你的 ContentAI 邮箱",
            text=(
                f"请在 {self.settings.auth.verification_hours} 小时内打开以下链接完成验证：\n{link}"
            ),
        )

    def send_password_reset(self, user: AppUser, token: str) -> None:
        link = f"{self.settings.auth.public_base_url.rstrip('/')}/reset-password?token={token}"
        self.mailer.send(
            recipient=user.email,
            subject="重置你的 ContentAI 密码",
            text=f"请在 {self.settings.auth.reset_minutes} 分钟内打开以下链接重置密码：\n{link}",
        )

    @staticmethod
    def to_response(user: AppUser) -> CurrentUserResponse:
        return CurrentUserResponse(
            id=user.id,
            tenant_id=user.tenant_id,
            email=user.email,
            role=user.role,
            status=user.status,
            email_verified_at=user.email_verified_at,
            password_changed_at=user.password_changed_at,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
        )
