from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from services.errors import InvalidCursorError
from sqlalchemy import tuple_

CURSOR_SEPARATOR = "|"
MAX_RESPONSE_BYTES = 1024 * 1024
_RESPONSE_RESERVE_BYTES = 64 * 1024


def encode_cursor(timestamp: datetime, row_id: str) -> str:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise InvalidCursorError("Cursor timestamp must be timezone-aware")
    value = timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return f"{value}{CURSOR_SEPARATOR}{row_id}"


def decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor.strip():
        raise InvalidCursorError("Cursor cannot be empty")
    if CURSOR_SEPARATOR not in cursor:
        raise InvalidCursorError("Cursor format is invalid")
    timestamp_raw, row_id = cursor.split(CURSOR_SEPARATOR, 1)
    if not timestamp_raw or not row_id or len(row_id) > 120:
        raise InvalidCursorError("Cursor format is invalid")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if any(character not in allowed for character in row_id):
        raise InvalidCursorError("Cursor format is invalid")
    try:
        timestamp = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidCursorError("Cursor timestamp is invalid") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise InvalidCursorError("Cursor timestamp must be timezone-aware")
    return timestamp.astimezone(UTC), row_id


def apply_descending_cursor(
    statement: Any, timestamp_column: Any, id_column: Any, cursor: str | None
):
    decoded = decode_cursor(cursor)
    if decoded is None:
        return statement
    timestamp, row_id = decoded
    return statement.where(tuple_(timestamp_column, id_column) < tuple_(timestamp, row_id))


def apply_ascending_cursor(
    statement: Any, timestamp_column: Any, id_column: Any, cursor: str | None
):
    decoded = decode_cursor(cursor)
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
