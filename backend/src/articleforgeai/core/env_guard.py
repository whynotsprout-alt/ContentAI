from __future__ import annotations

import os
import sys


def assert_project_virtualenv() -> None:
    """Prevent running the API from the global Python environment by accident."""

    if os.getenv("ARTICLEFORGE_ALLOW_SYSTEM_PYTHON") == "1":
        return
    if sys.prefix == sys.base_prefix:
        raise RuntimeError(
            "ContentAI must run inside the project virtual environment. "
            "Create it with scripts/setup.ps1, then start through scripts/dev-api.ps1."
        )
