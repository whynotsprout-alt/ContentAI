# ContentAI 项目部署与运维

> 适用版本：`0.6.0`（2026-07-27）
>
> 本文覆盖 Windows 本地开发、Docker Compose 联调、Ubuntu 22.04/24.04 LTS 生产部署、升级、备份、恢复和故障排查。命令与当前脚本保持一致。

## 1. 部署拓扑

生产环境由 `compose.yaml` 定义 10 个服务：

| 服务 | 职责 | 持久化/端口 |
| --- | --- | --- |
| `postgres` | 业务数据、模型配置、checkpoint、Outbox | `postgres-data`，生产不暴露宿主机端口 |
| `redis` | Celery broker、限流、SSE Streams | `redis-data`，生产不暴露宿主机端口 |
| `migration` | 执行初始 Alembic 迁移和 checkpoint setup | 一次性容器，成功后显示 `Exited (0)` 属正常现象 |
| `api` | FastAPI HTTP/SSE 服务 | 容器内 8000，不直接暴露 |
| `dispatcher` | 数据库 Outbox 投递 Celery | 无外部端口 |
| `agent-worker` | LangGraph 主执行 | 队列 `agent-executions`，默认并发 4 |
| `background-worker` | 标题、摘要、长期记忆后处理 | 队列 `agent-background`，默认并发 2 |
| `side-effect-worker` | 已批准副作用执行与幂等收据 | 队列 `agent-side-effects`，默认并发 1 |
| `beat` | 过期执行恢复、心跳、副作用对账 | 单实例 |
| `web` | 构建后的 Vue 静态站点和 `/api/` 反向代理 | 仅绑定 `127.0.0.1:${WEB_PORT:-5180}` |

API 镜像使用 Python 3.12 slim 和非 root `contentai` 用户；Web 使用 Node 22 构建后由 Nginx 1.27 提供服务。内置 Nginx 对 `/api/` 关闭缓冲并设置约 35 分钟超时，适配 SSE；带 hash 的 `/assets/` 缓存一年，入口页不缓存，其他前端路径回退 `index.html`。

生产 HTTPS 必须由宿主机 Nginx/Caddy 或云负载均衡终止，再代理到 `127.0.0.1:5180`。不要把 PostgreSQL、Redis 或 API 容器直接暴露到公网。

## 2. 配置准备

复制模板后填写所有占位符：

```bash
cp .env.example .env
chmod 600 .env
```

`.env` 不在发布包中自动生成，也不应提交到 Git。**不要在 `.env` 配置大模型 Base URL、模型名或模型 API Key**：部署完成后由管理员在“管理后台 > 模型管理”中探测、保存并启用；凭证会加密保存到 PostgreSQL。已移除的 `CONTENTAI_SEARCH__TRAFFIC_RELAY_API_KEY` 与 `CONTENTAI_SEARCH__TRAFFIC_RELAY_BASE_URL` 不再被读取，也不能恢复为模型配置。

### 2.1 必填配置

| 变量 | 用途与要求 |
| --- | --- |
| `CONTENTAI_ENV` | 本地源码开发用 `development`；生产用 `production` |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Compose PostgreSQL 初始化参数；密码使用长随机 URL-safe 值 |
| `CONTENTAI_DATABASE__URL` | SQLAlchemy PostgreSQL URL；Compose 内主机名为 `postgres` |
| `CONTENTAI_REDIS__URL` | Compose 默认 `redis://redis:6379/0` |
| `CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY` | 仅用于加密数据库内、由管理后台保存的模型 API Key 的 Fernet 密钥；它不是模型 API Key，所有应用进程必须一致 |
| `CONTENTAI_AUTH__BOOTSTRAP_ADMIN_EMAIL` | 首次启动管理员邮箱 |
| `CONTENTAI_AUTH__BOOTSTRAP_ADMIN_PASSWORD` | 首次启动管理员强密码；重启不会覆盖已有密码 |
| `CONTENTAI_SERVER__FRONTEND_ORIGINS` | 生产 Web Origin，例如 `https://content.example.com` |

生成 Fernet 密钥：

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

该密钥必须与数据库备份一起进入机密备份。当前代码没有在线密钥轮换或旧密钥回退；丢失/直接替换后，数据库内现有模型凭证将无法解密。首次部署后，用 Bootstrap 管理员登录“管理后台 > 模型管理”，填写模型 Base URL、模型 API Key、模型名和运行参数，先测试连接再保存并启用。

### 2.2 可选和容量配置

| 变量 | 默认/说明 |
| --- | --- |
| `WEB_PORT` | 5180，仅绑定宿主机回环地址 |
| `LOG_MAX_SIZE`, `LOG_MAX_FILES` | Docker json-file 日志轮转，默认 10m × 5 |
| `CONTENTAI_SEARCH__TIKHUB_API_KEY` | 选择抖音/B站/小红书/微博热点来源时需要 |
| `CONTENTAI_SEARCH__METASO_API_KEY` | Metaso 研究提供方；缺失时该提供方不可用 |
| `CONTENTAI_SEARCH__ANSPIRE_API_KEY` | Anspire 研究提供方；缺失时该提供方不可用 |
| `CONTENTAI_SERVER__TRUSTED_PROXY_CIDRS` | 逗号分隔的受控直接代理网段；为空时不信任 `X-Forwarded-For` |
| `CONTENTAI_SERVER__OUTBOX_MAX_AGE_SECONDS` | readiness 允许已发布但未领取 Outbox 的最大年龄，默认 30 秒 |
| `CONTENTAI_AGENT__WORKER_CONCURRENCY` | Agent Worker 并发，默认 4；应与数据库预算声明一致 |
| `CONTENTAI_AGENT__CELERY_QUEUE` | 默认 `agent-executions` |
| `CONTENTAI_AGENT__CELERY_BACKGROUND_QUEUE` | 默认 `agent-background` |
| `CONTENTAI_AGENT__CELERY_SIDE_EFFECT_QUEUE` | 默认 `agent-side-effects` |
| `CONTENTAI_AUTH__SESSION_COOKIE_NAME` | 默认 `contentai_session` |
| `CONTENTAI_AUTH__CSRF_COOKIE_NAME` | 默认 `contentai_csrf` |

数据库容量声明如下：

| 变量 | 默认值 | 含义 |
| --- | ---: | --- |
| `CONTENTAI_DATABASE__MAX_CONNECTIONS` | 100 | PostgreSQL `max_connections` 声明 |
| `CONTENTAI_DATABASE__RESERVED_CONNECTIONS` | 5 | 为管理/应急保留的连接 |
| `CONTENTAI_DATABASE__API_REPLICAS` | 1 | API 副本数 |
| `CONTENTAI_DATABASE__DISPATCHER_REPLICAS` | 1 | Dispatcher 副本数 |
| `CONTENTAI_DATABASE__AGENT_WORKER_REPLICAS` | 1 | Agent Worker 副本数 |
| `CONTENTAI_DATABASE__AGENT_WORKER_CONCURRENCY` | 4 | 用于数据库预算的 Agent Worker 并发声明 |
| `CONTENTAI_DATABASE__BACKGROUND_WORKER_REPLICAS` | 1 | Background Worker 副本数 |
| `CONTENTAI_DATABASE__BACKGROUND_WORKER_CONCURRENCY` | 2 | Background Worker 并发 |
| `CONTENTAI_DATABASE__SIDE_EFFECT_WORKER_REPLICAS` | 1 | Side-effect Worker 副本数 |
| `CONTENTAI_DATABASE__SIDE_EFFECT_WORKER_CONCURRENCY` | 1 | Side-effect Worker 并发 |
| `CONTENTAI_DATABASE__CHECKPOINT_POOL_SIZE` | 2 | 每个相关进程的 checkpoint 连接池预算 |
| `CONTENTAI_DATABASE__BEAT_REPLICAS` | 1 | Beat 副本数；生产应保持 1 |
| `CONTENTAI_DATABASE__MIGRATION_REPLICAS` | 1 | Migration 角色预算 |
| `CONTENTAI_DATABASE__OPS_REPLICAS` | 1 | 运维命令角色预算 |

程序会计算数据库连接总预算；结果必须严格低于 `70% × (MAX_CONNECTIONS - RESERVED_CONNECTIONS)`。默认 PostgreSQL 100 连接、保留 5，单机声明预算为 64。扩大 Worker 或副本数时先同步调整容量声明并核算 PostgreSQL 上限。`CONTENTAI_AGENT__WORKER_CONCURRENCY` 控制实际 Celery 并发，应与 `CONTENTAI_DATABASE__AGENT_WORKER_CONCURRENCY` 保持一致。

### 2.3 可信代理配置

浏览器首先访问宿主机反向代理，再进入 `web` 容器，最后由 Web Nginx 代理 API。API 只有在直接上游 IP 属于 `CONTENTAI_SERVER__TRUSTED_PROXY_CIDRS` 时才采用 `X-Forwarded-For` 中的客户端地址。

如果不配置，功能仍可用，但限流和审计可能把多个用户识别成同一个代理 IP。需要真实客户端 IP 时，只加入自己控制的、实际直连 API 的代理网段，不要使用 `0.0.0.0/0` 或不受控网段。

## 3. Windows 本地开发

### 3.1 前置条件

- Python 3.12；
- Node.js 22 与 npm；
- Docker Desktop（用于 PostgreSQL/完整联调）；
- PowerShell。

首次安装：

```powershell
Copy-Item .env.example .env
# 将 CONTENTAI_ENV 改为 development，并填写数据库、Fernet 和 Bootstrap 管理员等配置
tools/setup.ps1
```

`tools/setup.ps1` 创建 `.venv`、以 editable + dev extras 安装后端，并运行 `npm install`。只安装后端可传 `-SkipFrontend`。

### 3.2 源码进程开发

先启动隔离测试 PostgreSQL：

```powershell
tools/test-deps.ps1 up
```

该脚本当前只启动 PostgreSQL，不启动 Redis；需要完整异步/SSE 流程时应使用下一节的完整 Compose，或另行提供 Redis。

分别启动后端和前端：

```powershell
tools/dev-api.ps1
tools/dev-web.ps1
```

- API 默认：`http://127.0.0.1:8000`；API 文档：`/docs`；
- Web：`http://127.0.0.1:5180`，端口被占用会直接失败；
- 前端日志追加到 `logs/frontend.log`；
- `dev-api.ps1` 会把 `.env` 中 Compose 主机名 `postgres`、`redis` 改写为本机端口；
- 源码 API 端口变量是 `CONTENTAI_API_PORT`，热重载开关是 `CONTENTAI_API_RELOAD=true`；它们不同于 `compose.dev.yaml` 的 `API_PORT`。

关闭隔离数据库并删除其专用 volume：

```powershell
tools/test-deps.ps1 down
```

### 3.3 完整 Docker 联调

```powershell
tools/restart.ps1
```

该脚本组合 `compose.yaml` 与 `compose.dev.yaml`，构建并等待服务健康，验证 API readiness 和 Web。开发覆盖会额外绑定：

- PostgreSQL `127.0.0.1:${POSTGRES_PORT:-5432}`；
- Redis `127.0.0.1:${REDIS_PORT:-6379}`；
- API `127.0.0.1:${API_PORT:-8000}`。

注意：`compose.dev.yaml` 当前显式把 migration、api、dispatcher、agent-worker、background-worker、beat 设为 development，但没有单独覆盖 `side-effect-worker`。本地 `.env` 应将全局 `CONTENTAI_ENV=development`，确保所有服务环境一致。

`tools/restart.ps1` 只接受空数据库或 Alembic revision `202607210001`；发现其他 revision 会在改动容器前中止。

## 4. 测试与交付前验收

后端测试：

```powershell
tools/test.ps1
```

完整审查：

```powershell
tools/review.ps1
```

完整审查依次运行：

1. Ruff；
2. Vulture（最低置信度 90）；
3. Pytest；
4. 前端 Vitest；
5. 前端生产构建。

生产或联调环境还应检查：

```powershell
docker compose -f compose.yaml -f compose.dev.yaml ps
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/api/ready
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5180/
```

## 5. Ubuntu 首次生产部署

### 5.1 主机准备

建议使用全新 Ubuntu 22.04 或 24.04 LTS、64 位系统、可访问镜像仓库的非 root sudo 用户。安装 Docker Engine 和 Compose plugin 后确认：

```bash
docker version
docker compose version
```

将发布包解压到固定目录，例如 `/opt/contentai`，由专用运维用户持有：

```bash
sudo mkdir -p /opt/contentai
sudo chown "$USER":"$USER" /opt/contentai
tar -xzf contentai-0.6.0-ubuntu.tar.gz -C /opt/contentai --strip-components=1
cd /opt/contentai
cp .env.example .env
chmod 600 .env
```

填写 `.env` 后部署：

```bash
bash infra/ubuntu/deploy.sh
```

脚本依次验证 `.env` 和 Compose 配置、`build --pull`、`up --detach --wait` 并显示服务状态。首次启动会运行唯一初始迁移和 checkpoint setup，并创建不存在的 Bootstrap 管理员。

当前版本 V0.6.0 使用 `202607210001` 初始迁移；不存在 V0.4.x 或任意旧库的原地升级链路。旧环境必须保留原部署，或按业务批准的数据迁移方案导入全新 V0.6 数据库，不能仅替换镜像。

### 5.2 宿主机 HTTPS 反向代理

以宿主机 Nginx 为例：

```nginx
server {
    listen 443 ssl http2;
    server_name content.example.com;

    ssl_certificate     /etc/letsencrypt/live/content.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/content.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:5180;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_read_timeout 2100s;
        proxy_send_timeout 2100s;
    }
}
```

同时将 `CONTENTAI_SERVER__FRONTEND_ORIGINS=https://content.example.com`。变更 Origin、Cookie 安全属性或可信代理 CIDR 后重建/重启应用服务并重新验证登录、CSRF 和 SSE。

## 6. 发布包制作

Windows/PowerShell 下：

```powershell
tools/package-ubuntu.ps1
```

输出：

- `dist/contentai-0.6.0-ubuntu.tar.gz`；
- 同名 `.sha256`；
- 包内 `release-manifest.json`，包含版本和 Git commit。

版本来自已提交 HEAD 的 `pyproject.toml`，可选 `-Version` 必须与它完全一致。脚本使用 `git archive`，只打包当前提交中的受控文件；未提交的工作区改动、测试和本地 `.env` 不会进入包。发包前必须提交预期变更并运行完整审查。

Linux 上校验：

```bash
sha256sum -c contentai-0.6.0-ubuntu.tar.gz.sha256
tar -tzf contentai-0.6.0-ubuntu.tar.gz | head
```

## 7. 日常运维

### 7.1 健康检查

```bash
bash infra/ubuntu/health.sh
```

脚本显示所有容器，并在 API 容器内访问 `/api/ready`。readiness 只有在以下条件全部满足时成功：

- 应用启动完成、数据库可查询；
- Alembic revision 是当前 head；
- 四张 checkpoint 表存在；
- Redis 可 ping；
- Dispatcher、agent/background/side-effect 三个 Worker 队列心跳在默认 30 秒内；
- 已发布但尚未被 Worker 领取的最老 Outbox 不超过配置年龄。

响应会显示 pending Outbox 数量供观察，但当前代码不以 pending 数量阈值直接判失败。`migration` 为 `Exited (0)` 不代表故障。

### 7.2 查看日志

```bash
docker compose logs --since 30m api
docker compose logs --since 30m dispatcher agent-worker background-worker side-effect-worker beat
docker compose logs -f --tail 200 web
```

应用日志写 stdout/stderr；Docker json-file 按 `.env` 中大小和文件数轮转。排障时优先用 `request_id`、`execution_id`、`session_id` 串联 API、Dispatcher 和 Worker 日志，不要复制包含 Cookie、CSRF、Authorization 或 `.env` 的内容。

### 7.3 备份

```bash
bash infra/ubuntu/backup.sh
# 或指定目录
bash infra/ubuntu/backup.sh /srv/contentai-backups
```

脚本使用 `pg_dump --clean --if-exists | gzip -9`，生成 UTC 时间命名的 `.sql.gz` 及 `.sha256`。数据库备份之外，还要分别备份：

- `CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY`；
- 生产 `.env` 中其他密钥（通过机密管理系统，不与 SQL 包放在同一公开位置）；
- 对应发布包和 `release-manifest.json`。

Redis 不是消息最终事实源，通常不作为业务恢复入口；但恢复后可能丢失未过期 SSE 和队列瞬时状态，应依靠数据库恢复逻辑重新对账。

### 7.4 恢复

恢复会覆盖当前数据库。先校验摘要：

```bash
sha256sum -c /srv/contentai-backups/contentai-20260727T000000Z.sql.gz.sha256
```

当前 `restore.sh` 停止 api、dispatcher、agent-worker、background-worker、beat，但没有停止 `side-effect-worker`。为避免恢复期间副作用 Worker 继续访问数据库，执行脚本前必须手动停止它：

```bash
docker compose stop side-effect-worker
CONTENTAI_CONFIRM_RESTORE=yes bash infra/ubuntu/restore.sh \
  /srv/contentai-backups/contentai-20260727T000000Z.sql.gz
bash infra/ubuntu/health.sh
```

脚本本身不会读取 `.sha256`，所以摘要校验必须由运维人员先执行。脚本会解压导入 SQL、重新运行 migration，然后启动并等待全部服务。恢复后验证：管理员登录、模型配置可解密、普通对话、热点/研究工具和 SSE。

### 7.5 升级

```bash
bash infra/ubuntu/upgrade.sh
```

脚本先备份，再验证 Compose、拉取基础镜像并构建、更新服务、移除孤儿容器，最后运行健康检查。升级前仍应阅读目标版本迁移说明；当前 V0.5 只有全新数据库基线，不支持从旧 schema 直接升级。

## 8. 常见故障定位

### `/api/ready` 返回 503

1. `docker compose ps --all` 检查 postgres、redis、api、dispatcher、三个 Worker 和 beat；
2. 查看 readiness JSON 的具体失败项；
3. `docker compose logs migration api dispatcher agent-worker background-worker side-effect-worker beat`；
4. 若是 Worker 心跳缺失，核对队列名和服务进程；
5. 若是 Outbox 超龄，优先修复 Dispatcher/Agent Worker，不要直接删除 Outbox；
6. 若迁移不一致，停止升级，不要手工修改 `alembic_version`。

### API 启动时报数据库容量错误

副本数、Worker 并发或 checkpoint pool 声明超过 PostgreSQL 安全预算。同步调整 `CONTENTAI_DATABASE__*` 声明和数据库 `max_connections`，确保计算值严格低于 70% 可用连接数；不要只绕过校验。

### 登录正常但写请求 403

确认浏览器同时收到 Session 和 CSRF Cookie，请求包含 `X-CSRF-Token`，公网使用 HTTPS，Origin 与 `CONTENTAI_SERVER__FRONTEND_ORIGINS` 完全一致。经多层代理时检查 Host 和 `X-Forwarded-Proto`。

### 所有用户共享一个限流桶

API 未信任实际直连代理，或可信 CIDR 配错。确认容器网络中的直接代理地址后，只将该受控网段加入 `CONTENTAI_SERVER__TRUSTED_PROXY_CIDRS`。

### 对话一直停在 dispatching / waiting_worker

检查 Dispatcher、Redis、`agent-executions` Worker 心跳和日志；确认所有进程使用相同数据库、Redis、队列名和 Fernet 密钥。不要通过直接改执行状态“修复”，Beat 和 lease 恢复会按既定重试语义处理。

### 流式输出断开但任务仍在运行

这是支持的降级路径。前端应轮询 status；检查 Redis 容量、SSE 代理缓冲/超时和 `streaming_degraded` 标志。不要把 Redis Stream 当作恢复最终消息的数据库。

### 模型配置探测失败

检查 Base URL 必须符合 HTTPS/企业内网规则、DNS 可解析、模型支持原生 tool call 与 JSON Schema。后端关闭环境代理和重定向，不会透传远端详细正文；用 request ID 查服务端安全日志。

### 研究无结果或证据校验失败

分别确认 Metaso/Anspire 密钥、网络和限额。系统不会抓网页正文，也不会在引用不完整时继续生成研究结论；这是设计约束，不应通过放宽引用校验绕过。

## 9. 交接检查清单

- 已记录 Git commit、版本、发布时间和发布包 SHA-256；
- `.env` 与 Fernet/搜索/管理员密钥已进入机密管理系统；
- 数据库和密钥有离机备份，并实际演练过恢复；
- 域名、TLS、CORS Origin 和可信代理 CIDR 已验证；
- `/api/ready`、Web、登录、改密、管理员模型探测均通过；
- Agent 创建/新版本、会话发送、SSE 降级恢复、取消、人工确认均通过；
- 热点来源与 Metaso/Anspire 研究链路按生产密钥验证；
- 日志轮转、磁盘/数据库容量、备份保留和告警负责人已明确；
- 已知当前恢复脚本不自动停止 `side-effect-worker`、不自动校验 SHA-256，运维手册已纳入人工步骤；
- 接手人已阅读 `FRONTEND_DESIGN.md`、`BACKEND_DESIGN.md` 与本文。
