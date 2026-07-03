import os

import pytest

os.environ.setdefault("CONTENTAI_ENV", "test")
os.environ.setdefault(
    "CONTENTAI_TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test",
)
os.environ.setdefault("CONTENTAI_RUN_WORKER_ENABLED", "true")
os.environ.setdefault("TRAFFIC_RELAY_API_KEY", "test-key")
os.environ.setdefault("TRAFFIC_RELAY_BASE_URL", "https://example.test/v1")


def pytest_configure() -> None:
    from alembic import command
    from alembic.config import Config
    from core.paths import PROJECT_ROOT

    _ensure_test_database_exists()
    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(alembic_config, "head")


def _ensure_test_database_exists() -> None:
    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import make_url

    raw_url = os.environ["CONTENTAI_TEST_DATABASE_URL"]
    url = make_url(raw_url)
    database = url.database
    if not database:
        raise RuntimeError("CONTENTAI_TEST_DATABASE_URL must include a database name.")
    if database in {"postgres", "template0", "template1"}:
        raise RuntimeError("CONTENTAI_TEST_DATABASE_URL must point to a dedicated test database.")

    maintenance_url = (
        url.set(database="postgres")
        .render_as_string(hide_password=False)
        .replace("postgresql+psycopg://", "postgresql://", 1)
        .replace("postgresql+psycopg2://", "postgresql://", 1)
    )
    with psycopg.connect(maintenance_url, autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (database,),
        ).fetchone()
        if exists is None:
            connection.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
            )


@pytest.fixture(autouse=True)
def reset_database() -> None:
    from db.session import engine
    from models.account import Account
    from sqlalchemy import text
    from sqlmodel import Session

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                TRUNCATE TABLE
                    agentrunevent,
                    chatmessage,
                    agentrun,
                    chatsession,
                    memoryrecord,
                    account
                RESTART IDENTITY CASCADE
                """
            )
        )

    with Session(engine) as session:
        session.add(
            Account(
                id="default-agent",
                name="默认 Agent",
                positioning="用于测试的财经内容账号。",
                topic_scoring_prompt="按账号定位、受众价值、时效性和风险对选题进行 0-100 分评分。",
                content_creation_prompt="内容输出保持直接、清晰，包含标题和正文。",
                hotspot_sources=["douyin", "weibo"],
            )
        )
        session.commit()
