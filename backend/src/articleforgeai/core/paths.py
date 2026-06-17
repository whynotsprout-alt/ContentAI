from __future__ import annotations

from pathlib import Path


def _find_project_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "configs").is_dir() and (parent / "backend").is_dir():
            return parent
    # Fallback for old layout (backend/src) or unusual checkouts.
    return Path(__file__).resolve().parents[4]


PROJECT_ROOT = _find_project_root()
WORKSPACE_ROOT = PROJECT_ROOT.parent
CONFIG_DIR = PROJECT_ROOT / "configs"
SQL_DIR = WORKSPACE_ROOT / "Sql"
DATA_DIR = PROJECT_ROOT / "data"
RUNS_DIR = SQL_DIR / "runs"
DOCS_DIR = WORKSPACE_ROOT / "Docs"


def ensure_runtime_dirs() -> None:
    SQL_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def safe_relative_to(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"Path escapes allowed root: {path}") from exc
    return resolved
