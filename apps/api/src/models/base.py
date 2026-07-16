import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


def utcnow() -> datetime:
    # PostgreSQL models currently use TIMESTAMP WITHOUT TIME ZONE. Store UTC
    # consistently so values loaded from the database remain comparable.
    return datetime.now(UTC).replace(tzinfo=None)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"
