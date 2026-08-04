from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr


class ModelConfigurationSecretError(RuntimeError):
    """Raised when model-configuration secret material cannot be used safely."""


class ModelConfigurationSecretProtector:
    def __init__(self, encryption_key: str | SecretStr) -> None:
        key = (
            encryption_key.get_secret_value()
            if isinstance(encryption_key, SecretStr)
            else encryption_key
        )
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (UnicodeEncodeError, ValueError) as exc:
            raise ModelConfigurationSecretError(
                "Model configuration encryption key is invalid."
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            raise ModelConfigurationSecretError("Model configuration API key cannot be empty.")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError, ValueError) as exc:
            raise ModelConfigurationSecretError(
                "Model configuration API key could not be decrypted."
            ) from exc

    @staticmethod
    def fingerprint(plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

    @staticmethod
    def hint(plaintext: str) -> str:
        suffix = plaintext[-4:] if len(plaintext) > 4 else ""
        return f"...{suffix}"
