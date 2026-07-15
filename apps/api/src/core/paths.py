from pathlib import Path


def _find_project_root() -> Path:
    current = Path(__file__).resolve()

    def _is_project_root(path: Path) -> bool:
        has_pyproject = (path / "pyproject.toml").is_file()
        has_api = (path / "apps" / "api" / "src").is_dir()
        return has_api and has_pyproject

    for parent in current.parents:
        if _is_project_root(parent):
            return parent

    raise RuntimeError(f"Could not locate project root from: {current}")


PROJECT_ROOT = _find_project_root()
WORKSPACE_ROOT = PROJECT_ROOT.parent
CONFIG_DIR = PROJECT_ROOT / "configs"
DATA_DIR = PROJECT_ROOT / "data"
DOCS_DIR = PROJECT_ROOT / "docs"
PROMPTS_DIR = PROJECT_ROOT / "apps" / "api" / "src" / "agent" / "prompts"


def ensure_runtime_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def safe_relative_to(path: Path, root: Path) -> Path:
    root_resolved = root.resolve()
    if path.is_absolute():
        resolved = path.resolve()
    else:
        resolved = (root_resolved / path).resolve()

    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"Path escapes allowed root: {path}") from exc
    return resolved
