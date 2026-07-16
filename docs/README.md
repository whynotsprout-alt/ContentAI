# ContentAI 文档导航

本文档集以当前 `apps/api`、`apps/web`、`infra`、`docs` 和 `tools` 布局为准。

| 文档 | 用途 |
| --- | --- |
| [PRODUCT.md](PRODUCT.md) | 产品目标、对话式内容流程及明确不做的能力 |
| [DESIGN.md](DESIGN.md) | 可靠执行、恢复、数据边界与服务设计 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 目录职责、服务拓扑与请求链路 |
| [DEVELOPMENT.md](DEVELOPMENT.md) | 本地配置、开发、测试与质量检查 |
| [OPERATIONS.md](OPERATIONS.md) | 容器启动、健康检查、迁移、日志与故障处理 |
| [API.md](API.md) | 35 个公开端点、认证、调用方与请求响应边界 |
| [SESSION_TO_CONTENT_PIPELINE.md](SESSION_TO_CONTENT_PIPELINE.md) | 从会话到内容交付的执行细节 |

## 当前目录

```text
apps/
  api/     FastAPI、LangGraph、迁移、后端测试
  web/     Vue 3、TypeScript、Vite、前端测试
docs/      本文档集
infra/     API/Web 镜像与 Nginx 配置
tools/     Windows 开发、验证与重启脚本
```

仓库根目录保留跨应用配置：`pyproject.toml`、`uv.lock`、`alembic.ini`、`.env.example`、`compose.yaml` 与 `compose.dev.yaml`。

## 快速入口

```powershell
# 首次准备本地依赖
tools/setup.ps1

# 完整容器化启动或重启
tools/restart.ps1

# 代码质量与构建验收
tools/review.ps1
```

- Web：`http://127.0.0.1:5180/`
- API OpenAPI：`http://127.0.0.1:8000/docs`
- API 就绪检查：`http://127.0.0.1:8000/api/ready`
