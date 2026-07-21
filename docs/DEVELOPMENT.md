# 开发指南

## 前置条件

- Python 3.12
- Node.js 22（与 Web 容器一致）
- Docker Desktop（推荐，用于 PostgreSQL、Redis 和完整环境）
- 从 `.env.example` 复制出本地 `.env`，填入实际的数据库、搜索服务密钥和 Fernet 主密钥

`.env` 是本地密钥文件，不应提交。必须为 `CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY` 生成一个新的 Fernet key，例如 `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`；将其保存到本地密钥管理中并安全备份。开发和生产环境在该值缺失或格式错误时都会拒绝启动，所有应用进程必须使用同一个值。不要把生成结果粘贴到终端记录、文档或版本库，也不要尝试在线轮换该主密钥。

生产环境还必须配置默认管理员邮箱和初始密码、前端来源及实际启用的搜索服务密钥。Traffic Relay 的 Key 仅服务于搜索，不是模型配置 Key。首次启动后使用管理员账号访问 `/admin/models` 创建唯一的 active OpenAI-compatible Base URL、API Key 和模型名；系统不会读取旧模型环境变量或提供数据库配置回退。服务在启动时仅当该邮箱不存在才创建已激活的管理员；后续重启绝不会覆盖其密码、角色或状态。邮箱验证和邮件密码重置功能当前均已停用，不需要配置邮件服务。

## 初始化

```powershell
Copy-Item .env.example .env
tools/setup.ps1
```

该脚本创建 `.venv`、以 editable 模式安装根目录 Python 项目，并安装 `apps/web` 的 Node 依赖。

## 本地开发

完整容器环境最接近生产；开发时叠加 loopback 端口 override：

```powershell
docker compose -f compose.yaml -f compose.dev.yaml up --detach --build --wait
```

如需分别启动前后端开发服务器，先保证 `.env` 指向可用 PostgreSQL 与 Redis，再在不同终端运行：

```powershell
tools/dev-api.ps1
tools/dev-web.ps1
```

默认地址：

- API：`http://127.0.0.1:8000`，OpenAPI 在 `/docs`
- Web：`http://127.0.0.1:5180`

`tools/dev-api.ps1` 自动设置 `PYTHONPATH=apps/api/src`。若要使用 API 自动重载，可在 `.env` 设置 `CONTENTAI_API_RELOAD=true`。

## 测试与质量检查

```powershell
# 后端测试
tools/test.ps1

# Ruff + 后端测试 + Web production build
tools/review.ps1

# 启动、验证并清理隔离测试 PostgreSQL
tools/test-deps.ps1 up
tools/review.ps1
tools/test-deps.ps1 down

# 只在 Web 目录执行前端测试
Push-Location apps/web
npm.cmd test
Pop-Location
```

后端测试环境必须使用独立名称中含 `test` 的 PostgreSQL 数据库，并设置 `CONTENTAI_ENV=test`；应用配置会拒绝把测试运行到非测试数据库。

`tools/test-deps.ps1` 创建名为 `contentai-test-deps` 的隔离 Compose 项目，只使用一次性的 PostgreSQL volume。它仅用于本地验证的测试依赖，绝不能用于生产服务。

## 代码组织约定

- Python import 根为 `apps/api/src`；应用入口是 `api.app:app`。
- Web 源码、构建配置和测试都位于 `apps/web`；不要把生成的 `dist/` 或 `node_modules/` 加入版本控制。
- 当前只保留 `apps/api/src/contentai_migrations/versions/202607210001_v050_initial_schema.py` 单一初始迁移；LangGraph checkpoint/store 不进入 Alembic 自动生成结果。
- 面向用户的内容只能通过 Assistant 消息交付，不新增研究包、稿件或工作流阶段模型。
- 文件修改后优先运行与改动范围相符的检查；涉及构建、路径或容器时运行 `tools/review.ps1` 与 `docker compose config --quiet`。
