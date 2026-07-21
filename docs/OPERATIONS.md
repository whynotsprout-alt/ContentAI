# Ubuntu 运行与部署手册

## 支持范围

生产环境面向受支持的 Ubuntu LTS、Docker Engine 与 Docker Compose v2。V0.5.0 只支持全新空数据库安装，不支持任何旧数据库或旧 Alembic revision 原地升级。部署时必须创建新的 PostgreSQL 数据库或数据卷；迁移服务不会自动删除旧数据库。

生产 `compose.yaml` 启动九个服务：PostgreSQL、Redis、migration、API、dispatcher、agent worker、background worker、beat 和 Web。只有 Web 通过 `127.0.0.1:${WEB_PORT}` 暴露给宿主机，API、PostgreSQL 和 Redis不发布生产端口。TLS 由宿主机 Nginx/Caddy 或云负载均衡终止。

## 首次部署

```bash
tar -xzf contentai-0.4.3-ubuntu.tar.gz
cd contentai-0.4.3-ubuntu
cp .env.example .env
chmod 600 .env
# 编辑 .env：数据库密码、搜索密钥与默认管理员初始密码
bash infra/ubuntu/deploy.sh
```

生产配置必须通过 `docker compose config --quiet`。数据库 URL 中的密码必须与 `POSTGRES_PASSWORD` 一致；`CONTENTAI_SERVER__FRONTEND_ORIGINS` 必须使用实际的前端来源。`CONTENTAI_AUTH__BOOTSTRAP_ADMIN_EMAIL` 与 `CONTENTAI_AUTH__BOOTSTRAP_ADMIN_PASSWORD` 必须成对设置：仅在该邮箱不存在时创建已激活管理员，已有账号在重启时不会被改写。初始登录完成后，应将密码保留在受控的部署密钥管理中；系统不提供邮件验证或邮件密码重置。

## 健康检查

```bash
bash infra/ubuntu/health.sh
curl --fail http://127.0.0.1:${WEB_PORT:-5180}/api/ready
```

`/api/ready` 同时检查数据库、Alembic revision、LangGraph checkpoint、Redis、队列与 outbox。一次性 `migration` 显示 `Exited (0)` 是正常状态，其余服务应为 `healthy` 或 `Up`。

## 日志

应用不创建日志目录或文件，全部写入 stdout/stderr。九个服务统一使用 Docker `json-file` 驱动并按 `LOG_MAX_SIZE`、`LOG_MAX_FILES` 滚动：

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
tools/package-ubuntu.ps1 -Version 0.4.3
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
