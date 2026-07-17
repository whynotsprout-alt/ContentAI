# ContentAI V0.5.0 Production Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship ContentAI V0.5.0 with the audited production blockers fixed, broken public contracts replaced, and obsolete code removed without expanding the product into new workflows.

**Architecture:** Keep the existing FastAPI + PostgreSQL + Redis/Celery + LangGraph + Vue architecture. Add database-enforced lineage, execution-scoped checkpoints and idempotent materialization; make authentication recovery admin-driven; make the existing workbench responsive rather than creating a separate mobile application. Web/API/dispatcher/workers migrate and deploy atomically.

**Tech Stack:** Python 3.12, FastAPI, SQLModel/SQLAlchemy, Alembic, PostgreSQL, Redis, Celery, LangGraph, Vue 3, Pinia, TypeScript, Vitest, Playwright.

## Global Constraints

- Target release is `0.5.0-rc.1` followed by `V0.5.0`; `pyproject.toml` is the canonical release version.
- Direct contract cut: do not retain compatibility adapters for removed password, pagination, resume, or naive-time contracts.
- V0.4.3 is the only supported in-place source version; do not rewrite `202607150001_initial_schema.py`.
- All behavior changes follow RED -> GREEN -> REFACTOR and include focused regression tests.
- Preserve the conversational product model: no workflow-state UI, content artifact CRUD, memory control center, model-routing platform, or Redis topology project.
- Research packages never cross execution boundaries in V0.5.0.
- Frontend work follows Impeccable `adapt`, `harden`, `quieter`, then `audit`/`polish`; 360/768/1024/1280/1440 are required viewports.
- Do not modify or remove unrelated user-owned untracked files.

---

### Task 1: Version, migration foundation, UTC, tenant lineage, and admin temporary passwords

**Files:**
- Modify: `pyproject.toml`, `apps/api/src/models/{base,user,agent,chat,memory,research}.py`, `apps/api/src/models/schemas/{auth,admin,chat}.py`
- Modify: `apps/api/src/services/{auth_service,admin_service,conversation_service}.py`, `apps/api/src/api/{auth,admin,dependencies}.py`
- Create: two linear revisions under `apps/api/src/contentai_migrations/versions/`
- Test: `apps/api/tests/test_auth_admin.py`, new migration/tenant/time contract tests

**Produces:** aware UTC primitives; `TemporaryPasswordResponse`; forced-password authentication state; composite lineage constraints; execution/message lineage columns.

- [ ] Add failing tests proving public registration cannot consume any admin allowlist, the temporary-password endpoint returns a one-time 24-hour password, old sessions are revoked, disabled/self targets are rejected, and forced-password sessions can access only me/logout/change-password.
- [ ] Add failing migration tests for V0.4.3 backfill, invalid tenant relations, duplicate executions/final messages, RFC3339 `Z`, and preflight abort behavior.
- [ ] Implement aware UTC helpers and schema serialization, then update timestamp columns to `DateTime(timezone=True)`.
- [ ] Implement expand/backfill and validate/contract migrations with `AT TIME ZONE 'UTC'`, composite unique/FK anchors, `AgentExecution.session_id`, `ChatMessage.execution_id`, resume-decision linkage, and the new operational tables.
- [ ] Implement admin temporary-password generation with `secrets.token_urlsafe(18)`, no-store response, session revocation, audit metadata only, forced change and session rotation.
- [ ] Remove forgot/reset/verification token models and endpoints after migration preflight guarantees no live token rows.
- [ ] Run focused auth, migration, tenant and time tests; run Ruff for touched Python code.

### Task 2: Execution-scoped checkpoints, exactly-once final messages, structured resume, and request idempotency

**Files:**
- Modify: `apps/api/src/agent/runtime/*`, `apps/api/src/agent/graph/*`, `apps/api/src/services/{conversation_service,execution_claim,execution_resume,tasks,execution_watchdog}.py`
- Modify: `apps/api/src/api/chat.py`, chat schemas and stores that consume these responses
- Test: execution outbox/resume/checkpoint tests plus new kill-window and idempotency tests

**Produces:** `ExecutionLineage.resolve_for_update`; `{interrupt_id,decision}` resume contract; execution-level final-message uniqueness and checkpoint namespace.

- [ ] Add failing tests for fresh execution namespace, resume in the original namespace, crashes at END/assistant insert/terminal commit, stale/repeated decision requests, waiting-input cancellation, and same-key/different-payload `409`.
- [ ] Centralize invocation/execution creation so caller supplies only locked session + auth; derive user/agent/version/session lineage inside the transaction.
- [ ] Set LangGraph configurable `thread_id=session.langgraph_thread_id` and `checkpoint_ns=execution.id` on all run, inspect, resume, and delete paths.
- [ ] Materialize the final assistant message and terminal state idempotently using the execution partial-unique index; retries read END checkpoint state instead of invoking the model again.
- [ ] Replace free-text approval coercion with approve/reject decisions, persist the decision ChatMessage and ResumeRequest in the same transaction, and terminate rejection with one explicit assistant result.
- [ ] Bind idempotency keys to a canonical SHA-256 of the request and return `IDEMPOTENCY_PAYLOAD_MISMATCH` on mismatch.
- [ ] Base published-task timeout on `published_at` under row locking/fencing.

### Task 3: Operational health, bounded queries, atomic limiting, trusted proxy, durable deletion, and side-effect receipts

**Files:**
- Modify: database/agent settings, readiness/app wiring, dispatcher/watchdog/celery services, admin and conversation repositories
- Modify: `apps/api/src/core/rate_limit.py`, proxy/client-IP handling, `purge_business_data.py`, checkpoint deletion and tool execution services
- Modify: `compose.yaml`, environment templates and operations scripts
- Test: readiness, pagination/capacity, rate limit, proxy matrix, purge, deletion and tool-timeout tests

**Produces:** service heartbeat model; keyset list envelopes; connection-budget preflight; durable checkpoint-deletion and side-effect reconciliation.

- [ ] Add failing tests for stale dispatcher/worker heartbeat, oldest published backlog, bounded list queries, tuple cursors, atomic counter TTL, trusted/untrusted XFF, business-delete crash windows, purge dry-run/idempotence, and side-effect timeout recovery.
- [ ] Upsert dispatcher and required queue heartbeats every 10 seconds; readiness returns 503 after 30 seconds stale/backlogged.
- [ ] Split database pool settings by runtime role and validate declared replicas/concurrency against the 70% connection cap.
- [ ] Replace unbounded/OFFSET responses with `{items,next_cursor}`, default 50/max 200, tuple comparisons and batched aggregates.
- [ ] Implement Lua-based rate limiting and right-to-left XFF parsing only when the immediate peer is in configured trusted CIDRs.
- [ ] Commit business deletion plus checkpoint-deletion outbox atomically; clean checkpoints idempotently after commit.
- [ ] Fix purge table order/name and add dry-run plus bounded batches.
- [ ] Run side-effecting tools in the dedicated queue; write business mutation and `SideEffectReceipt` in one transaction, then reconcile stale executions from receipts without blind replay.

### Task 4: Research lineage, evidence validation, token budgeting, external-content quarantine, and stream batching

**Files:**
- Modify: research workflow/repository, context assembler/window, model gateway, hotspot and research sanitizers, runtime event publisher
- Test: deep research, context budget, citation corruption, hotspot injection and event amplification tests

**Produces:** execution-local research package state; evidence-gated structured claims; provider-aware TokenCounter.

- [ ] Add failing tests proving a later execution cannot load a previous package, unknown/isolated source claims never reach generation, invalid output gets one repair then `CONTENT_EVIDENCE_INVALID`, and oversized current input returns 413.
- [ ] Carry the package ID returned by `prepare_topic_research` in graph state and load only that package after validating execution + topic hash; delete session-latest fallback.
- [ ] Split supported and diagnostic findings; expose only supported claims/sources to generation, with a zero-supported-evidence terminal error.
- [ ] Validate structured claims and source IDs, allow one constrained repair, and never persist an unsupported final answer.
- [ ] Count full model input with provider tokenizer where available and UTF-8 byte upper bound otherwise; reserve configured output tokens and trim only older history.
- [ ] Reuse the research quarantine for hotspot control characters, bidi, HTML and instruction-injection text.
- [ ] Batch stream chunks at 50ms or 256 characters and poll status/cancellation no more than once per second, flushing on every terminal path.

### Task 5: Responsive workbench, structured approval UI, forced-password UI, stable surfaces, and frontend cleanup

**Files:**
- Modify: `apps/web/src/{Root,App}.vue`, router, Pinia stores, API types/client, auth/admin/chat components and workbench styles/tokens
- Test: Vitest contracts plus Playwright responsive/a11y journeys

**Produces:** one responsive workbench; safe interrupt card; temporary-password admin flow; forced-password route guard.

- [ ] Add failing component/store tests for no desktop gate, waiting-input approve/reject/cancel, sanitized interrupt rendering, forced-password routing, temporary-password modal, cursor loading and video unloading.
- [ ] Under Impeccable `adapt`, remove the gate and min-width; implement mobile session drawer, tablet collapsible rail and sticky single-column composer with 44px touch targets.
- [ ] Under `harden`, add keyboard/focus management, retry/error/empty/loading states, long CJK/RTL wrapping, duplicate-submit guards and refresh recovery.
- [ ] Under `quieter`, unload video outside login/empty state, use stable near-opaque work surfaces, raise normal/placeholder contrast to 4.5:1, and remove incorrect tabpanel semantics.
- [ ] Replace free-text resume input with safe memory content/kind and three explicit actions; never render raw interrupt JSON or runtime IDs.
- [ ] Add one-time temporary-password modal and mandatory change-password screen; remove old forgot/reset routes and client methods.
- [ ] Delete verified dead CSS/getters/tokens, move `@vitejs/plugin-vue` to devDependencies, and preserve `playwright-core`.
- [ ] Run Vitest, build, 360/768/1024/1280/1440 Playwright journeys, axe, reduced-motion and no-video-request checks; finish with Impeccable audit/polish.

### Task 6: Release tooling, documentation, dependency remediation, and full verification

**Files:**
- Modify: version metadata, lockfiles, `tools/review.ps1`, packaging/upgrade scripts, Compose/CI, PRODUCT/DESIGN/OPERATIONS/release docs
- Test: clean install, migration rehearsal, deterministic package and full gate commands

**Produces:** reproducible `0.5.0-rc.1` artifact and a documented V0.4.3 in-place upgrade/rollback runbook.

- [ ] Add failing version/artifact tests proving API/Web/package metadata match and untracked working-tree files cannot enter an archive.
- [ ] Make `pyproject.toml` the version source; inject version/commit into Web, API and image metadata and generate a sidecar SHA-256.
- [ ] Upgrade vulnerable Python dependencies to compatible fixed releases and regenerate `uv.lock`/npm lockfiles.
- [ ] Expand the local and CI review gates to cover Alembic, audits, responsive/a11y, fault injection, content evaluation, capacity and reproducibility.
- [ ] Update product/design/operations docs for V0.5.0 contracts, maintenance-mode preflight, drain, backup, atomic deployment and snapshot rollback.
- [ ] Rehearse V0.4.3 snapshot -> V0.5.0 migration and rollback; verify no mixed-version workers.
- [ ] Run all backend/frontend/audit/build/browser/content/capacity gates and two isolated deterministic package builds.
- [ ] Dispatch final whole-branch review, fix all Critical/Important findings, then use finishing-a-development-branch.
