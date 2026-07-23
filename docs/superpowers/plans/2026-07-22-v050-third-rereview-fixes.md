# ContentAI V0.5.0 Third Rereview Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close all remaining third-round V0.5.0 rereview findings with retryable model-resource shutdown, PowerShell-portable release manifests, and complete terminal-cancellation regression coverage.

**Architecture:** Keep the existing owner-loop HTTP client and runtime cache design. Make each layer report real close completion, retain retired owners until shutdown can retry them, and await the service chain from FastAPI lifespan; independently remove shell-dependent JSON escaping from the deterministic Git archive path and expand the cancellation test matrix.

**Tech Stack:** Python 3.13, asyncio, concurrent.futures, FastAPI lifespan, httpx, pytest, PowerShell 5/7, Git archive.

## Global Constraints

- Preserve the current model invocation, cache-key, cache-capacity, eviction-order, archive-layout, checksum, and cancellation production contracts.
- Do not replace `_RetainedRuntimeValue` or redesign all runtime leases.
- Finalizers may advance reference and close state but must not be the only shutdown mechanism and must not emit unraisable close errors.
- FastAPI shutdown must await agent model-resource closure before database shutdown.
- `release-manifest.json` must be valid JSON and archive bytes must remain deterministic on every available PowerShell host.
- Do not modify cancellation production logic for TRR-MIN-01; prove both boolean consumers through tests.
- Do not create commits, push, or open a PR without explicit user authorization.
- Do not run the unbounded full backend suite; record only fresh commands that reach a terminal result.

---

## File Map

- `apps/api/src/agent/infrastructure/llm/client.py`: canonical owner-loop close Future and retry transition.
- `apps/api/src/agent/infrastructure/llm/gateway.py`: synchronous/async gateway close completion boundary.
- `apps/api/src/agent/runtime/container.py`: owner state machine, retired-owner registry, synchronous and async container shutdown.
- `apps/api/src/services/agent_service.py`: service-level async shutdown bridge.
- `apps/api/src/api/app.py`: awaited lifespan shutdown hooks.
- `apps/api/tests/test_llm_client.py`: immediate-completion and retry race regressions.
- `apps/api/tests/test_runtime_container.py`: flaky owner, eviction, retry, and async shutdown coverage.
- `apps/api/tests/test_main.py`: lifespan awaits asynchronous agent shutdown.
- `tools/package-ubuntu.ps1`: temporary UTF-8-without-BOM manifest and `--add-file` archive injection.
- `apps/api/tests/test_release_contract.py`: every-host deterministic builds and manifest-file contract.
- `apps/api/tests/test_api.py`: active-user and disabled-user terminal takeover matrix.
- `.superpowers/sdd/v050-third-rereview-fix-report.md`: local verification evidence and finding disposition.

---

### Task 1: Finalize Canonical Client Close Transition

**Files:**
- Modify: `apps/api/src/agent/infrastructure/llm/client.py:59-186`
- Test: `apps/api/tests/test_llm_client.py:218-323`

**Interfaces:**
- Consumes: `_OwnerLoopAsyncClient._submit_close() -> Future[None]`.
- Produces: `_finish_close(future: Future[None], *, generation: int) -> None`; one canonical Future remains installed until its callback commits success or retry state.

- [ ] **Step 1: Preserve the recovered race tests**

Keep the worktree tests named `test_concurrent_immediate_close_keeps_the_first_future_canonical_until_callback_transition` and `test_failed_canonical_close_allows_one_shared_retry_after_callback_transition` unchanged.

They must assert one submission before callback transition, shared Future identity, one retry after failure, and eventual owner-loop stop after success.

- [ ] **Step 2: Verify the recovered GREEN state**

Run:

```powershell
.venv\Scripts\python.exe -m pytest apps/api/tests/test_llm_client.py -q
```

Expected: `20 passed`; no hanging owner thread and no unhandled Future exception.

- [ ] **Step 3: Keep the minimal canonical transition implementation**

The implementation must retain this state ordering:

```python
def _submit_close(self) -> Future[None]:
    with self._close_state_lock:
        if self._async_closed:
            completed: Future[None] = Future()
            completed.set_result(None)
            return completed
        if self._close_future is not None:
            return self._close_future
        future = asyncio.run_coroutine_threadsafe(
            self._close_on_owner_loop(),
            self._require_owner_loop(),
        )
        self._close_generation += 1
        generation = self._close_generation
        self._close_future = future
    future.add_done_callback(
        lambda completed: self._finish_close(completed, generation=generation)
    )
    return future

def _finish_close(self, future: Future[None], *, generation: int) -> None:
    succeeded = not future.cancelled() and future.exception() is None
    should_stop = False
    with self._close_state_lock:
        if future is not self._close_future or generation != self._close_generation:
            return
        if succeeded:
            self._async_closed = True
            should_stop = True
        else:
            self._close_future = None
    if should_stop:
        owner_loop = self._require_owner_loop()
        owner_loop.call_soon_threadsafe(owner_loop.stop)
```

- [ ] **Step 4: Run task-level static checks**

Run:

```powershell
.venv\Scripts\python.exe -m ruff check apps/api/src/agent/infrastructure/llm/client.py apps/api/tests/test_llm_client.py
git diff --check -- apps/api/src/agent/infrastructure/llm/client.py apps/api/tests/test_llm_client.py
```

Expected: both commands exit `0`.

---

### Task 2: Make Retired Gateway Closure Retryable

**Files:**
- Modify: `apps/api/src/agent/runtime/container.py:44-116,128-150,225-257,398-407`
- Modify: `apps/api/src/services/agent_service.py:8-26`
- Modify: `apps/api/src/api/app.py:77-108`
- Test: `apps/api/tests/test_runtime_container.py:1-52,241-404`
- Test: `apps/api/tests/test_main.py:106-151`

**Interfaces:**
- Consumes: `ModelGateway.close() -> bool`, `ModelGateway.aclose() -> None`.
- Produces: `_GatewayOwner.close() -> bool`, `_GatewayOwner.aclose() -> None`, `_GatewayOwner.closed -> bool`, `RuntimeContainer.aclose() -> None`, `AgentService.aclose() -> None`, `async _shutdown(app: FastAPI) -> None`.

- [ ] **Step 1: Write the flaky owner and async shutdown tests**

Add `asyncio` and `pytest` imports to `test_runtime_container.py`, then add a gateway that fails once and succeeds asynchronously:

```python
class _FlakyClosableGateway(_ClosableGateway):
    def __init__(self, *, model_config_id: str, **kwargs: Any) -> None:
        super().__init__(model_config_id=model_config_id, **kwargs)
        self.close_attempts = 0

    def close(self) -> bool:
        self.close_attempts += 1
        if self.close_attempts == 1:
            raise RuntimeError("first close failed")
        return True

    async def aclose(self) -> None:
        self.close_attempts += 1
```

Add an eviction test that retains the first gateway wrapper through eviction, releases it, and then awaits container shutdown:

```python
def test_async_container_shutdown_retries_failed_evicted_gateway_close(monkeypatch) -> None:
    settings = get_settings().model_copy(deep=True)
    settings.agent.runtime_cache_capacity = 1
    created: dict[str, _FlakyClosableGateway] = {}

    def build_gateway(**kwargs: Any) -> _FlakyClosableGateway:
        gateway = _FlakyClosableGateway(**kwargs)
        created[gateway.model_config_id] = gateway
        return gateway

    monkeypatch.setattr(
        "agent.runtime.container.ModelConfigurationService.get_runtime_by_id",
        lambda _service, _session, model_config_id: SimpleNamespace(
            id=model_config_id,
            base_url="https://models.example.test/v1",
            api_key=SecretStr("test-secret"),
            model_name="test-model",
        ),
    )
    monkeypatch.setattr("agent.runtime.container.ModelGateway", build_gateway)
    container = RuntimeContainer(settings=settings, checkpointer=object())

    first = container.gateway_for_model_config("model-config-v1")
    container.gateway_for_model_config("model-config-v2")
    del first
    gc.collect()

    assert created["model-config-v1"].close_attempts == 1
    asyncio.run(container.aclose())
    assert created["model-config-v1"].close_attempts == 2
```

Add a lifespan test in `test_main.py` whose dummy agent service sets an event only after an awaited suspension:

```python
def test_lifespan_awaits_agent_service_async_close(monkeypatch):
    close_order: list[str] = []

    class DummyAgentService:
        def __init__(self, settings, runtime=None):
            self.settings = settings
            self.runtime = runtime or object()
            self.runner = object()

        def start(self):
            return None

        async def aclose(self):
            await asyncio.sleep(0)
            close_order.append("agent")

    monkeypatch.setattr("api.app.AgentService", DummyAgentService)
    monkeypatch.setattr("api.app.init_database", lambda _settings: None)
    monkeypatch.setattr("api.app.close_database", lambda: close_order.append("database"))

    with TestClient(create_app()):
        pass

    assert close_order == ["agent", "database"]
```

- [ ] **Step 2: Run the new tests to verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest apps/api/tests/test_runtime_container.py::test_async_container_shutdown_retries_failed_evicted_gateway_close apps/api/tests/test_main.py::test_lifespan_awaits_agent_service_async_close -q
```

Expected: FAIL because `RuntimeContainer.aclose()` and `AgentService.aclose()` do not exist and lifespan does not await asynchronous shutdown.

- [ ] **Step 3: Implement the owner state transition**

Replace `_closed` with explicit state and recorded failure. The complete public transition surface must be:

```python
class _GatewayOwner:
    def __init__(self, gateway: ModelGateway, *, close_when_retired: bool) -> None:
        self.gateway = gateway
        self.close_when_retired = close_when_retired
        self._lock = threading.Lock()
        self._references = 0
        self._retired = False
        self._state = "active"
        self._close_error: Exception | None = None

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._state == "closed"

    def close(self) -> bool:
        with self._lock:
            self._retired = True
            if not self.close_when_retired:
                return True
            if not self._can_close_locked():
                return self._state == "closed"
            self._state = "closing"
            self._close_error = None
        try:
            completed = self.gateway.close()
        except Exception as exc:
            self._finish_close(error=exc)
            raise
        if completed is not False:
            self._finish_close(error=None)
            return True
        return False

    async def aclose(self) -> None:
        with self._lock:
            self._retired = True
            if not self.close_when_retired:
                return
            if self._state == "closed":
                return
            if self._references != 0:
                raise RuntimeError("Cannot close a gateway owner with retained runtime values.")
            self._state = "closing"
            self._close_error = None
        try:
            await self.gateway.aclose()
        except Exception as exc:
            self._finish_close(error=exc)
            raise
        self._finish_close(error=None)

    def _finish_close(self, *, error: Exception | None) -> None:
        with self._lock:
            self._close_error = error
            self._state = "closed" if error is None else "active"

    def _can_close_locked(self) -> bool:
        return (
            self.close_when_retired
            and self._retired
            and self._state != "closed"
            and self._references == 0
        )
```

`release()` must decrement references under the lock, call `close()` outside the lock when eligible, and catch/store exceptions so a weakref finalizer never raises. `retire()` must set `_retired`, start synchronous closure when references are zero, and return whether closure completed. A fake or legacy gateway returning `None` from `close()` counts as completed; only the literal value `False` means asynchronous closure remains pending. Owners with `close_when_retired=False` are immediately settled without closing the injected gateway.

- [ ] **Step 4: Retain failed or pending retired owners in the container**

Add the registry and route every eviction through one helper:

```python
_retired_owners: set[_GatewayOwner] = field(default_factory=set, init=False)

def _retire_entry(self, entry: _RuntimeCacheEntry) -> None:
    owner = entry.owner
    entry.retire()
    if not owner.closed:
        self._retired_owners.add(owner)

async def aclose(self) -> None:
    with self._cache_lock:
        entries = tuple(self._runtime_entries.values())
        self._runtime_entries.clear()
        for entry in entries:
            self._retire_entry(entry)
        owners = tuple(self._retired_owners)

    errors: list[Exception] = []
    for owner in owners:
        try:
            await owner.aclose()
        except Exception as exc:
            errors.append(exc)
        else:
            with self._cache_lock:
                self._retired_owners.discard(owner)

    self._close_owned_persistence()
    if errors:
        raise errors[0]
```

Extract the existing checkpointer shutdown into idempotent `_close_owned_persistence()`. Keep `close()` synchronous and idempotent; it retires entries, calls `owner.close()` for retryable owners, closes owned persistence, and raises only from this explicit call path.

- [ ] **Step 5: Add service and lifespan async bridges**

Implement:

```python
class AgentService:
    async def aclose(self) -> None:
        self.runner.close()
        runtime_aclose = getattr(self.runtime, "aclose", None)
        if callable(runtime_aclose):
            await runtime_aclose()
            return
        self.runtime.close()
```

In `api/app.py`, import `inspect` and make shutdown await any awaitable hook while continuing after individual failures:

```python
async def _shutdown(app: FastAPI) -> None:
    app.state.ready = False
    agent_service = getattr(app.state, "agent_service", None)
    agent_closer = getattr(agent_service, "aclose", None) or getattr(
        agent_service, "close", None
    )
    for closer in (
        getattr(getattr(app.state, "conversation_service", None), "close", None),
        agent_closer,
        close_database,
    ):
        if callable(closer):
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Application shutdown hook failed: %s", closer)
```

Change lifespan finalization to `await _shutdown(app)`. In `test_lifespan_uses_app_settings_for_database_and_agent_service`, replace the dummy service's `close()` method with an `async def aclose()` method returning `None` so the existing fixture exercises the production async path.

- [ ] **Step 6: Run owner/lifespan GREEN and adjacent regressions**

Run:

```powershell
.venv\Scripts\python.exe -m pytest apps/api/tests/test_runtime_container.py apps/api/tests/test_main.py apps/api/tests/test_llm_client.py -q
.venv\Scripts\python.exe -m ruff check apps/api/src/agent/runtime/container.py apps/api/src/services/agent_service.py apps/api/src/api/app.py apps/api/tests/test_runtime_container.py apps/api/tests/test_main.py
.venv\Scripts\python.exe -m compileall -q apps/api/src
```

Expected: all focused tests pass and static commands exit `0` with no warnings.

---

### Task 3: Make Manifest File Injection PowerShell-Portable

**Files:**
- Modify: `tools/package-ubuntu.ps1:52-74`
- Test: `apps/api/tests/test_release_contract.py:25-260`

**Interfaces:**
- Consumes: compressed JSON from `ConvertTo-Json -Compress`, serialized with `[IO.File]::WriteAllText` and `Text.UTF8Encoding($false)`.
- Produces: `<release-prefix>/release-manifest.json` through `git archive --add-file=<temporary-manifest-path>` without passing JSON through native argv.

- [ ] **Step 1: Expand the release test across every available host**

Replace the first-match host lookup with:

```python
def _powershell_hosts() -> list[str]:
    return [
        executable
        for name in ("powershell", "pwsh")
        if (executable := shutil.which(name)) is not None
    ]
```

Parameterize the deterministic build test with `@pytest.mark.parametrize("powershell", _powershell_hosts())`; pytest automatically skips an empty parameter set. Keep two isolated clones per host and name each clone `release-build-{Path(powershell).stem}-{index}`.

- [ ] **Step 2: Add the manifest-file RED contract**

Replace the standard-argv virtual-file probe with a static contract that forbids both known shell-dependent forms and requires the portable file transport:

```python
def test_release_manifest_uses_add_file_without_shell_escaping() -> None:
    package_script = (ROOT / "tools/package-ubuntu.ps1").read_text(encoding="utf-8")

    assert "--add-file=" in package_script
    assert "--add-virtual-file" not in package_script
    assert "$escapedManifest" not in package_script
    assert "[IO.File]::WriteAllText" in package_script
    assert "Text.UTF8Encoding($false)" in package_script
```

In the existing deterministic archive test, read the manifest bytes once and assert `not manifest_bytes.startswith(b"\xef\xbb\xbf")` before parsing them as JSON. This preserves the dynamic commit assertion while proving the temporary source file was written without a BOM.

- [ ] **Step 3: Run the focused release RED**

Run:

```powershell
& "D:\Fan'sWork\code_project\ContentAI\.venv\Scripts\python.exe" -m pytest --noconftest --basetemp .pytest_cache\task-3-red-basetemp apps/api/tests/test_release_contract.py::test_release_manifest_uses_add_file_without_shell_escaping -q
```

Expected: FAIL because the script still uses `--add-virtual-file` and does not yet contain the temporary-file encoding calls.

- [ ] **Step 4: Inject a temporary manifest file**

Replace the manifest block with:

```powershell
$manifest = [ordered]@{ version = $Version; commit = $commit } | ConvertTo-Json -Compress
$manifestDirectory = Join-Path ([IO.Path]::GetTempPath()) ("contentai-package-" + [Guid]::NewGuid().ToString("N"))
$manifestFile = Join-Path $manifestDirectory "release-manifest.json"

try {
  New-Item -ItemType Directory -Path $manifestDirectory -Force | Out-Null
  $encoding = New-Object Text.UTF8Encoding($false)
  [IO.File]::WriteAllText($manifestFile, $manifest, $encoding)
  $archiveArguments = @(
    "archive",
    "--format=tar.gz",
    "--prefix=$name/",
    "--output=$archive",
    "--add-file=$manifestFile",
    $commit,
    "--"
  ) + $requiredPaths
  & git -C $root @archiveArguments
  if ($LASTEXITCODE -ne 0) { throw "Unable to create the release archive." }
}
finally {
  if (Test-Path -LiteralPath $manifestFile) { Remove-Item -LiteralPath $manifestFile -Force }
  if (Test-Path -LiteralPath $manifestDirectory) { Remove-Item -LiteralPath $manifestDirectory -Force }
}
```

Keep checksum generation after the `finally` block. Do not introduce host-version branches, quote escaping, recursive cleanup, or `--add-virtual-file`.

- [ ] **Step 5: Run real-host deterministic GREEN**

Run:

```powershell
& "D:\Fan'sWork\code_project\ContentAI\.venv\Scripts\python.exe" -m pytest --noconftest --basetemp .pytest_cache\task-3-green-basetemp apps/api/tests/test_release_contract.py -q
powershell -NoProfile -Command '$errors=$null; [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path ''tools/package-ubuntu.ps1''), [ref]$null, [ref]$errors) > $null; if ($errors.Count) { $errors | Out-String | Write-Error; exit 1 }'
```

If `pwsh` exists, also run the same parser command with `pwsh`. Expected: all release tests pass, every available shell parser exits `0`, both builds per host have identical archive bytes and checksum text, and extracted manifest bytes have no BOM and parse as valid JSON.

---

### Task 4: Cover Both Terminal Cancellation Consumers

**Files:**
- Modify: `apps/api/tests/test_api.py:3305-3403`

**Interfaces:**
- Consumes: `settle_execution_cancellation(session: Session, execution: AgentExecution, *, now: datetime, error: str | None = None) -> bool` at both waiting-input branches in `AgentRunner`.
- Produces: a four-case matrix for `completed`/`failed` crossed with active/disabled user status.

- [ ] **Step 1: Expand the existing terminal takeover parameterization**

Add `disable_user` to the parameter matrix and conditionally mutate the user with this exact diff:

```diff
+@pytest.mark.parametrize("disable_user", [False, True], ids=["active-user", "disabled-user"])
 @pytest.mark.parametrize(
     ("terminal_status", "terminal_error", "attempt_status"),
     [
         (RunStatus.completed, "", ExecutionAttemptStatus.completed),
         (RunStatus.failed, "competing terminal failure", ExecutionAttemptStatus.failed),
     ],
 )
 def test_waiting_input_stale_worker_does_not_project_cancellation_over_terminal_state(
     terminal_status: RunStatus,
     terminal_error: str,
     attempt_status: ExecutionAttemptStatus,
+    disable_user: bool,
     monkeypatch: pytest.MonkeyPatch,
 ) -> None:
@@
             execution.finished_at = now
+            execution.cancel_requested_at = now
             competing_session.add(execution)
@@
-            user = competing_session.get(AppUser, claimed.auth.user_id)
-            assert user is not None
-            user.status = "disabled"
-            competing_session.add(user)
+            if disable_user:
+                user = competing_session.get(AppUser, claimed.auth.user_id)
+                assert user is not None
+                user.status = "disabled"
+                competing_session.add(user)
```

The stale `cancel_requested_at` assignment is required in all four cases: it drives active users through the second cancellation consumer while disabled users still exercise the first. Keep the final assertions that terminal execution/attempt state is unchanged and neither `run_cancel` nor `attempt_end(status=cancelled)` is emitted.

- [ ] **Step 2: Run the new matrix**

Run:

```powershell
& "D:\Fan'sWork\code_project\ContentAI\.venv\Scripts\python.exe" -m pytest --basetemp .pytest_cache\task-4-basetemp apps/api/tests/test_api.py::test_waiting_input_stale_worker_does_not_project_cancellation_over_terminal_state -q
```

Expected: four cases pass against the current production logic.

- [ ] **Step 3: Prove the active-user cases are mutation-sensitive**

Temporarily mutate only the active-user cancellation branch from:

```python
if not settle_execution_cancellation(
    db_session,
    execution,
    now=utcnow(),
):
    db_session.rollback()
    return
```

to an unchecked call:

```python
settle_execution_cancellation(
    db_session,
    execution,
    now=utcnow(),
)
```

Keep the existing commit and post-commit event projection after that call, run the command from Step 2, and confirm the two `active-user` cases fail on the SSE assertions. Immediately restore the production guard with `apply_patch` and rerun Step 2.

Expected: mutation run fails exactly two active-user cases; restored run passes all four. No production diff remains from this step.

---

### Task 5: Verify and Reconcile the Third Rereview

**Files:**
- Create: `.superpowers/sdd/v050-third-rereview-fix-report.md`

**Interfaces:**
- Consumes: Tasks 1-4 and findings TRR-IMP-01, TRR-IMP-02, TRR-IMP-03, TRR-MIN-01.
- Produces: exact command evidence and one disposition per finding.

- [ ] **Step 1: Run the integrated focused tests**

Run:

```powershell
& "D:\Fan'sWork\code_project\ContentAI\.venv\Scripts\python.exe" -m pytest --basetemp .pytest_cache\task-5-integrated-basetemp apps/api/tests/test_llm_client.py apps/api/tests/test_runtime_container.py apps/api/tests/test_main.py apps/api/tests/test_release_contract.py -q
& "D:\Fan'sWork\code_project\ContentAI\.venv\Scripts\python.exe" -m pytest --basetemp .pytest_cache\task-5-cancellation-basetemp apps/api/tests/test_api.py::test_waiting_input_stale_worker_does_not_project_cancellation_over_terminal_state -q
```

Expected: all selected tests pass with terminal summaries.

- [ ] **Step 2: Run static verification**

Run:

```powershell
& "D:\Fan'sWork\code_project\ContentAI\.venv\Scripts\python.exe" -m ruff check apps/api/src/agent/infrastructure/llm/client.py apps/api/src/agent/infrastructure/llm/gateway.py apps/api/src/agent/runtime/container.py apps/api/src/services/agent_service.py apps/api/src/api/app.py apps/api/tests/test_llm_client.py apps/api/tests/test_runtime_container.py apps/api/tests/test_main.py apps/api/tests/test_release_contract.py apps/api/tests/test_api.py
& "D:\Fan'sWork\code_project\ContentAI\.venv\Scripts\python.exe" -m compileall -q apps/api/src
powershell -NoProfile -Command '$errors=$null; [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path ''tools/package-ubuntu.ps1''), [ref]$null, [ref]$errors) > $null; if ($errors.Count) { $errors | Out-String | Write-Error; exit 1 }'
git diff --check
```

If `pwsh` exists, run the same parser command with `pwsh`. Expected: all available-host parser commands and every other command exit `0`.

- [ ] **Step 3: Review the final diff and protected worktree state**

Run:

```powershell
git status --short
git diff --stat
git diff -- apps/api/src/agent/infrastructure/llm/client.py apps/api/src/agent/infrastructure/llm/gateway.py apps/api/src/agent/runtime/container.py apps/api/src/services/agent_service.py apps/api/src/api/app.py apps/api/tests/test_llm_client.py apps/api/tests/test_runtime_container.py apps/api/tests/test_main.py apps/api/tests/test_release_contract.py apps/api/tests/test_api.py tools/package-ubuntu.ps1
```

Expected: only intended files plus pre-existing user-owned untracked paths are present; no protected file is staged or deleted.

- [ ] **Step 4: Record evidence and finding disposition**

Write `.superpowers/sdd/v050-third-rereview-fix-report.md` with the title `ContentAI V0.5.0 Third Rereview Fix Report` and three sections. `Findings` must contain one bullet for each finding with its observed root cause, changed interface, and fresh test evidence. `Verification` must contain every exact command, exit code, and terminal pass count or static result. `Constraints` must state that the full backend suite was intentionally not run and that no commit, push, or PR was created without user authorization. Do not copy expected values from this plan as evidence.
