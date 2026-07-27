# Model Protocol Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the currently configured partially OpenAI-compatible endpoint usable while preventing textual tool protocol leakage, providing validated structured-output fallback, recording hotspot scoring failures accurately, and warning administrators about compatibility mode.

**Architecture:** Add a small protocol-compatibility module in the LLM infrastructure layer. Runtime models share a process-local capability cache keyed by immutable `model_config_id`; native providers keep their fast paths, while partial providers use non-streamed textual tool normalization and one schema-constrained JSON fallback. Admin probe reports the same capability concepts without persisting them, and hotspot filtering raises real tool errors so the existing execution wrapper records failed audits.

**Tech Stack:** Python 3.12, LangChain/LangGraph, Pydantic v2, FastAPI, SQLModel, httpx, Vue 3, TypeScript, Vitest-style Node tests, pytest.

## Global Constraints

- Continue supporting the current active endpoint; capability warnings must not block saving.
- Do not add a database column or migration and do not require database recreation.
- Never log API keys, Authorization headers, ciphertext, provider response bodies, probe nonces, or user content.
- Native tool calling and native JSON Schema paths remain preferred; fallback is bounded to one extra structured request.
- Network, timeout, authentication, rate-limit, and 5xx failures do not become protocol-capability misses.
- Textual tool compatibility accepts at most one registered tool per response, drops unknown parameters, and rejects unknown tools or multiple tool blocks.
- Frontend warnings use existing tokens, remain responsive, include text in addition to color, and use an `aria-live="polite"` region.
- Preserve unrelated dirty-worktree changes. Several target files were already modified before this plan, so overlapping task checkpoints use `git diff --check` and defer commits instead of staging whole files; commit only after the user has separated or explicitly authorized those pre-existing hunks.

---

### Task 1: Protocol parsing and process-local capability cache

**Files:**
- Create: `apps/api/src/agent/infrastructure/llm/compatibility.py`
- Create: `apps/api/tests/test_llm_compatibility.py`

**Interfaces:**
- Produces: `ModelProtocolCapabilities(native_tool_calls: bool | None, json_schema: bool | None)`.
- Produces: `ModelProtocolCapabilityCache.get(model_config_id: str) -> ModelProtocolCapabilities`, `.set_tool_calls(model_config_id: str, supported: bool) -> None`, and `.set_json_schema(model_config_id: str, supported: bool) -> None` with one process-global instance `model_protocol_capabilities`.
- Produces: `normalize_textual_tool_call(message: AIMessage, tools: Sequence[BaseTool]) -> AIMessage`.
- Produces: `parse_single_json_object(text: str) -> dict[str, Any]` and `validate_structured_text(message, schema)`.

- [ ] **Step 1: Write failing protocol parser tests**

Add tests that construct real `AIMessage` and `@tool` objects and assert:

```python
def test_textual_tool_parser_accepts_one_registered_call_and_drops_unknown_arguments():
    message = AIMessage(content='''<function_calls><invoke name="fetch_hotspots"><parameter name="source">all</parameter><parameter name="limit">10</parameter></invoke></function_calls>''')
    normalized = normalize_textual_tool_call(message, [fetch_hotspots])
    assert normalized.content == ""
    assert normalized.tool_calls[0]["name"] == "fetch_hotspots"
    assert normalized.tool_calls[0]["args"] == {"source": "all"}

@pytest.mark.parametrize("content", [
    '<invoke name="unknown_tool"></invoke>',
    '<invoke name="fetch_hotspots"></invoke><invoke name="fetch_hotspots"></invoke>',
])
def test_textual_tool_parser_rejects_unknown_or_multiple_calls(content):
    with pytest.raises(TextualToolCallInvalid):
        normalize_textual_tool_call(AIMessage(content=content), [fetch_hotspots])
```

Add JSON extraction tests for a bare object, fenced object, one object surrounded by prose, truncated JSON, a top-level array, and two objects. The last four invalid forms must raise `StructuredOutputInvalid`.

- [ ] **Step 2: Run parser tests and verify RED**

Run:

```powershell
$env:PYTHONPATH="$PWD\apps\api\src"
.\.venv\Scripts\python.exe -m pytest apps/api/tests/test_llm_compatibility.py -q
```

Expected: collection fails because `agent.infrastructure.llm.compatibility` does not exist.

- [ ] **Step 3: Implement minimal parser and cache**

Implement frozen dataclasses and locked cache updates. Implement JSON extraction with `json.JSONDecoder.raw_decode`: locate the first non-fenced `{`, decode exactly one object, reject any second non-whitespace/non-fence JSON object, and require `dict`. Implement textual-tool normalization by reusing the existing XML grammar, allowing only one `<invoke>`, resolving the registered tool, filtering arguments through `tool.get_input_schema().model_fields`, and returning `message.model_copy(update={"content": "", "tool_calls": normalized_calls, "invalid_tool_calls": []})`.

- [ ] **Step 4: Run parser tests and verify GREEN**

Run the command from Step 2. Expected: all tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add apps/api/src/agent/infrastructure/llm/compatibility.py apps/api/tests/test_llm_compatibility.py
git commit -m "feat(llm): add protocol compatibility primitives"
```

### Task 2: Capability-aware Agent tool calling without SSE leakage

**Files:**
- Modify: `apps/api/src/agent/infrastructure/llm/client.py`
- Modify: `apps/api/src/agent/infrastructure/llm/gateway.py`
- Modify: `apps/api/src/agent/graph/nodes.py`
- Modify: `apps/api/tests/test_llm_client.py`
- Modify: `apps/api/tests/test_graph_nodes.py`
- Modify: `apps/api/tests/test_runner_stream.py`

**Interfaces:**
- Consumes: `model_protocol_capabilities` and `normalize_textual_tool_call` from Task 1.
- Produces: `LangChainChatClient.build_agent_model(*, model_config_id: str, temperature: float | None, max_tokens: int, tools: list[Any]) -> Any` returning a capability-aware invokable.
- Produces: `_probe_native_tool_calls()` using a synthetic `contentai_protocol_probe` tool with a random nonce and no side effects.

- [ ] **Step 1: Write failing Agent compatibility tests**

Add a fake chat model that returns native tool calls for the probe and verify the actual call keeps the original callback config. Add a partial-provider fake that returns textual XML for the probe and actual call; verify its actual invocation receives `config={"callbacks": []}` and the node returns a normalized tool call.

Add a stream regression in `test_runner_stream.py` whose graph update contains `AIMessage(content="", tool_calls=[{"name": "fetch_hotspots", "args": {}, "id": "call-compat", "type": "tool_call"}])` and whose simulated partial-provider model never yields a `messages` token. Assert no `assistant_message_delta` contains `<function_calls>`, `<invoke>`, or `function_results`. Keep the existing native plain-response stream test unchanged and passing.

- [ ] **Step 2: Run the three focused tests and verify RED**

```powershell
$env:PYTHONPATH="$PWD\apps\api\src"
.\.venv\Scripts\python.exe -m pytest apps/api/tests/test_llm_client.py apps/api/tests/test_graph_nodes.py apps/api/tests/test_runner_stream.py -q
```

Expected: new assertions fail because Agent models do not probe/cache capability and partial-provider calls still inherit streaming callbacks.

- [ ] **Step 3: Implement capability-aware invokable**

Move XML normalization ownership from `nodes.py` into the compatibility module while retaining a small node-level call. Wrap the bound chat model with an invokable that:

```python
capabilities = model_protocol_capabilities.get(model_config_id)
if capabilities.native_tool_calls is None:
    supported = probe_native_tool_calls(base_chat_model)
    model_protocol_capabilities.set_tool_calls(model_config_id, supported)
if supported:
    return bound_model.invoke(messages, config=config)
return normalize_textual_tool_call(
    bound_model.invoke(messages, config={"callbacks": []}),
    tools,
)
```

The synthetic probe must force only `contentai_protocol_probe`, validate the returned nonce, use a short timeout and zero retries, and propagate transport/auth/provider errors without caching them. Protocol-invalid HTTP 200 responses cache `False`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all tests pass; native stream regression still observes delta events, partial compatibility regression observes none.

- [ ] **Step 5: Checkpoint Task 2 without staging overlapping files**

```powershell
git diff --check -- apps/api/src/agent/infrastructure/llm/client.py apps/api/src/agent/infrastructure/llm/gateway.py apps/api/src/agent/graph/nodes.py apps/api/tests/test_llm_client.py apps/api/tests/test_graph_nodes.py apps/api/tests/test_runner_stream.py
```

Expected: exit 0. Do not stage these already-dirty files in this task.

### Task 3: Validated structured-output fallback

**Files:**
- Modify: `apps/api/src/agent/infrastructure/llm/compatibility.py`
- Modify: `apps/api/src/agent/infrastructure/llm/client.py`
- Modify: `apps/api/tests/test_llm_compatibility.py`
- Modify: `apps/api/tests/test_llm_client.py`

**Interfaces:**
- Consumes: JSON parser and capability cache from Task 1.
- Produces: `CapabilityAwareStructuredModel.invoke(input, config=None)`.
- Produces: stable exception `ModelStructuredOutputInvalid(code="MODEL_STRUCTURED_OUTPUT_INVALID")`.

- [ ] **Step 1: Write failing structured fallback tests**

Cover these behaviors with fake native and plain chat models. The concrete test setup uses `QueueInvoker`, which records calls and returns or raises queued values:

```python
def test_http_200_plain_text_uses_one_schema_prompt_fallback():
    native = QueueInvoker([OutputParserException("invalid structured response")])
    fallback = QueueInvoker([AIMessage(content='```json\n{"selected_candidates": []}\n```')])
    model = CapabilityAwareStructuredModel(
        model_config_id="mcf-partial",
        schema=HotspotFilterResult,
        native=native,
        fallback=fallback,
    )

    assert model.invoke("score").selected_candidates == []
    assert len(native.calls) == 1
    assert len(fallback.calls) == 1

def test_timeout_does_not_trigger_text_fallback():
    request = httpx.Request("POST", "https://models.example.test/v1/chat/completions")
    native = QueueInvoker([httpx.ReadTimeout("timed out", request=request)])
    fallback = QueueInvoker([AIMessage(content='{"selected_candidates": []}')])
    model = CapabilityAwareStructuredModel(
        model_config_id="mcf-timeout",
        schema=HotspotFilterResult,
        native=native,
        fallback=fallback,
    )

    with pytest.raises(httpx.ReadTimeout):
        model.invoke("score")
    assert fallback.calls == []
```

Add adjacent tests named `test_native_structured_success_does_not_call_text_fallback`, `test_fenced_json_fallback_validates_as_pydantic_schema`, and `test_two_invalid_protocol_responses_raise_stable_error_without_body` using the same concrete helper and assertions.

Assert the fallback system message contains `schema.model_json_schema()` and “one JSON object only”, the second request occurs at most once, and exception strings contain neither raw provider content nor input payload.

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
$env:PYTHONPATH="$PWD\apps\api\src"
.\.venv\Scripts\python.exe -m pytest apps/api/tests/test_llm_compatibility.py apps/api/tests/test_llm_client.py -q
```

Expected: tests fail because `build_structured_output_model` currently returns `chat_model.with_structured_output(schema)` directly.

- [ ] **Step 3: Implement structured model state machine**

Keep the native `with_structured_output(schema)` runnable. On success cache `json_schema=True`. Catch only output-parser/Pydantic/empty-structured-result failures, cache `False`, and invoke a non-streamed base chat model once with the JSON Schema system constraint. Parse with `validate_structured_text` and return the Pydantic instance. If both paths are invalid, raise `ModelStructuredOutputInvalid` from `None`. Do not catch `httpx`, OpenAI status/auth, cancellation, or timeout exceptions as capability misses.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all tests pass.

- [ ] **Step 5: Checkpoint Task 3 without staging overlapping files**

```powershell
git diff --check -- apps/api/src/agent/infrastructure/llm/compatibility.py apps/api/src/agent/infrastructure/llm/client.py apps/api/tests/test_llm_compatibility.py apps/api/tests/test_llm_client.py
```

Expected: exit 0. Keep the Task 1 commit intact and leave later overlapping hunks unstaged.

### Task 4: Make hotspot scoring failures real tool failures

**Files:**
- Modify: `apps/api/src/agent/tools/hotspots.py`
- Modify: `apps/api/tests/test_hotspots.py`
- Modify: `apps/api/tests/test_tool_execution.py`

**Interfaces:**
- Consumes: `ModelStructuredOutputInvalid` from Task 3.
- Produces: `HotspotFilterFailed(code="HOTSPOT_FILTER_FAILED")` raised by `fetch_hotspots` when the scorer is missing or invalid.

- [ ] **Step 1: Write failing hotspot and audit tests**

Change the missing-prompt and invalid-model tests to expect `HotspotFilterFailed`, not a successful dictionary with `filtering.status=error`. Add an execution-wrapper test that invokes a raising hotspot function and asserts:

```python
assert result.status == "error"
assert result.content["status"] == "failed"
assert stored_tool_execution.status == ToolExecutionStatus.failed
assert stored_tool_execution.error
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
$env:PYTHONPATH="$PWD\apps\api\src"
.\.venv\Scripts\python.exe -m pytest apps/api/tests/test_hotspots.py apps/api/tests/test_tool_execution.py -q
```

Expected: hotspot tests receive successful error dictionaries and the desired exception is absent.

- [ ] **Step 3: Implement minimal failure semantics**

Define a safe exception whose public classification does not include provider text. Remove the `except ValueError`/`except Exception` success dictionaries in `fetch_hotspots`; translate known validation failures to `HotspotFilterFailed` using exception chaining suppressed from public output, and let the existing `execute_tool_call` failure path update the audit.

- [ ] **Step 4: Run tests and verify GREEN**

Run Step 2. Expected: all tests pass and successful hotspot tests retain `filtering.status="ok"`.

- [ ] **Step 5: Checkpoint Task 4 without staging overlapping files**

```powershell
git diff --check -- apps/api/src/agent/tools/hotspots.py apps/api/tests/test_hotspots.py apps/api/tests/test_tool_execution.py
```

Expected: exit 0. Do not stage pre-existing changes from these files.

### Task 5: Extend admin probe capability reporting

**Files:**
- Modify: `apps/api/src/services/model_config_network.py`
- Modify: `apps/api/src/models/schemas/admin.py`
- Modify: `apps/api/src/api/admin.py`
- Modify: `apps/api/tests/test_model_configuration.py`
- Modify: `apps/api/tests/test_model_config_admin.py`

**Interfaces:**
- Produces: `ModelProbeResult.native_tool_calls`, `.json_schema`, and `.warnings: tuple[str, ...]`.
- Produces API fields `native_tool_calls: bool`, `json_schema: bool`, `warnings: list[str]`.

- [ ] **Step 1: Write failing probe tests**

Extend MockTransport fixtures so a fully compatible endpoint returns a nonce-matching tool call and schema-valid JSON, while a partial endpoint returns ordinary content for both capability checks. Assert both probes return HTTP 200 and `model_validated=True`, but only the partial result contains:

```python
assert result.native_tool_calls is False
assert result.json_schema is False
assert result.warnings == (
    "MODEL_NATIVE_TOOL_CALLS_UNSUPPORTED",
    "MODEL_JSON_SCHEMA_UNSUPPORTED",
)
```

Add API response assertions for the three new fields and verify credentials/provider bodies remain absent.

- [ ] **Step 2: Run probe tests and verify RED**

```powershell
$env:PYTHONPATH="$PWD\apps\api\src"
.\.venv\Scripts\python.exe -m pytest apps/api/tests/test_model_configuration.py apps/api/tests/test_model_config_admin.py -q
```

Expected: result/schema fields do not exist.

- [ ] **Step 3: Implement bounded capability probes**

Use the existing pinned `_request_json` transport for both new POSTs. The tool request includes exactly one synthetic tool and forced `tool_choice`; the JSON Schema request uses a minimal object with one required nonce field. Treat valid HTTP 200 but invalid protocol shape as `False`; continue propagating endpoint, authentication, reachability, and response-size failures. Populate stable warnings in deterministic order. Do not change save acceptance logic.

- [ ] **Step 4: Run probe tests and verify GREEN**

Run Step 2. Expected: all tests pass.

- [ ] **Step 5: Checkpoint Task 5 without staging overlapping files**

```powershell
git diff --check -- apps/api/src/services/model_config_network.py apps/api/src/models/schemas/admin.py apps/api/src/api/admin.py apps/api/tests/test_model_configuration.py apps/api/tests/test_model_config_admin.py
```

Expected: exit 0. Defer staging because the admin/model configuration files already contain unrelated worktree changes.

### Task 6: Show an accessible non-blocking compatibility warning

**Files:**
- Modify: `apps/web/src/services/api.ts`
- Modify: `apps/web/src/views/AdminModelsView.vue`
- Modify: `apps/web/src/styles/admin.css`
- Modify: `apps/web/tests/model-config-admin.spec.mjs`
- Modify: `apps/web/tests/admin-responsive.spec.mjs`

**Interfaces:**
- Consumes: API capability fields from Task 5.
- Produces: `probeWarnings` state and a compatibility notice in the model configuration status region.

- [ ] **Step 1: Write failing UI tests**

Mock a successful partial probe with both warning codes. Assert the view renders “连接成功，将启用兼容模式”, lists human-readable tool/structured limitations, retains the enabled save button, uses `role="status"`/`aria-live="polite"`, and contains no fixed width that overflows a 320px viewport. Add a compatible probe assertion that the notice is absent.

- [ ] **Step 2: Run UI tests and verify RED**

```powershell
Set-Location apps/web
npm test -- --run tests/model-config-admin.spec.mjs tests/admin-responsive.spec.mjs
```

Expected: TypeScript fixture/view assertions fail because capability fields and warning UI are absent.

- [ ] **Step 3: Implement warning state and styling**

Extend `AdminModelProbeResult`. Map stable codes to fixed Chinese copy in the view, clear stale warnings when connection inputs change, and keep warnings separate from `errorMessage`. Render one token-based notice using existing admin feedback vocabulary; include a leading text label/icon, natural wrapping, `overflow-wrap:anywhere`, and no modal or color-only meaning.

- [ ] **Step 4: Run UI tests and web verification**

```powershell
Set-Location apps/web
npm test -- --run tests/model-config-admin.spec.mjs tests/admin-responsive.spec.mjs
npm run verify
```

Expected: focused tests and repository web verification pass.

- [ ] **Step 5: Checkpoint Task 6 without staging overlapping files**

```powershell
git diff --check -- apps/web/src/services/api.ts apps/web/src/views/AdminModelsView.vue apps/web/src/styles/admin.css apps/web/tests/model-config-admin.spec.mjs apps/web/tests/admin-responsive.spec.mjs
```

Expected: exit 0. Do not stage the existing admin-model worktree changes.

### Task 7: Integrated regression and live endpoint verification

**Files:**
- Modify: `docs/API.md`
- Modify: `docs/ARCHITECTURE.md`
- Test only: API and Web suites listed below

**Interfaces:**
- Consumes all prior tasks.
- Produces final verification evidence; no new runtime interface.

- [ ] **Step 1: Run focused backend regression**

```powershell
$env:PYTHONPATH="$PWD\apps\api\src"
.\.venv\Scripts\python.exe -m pytest apps/api/tests/test_llm_compatibility.py apps/api/tests/test_llm_client.py apps/api/tests/test_graph_nodes.py apps/api/tests/test_runner_stream.py apps/api/tests/test_hotspots.py apps/api/tests/test_tool_execution.py apps/api/tests/test_model_configuration.py apps/api/tests/test_model_config_admin.py -q
```

Expected: all selected tests pass with zero failures.

- [ ] **Step 2: Run repository verification**

```powershell
.\.venv\Scripts\python.exe -m pytest apps/api/tests -q
Set-Location apps/web
npm run verify
```

Expected: backend suite and web verification exit 0.

- [ ] **Step 3: Rebuild services without mutating database data**

```powershell
Set-Location ..\..
docker compose build api agent-worker background-worker side-effect-worker web
docker compose up -d api agent-worker background-worker side-effect-worker web
docker compose ps
```

Expected: rebuilt services report healthy; no volume removal or database reset is performed.

- [ ] **Step 4: Verify the current endpoint with a bounded live diagnostic**

Submit one “获取今日热点，并按我的账号定位给出推荐顺序” execution through the normal application path. Verify:

- no SSE `assistant_message_delta` contains `<function_calls>`, `<invoke>`, `system_warning`, or `function_results`;
- the hotspot tool audit is completed only when scoring succeeds;
- a successful run contains ranked candidates; or, if both structured attempts are invalid, the tool audit is failed with the stable safe error;
- logs contain neither credentials nor remote response bodies.

- [ ] **Step 5: Update contract docs only where behavior changed**

Document the three probe response fields and compatibility-mode runtime behavior in `docs/API.md` and `docs/ARCHITECTURE.md`. Do not alter product scope or migration policy.

- [ ] **Step 6: Run final verification after documentation changes**

Repeat Step 1 and `npm run verify`. Run `git diff --check`. Expected: all commands exit 0.

- [ ] **Step 7: Checkpoint final integration/docs changes**

```powershell
git diff --check -- docs/API.md docs/ARCHITECTURE.md
git status --short
```

Expected: diff check exits 0 and status lists the intended compatibility files alongside preserved pre-existing changes. Ask the user before creating a combined implementation commit.

## Final Review Checklist

- [ ] Every production behavior was preceded by a test that failed for the intended reason.
- [ ] Native providers do not enter fallback in tests.
- [ ] Partial providers never expose textual tool protocol through Assistant SSE.
- [ ] Structured fallback is bounded, schema-validated, and secret/body safe.
- [ ] Hotspot scoring failures create failed tool audits.
- [ ] Probe warnings are non-blocking and accessible.
- [ ] No database schema or migration changed.
- [ ] Targeted backend tests, full backend tests, and web verification pass.
- [ ] Live current-endpoint evidence matches the acceptance criteria.
