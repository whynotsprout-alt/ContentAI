from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _canonical_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def test_release_artifacts_and_maintained_docs_use_the_canonical_version() -> None:
    version = _canonical_version()
    web_package = json.loads((ROOT / "apps/web/package.json").read_text(encoding="utf-8"))
    web_lock = json.loads((ROOT / "apps/web/package-lock.json").read_text(encoding="utf-8"))
    package_script = (ROOT / "tools/package-ubuntu.ps1").read_text(encoding="utf-8")
    maintained_docs = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in ("docs/OPERATIONS.md", "docs/UBUNTU_FROM_SCRATCH.md")
    )

    assert version == "0.5.0-rc.1"
    assert web_package["version"] == version
    assert web_lock["version"] == version
    assert web_lock["packages"][""]["version"] == version
    assert "pyproject.toml" in package_script
    assert "release-manifest.json" in package_script
    assert ".sha256" in package_script
    assert 'param([string]$Version = "0.4.3")' not in package_script
    assert "0.4.3" not in maintained_docs
    assert f"contentai-{version}-ubuntu.tar.gz" in maintained_docs
    assert f"V{version}" in maintained_docs


def test_maintained_docs_match_current_authentication_and_compose_topology() -> None:
    design = (ROOT / "docs/DESIGN.md").read_text(encoding="utf-8")
    operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")

    assert "pending_verification" in design
    assert "403" in design
    assert "404" in design
    assert "410 Gone" not in design
    for document in (design, operations, architecture):
        assert "side-effect-worker" in document
        assert "十个" in document
        assert "三个 Worker" in document or "三个 worker" in document


def test_release_diff_files_have_one_canonical_eof_newline() -> None:
    for relative_path in (
        "apps/api/src/core/client_ip.py",
        "docs/superpowers/plans/2026-07-17-bootstrap-admin-and-env-cleanup.md",
        "docs/superpowers/specs/2026-07-17-bootstrap-admin-design.md",
    ):
        content = (ROOT / relative_path).read_bytes()
        assert content.endswith(b"\n"), relative_path
        assert not content.endswith((b"\n\n", b"\r\n\r\n")), relative_path


def test_model_config_persistence_failure_is_a_documented_stable_code() -> None:
    api_documentation = (ROOT / "docs/API.md").read_text(encoding="utf-8")

    assert "MODEL_CONFIG_PERSISTENCE_FAILED" in api_documentation
    assert "503" in api_documentation


def test_release_package_uses_the_fixed_head_tree(tmp_path: Path) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("Ubuntu package script requires PowerShell.")

    repository = tmp_path / "release-repository"
    (repository / "tools").mkdir(parents=True)
    shutil.copy2(ROOT / "tools/package-ubuntu.ps1", repository / "tools/package-ubuntu.ps1")
    required_files = {
        "apps/api/src/tracked.py": "from-tree\n",
        "apps/api/src/contentai_migrations/env.py": "\n",
        "apps/api/src/contentai_migrations/versions/202607210001_v050_initial_schema.py": "\n",
        "apps/web/src/main.ts": "\n",
        "apps/web/index.html": "<main></main>\n",
        "apps/web/package.json": "{}\n",
        "apps/web/package-lock.json": "{}\n",
        "apps/web/tsconfig.json": "{}\n",
        "apps/web/tsconfig.node.json": "{}\n",
        "apps/web/vite.config.ts": "\n",
        "docs/OPERATIONS.md": "tracked documentation\n",
        "infra/ubuntu/deploy.sh": "#!/usr/bin/env bash\n",
        "infra/ubuntu/health.sh": "#!/usr/bin/env bash\n",
        "infra/ubuntu/backup.sh": "#!/usr/bin/env bash\n",
        "infra/ubuntu/restore.sh": "#!/usr/bin/env bash\n",
        "infra/ubuntu/upgrade.sh": "#!/usr/bin/env bash\n",
        "compose.yaml": "services: {}\n",
        "pyproject.toml": "[project]\nversion = \"0.5.0-rc.1\"\n",
        "uv.lock": "version = 1\n",
        "requirements.txt": "\n",
        "alembic.ini": "\n",
        ".env.example": "\n",
        ".dockerignore": "\n",
        "README.md": "# ContentAI\n",
    }
    for relative_path, content in required_files.items():
        destination = repository / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")

    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=ContentAI Test",
            "-c",
            "user.email=contentai-test@example.test",
            "commit",
            "-m",
            "release tree",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    tracked_from_tree = (repository / "apps/api/src/tracked.py").read_bytes()
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "apps/api/src/tracked.py").write_text("from-worktree\n", encoding="utf-8")
    (repository / "docs/untracked-review.md").write_text("untracked\n", encoding="utf-8")

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(repository / "tools/package-ubuntu.ps1"),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    archive = repository / "dist/contentai-0.5.0-rc.1-ubuntu.tar.gz"
    with tarfile.open(archive, "r:gz") as packaged:
        names = packaged.getnames()
        assert "contentai-0.5.0-rc.1-ubuntu/docs/untracked-review.md" not in names
        tracked = packaged.extractfile(
            "contentai-0.5.0-rc.1-ubuntu/apps/api/src/tracked.py"
        )
        manifest = packaged.extractfile("contentai-0.5.0-rc.1-ubuntu/release-manifest.json")
        assert tracked is not None
        assert manifest is not None
        tracked_content = tracked.read()
        assert tracked_content == tracked_from_tree, tracked_content
        assert json.loads(manifest.read())["commit"] == commit
