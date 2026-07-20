# Task 3A Recovery Report

## Scope and implementation audit

Audited `5d51e10` against `.superpowers/sdd/task-3a-brief.md`. The prior commit
implemented the role pool profiles, budget calculation, heartbeat upsert,
queue probes, readiness fields, and Compose runtime roles without a third
Alembic revision.

This recovery found and corrected four contract gaps:

- Dispatcher default identity was `hostname-object_id`, not stable `hostname:pid`.
- Test-environment readiness bypassed dispatcher and worker heartbeats.
- API startup eagerly opened the checkpoint pool through `AgentService.start()`.
- Checkpoint pool capacity was hard-coded in role profiles rather than configured
  independently and declared with the deployment capacity inputs.

The corrected implementation adds `checkpoint_pool_size` to database settings,
counts it only for prefork agent-worker processes, opens it eagerly only for the
agent-worker role, uses the shared `service_instance_id()`, and exposes replica,
checkpoint, and worker-concurrency declarations in Compose and `.env.example`.

## TDD evidence

Historical RED evidence for `5d51e10` cannot be recovered from the interrupted
session and is not asserted here. Recovery RED was observed before production
edits with:

```text
.venv\\Scripts\\python.exe -m pytest apps/api/tests/test_task3_operational_readiness.py -q
4 failed, 7 passed
```

The failures demonstrated ignored configured checkpoint capacity (`32 != 36`),
API eager checkpoint initialization, test-environment readiness incorrectly
returning ready with no service heartbeats, and Dispatcher identity differing
from `hostname:pid`. After the minimal fixes:

```text
.venv\\Scripts\\python.exe -m pytest apps/api/tests/test_task3_operational_readiness.py -q
14 passed
```

The Compose declaration test was also added first and failed because the
replica/checkpoint settings were absent from `.env.example`; it passed after the
Compose and environment declaration changes.

## Verification

| Command | Result |
| --- | --- |
| `.venv\\Scripts\\python.exe -m pytest apps/api/tests/test_task3_operational_readiness.py apps/api/tests/test_main.py -q` | `29 passed` |
| `.venv\\Scripts\\python.exe -m pytest apps/api/tests/test_task3_operational_readiness.py -q` | `14 passed` |
| `.venv\\Scripts\\ruff.exe check apps/api/src apps/api/tests` | passed |
| `.venv\\Scripts\\python.exe -m compileall -q apps/api/src` | passed |
| `CONTENTAI_ENV=test ... .venv\\Scripts\\python.exe -m alembic check` | `No new upgrade operations detected.` |
| `.venv\\Scripts\\python.exe -m pytest -q` | not completed: first attempt hit the 120-second command limit; a second attempt was stopped after about four minutes with no output or interactive prompt. |

## Changed files

- `.env.example`
- `compose.yaml`
- `apps/api/src/core/config/database.py`
- `apps/api/src/core/config/server.py`
- `apps/api/src/agent/runtime/checkpoint.py`
- `apps/api/src/services/agent_service.py`
- `apps/api/src/services/dispatcher.py`
- `apps/api/src/services/readiness.py`
- `apps/api/tests/test_task3_operational_readiness.py`
- `apps/api/tests/test_main.py`

## Self-review and known limits

- `git diff --check` reported no whitespace errors.
- No Alembic revision was added.
- Redis remains intentionally short-circuited in test settings, but queue
  readiness now still depends on persisted service heartbeats.
- Full backend pytest has not been observed to completion in this recovery due
  to the documented no-output timeout; this is the sole handoff concern.

## Reviewer remediation

The independent review of `d26082d` identified two readiness contract defects
and missing integration coverage. This follow-up corrects all Critical and
Important findings without expanding into the deferred migration replica Minor:

- `ServerSettings.outbox_max_age_seconds` now defaults to 30 seconds, and the
  same value is explicitly declared in `.env.example` and Compose.
- Readiness no longer fails solely because pending outbox volume exceeds
  `outbox_readiness_threshold`; it fails only when the oldest genuinely
  unclaimed published execution exceeds the configured 30-second age.
- Real PostgreSQL readiness tests now prove claimed/completed historical
  published rows do not fail readiness, old unclaimed published rows do,
  every required checkpoint table is checked independently, and restoring all
  service heartbeats restores readiness.

### Follow-up TDD evidence

Before changing production code, the new focused suite produced the expected
RED result:

```text
.venv\\Scripts\\python.exe -m pytest apps/api/tests/test_task3_operational_readiness.py -q
3 failed, 17 passed
```

The failures were exactly the reviewer findings: `120 != 30`, a 31-second
unclaimed published row still returned ready, and pending count still blocked
readiness. After the minimal changes:

```text
.venv\\Scripts\\python.exe -m pytest apps/api/tests/test_task3_operational_readiness.py -q
20 passed

.venv\\Scripts\\python.exe -m pytest apps/api/tests/test_task3_operational_readiness.py apps/api/tests/test_main.py -q
35 passed
```

Ruff, `compileall`, and Alembic check all passed again. A renewed complete
`.venv\\Scripts\\python.exe -m pytest -q` attempt timed out after 360 seconds
without output, so the existing full-suite verification limitation remains.

## Timestamp precision remediation

Re-review found that readiness truncated the published outbox age with
`int(total_seconds())` before comparing it to the 30-second threshold. That
made an item aged 30 seconds plus one microsecond appear healthy. The new real
database regression freezes only the readiness clock, verifies that exactly 30
seconds remains healthy, then updates the same unclaimed published row to
30 seconds plus one microsecond and requires readiness to fail while the
display field remains integer `30`.

```text
.venv\\Scripts\\python.exe -m pytest apps/api/tests/test_task3_operational_readiness.py::test_readiness_outbox_age_boundary_preserves_microseconds -q
RED: 1 failed (30s + 1us incorrectly returned ready)
GREEN: 1 passed

.venv\\Scripts\\python.exe -m pytest apps/api/tests/test_task3_operational_readiness.py apps/api/tests/test_main.py -q
36 passed
```

Readiness now retains the `timedelta` for the strict inclusive comparison and
only truncates the value used in the response's display field. Ruff,
`compileall`, and Alembic check passed after this change.
