# Task 3D Report - Durable deletion and bounded purge

## Status

Implemented the durable session-deletion boundary, checkpoint deletion outbox drain,
and bounded business/checkpoint purge for V0.5.0 without adding a migration.

## RED / GREEN evidence

- RED: `pytest -q tests/test_task3d_deletion.py` failed at collection because
  `services.checkpoint_deletion` did not exist.
- GREEN: the same focused file passed after adding the drain service.
- RED: the existing API cleanup-failure test failed because it patched the removed
  thread-wide cleanup and asserted rollback/HTTP 500.
- GREEN: the updated API regression now creates a terminal execution, simulates an
  execution-scoped cleanup failure, observes HTTP 204 plus a pending outbox row, and
  completes it through a later drain.
- RED: a focused failed-row reclaim test returned zero completed rows.
- GREEN: the drain now reclaims available `failed` rows while respecting `batch_size`.

## Changed files

- `apps/api/src/services/conversation_service.py`
- `apps/api/src/services/checkpoint_deletion.py`
- `apps/api/src/services/purge_business_data.py`
- `apps/api/src/agent/runtime/checkpoint.py`
- `apps/api/tests/test_task3d_deletion.py`
- `apps/api/tests/test_api.py`

## Behavior

- Session deletion writes one deletion intent per execution namespace in the same
  business transaction, preserving the active-execution guard and audit log.
- Business deletion commits before execution-scoped checkpoint cleanup. Failures
  remain pending and retryable; missing namespaces count as success.
- The drain uses bounded `FOR UPDATE SKIP LOCKED` claims, lock expiry, attempt counts,
  exponential backoff, deterministic claim order, and completed-row idempotence.
- Purge uses a fixed actual-table allowlist in FK-safe order, bounded batches,
  `--dry-run`, `--retention-cutoff`, and the existing exact confirmation token for
  destructive execution.
- Checkpoint maintenance runs only after the business transaction commits and is
  included in the purge summary.

## Verification

- `pytest -q tests/test_task3d_deletion.py tests/test_api.py -k 'task3d or delete_session or session_delete or persistence_cleanup' --disable-warnings`
  - 6 passed, 58 deselected.
- `pytest -q tests/test_api.py::test_chat_session_delete_commits_business_rows_when_persistence_cleanup_fails`
  - 1 passed.
- `ruff check` on Task 3D touched files passes.
- `python -m compileall -q apps/api/src apps/api/tests` exits 0.

## Self-review and known limits

- No third V0.5 migration was added; the existing unpublished outbox schema is used.
- Request deletion never calls thread-wide checkpoint deletion.
- The retention cutoff applies only to timestamped business tables. LangGraph
  checkpoint tables do not expose a business timestamp, so checkpoint maintenance is
  bounded but not retention-filtered.
- Repository-wide Ruff still reports the pre-existing `UP038` finding in
  `apps/api/src/models/schemas/base.py`; Task 3D touched-file Ruff is clean.
- Full backend and Alembic roundtrip are left to the parent integration pass.
