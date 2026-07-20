# Task 3C Report

## RED / GREEN

- RED: `python -m pytest apps/api/tests/test_task3c.py -q` first failed during collection because the required shared `core.client_ip` resolver did not exist.
- GREEN: added the resolver, Lua fixed-window limiter, settings validation, route wiring, and Compose/.env declaration; focused suite now passes 11/11, including real Redis execution and HTTP/auth integration.

## Changed files

- `apps/api/src/core/rate_limit.py`: one atomic Lua `EVAL` increments, assigns first TTL, and repairs TTL-less counters.
- `apps/api/src/core/client_ip.py`: shared trusted-peer/XFF resolver using `ipaddress`.
- `apps/api/src/core/config/server.py`, `apps/api/src/core/config/settings.py`: explicit validated `trusted_proxy_cidrs`, empty by default.
- `apps/api/src/api/app.py`, `apps/api/src/api/auth.py`: shared resolver for anonymous rate-limit identity and auth session IPs; test bypass remains at middleware boundary only.
- `compose.yaml`, `.env.example`: explicit CIDR environment wiring with no broad default.
- `apps/api/tests/test_task3c.py`: RED/GREEN coverage for Lua invocation, real Redis concurrent first windows, TTL-less repair, positive TTL preservation, failure policy, proxy chains, malformed headers, CIDR validation, and HTTP/auth IP integration.

## Verification

- `python -m pytest apps/api/tests/test_task3c.py -q`: 11 passed (real Redis DB 15 and HTTP/service integration).
- `python -m pytest apps/api/tests/test_auth_admin.py -q`: 17 passed.
- `python -m ruff check ...`: passed.
- `python -m compileall -q apps/api/src`: passed.
- `python -m pytest apps/api/tests -q` (300s limit): no output; manually terminated at the maintainer's request before natural completion, so no exit code was produced and this is not reported as passing. The known prior no-output timeout convention is exit 124.

## Self-review / known limits

- No Uvicorn blanket forwarded-header trust, Redis topology changes, deletion/purge, side effects, or pagination were added.
- Resolver fails closed to the immediate peer for malformed/empty XFF and ignores XFF from untrusted peers.
- Full backend suite did not complete; no success is claimed. Existing `DummyAgentService.settings` regression remains outside Task 3C scope.
