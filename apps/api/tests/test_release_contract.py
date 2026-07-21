from __future__ import annotations

import json
import tomllib
from pathlib import Path

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
