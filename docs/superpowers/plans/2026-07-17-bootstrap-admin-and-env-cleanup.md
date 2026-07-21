# 启动引导管理员与环境变量整理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 首次启动时根据环境变量创建可登录的默认管理员，并移除已下线邮件功能的配置残留。

**Architecture:** `AuthSettings` 承担默认管理员邮箱和密码的受控配置，`AuthService` 提供幂等的用户创建操作。应用 lifespan 在数据库初始化后调用该操作，因此不需要数据库迁移；配置和密码哈希复用现有实现。环境文件仅保留仍被应用或 Compose 使用的变量，示例文件不含任何真实凭据。

**Tech Stack:** Python 3.12、FastAPI lifespan、Pydantic Settings、SQLModel、pwdlib、pytest、Docker Compose。

## Global Constraints

- 默认管理员仅在邮箱不存在时创建，绝不在重启时覆盖用户密码、角色或状态。
- 默认管理员必须是 `active`、邮箱已验证、角色为 `admin`。
- 密码只能用于哈希入库，不可写入日志、HTTP 响应、测试断言或文档。
- 邮件验证和密码重置功能保持禁用；不得恢复其环境变量或 Mailer 依赖。
- 不新增数据库迁移，复用现有 `appuser` 表。

---

### Task 1: 默认管理员配置与认证服务

**Files:**
- Modify: `apps/api/src/core/config/auth.py`
- Modify: `apps/api/src/core/config/settings.py`
- Modify: `apps/api/src/services/auth_service.py`
- Test: `apps/api/tests/test_auth_admin.py`

**Interfaces:**
- Consumes: `AuthSettings`、`Settings._validate_auth()`、`AppUser`、`AuthService.password_hash`。
- Produces: `AuthService.bootstrap_default_admin(session: Session) -> AppUser | None`；`AuthSettings.bootstrap_admin_email: str`；`AuthSettings.bootstrap_admin_password: SecretStr`。

- [ ] **Step 1: 写出失败的配置和服务测试**

~~~python
def test_startup_bootstraps_an_active_admin_once():
    app = create_app(Settings(env="test", database={"url": TEST_DATABASE_URL}, auth={
        "bootstrap_admin_email": "admin@example.com",
        "bootstrap_admin_password": "bootstrap password 123",
    }))
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={
            "email": "admin@example.com", "password": "bootstrap password 123",
        }).status_code == 200
~~~

- [ ] **Step 2: 运行该测试并确认失败**

Run: `uv run python -m pytest apps/api/tests/test_auth_admin.py::test_startup_bootstraps_an_active_admin_once -q`

Expected: FAIL，因为配置字段和 `bootstrap_default_admin` 均不存在，应用尚未创建该用户。

- [ ] **Step 3: 实现最小配置和幂等创建逻辑**

~~~python
class AuthSettings(BaseModel):
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: SecretStr = SecretStr("")

def bootstrap_default_admin(self, session: Session) -> AppUser | None:
    email = self.normalize_email(self.settings.auth.bootstrap_admin_email)
    if not email or self.get_user_by_email(session, email) is not None:
        return None
    now = utcnow()
    user = AppUser(
        email=email, email_normalized=email,
        password_hash=self.password_hash.hash(
            self.settings.auth.bootstrap_admin_password.get_secret_value()
        ),
        role="admin", status="active", email_verified_at=now,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
~~~

`Settings._validate_auth()` 将邮箱规范化；若只设置了邮箱或密码之一则抛出明确的 `ValueError`，生产环境要求完整的一对默认管理员凭据。

- [ ] **Step 4: 运行认证测试并确认通过**

Run: `uv run python -m pytest apps/api/tests/test_auth_admin.py -q`

Expected: PASS，且既有邮箱 bootstrap 注册测试仍通过。

- [ ] **Step 5: Commit**

~~~bash
git add apps/api/src/core/config/auth.py apps/api/src/core/config/settings.py apps/api/src/services/auth_service.py apps/api/tests/test_auth_admin.py
git commit -m "feat: bootstrap default administrator"
~~~

### Task 2: 应用启动接线与幂等性测试

**Files:**
- Modify: `apps/api/src/api/app.py`
- Modify: `apps/api/tests/test_main.py`
- Modify: `apps/api/tests/test_auth_admin.py`

**Interfaces:**
- Consumes: `AuthService.bootstrap_default_admin(session)` 与 `db.session.get_engine(settings)`。
- Produces: 生命周期在 `init_database(settings)` 后且应用 ready 前确保默认管理员存在。

- [ ] **Step 1: 写出重复启动不覆盖账号的失败测试**

~~~python
def test_bootstrap_does_not_change_an_existing_user():
    settings = Settings(env="test", database={"url": TEST_DATABASE_URL}, auth={
        "bootstrap_admin_email": "admin@example.com",
        "bootstrap_admin_password": "new bootstrap password",
    })
    app = create_app(settings)
    with Session(get_engine(settings)) as session:
        existing = AppUser(email="admin@example.com", email_normalized="admin@example.com",
            password_hash=AuthService(settings).password_hash.hash("existing password"),
            role="user", status="disabled")
        session.add(existing)
        session.commit()
    with TestClient(app):
        pass
    with Session(get_engine(settings)) as session:
        user = session.exec(select(AppUser).where(AppUser.email_normalized == "admin@example.com")).one()
        assert user.role == "user"
        assert user.status == "disabled"
        assert AuthService(settings).password_hash.verify("existing password", user.password_hash)
~~~

- [ ] **Step 2: 运行该测试并确认失败**

Run: `uv run python -m pytest apps/api/tests/test_auth_admin.py::test_bootstrap_does_not_change_an_existing_user -q`

Expected: FAIL，因为生命周期尚未调用 bootstrap 方法。

- [ ] **Step 3: 在数据库初始化后接线**

~~~python
init_database(app.state.settings)
_register_services(app)
with Session(get_engine(app.state.settings)) as session:
    app.state.auth_service.bootstrap_default_admin(session)
app.state.ready = True
~~~

导入 `Session` 和 `get_engine`，并保持 bootstrap 调用在运行时服务注册完成、ready 标志置位之前。

- [ ] **Step 4: 运行生命周期和认证测试并确认通过**

Run: `uv run python -m pytest apps/api/tests/test_auth_admin.py apps/api/tests/test_main.py -q`

Expected: PASS，重复启动后仅存在一个用户，既有用户不被改写。

- [ ] **Step 5: Commit**

~~~bash
git add apps/api/src/api/app.py apps/api/tests/test_auth_admin.py apps/api/tests/test_main.py
git commit -m "feat: bootstrap admin during application startup"
~~~

### Task 3: 整理环境文件、文档与部署验证

**Files:**
- Modify: `.env`
- Modify: `.env.example`
- Modify: `docs/DEVELOPMENT.md`
- Modify: `docs/OPERATIONS.md`
- Test: `apps/api/tests/test_auth_admin.py`
- Test: `apps/api/tests/test_main.py`
- Test: `apps/api/tests/test_api.py`
- Test: `apps/api/tests/test_api_contract.py`

**Interfaces:**
- Consumes: `CONTENTAI_AUTH__BOOTSTRAP_ADMIN_EMAIL`、`CONTENTAI_AUTH__BOOTSTRAP_ADMIN_PASSWORD` 以及当前 Compose 配置。
- Produces: 分组后的本地 `.env` 与不含真实密钥的 `.env.example`；运行实例可用默认管理员登录。

- [ ] **Step 1: 写出配置配对验证测试**

~~~python
def test_bootstrap_admin_requires_email_and_password_together():
    with pytest.raises(ValidationError, match="BOOTSTRAP_ADMIN_PASSWORD"):
        Settings(env="test", database={"url": TEST_DATABASE_URL}, auth={
            "bootstrap_admin_email": "admin@example.com",
        })
~~~

- [ ] **Step 2: 运行配置测试并确认失败**

Run: `uv run python -m pytest apps/api/tests/test_auth_admin.py::test_bootstrap_admin_requires_email_and_password_together -q`

Expected: FAIL，直到设置验证实现配对检查。

- [ ] **Step 3: 完成环境文件和文档整理**

将 `.env` 的现有有效配置按以下顺序分组，删除已废弃的 `REQUIRE_EMAIL_VERIFICATION`、`MAIL_BACKEND` 与邮件变量，以及重复的搜索变量；添加实际默认管理员邮箱与密码。将 `.env.example` 使用占位密码：

~~~dotenv
# Authentication: created only when this email is absent; never overwritten on restart.
CONTENTAI_AUTH__BOOTSTRAP_ADMIN_EMAIL=admin@example.com
CONTENTAI_AUTH__BOOTSTRAP_ADMIN_PASSWORD=replace-with-a-long-unique-password
CONTENTAI_AUTH__BOOTSTRAP_ADMIN_EMAILS=["admin@example.com"]
~~~

在开发和运维文档中说明首次启动语义、替换示例密码、启动成功后可从部署密钥管理中移除初始密码，以及邮件/重置 API 仍为 410。

- [ ] **Step 4: 执行静态检查和全部相关测试**

Run: `uv run ruff check apps/api/src apps/api/tests && uv run python -m pytest apps/api/tests/test_auth_admin.py apps/api/tests/test_main.py apps/api/tests/test_api.py apps/api/tests/test_api_contract.py apps/api/tests/test_deep_research_workflow.py apps/api/tests/test_search.py -q && docker compose config --quiet && git diff --check`

Expected: Ruff 无错误、所有测试通过、Compose 配置有效、diff 无空白错误。

- [ ] **Step 5: 重建并验证运行实例**

~~~bash
docker compose up --detach --build --wait
docker compose ps
~~~

Expected: `api`、`web`、Redis、PostgreSQL 与 worker 服务健康；默认管理员可通过登录接口认证，且启动日志不包含明文密码。

- [ ] **Step 6: Commit**

~~~bash
git add .env.example docs/DEVELOPMENT.md docs/OPERATIONS.md apps/api/tests/test_auth_admin.py
git commit -m "docs: document bootstrap administrator setup"
~~~
