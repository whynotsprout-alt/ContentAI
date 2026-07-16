from __future__ import annotations

from agent.runtime.checkpoint import RuntimePersistence
from alembic import command
from core.alembic import build_alembic_config
from core.config import get_settings
from core.logging import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging("migration", settings)
    config = build_alembic_config()
    command.upgrade(config, "head")
    persistence = RuntimePersistence(settings)
    try:
        persistence.setup()
    finally:
        persistence.close()


if __name__ == "__main__":
    main()
