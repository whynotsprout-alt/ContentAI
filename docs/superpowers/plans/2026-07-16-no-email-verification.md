# 取消邮箱验证流程 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让注册用户无需邮箱验证即可登录，同时保留密码重置邮件与两个返回 410 的兼容验证端点。

**Architecture:** 后端将所有新账号直接持久化为活跃状态，并在成功登录时修复历史待验证账号；验证端点保留路由但不执行任何令牌或邮件副作用。Web 端删除验证路由和 UI 分支，只保留注册、登录及密码重置；文档和配置校验以无验证流程为准。

**Tech Stack:** Python 3.12、FastAPI、SQLModel、pytest、Vue 3、TypeScript、Vue Router、Vitest。

## Global Constraints

- 新注册用户必须为 `active` 且有 `email_verified_at`；不生成验证令牌或验证邮件。
- 禁用账号仍不可登录或被兼容逻辑恢复。
- 忘记/重置密码流程、一次性令牌和撤销既有会话行为不得改变。
- `POST /api/auth/verify-email` 与 `POST /api/auth/resend-verification` 必须保留并返回 `410 Gone`，且无副作用。
- 生产环境配置 `CONTENTAI_AUTH__REQUIRE_EMAIL_VERIFICATION=true` 必须拒绝启动；SMTP 不再是生产启动前置条件。
- 不删除数据库中的验证历史字段或令牌记录，也不修改此前存在的未提交文件。

---

## File Structure

- `apps/api/src/core/config/auth.py`：认证配置默认值，默认关闭验证。
- `apps/api/src/core/config/settings.py`：认证及生产启动配置校验。
- `apps/api/src/services/auth_service.py`：注册、旧账号登录规范化、密码重置与遗留验证代码边界。
- `apps/api/src/api/auth.py`：注册响应及两个保留验证端点的 `410` 合约。
- `apps/api/src/core/security.py`、`apps/api/src/models/user.py`：认证会话与用户默认状态不再将邮箱验证字段作为门槛。
- `apps/api/src/services/admin_service.py`、`apps/web/src/views/AdminUsersView.vue`：允许已启用但缺少历史验证时间的用户取得管理员发送的重置邮件。
- `apps/api/tests/test_auth_admin.py`：认证行为与遗留端点集成测试。
- `apps/api/tests/test_api_contract.py`：保留 35 个 OpenAPI 业务端点的契约。
- `apps/web/src/router.ts`、`apps/web/src/views/AuthView.vue`、`apps/web/src/services/api.ts`：移除验证页面和客户端调用。
- `apps/web/tests/frontend-plan.spec.mjs`、`apps/web/tests/api-client-contract.spec.mjs`：前端无验证流程契约。
- `.env.example`、`docs/API.md`、`docs/DEVELOPMENT.md`、`docs/OPERATIONS.md`、`docs/DESIGN.md`：生产设置与支持的认证流程说明。

### Task 1: 后端取消验证前置条件并保留 410 兼容端点

**Files:**
- Modify: `apps/api/src/core/config/auth.py:12-18`
- Modify: `apps/api/src/core/config/settings.py:228-266`
- Modify: `apps/api/src/core/security.py:55-68`
- Modify: `apps/api/src/models/user.py:19-25`
- Modify: `apps/api/src/services/auth_service.py:16-101, 125-173, 220-290`
- Modify: `apps/api/src/api/auth.py:63-123`
- Modify: `apps/api/src/services/admin_service.py:88-130`
- Modify: `apps/api/tests/test_auth_admin.py:20-170`
- Test: `apps/api/tests/test_api_contract.py`

**Interfaces:**
- Consumes: `AuthSettings.require_email_verification: bool`, `AuthService.register()`, `AuthService.login()`, `AuthService.forgot_password()`.
- Produces: registration response `"Registration successful. You can sign in now."`; legacy verification routes returning HTTP `410` and detail `"Email verification has been retired."`.

- [x] **Step 1: Write failing backend tests**

Replace the verification helper with a registration helper and add assertions that make the new policy explicit:

```python
def _register(client: TestClient, email: str, password: str) -> None:
    response = client.post("/api/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201
    assert response.json()["message"] == "Registration successful. You can sign in now."

def test_registration_logs_in_without_email_delivery():
    app = auth_app()
    with TestClient(app) as client:
        _register(client, "person@example.com", "correct horse battery")
        assert app.state.mailer.outbox == []
        _login(client, "person@example.com", "correct horse battery")

def test_legacy_pending_user_is_activated_after_valid_login():
    app = auth_app()
    with TestClient(app) as client:
        _register(client, "legacy@example.com", "correct horse battery")
        with Session(get_engine(app.state.settings)) as session:
            user = session.exec(
                select(AppUser).where(AppUser.email_normalized == "legacy@example.com")
            ).one()
            user.status = "pending_verification"
            user.email_verified_at = None
            session.add(user)
            session.commit()
        _login(client, "legacy@example.com", "correct horse battery")
        with Session(get_engine(app.state.settings)) as session:
            user = session.exec(
                select(AppUser).where(AppUser.email_normalized == "legacy@example.com")
            ).one()
            assert user.status == "active"
            assert user.email_verified_at is not None

def test_verification_endpoints_are_retired_without_side_effects():
    app = auth_app()
    with TestClient(app) as client:
        assert client.post("/api/auth/verify-email", json={"token": "old-token"}).status_code == 410
        assert client.post("/api/auth/resend-verification", json={"email": "person@example.com"}).status_code == 410
        assert app.state.mailer.outbox == []
```

Also add a settings test that accepts production `mail_backend="console"` when verification is disabled and raises `ValueError` when `env="production"` plus `require_email_verification=True`.

```python
def _production_settings(**auth_overrides: object) -> Settings:
    return Settings(
        env="production",
        server={"frontend_origins": "https://content.example.com"},
        database={"url": "postgresql+psycopg://postgres:postgres@db/contentai"},
        search={"traffic_relay_api_key": "test-relay-key"},
        auth={
            "public_base_url": "https://content.example.com",
            "bootstrap_admin_emails": ["admin@example.com"],
            **auth_overrides,
        },
    )

def test_production_rejects_reenabling_email_verification():
    assert _production_settings(mail_backend="console").auth.mail_backend == "console"
    with pytest.raises(ValueError, match="must remain disabled in production"):
        _production_settings(require_email_verification=True)
```

- [x] **Step 2: Run the focused test to verify it fails**

Run: `uv run pytest apps/api/tests/test_auth_admin.py -q`

Expected: failures because registration sends a verification email, pending users cannot log in, and the two endpoints return their former success/error behavior.

- [x] **Step 3: Implement the smallest policy change**

Make the configuration and services obey the following concrete rules:

```python
# AuthSettings
require_email_verification: bool = False

# AuthService.register
user = AppUser(
    email=normalized,
    email_normalized=normalized,
    password_hash=self.password_hash.hash(password),
    role=role,
    status="active",
    email_verified_at=now,
)

# AuthService.login, after disabled-user rejection
if user.status == "pending_verification" or user.email_verified_at is None:
    user.status = "active"
    user.email_verified_at = now

# AuthService.forgot_password
if user is None or user.status == "disabled":
    return
raw = self.issue_action_token(session, user=user, purpose=RESET_PURPOSE)
self.send_password_reset(user, raw)
```

Remove `VERIFY_PURPOSE`, `verify_email`, `resend_verification`, `send_verification`, verification-lifetime branching and resend limit usage from the normal service code. In `settings.py`, reject only a true production verification flag, retain HTTPS/base URL and bootstrap-admin validation, and remove the production SMTP requirement. In `auth.py`, make both retained route handlers raise `HTTPException(status_code=410, detail="Email verification has been retired.")` before touching sessions, rate limits, tokens, or the mailer. Keep the routes in OpenAPI. Remove the verification timestamp predicate from session authentication, make the model's default user status `active`, and change administrator reset eligibility to require only a non-disabled account.

- [x] **Step 4: Run focused backend tests to verify they pass**

Run: `uv run pytest apps/api/tests/test_auth_admin.py apps/api/tests/test_api_contract.py -q`

Expected: all selected tests pass and OpenAPI still lists 35 operations.

- [x] **Step 5: Commit the backend task**

```bash
git add apps/api/src/core/config/auth.py apps/api/src/core/config/settings.py apps/api/src/core/security.py apps/api/src/models/user.py apps/api/src/services/auth_service.py apps/api/src/api/auth.py apps/api/src/services/admin_service.py apps/api/tests/test_auth_admin.py apps/api/tests/test_api_contract.py
git commit -m "feat: retire email verification flow"
```

### Task 2: 移除 Web 验证路径与重发界面

**Files:**
- Modify: `apps/web/src/router.ts:6-24`
- Modify: `apps/web/src/views/AuthView.vue:1-145, 171-191`
- Modify: `apps/web/src/services/api.ts:416-447`
- Modify: `apps/web/src/views/AdminUsersView.vue:46, 131, 227`
- Modify: `apps/web/src/styles/auth.css:201-203`
- Modify: `apps/web/tests/frontend-plan.spec.mjs:43-56`
- Modify: `apps/web/tests/api-client-contract.spec.mjs:10-17`

**Interfaces:**
- Consumes: `authApi.register(email, password): Promise<{ message: string }>` and existing login/reset methods.
- Produces: `/register` submission that displays the success message then uses `router.replace('/login')`; no `/verify-email` route or verification client methods.

- [x] **Step 1: Write failing frontend contract tests**

Replace the current verification-flow assertions with:

```js
it('registers without exposing verification or resend UI', async () => {
  const [auth, router, api] = await Promise.all([
    read('../src/views/AuthView.vue'),
    read('../src/router.ts'),
    read('../src/services/api.ts')
  ]);
  expect(auth).toContain("await router.replace('/login')");
  expect(auth).not.toMatch(/verify-email|registrationSent|resendVerification|resendCooldown/);
  expect(router).not.toContain("path: '/verify-email'");
  expect(api).not.toMatch(/verifyEmail|resendVerification/);
});
```

Remove `verifyEmail` and `resendVerification` from `authApi` in `api-client-contract.spec.mjs`; retain all password reset methods as required callers.

- [x] **Step 2: Run the focused frontend tests to verify they fail**

Run: `npm test -- --run tests/frontend-plan.spec.mjs tests/api-client-contract.spec.mjs`

Working directory: `apps/web`

Expected: failures because the router, view, and client still expose verification flow identifiers.

- [x] **Step 3: Implement the smallest UI and client change**

Delete the `verify-email` route, `isVerify`, lifecycle verification calls, resend timer/function/state, email-sent and verification template branches, plus unused `CheckCircle2`, `Mail`, `RefreshCw`, `onMounted`, `onBeforeUnmount`, and `watch` imports. After `authApi.register`, clear password fields, set the message, then call `await router.replace('/login')`. Remove the two verification client methods. Change administrator reset eligibility and its explanatory title to require only `selected.status !== 'disabled'`; remove the obsolete `.email-sent .auth-submit` rule when no selector remains.

- [x] **Step 4: Run frontend tests and type/build verification**

Run: `npm test -- --run tests/frontend-plan.spec.mjs tests/api-client-contract.spec.mjs && npm run build`

Working directory: `apps/web`

Expected: both contract files pass and `vue-tsc -b && vite build` exits with code 0.

- [x] **Step 5: Commit the Web task**

```bash
git add apps/web/src/router.ts apps/web/src/views/AuthView.vue apps/web/src/services/api.ts apps/web/src/views/AdminUsersView.vue apps/web/src/styles/auth.css apps/web/tests/frontend-plan.spec.mjs apps/web/tests/api-client-contract.spec.mjs
git commit -m "feat: remove email verification UI"
```

### Task 3: 更新支持边界、示例配置与发布文档

**Files:**
- Modify: `.env.example:82-103`
- Modify: `docs/API.md:1-27, 末尾契约约束`
- Modify: `docs/DEVELOPMENT.md:10`
- Modify: `docs/OPERATIONS.md:16-20`
- Modify: `docs/DESIGN.md:删除、安全与限流段落`
- Test: `apps/api/tests/test_api_contract.py`
- Test: `apps/web/tests/frontend-plan.spec.mjs`

**Interfaces:**
- Consumes: 已完成的 API 路由、生产配置校验与 Web 路由。
- Produces: 明确的“注册即登录资格、验证端点 410、SMTP 仅用于密码重置”运维合同。

- [x] **Step 1: Confirm the implemented contract before documenting it**

Run: `uv run pytest apps/api/tests/test_api_contract.py apps/api/tests/test_auth_admin.py -q` and `npm test -- --run tests/frontend-plan.spec.mjs tests/api-client-contract.spec.mjs`

Working directory for the second command: `apps/web`

Expected: the retained verification routes appear in OpenAPI but return `410`; the Web contract has no verification route or client calls.

- [x] **Step 2: Update exact documentation and configuration language**

Remove `CONTENTAI_AUTH__VERIFICATION_HOURS`, `CONTENTAI_AUTH__RESEND_VERIFICATION_LIMIT`, and `CONTENTAI_AUTH__RESEND_VERIFICATION_WINDOW_SECONDS` from `.env.example`; set `CONTENTAI_AUTH__REQUIRE_EMAIL_VERIFICATION=false` and label it as an enforced disabled compatibility flag. Keep `PUBLIC_BASE_URL` and SMTP variables, but explain SMTP is needed to deliver password resets rather than to create or access accounts. In `docs/API.md`, retain the two verification rows as `410 Gone / 已停用兼容接口` with no caller, and describe register as immediately eligible for login. Update development, operations, and architecture documentation to remove production verification/SMTP startup requirements, retain HTTPS and password-reset guidance, and describe legacy pending-account normalization.

- [x] **Step 3: Run documentation-adjacent contract tests**

Run: `uv run pytest apps/api/tests/test_api_contract.py -q` and `npm test -- --run tests/frontend-plan.spec.mjs tests/api-client-contract.spec.mjs`

Working directory for the second command: `apps/web`

Expected: all selected contracts pass with no verification UI or active verification endpoint behavior reintroduced.

- [x] **Step 4: Commit documentation and plan updates**

```bash
git add .env.example docs/API.md docs/DEVELOPMENT.md docs/OPERATIONS.md docs/DESIGN.md docs/superpowers/plans/2026-07-16-no-email-verification.md
git commit -m "docs: document retired email verification"
```

## Final Verification

- [x] Run `uv run pytest -q` from the repository root.
- [x] Run `uv run ruff check apps/api/src apps/api/tests` from the repository root.
- [x] Run `npm test && npm run build` from `apps/web`.
- [x] Run `git diff --check` and `git status --short`; confirm only the intended files changed and previously existing unrelated changes remain unstaged.
- [x] Run `rg -n -i "resendVerification|verifyEmail|path: '/verify-email'|require_email_verification: bool = True" apps/web/src apps/web/tests apps/api/src .env.example docs` and inspect every remaining match; only the retained 410 compatibility endpoints and their documentation may reference verification.
