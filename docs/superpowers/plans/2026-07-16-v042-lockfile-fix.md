# ContentAI V0.4.2 Lockfile Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Synchronize the dependency lock with version 0.4.2, publish V0.4.2, and start a fresh isolated test deployment.

**Architecture:** Change only release metadata and the generated uv lock, then verify the exact Docker build path that failed. The test deployment uses a separate Compose project, fresh named volumes, and loopback ports distinct from the legacy stack.

**Tech Stack:** uv, Docker Compose v2, PowerShell, pytest, npm, GitHub CLI.

## Global Constraints

- V0.4 and V0.4.1 tags, Releases and assets remain immutable.
- Package/archive version is `0.4.2`; Git tag/Release tag is `V0.4.2`.
- Exclude `AGENTS.md` from every commit.
- Do not release before `uv lock --check`, full checks, package build and Compose image build pass.
- The test deployment uses project `contentai-v042-test` and ports Web `5290`, API `9010`, PostgreSQL `56433`, Redis `57379`.

### Task 1: Synchronize release metadata and uv lock

**Files:**
- Modify: `pyproject.toml:3`
- Modify: `uv.lock`
- Modify: `tools/package-ubuntu.ps1:1`
- Modify: `docs/OPERATIONS.md` current package examples
- Create: `docs/superpowers/specs/2026-07-16-v042-lockfile-fix-design.md`
- Create: `docs/superpowers/plans/2026-07-16-v042-lockfile-fix.md`

- [ ] Run `uv lock --check` before editing; it must fail because `uv.lock` is stale.
- [ ] Update version values to `0.4.2`, run `uv lock`, then rerun `uv lock --check`; it must exit 0 and lock `contentai` at `0.4.2`.
- [ ] Run `tools/test-deps.ps1 up`, then `tools/review.ps1`, then `tools/test-deps.ps1 down` in a finally block; stop on any failed command.
- [ ] Run `tools/package-ubuntu.ps1 -Version 0.4.2`, `uv build`, fresh-wheel import of `api.app`, and `docker compose --env-file <temporary test env> -p contentai-v042-test -f compose.yaml -f compose.dev.yaml build`; all must exit 0.
- [ ] Commit only the listed text/lock files as `fix: synchronize V0.4.2 uv lock`.

### Task 2: Publish V0.4.2 and start the isolated deployment

- [ ] Verify only `AGENTS.md` is untracked, create annotated tag `V0.4.2`, push `main` and the new tag without force, and verify V0.4/V0.4.1 objects remain unchanged.
- [ ] Create a formal GitHub Release `ContentAI V0.4.2` with `contentai-0.4.2-ubuntu.tar.gz`, a concrete SHA256, the lock-file correction, fresh-install limitation, and note that earlier releases are unchanged.
- [ ] Create a temporary Compose env by copying `.env` without exposing it, override only the test-project database/Redis/ports, start `contentai-v042-test` with `--build --wait`, and verify `http://127.0.0.1:9010/api/ready` plus `http://127.0.0.1:5290/`.
