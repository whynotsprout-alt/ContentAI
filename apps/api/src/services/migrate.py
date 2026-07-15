from __future__ import annotations

from agent.runtime.checkpoint import RuntimePersistence
from alembic import command
from alembic.config import Config
from core.config import get_settings
from core.logging import configure_logging
from core.paths import PROJECT_ROOT


def main() -> None:
    settings = get_settings()
    configure_logging("migration", settings)
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    persistence = RuntimePersistence(settings)
    try:
        persistence.setup()
    finally:
        persistence.close()


if __name__ == "__main__":
    main()
