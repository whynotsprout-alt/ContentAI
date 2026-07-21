# Ubuntu 运行与部署手册

## 支持范围

生产环境面向受支持的 Ubuntu LTS、Docker Engine 与 Docker Compose v2。V0.5.0 只支持全新空数据库安装，不支持任何旧数据库或旧 Alembic revision 原地升级。部署时必须创建新的 PostgreSQL 数据库或数据卷；迁移服务不会自动删除旧数据库。

生产 `compose.yaml` 启动十个服务：PostgreSQL、Redis、migration、API、dispatcher、三个 Worker（`agent-worker`、`background-worker`、`side-effect-worker`）、beat 和 Web。只有 Web 通过 `127.0.0.1:${WEB_PORT}` 暴露给宿主机，API、PostgreSQL 和 Redis不发布生产端口。TLS 由宿主机 Nginx/Caddy 或云负载均衡终止。

## 首次部署

```bash
tar -xzf contentai-0.5.0-rc.1-ubuntu.tar.gz
cd contentai-0.5.0-rc.1-ubuntu
cp .env.example .env
chmod 600 .env
# 编辑 .env：数据库密码、搜索密钥与默认管理员初始密码
bash infra/ubuntu/deploy.sh
```

生产配置必须通过 `docker compose config --quiet` 检查；不要运行会展开或输出 `.env` 密钥的 Compose 配置命令。数据库 URL 中的密码必须与 `POSTGRES_PASSWORD` 一致；`CONTENTAI_SERVER__FRONTEND_ORIGINS` 必须使用实际的前端来源。`CONTENTAI_AUTH__BOOTSTRAP_ADMIN_EMAIL` 与 `CONTENTAI_AUTH__BOOTSTRAP_ADMIN_PASSWORD` 必须成对设置：仅在该邮箱不存在时创建已激活管理员，已有账号在重启时不会被改写。初始登录完成后，应将密码保留在受控的部署密钥管理中；系统不提供邮件验证或邮件密码重置。

## 模型配置与 Fernet 主密钥

在首次启动前为 `CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY` 生成一次 Fernet key，并把它写入受权限控制的 `.env` 或部署密钥管理：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

将该值与数据库备份分别安全备份，并在恢复历史环境时一并恢复；丢失、格式错误或更换该值会使应用拒绝启动或无法解密已有模型凭据。首个版本没有在线主密钥轮换、模型配置删除或回滚功能。Compose 会把同一个必填值传给 migration、API、dispatcher、worker 和 beat，不能为任何一个应用服务单独设置不同值。

首次部署后，管理员登录 `/admin/models` 创建全局 active 的 OpenAI-compatible Base URL、API Key 和模型名。可先 probe；保存时服务端会重做完整 probe。首次配置不能留空 API Key，后续修改留空才表示沿用当前 Key。API Key、密文、Authorization 与远端响应正文不得出现在 API、日志、审计、工单或故障报告中。Traffic Relay 是独立搜索服务凭据，不能当作模型 API Key。

模型切换只影响新 execution；queued、running、resume 和 retry 按其固化的历史配置继续执行。没有 active 模型时，消息提交会在写入业务记录前返回 `503 MODEL_NOT_CONFIGURED`。

## 健康检查

```bash
bash infra/ubuntu/health.sh
curl --fail http://127.0.0.1:${WEB_PORT:-5180}/api/ready
```

`/api/ready` 同时检查数据库、Alembic revision、LangGraph checkpoint、Redis、队列与 outbox。一次性 `migration` 显示 `Exited (0)` 是正常状态，其余服务应为 `healthy` 或 `Up`。

## 日志

应用不创建日志目录或文件，全部写入 stdout/stderr。十个服务统一使用 Docker `json-file` 驱动并按 `LOG_MAX_SIZE`、`LOG_MAX_FILES` 滚动；三个 Worker 中的 `side-effect-worker` 独立消费副作用队列，必须单独纳入健康、日志与告警：

```bash
docker compose logs --tail 200 api
docker compose logs --follow agent-worker
docker compose logs --since 30m postgres redis
```

每条 Python 日志包含 `service`、`level`、`logger`、`process` 和 `message`。容器名和服务参数提供日志隔离，不在应用内部重复按文件分流。

## 备份、恢复与升级

```bash
# 生成 gzip SQL 和 SHA-256 文件
bash infra/ubuntu/backup.sh /srv/contentai-backups

# 恢复会覆盖当前数据库，必须显式确认
CONTENTAI_CONFIRM_RESTORE=yes bash infra/ubuntu/restore.sh \
  /srv/contentai-backups/contentai-YYYYMMDDTHHMMSSZ.sql.gz

# 先备份，再构建、迁移、启动和检查
bash infra/ubuntu/upgrade.sh
```

恢复演练应在独立 Compose 项目和空卷执行。由于本发行版只支持全新安装，升级到包含破坏性初始迁移的版本时，应新建环境并按业务批准的数据导入方案切换，不得直接对旧数据库执行 `upgrade head`。

## 发布包

在 Windows 开发机根目录执行：

```powershell
tools/package-ubuntu.ps1
```

输出为 `dist/contentai-<version>-ubuntu.tar.gz`，只包含运行源码、Compose、镜像配置、环境模板、单一迁移、Ubuntu 脚本、锁定依赖和必要文档；不包含 `.env`、测试、缓存、依赖目录、日志或开发产物。

## 本地开发 override

```bash
docker compose -f compose.yaml -f compose.dev.yaml up --detach --build --wait
```

开发 override 仅在 loopback 暴露 PostgreSQL、Redis 与 API，用于宿主机测试。不要在公网主机使用该 override。

## 常见故障

| 现象 | 首要检查 |
| --- | --- |
| `migration` 失败 | `.env` 数据库凭据、`docker compose logs migration` |
| API 不健康 | `docker compose logs api` 与 `/api/ready` 的 `checks` |
| 消息一直等待 | dispatcher、agent-worker 状态及 outbox 日志 |
| 恢复确认失败 | 执行仍为 waiting_input、会话 Agent 一致、checkpoint identity 匹配 |
| SSE 中断 | Web Nginx 已关闭代理缓冲、Redis 流未过期、使用最后 sequence 重连 |
| Web 可打开但 API 失败 | Web 容器日志及 `http://127.0.0.1:${WEB_PORT}/api/ready` |
