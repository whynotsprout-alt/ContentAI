from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from core.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutboxMessage:
    recipient: str
    subject: str
    text: str


class Mailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.outbox: list[OutboxMessage] = []

    def send(self, *, recipient: str, subject: str, text: str) -> None:
        message = OutboxMessage(recipient=recipient, subject=subject, text=text)
        if self.settings.auth.mail_backend == "console":
            self.outbox.append(message)
            logger.info("Development email to %s: %s\n%s", recipient, subject, text)
            return

        email = EmailMessage()
        email["To"] = recipient
        email["From"] = self.settings.auth.smtp_from_email
        email["Subject"] = subject
        email.set_content(text)
        with smtplib.SMTP(
            self.settings.auth.smtp_host,
            self.settings.auth.smtp_port,
            timeout=20,
        ) as client:
            client.ehlo()
            if self.settings.auth.smtp_use_tls:
                client.starttls()
                client.ehlo()
            password = self.settings.auth.smtp_password.get_secret_value()
            if self.settings.auth.smtp_username:
                client.login(self.settings.auth.smtp_username, password)
            client.send_message(email)
