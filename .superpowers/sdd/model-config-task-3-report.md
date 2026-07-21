# Model configuration Task 3 report

## Scope

Migrated the complete agent runtime from the Anthropic relay client to the immutable,
execution-selected OpenAI-compatible model configuration delivered by Tasks 1 and 2.

## Delivered behavior

- `ChatOpenAI` now receives the selected configuration's exact `base_url`, decrypted API key,
  and `model_name`; the Anthropic compatibility event patch and direct Anthropic dependencies
  were removed.
- Dialogue, hotspot scoring, research structured output, memory/title structured output, and
  token counting all use the execution-selected model name. Existing temperature, context, and
  output budgets remain environment-backed non-model tuning.
- A new turn reads the active configuration and stores `AgentExecution.model_config_id` in the
  same transaction as its message, invocation, execution, and execution outbox rows.
- Missing active configuration returns `503 MODEL_NOT_CONFIGURED` before any business row is
  created. The regression test verifies zero messages, invocations, executions, and outbox rows.
- Workers, resume/retry processing, queued execution delivery, and post-execution memory/title
  work resolve the immutable configuration referenced by the execution rather than the current
  active version.
- Execute and postprocess delivery carry `model_config_id`; dispatcher joins and worker claims
  fence delivery against the execution snapshot.
- Runtime gateway/model/graph caches are bounded LRU caches. Model and graph keys include
  `model_config_id`, normalized permissions, and resolved tool names, preventing cross-version
  or cross-permission reuse. Concurrent misses for the same key are single-flighted.
- Provider message token counting is combined with local tool-schema sizing without passing the
  unsupported OpenAI `tools` counting argument.
- Provider exception bodies, Authorization values, API keys, and ciphertext are not copied into
  public runtime errors, durable failure messages, event payloads, or runtime logs.
- The tool-execution boundary applies the same redaction before writing a failed ToolMessage,
  tool event, or ToolExecution audit row.
- Legacy chat/planning/summary/embedding model settings were removed. Search-tool relay settings
  remain tool-only and are not used by any model construction path.

## TDD evidence

RED was observed before the corresponding production changes:

- `test_llm_client.py` failed because the OpenAI client path did not exist and the runtime still
  constructed the Anthropic relay client.
- `test_runtime_container.py` failed because `create_runtime()` did not accept or isolate by
  `model_config_id`.
- `test_model_config_runtime.py` exposed the plain `RuntimeError` and partial transaction path
  when no active configuration existed instead of `503 MODEL_NOT_CONFIGURED` with zero writes.
- Outbox transport tests showed delivery kwargs without `model_config_id`, and worker claim did
  not fence a delivery for another configuration version.
- Provider token counting first passed an unsupported `tools` argument and then fell back instead
  of combining provider message count with local tool-schema size.
- The legacy-settings test was first RED after chat/planning/summary removal because
  `embedding_model` still remained in `LLMSettings`; deleting that final legacy field made the
  complete four-field contract GREEN.

GREEN evidence recorded during implementation:

- `test_llm_client.py`: 11 passed.
- `test_runtime_container.py`: 5 passed.
- `test_model_config_runtime.py`: 4 passed.
- `test_execution_outbox.py`: 16 passed.
- `test_task2_execution_contract.py`: 69 passed.
- Task 3 API/worker selection: 14 passed, 58 deselected.
- Affected graph/research/memory/hotspot/runner/context/settings/model-config selection:
  189 passed.

Fresh completion verification:

- `uv run python -m pytest -vv -s apps/api/tests/test_llm_client.py`:
  11 passed in 21.93s.
- `uv run python -m pytest -q apps/api/tests/test_runtime_container.py
  apps/api/tests/test_model_config_runtime.py apps/api/tests/test_execution_outbox.py`:
  25 passed in 39.82s.
- `uv run python -m pytest -q apps/api/tests/test_task2_execution_contract.py`:
  69 passed in 106.96s.
- `uv run ruff check apps/api/src apps/api/tests`: all checks passed.
- `uv run python -m compileall -q apps/api/src apps/api/tests`: exit 0.
- `uv lock --check`: resolved 93 packages, exit 0.
- `git diff --check`: exit 0.
- Source/dependency scan for Anthropic imports/packages: no matches.
- Source scan for legacy model configuration fields: no field accesses or declarations.

Independent review found four implementation issues and one coverage gap. The implementation
issues were converted into focused tests before their fixes:

- RED: five focused tests failed in 9.49s, proving duplicate concurrent gateway/model/graph
  construction, raw provider text in tool message/event/audit, byte/token unit mixing, and an
  optional postprocess delivery configuration ID.
- GREEN: the same five tests passed in 9.84s after same-key single-flight locking, tool-boundary
  redaction, an explicit ceil(bytes/4) schema-token estimate, and a required three-way
  execution/outbox/delivery configuration fence.
- The coverage follow-up runs the real `tasks.execute_agent.run -> claim_execution ->
  AgentRunner` path across queue, active switch, worker, resume, and retry, then proves a new
  execution selects v2. A second test constructs both wrong delivery ID and database-level
  outbox mismatch states and proves zero gateway calls or processing-attempt changes. Both
  passed in 66.14s.
- Final affected LLM/context/runtime/outbox/tool group: 69 passed in 113.86s.
- Final Task 2 execution-contract regression: 69 passed in 108.43s.
- Final model-config runtime plus direct-dispatch background/resume regression: 9 passed in
  79.43s.
- Final independent re-review: zero Critical, zero Important, ready to merge.

One earlier combined pytest launch exited 1 without producing pytest output. It was not counted
as test evidence. The same files were then run in explicit serial groups above and passed.

## Security and transaction notes

- Runtime API keys are wrapped in `SecretStr` after decryption and are passed directly to
  `ChatOpenAI`; no explicit Authorization header string is constructed or retained.
- Runtime configuration lookup by execution ID does not require `is_active`, so superseded
  versions remain usable by in-flight and retried executions.
- The active configuration is required before IDs or ORM business rows are created. The API's
  request-scoped transaction therefore has nothing to roll back on the missing-config path.
- Cache keys retain identifiers and tool signatures only; decrypted keys and ciphertext never
  enter cache keys, logs, errors, events, or outbox payloads.

## Known full-file concern

An exploratory full `test_api.py` run produced 10 failures outside the Task 3 model-configuration
paths: an existing checkpoint-deletion cleanup ordering issue and Redis minute-rate-limit state
caused later message/resume requests to return 429 and dependent waits to time out. The 14
Task 3-relevant API/worker tests passed when run serially in isolation. The pre-existing Python
process started at 15:36 was not terminated or modified.

The bounded caches currently use one reentrant lock around cold construction. This guarantees
same-key single-flight and has no observed deadlock, but different-key cold builds are serialized;
per-key single-flight is a future non-blocking performance refinement.

## User-owned paths

`.codex-logs/`, `.impeccable/`, `AGENTS.md`, `docs/reviews/`, and `.pytest-task3-*` were not
modified or staged.
