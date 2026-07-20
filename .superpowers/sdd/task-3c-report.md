# Task 3C Report

## RED / GREEN

- RED: `python -m pytest apps/api/tests/test_task3c.py -q` first failed during collection because the required shared `core.client_ip` resolver did not exist.
- GREEN: added the resolver, Lua fixed-window limiter, settings validation, route wiring, and Compose/.env declaration; focused suite now passes 8/8.

## Changed files

- `apps/api/src/core/rate_limit.py`: one atomic Lua `EVAL` increments, assigns first TTL, and repairs TTL-less counters.
- `apps/api/src/core/client_ip.py`: shared trusted-peer/XFF resolver using `ipaddress`.
- `apps/api/src/core/config/server.py`, `apps/api/src/core/config/settings.py`: explicit validated `trusted_proxy_cidrs`, empty by default.
- `apps/api/src/api/app.py`, `apps/api/src/api/auth.py`: shared resolver for anonymous rate-limit identity and auth session IPs; test bypass remains at middleware boundary only.
- `compose.yaml`, `.env.example`: explicit CIDR environment wiring with no broad default.
- `apps/api/tests/test_task3c.py`: RED/GREEN coverage for Lua invocation, failure policy, proxy chains, malformed headers, and CIDR validation.

## Verification

- `python -m pytest apps/api/tests/test_task3c.py -q`: 8 passed.
- `python -m ruff check ...`: passed.
- `python -m compileall -q apps/api/src`: passed.
- `python -m pytest apps/api/tests/test_main.py apps/api/tests/test_auth_admin.py -q`: 31 passed, 1 existing failure in `test_lifespan_uses_app_settings_for_database_and_agent_service` because its `DummyAgentService` lacks the pre-existing `settings` attribute.

## Self-review / known limits

- No Uvicorn blanket forwarded-header trust, Redis topology changes, deletion/purge, side effects, or pagination were added.
- Resolver fails closed to the immediate peer for malformed/empty XFF and ignores XFF from untrusted peers.
- Full backend suite was not rerun after focused verification; the affected regression command above completed with the unrelated baseline failure.
