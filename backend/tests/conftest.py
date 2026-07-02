import os

os.environ.setdefault("CONTENTAI_DATABASE_URL", "sqlite:///./data/test.db")
os.environ.setdefault("TRAFFIC_RELAY_API_KEY", "test-key")
os.environ.setdefault("TRAFFIC_RELAY_BASE_URL", "https://example.test/v1")


def pytest_configure() -> None:
    import models.db  # noqa: F401
    from db.session import engine
    from models.account import Account
    from sqlmodel import Session, SQLModel

    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Account(
                id="default-agent",
                name="默认 Agent",
                description="用于测试的开放式持续对话 Agent。",
                instructions="保持直接、清晰、中文回应。",
            )
        )
        session.commit()
