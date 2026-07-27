from __future__ import annotations

import os

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
)


def get_test_database_url() -> str:
    """Return the one database URL shared by the fixture and explicit test Settings."""
    return os.getenv("CONTENTAI_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


__all__ = ["DEFAULT_TEST_DATABASE_URL", "get_test_database_url"]
