from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path

from alembic.config import Config


def build_alembic_config() -> Config:
    """Load an explicit/cwd Alembic config or fall back to packaged migrations."""

    explicit = os.getenv("CONTENTAI_ALEMBIC_CONFIG", "").strip()
    config_path = Path(explicit) if explicit else Path.cwd() / "alembic.ini"
    config = Config(str(config_path)) if config_path.is_file() else Config()
    if not config.get_main_option("script_location"):
        config.set_main_option("script_location", str(files("contentai.migrations")))
    return config


__all__ = ["build_alembic_config"]
