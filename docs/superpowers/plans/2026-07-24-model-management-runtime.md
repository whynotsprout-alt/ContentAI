# Unified Model Management Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make versioned model management the only source of connection and runtime model parameters, support the proxy Fake-IP range, and guarantee a fresh installation has the `.env` bootstrap administrator.

**Architecture:** Extend the immutable `ModelConfiguration` aggregate with four strongly typed runtime fields and carry the selected version through `RuntimeModelConfiguration`, `RuntimeContainer`, `ModelGateway`, context assembly, and input preflight. Keep probe inputs connection-only, validate and persist the complete runtime snapshot on save, remove global `LLMSettings`, and update the existing model-management form without changing the surrounding admin shell.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLModel/SQLAlchemy, PostgreSQL/Alembic, Vue 3, TypeScript, Vitest, Playwright, Docker Compose.

## Global Constraints

- This release supports a fresh database only: modify `202607210001_v050_initial_schema.py`; do not add a migration, backfill, or legacy fallback.
- Persist exactly `temperature`, `context_window_tokens`, `chat_max_tokens`, and `structured_max_tokens` with every immutable model configuration version.
- Runtime model calls, input preflight, and context trimming must use the execution-bound `model_config_id`; no `settings.llm`, `CONTENTAI_LLM__*`, or code-default fallback may remain.
- Structured-output Temperature stays fixed at `0`; only ordinary Agent chat uses the versioned `temperature`.
- Permit exactly `198.18.0.0/15` as the new reserved-address exception. Public and Fake-IP HTTP remain forbidden; metadata, loopback, link-local, multicast, unspecified, and every other reserved range remain blocked.
- Keep `CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY`; never expose an API Key, ciphertext, Authorization header, upstream body, bootstrap password, or secret-bearing validation input.
- A development or production startup requires valid `.env` bootstrap administrator credentials; repeated or concurrent startup must not overwrite an existing account.
- Preserve every unrelated dirty-worktree change and commit only files belonging to the current task.
- Follow TDD for every behavior change: add a failing test, observe the expected failure, add the minimum implementation, rerun the focused test, then run the task regression set.
- The model-management UI must reuse the current product tokens and admin shell, retain the `<1280px` summary → form → details order, use 44px touch targets, and avoid horizontal scrolling at 320px.

---

### Task 1: Fresh model-configuration schema and fixtures

**Files:**
- Modify: `apps/api/src/models/model_configuration.py`
- Modify: `apps/api/src/contentai_migrations/versions/202607210001_v050_initial_schema.py`
- Modify: `apps/api/tests/model_config_helpers.py`
- Modify: `apps/api/tests/conftest.py`
- Modify: `apps/api/tests/test_model_configuration.py`
- Modify: `apps/api/tests/test_model_config_runtime.py`
- Modify: `apps/api/tests/test_task1_migration_contract.py`

**Interfaces:**
- Produces: required ORM attributes `temperature: float`, `context_window_tokens: int`, `chat_max_tokens: int`, `structured_max_tokens: int`.
- Produces: `DEFAULT_MODEL_RUNTIME_PARAMETERS: dict[str, int | float]` and `model_runtime_parameters(**overrides)`.
- Consumes: the existing single `202607210001` fresh-database revision and immutable `ModelConfiguration.version`.

- [ ] **Step 1: Add failing metadata and database-constraint tests**

Add assertions equivalent to:

```python
RUNTIME_COLUMNS = {
    "temperature",
    "context_window_tokens",
    "chat_max_tokens",
    "structured_max_tokens",
}

def test_model_configuration_contains_required_runtime_snapshot() -> None:
    table = ModelConfiguration.__table__
    assert RUNTIME_COLUMNS <= set(table.columns)
    assert all(not table.columns[name].nullable for name in RUNTIME_COLUMNS)

@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"temperature": -0.1}, "ck_modelconfiguration_temperature_range"),
        ({"temperature": 2.1}, "ck_modelconfiguration_temperature_range"),
        ({"context_window_tokens": 0}, "ck_modelconfiguration_context_window_positive"),
        ({"chat_max_tokens": 32000}, "ck_modelconfiguration_chat_output_fits_context"),
        ({"structured_max_tokens": 32000}, "ck_modelconfiguration_structured_output_fits_context"),
    ],
)
def test_postgres_rejects_invalid_runtime_parameters(overrides, constraint) -> None:
    runtime = model_runtime_parameters(**overrides)
    with Session(get_engine()) as session:
        session.add(
            ModelConfiguration(
                id=f"invalid-{constraint}",
                version=99,
                base_url="https://models.test.invalid/v1",
                model_name="test-model",
                api_key_ciphertext="test-ciphertext",
                api_key_fingerprint="a" * 64,
                api_key_hint="key-…7890",
                created_by_user_id="local-user",
                **runtime,
            )
        )
        with pytest.raises(IntegrityError, match=constraint):
            session.commit()
        session.rollback()
```

Extend the fresh-migration inspection to assert all four columns and named checks exist in the upgraded database.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
$env:PYTHONPATH = "apps/api/src"
.\.venv\Scripts\python.exe -m pytest `
  apps/api/tests/test_model_configuration.py `
  apps/api/tests/test_task1_migration_contract.py -q
```

Expected: failures report missing runtime columns or named constraints.

- [ ] **Step 3: Add the strong schema to ORM and the initial migration**

Add explicit fields and constraints:

```python
CheckConstraint(
    "temperature >= 0 AND temperature <= 2",
    name="ck_modelconfiguration_temperature_range",
),
CheckConstraint(
    "context_window_tokens > 0",
    name="ck_modelconfiguration_context_window_positive",
),
CheckConstraint(
    "chat_max_tokens > 0 AND chat_max_tokens < context_window_tokens",
    name="ck_modelconfiguration_chat_output_fits_context",
),
CheckConstraint(
    "structured_max_tokens > 0 AND structured_max_tokens < context_window_tokens",
    name="ck_modelconfiguration_structured_output_fits_context",
),

temperature: float
context_window_tokens: int
chat_max_tokens: int
structured_max_tokens: int
```

Mirror them in `202607210001_v050_initial_schema.py` with non-null `sa.Float()` / `sa.Integer()` columns and the same named SQL checks. Do not add server defaults.

- [ ] **Step 4: Make every test configuration explicit**

Create:

```python
DEFAULT_MODEL_RUNTIME_PARAMETERS = {
    "temperature": 0.2,
    "context_window_tokens": 32_000,
    "chat_max_tokens": 8_000,
    "structured_max_tokens": 8_000,
}

def model_runtime_parameters(**overrides: int | float) -> dict[str, int | float]:
    return {**DEFAULT_MODEL_RUNTIME_PARAMETERS, **overrides}
```

Use these values in the autouse SQL fixture and every direct `ModelConfiguration(...)` construction. This is test data only, not a production fallback.

- [ ] **Step 5: Run focused schema tests and regression tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  apps/api/tests/test_model_configuration.py `
  apps/api/tests/test_model_config_runtime.py `
  apps/api/tests/test_task1_migration_contract.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 1**

```powershell
git add -- apps/api/src/models/model_configuration.py apps/api/src/contentai_migrations/versions/202607210001_v050_initial_schema.py apps/api/tests/model_config_helpers.py apps/api/tests/conftest.py apps/api/tests/test_model_configuration.py apps/api/tests/test_model_config_runtime.py apps/api/tests/test_task1_migration_contract.py
git commit -m "feat(api): add versioned model runtime fields"
```

### Task 2: Model-management API, persistence, and auditing

**Files:**
- Modify: `apps/api/src/models/schemas/admin.py`
- Modify: `apps/api/src/models/schemas/__init__.py`
- Modify: `apps/api/src/api/admin.py`
- Modify: `apps/api/src/services/model_configuration_service.py`
- Modify: `apps/api/src/services/model_configuration_repository.py`
- Modify: `apps/api/tests/test_model_config_admin.py`

**Interfaces:**
- Produces: `ModelRuntimeParameters` validation contract.
- Produces: expanded `RuntimeModelConfiguration`.
- Produces: `ModelConfigurationService.update(session, *, actor_user_id, request_id, base_url, api_key, model_name, temperature, context_window_tokens, chat_max_tokens, structured_max_tokens, expected_version)`.
- Consumes: Task 1 ORM fields and constraints.

- [ ] **Step 1: Add failing request, response, version, and audit tests**

Use one complete payload in admin API tests:

```python
RUNTIME_PAYLOAD = {
    "temperature": 0.35,
    "context_window_tokens": 200_000,
    "chat_max_tokens": 12_000,
    "structured_max_tokens": 6_000,
}

response = admin_client.put(
    "/api/admin/model-config",
    json={
        "base_url": "https://models.example.test/v1",
        "api_key": "new-secret-key",
        "model_name": "claude-opus-4-6",
        "expected_version": 1,
        **RUNTIME_PAYLOAD,
    },
)
assert response.status_code == 200
assert RUNTIME_PAYLOAD.items() <= response.json().items()
assert RUNTIME_PAYLOAD.items() <= audit.detail.items()
assert "api_key" not in repr(audit.detail)
```

Add `422` cases for Temperature outside `0..2`, zero/negative integers, and either output limit greater than or equal to the context window. Assert validation responses do not echo any request value or API Key.

- [ ] **Step 2: Run admin tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest apps/api/tests/test_model_config_admin.py -q
```

Expected: response fields are absent and update requests either ignore or reject the new parameters.

- [ ] **Step 3: Implement the shared Pydantic contract**

Add:

```python
class ModelRuntimeParameters(BaseModel):
    temperature: float = Field(ge=0, le=2)
    context_window_tokens: int = Field(gt=0)
    chat_max_tokens: int = Field(gt=0)
    structured_max_tokens: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_output_budgets(self) -> Self:
        if self.chat_max_tokens >= self.context_window_tokens:
            raise ValueError("chat_max_tokens must be less than context_window_tokens")
        if self.structured_max_tokens >= self.context_window_tokens:
            raise ValueError("structured_max_tokens must be less than context_window_tokens")
        return self
```

Make `ModelConfigurationUpdateRequest` include this contract and add nullable copies of the four fields to `ModelConfigurationResponse`, while keeping `ModelConfigurationProbeRequest` connection-only.

- [ ] **Step 4: Thread the complete snapshot through router, service, and repository**

Expand the immutable DTO:

```python
@dataclass(frozen=True)
class RuntimeModelConfiguration:
    id: str
    base_url: str
    model_name: str
    api_key: SecretStr
    temperature: float
    context_window_tokens: int
    chat_max_tokens: int
    structured_max_tokens: int
```

Pass the four request values through `update_model_configuration`, `ModelConfigurationService.update`, and `ModelConfigurationRepository.replace_active`. Persist them in the new row, return them from GET/PUT, and add only these non-secret values to `AdminAuditLog.detail`.

- [ ] **Step 5: Run API and persistence regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  apps/api/tests/test_model_config_admin.py `
  apps/api/tests/test_model_configuration.py -q
```

Expected: all selected tests pass with no secret material in output.

- [ ] **Step 6: Commit Task 2**

```powershell
git add -- apps/api/src/models/schemas/admin.py apps/api/src/models/schemas/__init__.py apps/api/src/api/admin.py apps/api/src/services/model_configuration_service.py apps/api/src/services/model_configuration_repository.py apps/api/tests/test_model_config_admin.py
git commit -m "feat(api): persist complete model configuration versions"
```

### Task 3: Explicit RFC 2544 Fake-IP compatibility

**Files:**
- Modify: `apps/api/src/services/model_config_network.py`
- Modify: `apps/api/tests/test_model_config_network.py`

**Interfaces:**
- Produces: one explicit allowed CIDR, `198.18.0.0/15`.
- Consumes: the existing shared `_resolve_and_validate` path used by probe, sync transport, and async transport.

- [ ] **Step 1: Add failing boundary and transport tests**

Add:

```python
@pytest.mark.parametrize("address", ["198.18.0.0", "198.18.0.201", "198.19.255.255"])
def test_https_accepts_rfc2544_fake_ip_range(address: str) -> None:
    assert normalize_model_base_url(
        f"https://{address}/v1",
        resolver=lambda *_: [address],
    ) == f"https://{address}/v1"

def test_rfc2544_fake_ip_http_remains_forbidden() -> None:
    with pytest.raises(ModelEndpointForbidden):
        normalize_model_base_url(
            "http://fake-ip.example/v1",
            resolver=lambda *_: ["198.18.0.201"],
        )
```

Extend sync and async pinned-transport tests to prove a hostname resolving to `198.18.0.201` is pinned and sent with the original Host/SNI. Retain assertions that `192.0.2.1`, metadata, loopback, and link-local addresses are rejected.

- [ ] **Step 2: Run network tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest apps/api/tests/test_model_config_network.py -q
```

Expected: RFC 2544 HTTPS cases fail with `MODEL_ENDPOINT_FORBIDDEN`.

- [ ] **Step 3: Add only the explicit CIDR exception**

Implement:

```python
_IPV4_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")

def _is_allowed_address(address):
    if (
        address in _METADATA_ADDRESSES
        or address.is_multicast
        or address.is_unspecified
        or address.is_link_local
        or (isinstance(address, ipaddress.IPv6Address) and address.is_site_local)
    ):
        return False
    if isinstance(address, ipaddress.IPv4Address) and address in _IPV4_FAKE_IP_NETWORK:
        return True
    if _is_enterprise_local(address):
        return True
    return address.is_global and not address.is_reserved
```

Do not add the CIDR to enterprise-local networks; this preserves the HTTPS requirement.

- [ ] **Step 4: Run network and probe regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  apps/api/tests/test_model_config_network.py `
  apps/api/tests/test_model_config_admin.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 3**

```powershell
git add -- apps/api/src/services/model_config_network.py apps/api/tests/test_model_config_network.py
git commit -m "fix(api): support model proxy fake ip addresses"
```

### Task 4: Execution-bound runtime parameters and removal of global LLM settings

**Files:**
- Modify: `apps/api/src/agent/infrastructure/llm/gateway.py`
- Modify: `apps/api/src/agent/runtime/container.py`
- Modify: `apps/api/src/agent/context/assembler.py`
- Modify: `apps/api/src/agent/runtime/turn_context.py`
- Modify: `apps/api/src/services/conversation_service.py`
- Modify: `apps/api/src/core/config/settings.py`
- Modify: `apps/api/src/core/config/__init__.py`
- Delete: `apps/api/src/core/config/llm.py`
- Modify: `apps/api/tests/test_llm_client.py`
- Modify: `apps/api/tests/test_model_config_runtime.py`
- Modify: `apps/api/tests/test_runtime_container.py`
- Modify: `apps/api/tests/test_api.py`
- Modify: `apps/api/tests/test_research_persistence.py`
- Delete: `apps/api/tests/test_settings_limits.py`
- Modify outside Git: `.env`

**Interfaces:**
- Consumes: Task 2 `RuntimeModelConfiguration`.
- Produces: `ModelGateway(*, model_config_id, base_url, api_key, model_name, temperature, context_window_tokens, chat_max_tokens, structured_max_tokens, client=None)`.
- Produces: explicit `context_window_tokens` and `chat_max_tokens` arguments for context assembly.

- [ ] **Step 1: Add failing runtime-freeze tests**

Create two model configuration versions with deliberately different values and assert:

```python
old_gateway = container.gateway_for_model_config(DEFAULT_MODEL_CONFIG_ID)
new_gateway = container.gateway_for_model_config("model-config-v2")

assert old_gateway.temperature == 0.2
assert old_gateway.chat_max_tokens == 8_000
assert new_gateway.temperature == 0.7
assert new_gateway.context_window_tokens == 200_000
assert new_gateway.chat_max_tokens == 12_000
assert new_gateway.structured_max_tokens == 6_000
```

Update the model-building test to capture LangChain arguments and assert ordinary chat uses the selected version while every structured builder uses Temperature `0` and the selected `structured_max_tokens`.

Refactor input-size tests to update the active database configuration instead of mutating `settings.llm`, then assert preflight uses `context_window_tokens - chat_max_tokens`.

- [ ] **Step 2: Run runtime tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  apps/api/tests/test_llm_client.py `
  apps/api/tests/test_model_config_runtime.py `
  apps/api/tests/test_runtime_container.py `
  apps/api/tests/test_api.py -q
```

Expected: gateway attributes/arguments are absent or input preflight still follows global settings.

- [ ] **Step 3: Make `ModelGateway` an execution-version snapshot**

Replace the global Settings dependency with explicit immutable values:

```python
def __init__(
    self,
    *,
    model_config_id: str,
    base_url: str,
    api_key: SecretStr,
    model_name: str,
    temperature: float,
    context_window_tokens: int,
    chat_max_tokens: int,
    structured_max_tokens: int,
    client: LangChainChatClient | None = None,
) -> None:
    self.temperature = temperature
    self.context_window_tokens = context_window_tokens
    self.chat_max_tokens = chat_max_tokens
    self.structured_max_tokens = structured_max_tokens
```

Use these values in every builder. Update `RuntimeContainer._entry_for_model_config` to pass the complete `RuntimeModelConfiguration`.

- [ ] **Step 4: Inject token budgets into preflight and context assembly**

Make the context budget explicit:

```python
def assemble(
    self,
    *,
    context_window_tokens: int,
    chat_max_tokens: int,
    agent_profile: AgentProfile,
    agent_version: AgentVersion,
    messages: list[BaseMessage],
    short_term_summary: str,
    long_term_memories: list[MemoryEntry],
    tool_names: list[str],
    focus_message: str | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    run_id: str | None = None,
    permissions: list[str] | tuple[str, ...] | None = None,
    research_package: ResearchPackage | None = None,
    token_counter: TokenCounter | None = None,
) -> AgentContext:
    context_message_budget = max(1, context_window_tokens - chat_max_tokens)
```

Thread these arguments through `assemble_turn_context`. In `ConversationService`, pass the already loaded `RuntimeModelConfiguration` into `_ensure_current_input_fits`, use its values for both context assembly and final input-budget comparison, and use the gateway only for provider token counting.

- [ ] **Step 5: Remove the global LLM configuration surface**

Delete `LLMSettings`, the `Settings.llm` field, exports, `_validate_llm`, and obsolete limit tests. Remove the three `CONTENTAI_LLM__*` lines from the local `.env` without printing their values. Add a settings regression asserting undeclared `CONTENTAI_LLM__*` variables do not create a runtime fallback.

- [ ] **Step 6: Run runtime, conversation, and static regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  apps/api/tests/test_llm_client.py `
  apps/api/tests/test_model_config_runtime.py `
  apps/api/tests/test_runtime_container.py `
  apps/api/tests/test_api.py `
  apps/api/tests/test_research_persistence.py `
  apps/api/tests/test_model_configuration.py -q
.\.venv\Scripts\python.exe -m ruff check apps/api/src apps/api/tests
rg -n "settings\\.llm|LLMSettings|CONTENTAI_LLM__" apps/api/src .env
```

Expected: tests and Ruff pass; the negative search returns no matches.

- [ ] **Step 7: Commit Task 4**

```powershell
git add -- apps/api/src/agent/infrastructure/llm/gateway.py apps/api/src/agent/runtime/container.py apps/api/src/agent/context/assembler.py apps/api/src/agent/runtime/turn_context.py apps/api/src/services/conversation_service.py apps/api/src/core/config/settings.py apps/api/src/core/config/__init__.py apps/api/src/core/config/llm.py apps/api/tests/test_llm_client.py apps/api/tests/test_model_config_runtime.py apps/api/tests/test_runtime_container.py apps/api/tests/test_api.py apps/api/tests/test_research_persistence.py apps/api/tests/test_settings_limits.py apps/api/tests/test_model_configuration.py
git commit -m "refactor(api): bind model limits to execution versions"
```

### Task 5: Bootstrap administrator validation and startup concurrency

**Files:**
- Modify: `apps/api/src/core/config/settings.py`
- Modify: `apps/api/src/services/auth_service.py`
- Modify: `apps/api/tests/test_auth_admin.py`

**Interfaces:**
- Produces: valid normalized bootstrap email and `10..128` character password requirement for development/production.
- Produces: idempotent concurrent `AuthService.bootstrap_default_admin`.
- Consumes: existing unique `AppUser.email_normalized` constraint.

- [ ] **Step 1: Add failing startup-configuration tests**

Add cases equivalent to:

```python
@pytest.mark.parametrize(
    "auth",
    [
        {},
        {"bootstrap_admin_email": "not-an-email", "bootstrap_admin_password": "valid password 123"},
        {"bootstrap_admin_email": "admin@example.com", "bootstrap_admin_password": "short"},
        {"bootstrap_admin_email": "admin@example.com", "bootstrap_admin_password": "x" * 129},
    ],
)
def test_runtime_requires_valid_bootstrap_admin(auth) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, env="development", database=DEV_DATABASE, auth=auth)
```

Retain a test-only Settings case without bootstrap credentials so isolated unit tests remain possible.

- [ ] **Step 2: Add a failing concurrent-bootstrap test**

Start two independent PostgreSQL sessions behind a `threading.Barrier`, call `bootstrap_default_admin` for the same new email, and assert both calls settle without an unhandled `IntegrityError`, exactly one active verified admin exists, and neither call logs or returns a password.

Change the existing-account conflict test to assert the account is never modified and startup fails clearly when the configured email belongs to a disabled or non-admin account.

- [ ] **Step 3: Run auth tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest apps/api/tests/test_auth_admin.py -q
```

Expected: invalid credentials are accepted or the concurrent insert leaks an integrity failure.

- [ ] **Step 4: Validate startup credentials and close the insert race**

Use Pydantic `TypeAdapter(EmailStr)` (the project already depends on `email-validator`) to normalize and validate the email. Require credentials when `env` is development or production, require a non-whitespace password length of `10..128`, and retain paired-value validation.

In `bootstrap_default_admin`, catch `sqlalchemy.exc.IntegrityError`, roll back, reload the email, and continue only when the final row is an active, verified administrator. Raise a stable bootstrap conflict exception for any role/status conflict; never promote, enable, or reset an existing account.

- [ ] **Step 5: Run auth and application-startup regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  apps/api/tests/test_auth_admin.py `
  apps/api/tests/test_api_contract.py `
  apps/api/tests/test_task3_operational_readiness.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 5**

```powershell
git add -- apps/api/src/core/config/settings.py apps/api/src/services/auth_service.py apps/api/tests/test_auth_admin.py
git commit -m "fix(auth): harden bootstrap administrator startup"
```

### Task 6: Complete responsive model-management form

**Files:**
- Modify: `apps/web/src/services/api.ts`
- Modify: `apps/web/src/views/modelConfigRequestGuard.ts`
- Modify: `apps/web/src/views/AdminModelsView.vue`
- Modify: `apps/web/src/styles/admin.css`
- Modify: `apps/web/tests/model-config-admin.spec.mjs`
- Modify: `apps/web/tests/admin-responsive.spec.mjs`
- Modify: `apps/web/scripts/verify-web.mjs`

**Interfaces:**
- Consumes: Task 2 API fields and validation limits.
- Produces: full `AdminModelUpdatePayload`.
- Produces: responsive runtime-parameter fieldset and current-version summary.

- [ ] **Step 1: Load the required frontend workflow context**

Read `impeccable/SKILL.md`, run:

```powershell
node "C:\Users\Wna\.agents\skills\impeccable\scripts\context.mjs" --target "apps/web/src/views/AdminModelsView.vue"
```

Read `reference/product.md`, `reference/adapt.md`, the current `AdminModelsView.vue`, and `admin.css`. Preserve the established restrained product register and existing breakpoints.

- [ ] **Step 2: Add failing TypeScript/component/responsive tests**

Assert all API contracts contain:

```ts
temperature: number;
context_window_tokens: number;
chat_max_tokens: number;
structured_max_tokens: number;
```

Mount the view with an unconfigured response and assert initial draft values are `0.2`, `32000`, `8000`, `8000`. Add tests that `canSave` becomes false for Temperature outside `0..2`, non-positive token fields, and output values greater than or equal to context. Assert a valid PUT body contains all four values and a stale save response cannot overwrite a subsequently edited runtime draft.

Add source/CSS assertions for a semantic runtime-parameter fieldset, inline error IDs, `aria-invalid`, `inputmode`, a two-column runtime grid, and a `<=599px` single-column rule.

- [ ] **Step 3: Run frontend tests and verify RED**

Run:

```powershell
Push-Location apps/web
npm.cmd test -- model-config-admin.spec.mjs admin-responsive.spec.mjs
Pop-Location
```

Expected: missing fields/defaults/validation and layout assertions fail.

- [ ] **Step 4: Extend the API and request-guard types**

Add the four fields to `AdminModelConfiguration` and `AdminModelUpdatePayload`. Extend the save-draft signature to include the numeric values:

```ts
export type ModelConfigDraft = {
  baseUrl: string;
  apiKey: string;
  modelName: string;
  temperature?: number | null;
  contextWindowTokens?: number | null;
  chatMaxTokens?: number | null;
  structuredMaxTokens?: number | null;
};
```

Keep a connection-only draft for the probe guard and a complete draft for the save guard so editing runtime parameters invalidates only an in-flight save.

- [ ] **Step 5: Implement the complete form and live validation**

Add refs initialized to:

```ts
const temperature = ref<number | null>(0.2);
const contextWindowTokens = ref<number | null>(32_000);
const chatMaxTokens = ref<number | null>(8_000);
const structuredMaxTokens = ref<number | null>(8_000);
```

Load active values when configured, compute field-specific errors, require all errors to be empty in `canSave`, and submit exact snake_case values. Temperature uses `min="0"`, `max="2"`, `step="0.1"`, and `inputmode="decimal"`; token fields use `min="1"`, `step="1"`, dynamic `max=contextWindowTokens - 1`, and `inputmode="numeric"`. Every field uses `v-model.number`, `aria-invalid`, and `aria-describedby`.

Render the controls inside a borderless “运行参数” fieldset and display all four values in both compact and desktop effective-status views. Move the save response freshness check before applying `active` or form state.

- [ ] **Step 6: Add responsive styling without changing the shell**

Use:

```css
.model-runtime-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-4);
}

@media (max-width: 599px) {
  .model-runtime-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
```

Keep controls at least `44px`, reuse current borders/radii/tokens, and do not add cards, shadows, gradients, or decorative motion.

- [ ] **Step 7: Update browser mocks and run the frontend gate**

Extend `verify-web.mjs` GET/PUT mocks and assertions with all four values, fill the real controls in the model flow, and verify desktop plus 360/768/1024/1280 layouts.

Run:

```powershell
Push-Location apps/web
npm.cmd test -- model-config-admin.spec.mjs admin-responsive.spec.mjs
npm.cmd run build
npm.cmd run verify:web
Pop-Location
```

Expected: tests, build, axe checks, model flow, and responsive overflow checks pass.

- [ ] **Step 8: Commit Task 6**

```powershell
git add -- apps/web/src/services/api.ts apps/web/src/views/modelConfigRequestGuard.ts apps/web/src/views/AdminModelsView.vue apps/web/src/styles/admin.css apps/web/tests/model-config-admin.spec.mjs apps/web/tests/admin-responsive.spec.mjs apps/web/scripts/verify-web.mjs
git commit -m "feat(web): manage complete model runtime settings"
```

### Task 7: Documentation, full verification, fresh database, and live smoke test

**Files:**
- Modify: `docs/PRODUCT.md`
- Modify: `docs/DESIGN.md`
- Modify: `docs/API.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify if matched by negative search: `docs/DEVELOPMENT.md`
- Modify if matched by negative search: `docs/OPERATIONS.md`
- Modify outside Git: `.env`

**Interfaces:**
- Consumes: Tasks 1–6 completed implementation.
- Produces: documented fresh-install and model-management contract.
- Produces: a running project backed by a newly initialized database.

- [ ] **Step 1: Update product and operational documentation**

Document that the one global active OpenAI-compatible version contains Base URL, encrypted API Key, model ID, Temperature, context window, chat output limit, and structured output limit. State that runtime never reads model values from `.env`, structured calls remain deterministic at Temperature `0`, `198.18.0.0/15` is the sole reserved-range exception, and fresh startup creates the configured administrator.

- [ ] **Step 2: Run negative searches**

Run:

```powershell
rg -n "CONTENTAI_LLM__|settings\\.llm|LLMSettings" apps docs .env .env.example
rg -n "temperature|context_window_tokens|chat_max_tokens|structured_max_tokens" apps/api/src apps/web/src
```

Expected: the first search has no matches except historical approved spec/plan text; the second confirms database/API/runtime/UI consumers.

- [ ] **Step 3: Run backend and frontend verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest
Push-Location apps/web
npm.cmd test
npm.cmd run build
npm.cmd run verify:web
Pop-Location
.\tools\review.ps1
git diff --check
```

Expected: every command exits `0` with no serious/critical accessibility finding.

- [ ] **Step 4: Execute Impeccable delivery passes**

Read and follow `reference/audit.md`, `reference/polish.md`, and `reference/harden.md` for `AdminModelsView.vue`. Run the bundled detector against the changed Vue/CSS files, verify 320px and 200% zoom manually in the browser, and rerun the focused frontend tests after any correction.

- [ ] **Step 5: Reinitialize only the active development PostgreSQL volume**

Resolve and verify the exact Compose project and volume before deletion:

```powershell
docker compose -f compose.yaml -f compose.dev.yaml down
docker volume inspect contentai-postgres-data
docker volume rm contentai-postgres-data
```

Do not delete Redis or test volumes. The database removal is intentional and non-recoverable, as approved for this fresh-system design.

- [ ] **Step 6: Rebuild, start, and verify the real model flow**

Run:

```powershell
.\tools\restart.ps1
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/api/ready
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5180/
docker compose -f compose.yaml -f compose.dev.yaml ps
```

Use the `.env` administrator to log in without exposing the credentials. In `/admin/models`, verify the configured Base URL, refresh the candidates, select `claude-opus-4-6`, test the connection, save and enable the full runtime configuration, and send one minimal conversation message. Confirm the API database row contains all four runtime values and no API Key plaintext.

- [ ] **Step 7: Commit documentation**

```powershell
git add -- docs/PRODUCT.md docs/DESIGN.md docs/API.md docs/ARCHITECTURE.md docs/DEVELOPMENT.md docs/OPERATIONS.md
git commit -m "docs: document unified model management runtime"
```

- [ ] **Step 8: Request final review and finish the branch**

Generate a whole-branch review package, run the `requesting-code-review` workflow, fix every Critical/Important finding with focused regression tests, rerun final verification, then use `finishing-a-development-branch`.
