from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from services.errors import InvalidCursorError
from sqlalchemy import tuple_

CURSOR_SEPARATOR = "|"
_CURSOR_VERSION = "v1"
MAX_RESPONSE_BYTES = 1024 * 1024
_RESPONSE_RESERVE_BYTES = 64 * 1024


class CursorSigner:
    def __init__(self, secret: str) -> None:
        self._key = hashlib.sha256(secret.encode("utf-8")).digest()

    def encode(self, timestamp: datetime, row_id: str, *, scope: str) -> str:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise InvalidCursorError("Cursor timestamp must be timezone-aware")
        payload = {
            "at": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "id": row_id,
            "scope": scope,
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        encoded = _b64encode(raw)
        signature = _b64encode(
            hmac.new(self._key, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{_CURSOR_VERSION}.{encoded}.{signature}"

    def decode(self, cursor: str | None, *, scope: str) -> tuple[datetime, str] | None:
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not cursor.strip():
            raise InvalidCursorError("Cursor cannot be empty")
        parts = cursor.split(".")
        if len(parts) != 3 or parts[0] != _CURSOR_VERSION:
            raise InvalidCursorError("Cursor signature is invalid")
        _, encoded, provided_signature = parts
        expected_signature = _b64encode(
            hmac.new(self._key, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(provided_signature, expected_signature):
            raise InvalidCursorError("Cursor signature is invalid")
        try:
            payload = json.loads(_b64decode(encoded))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidCursorError("Cursor payload is invalid") from exc
        if not isinstance(payload, dict) or payload.get("scope") != scope:
            raise InvalidCursorError("Cursor scope is invalid")
        timestamp_raw = payload.get("at")
        row_id = payload.get("id")
        if not isinstance(timestamp_raw, str) or not isinstance(row_id, str):
            raise InvalidCursorError("Cursor payload is invalid")
        if len(row_id) > 120:
            raise InvalidCursorError("Cursor format is invalid")
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        if not row_id or any(character not in allowed for character in row_id):
            raise InvalidCursorError("Cursor format is invalid")
        try:
            timestamp = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidCursorError("Cursor timestamp is invalid") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise InvalidCursorError("Cursor timestamp must be timezone-aware")
        return timestamp.astimezone(UTC), row_id


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> str:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")


def signer_from_settings(settings: Any) -> CursorSigner:
    password = settings.auth.bootstrap_admin_password.get_secret_value()
    secret = password or str(settings.database.url)
    return CursorSigner(secret)


def encode_cursor(
    timestamp: datetime,
    row_id: str,
    *,
    scope: str = "",
    signer: CursorSigner | None = None,
) -> str:
    signer = signer or CursorSigner("contentai-cursor-fallback")
    return signer.encode(timestamp, row_id, scope=scope)


def decode_cursor(
    cursor: str | None,
    *,
    scope: str = "",
    signer: CursorSigner | None = None,
) -> tuple[datetime, str] | None:
    signer = signer or CursorSigner("contentai-cursor-fallback")
    return signer.decode(cursor, scope=scope)


def apply_descending_cursor(
    statement: Any,
    timestamp_column: Any,
    id_column: Any,
    cursor: str | None,
    *,
    scope: str = "",
    signer: CursorSigner | None = None,
):
    decoded = decode_cursor(cursor, scope=scope, signer=signer)
    if decoded is None:
        return statement
    timestamp, row_id = decoded
    return statement.where(tuple_(timestamp_column, id_column) < tuple_(timestamp, row_id))


def apply_ascending_cursor(
    statement: Any,
    timestamp_column: Any,
    id_column: Any,
    cursor: str | None,
    *,
    scope: str = "",
    signer: CursorSigner | None = None,
):
    decoded = decode_cursor(cursor, scope=scope, signer=signer)
    if decoded is None:
        return statement
    timestamp, row_id = decoded
    return statement.where(tuple_(timestamp_column, id_column) > tuple_(timestamp, row_id))


def fit_response_items[T: BaseModel](items: list[T]) -> tuple[list[T], bool]:
    size = len(b'{"items":[],"next_cursor":null}')
    accepted: list[T] = []
    for item in items:
        item_size = len(item.model_dump_json().encode("utf-8")) + (1 if accepted else 0)
        if size + item_size + _RESPONSE_RESERVE_BYTES > MAX_RESPONSE_BYTES:
            return accepted, True
        accepted.append(item)
        size += item_size
    return accepted, False
