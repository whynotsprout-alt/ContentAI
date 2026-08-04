# 项目架构

## 目录边界

```text
ContentAI/
├── apps/
│   ├── api/
│   │   ├── src/contentai/
│   │   │   ├── api/              HTTP 路由、认证依赖、SSE 边界
│   │   │   ├── agent/            LangGraph、运行时、工具和研究工作流
│   │   │   ├── core/             配置、安全、日志、基础设施策略
│   │   │   ├── db/               数据库引擎和会话管理
│   │   │   ├── integrations/     搜索、热点等外部系统适配器
│   │   │   ├── memory/           短期/长期记忆与持久化
│   │   │   ├── models/            数据库模型和 API Schema
│   │   │   ├── services/         会话、执行、outbox、任务和运维服务
│   │   │   └── migrations/       Alembic 业务迁移
│   │   └── tests/                后端单元、集成和契约测试
│   └── web/
│       ├── src/app/              应用壳、启动入口和路由
│       ├── src/features/auth/    登录、注册和认证状态
│       ├── src/features/admin/   管理员用户与模型配置
│       ├── src/features/workbench/ 会话工作台、SSE 和执行状态
│       ├── src/shared/           API 客户端、共享组件和样式
│       └── src/assets/           字体、背景等静态资源
├── infra/                        Dockerfile、Nginx 和发布配置
├── tools/                        本地开发、测试、验收和发布脚本
├── docs/                         产品、设计、架构和运维文档
└── compose*.yaml                 本地/生产服务编排
```

## 后端依赖方向

后端源码统一使用 `contentai.*` 命名空间，`apps/api/src` 只是 Python 源码根目录，不承载业务模块。

```text
api -> services -> agent / memory / integrations
api -> models / core
services -> models / db / core
agent -> models / memory / integrations / core
memory -> models / db / core
integrations -> core
models -> core
```

`models` 不依赖 `api` 或 `services`；`integrations` 只负责外部协议适配；跨领域编排放在 `services` 或 `agent`，避免路由直接操作数据库细节。

## 运行入口

| 运行角色 | 入口 |
| --- | --- |
| API | `python -m uvicorn contentai.api.app:app` |
| 数据库迁移 | `python -m contentai.services.migrate` |
| Dispatcher | `python -m contentai.services.dispatcher` |
| Celery Worker / Beat | `contentai.services.celery_app:celery_app` |
| Alembic | `apps/api/src/contentai/migrations` |

前端通过 `@/*` 指向 `apps/web/src/*`，业务代码禁止使用跨层级 `../../` 访问其他 feature；跨 feature 共享能力放入 `shared`。

## 执行链路

1. Web 创建或加载绑定 Agent 的会话。
2. API 在事务中写入消息、执行记录和 outbox。
3. Dispatcher 将 outbox 投递到 Redis/Celery。
4. Agent Worker 使用 LangGraph checkpoint 执行或恢复，并将可见结果写回 Assistant 消息。
5. Redis 事件流由 API 通过 SSE 重放；标题、摘要和记忆等后处理继续走 outbox 重试。

数据库迁移只管理 ContentAI 业务表；LangGraph checkpoint/store 表由其自身迁移机制管理。

Compose 服务拓扑共十个服务：PostgreSQL、Redis、migration、api、dispatcher、三个 Worker
（`agent-worker`、`background-worker`、`side-effect-worker`）、beat 和 web；所有应用服务共用同一模型配置加密密钥。
