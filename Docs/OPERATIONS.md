# 运行与部署手册

## 启动和重启

在仓库根目录准备好 `.env` 后：

```powershell
tools/restart.ps1
```

该脚本会构建镜像、移除当前 Compose 项目的孤立容器、等待健康检查，并检查 API 和 Web 可访问性。等价的 Compose 命令是：

```powershell
docker compose up --detach --build --remove-orphans --wait --wait-timeout 180
```

常用状态与日志命令：

```powershell
docker compose ps
docker compose logs --tail 200 api
docker compose logs --tail 200 agent-worker
docker compose logs --tail 200 dispatcher
```

## 健康检查

| 地址或命令 | 预期 |
| --- | --- |
| `http://127.0.0.1:8000/api/health` | 进程存活 |
| `http://127.0.0.1:8000/api/ready` | 数据库 revision、checkpoint、Redis、队列和 outbox 均就绪 |
| `http://127.0.0.1:5180/api/ready` | Web Nginx 到 API 的反向代理正常 |
| `docker compose ps` | 除一次性 `migration` 外，各服务为 `healthy` 或 `Up` |

`migration` 成功运行后显示为 `Exited (0)` 是正常状态。若 readiness 返回非 200，先查看 API 日志，再根据 `checks` 字段确定是数据库、checkpoint、Redis、队列还是 outbox 积压问题。

## 迁移与 checkpoint

Compose 的 `migration` 服务在每次启动时执行：

1. `alembic upgrade head` 更新业务 schema；
2. `PostgresSaver.setup()` 初始化或校验 LangGraph checkpoint schema。

正常迁移链不允许使用 `TRUNCATE`、清空业务数据或删除 checkpoint/store 表。请先在带有生产快照的环境验证升级，再部署到生产。

业务数据清理是独立的破坏性管理操作，绝不能放入 Alembic 或部署脚本。仅在明确授权、完成备份并确认目标数据库后，才可执行：

```powershell
$env:PYTHONPATH = "apps/api/src"
.venv\Scripts\python.exe -m services.purge_business_data --confirm PURGE-CONTENTAI-BUSINESS-DATA
```

该命令会删除业务数据和 checkpoint，不能撤销。

## 配置与安全

- `.env` 通过 Compose 注入每个 Python 服务；Web 为静态资源，不保存这些密钥。
- 生产环境必须使用 PostgreSQL、Redis、HTTPS 公共地址和 SMTP；配置缺失时服务会在启动时失败，而不是降级运行。
- 禁用用户会撤销验证/登录会话并取消排队或等待确认的执行；Worker 在领取和恢复时再次校验状态。
- API 通过稳定的认证用户范围进行 LLM 限流，并配置每日执行预算；验证邮件重发分别受用户、邮箱和 IP 限制。

## 常见故障

| 现象 | 首要检查 |
| --- | --- |
| `migration` 失败 | `.env`、PostgreSQL 可达性、`docker compose logs migration` |
| API 不健康 | `docker compose logs api` 与 `/api/ready` 的 `checks` |
| 消息一直等待 | `docker compose ps` 中 Dispatcher/Agent Worker 状态，以及 Dispatcher 日志中的 outbox 发布情况 |
| 恢复确认失败 | 执行是否仍为可恢复状态、会话与 `agent_id` 是否一致、Worker 日志中的 checkpoint 校验信息 |
| Web 能打开但无法请求 API | `http://127.0.0.1:5180/api/ready`、Web 容器状态与 `infra/nginx.conf` |

运行日志默认写入宿主机 `logs/`，同时可以通过 Compose 日志查看容器标准输出。
