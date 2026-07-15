# ContentAI

ContentAI 是一个面向内容创作者的持续对话 Agent 工作台。内容流程保持为自然对话：热点发现、选题判断、搜索研究、用户确认与稿件交付；所有结果都以会话消息输出。

## 目录

```text
apps/
  api/                 FastAPI、LangGraph、工具、数据库迁移与后端测试
  web/                 Vue 3、TypeScript、Vite 与前端测试
docs/                  产品边界与技术设计的唯一事实源
infra/                 容器镜像、Nginx 反向代理与部署配置
tools/                 Windows 本地开发、验收与整套重启脚本
data/                  本地运行数据（不提交）
logs/                  本地运行日志（不提交）
```

工程级配置保留在根目录：`pyproject.toml`、`requirements.txt`、`alembic.ini`、`.env.example` 与 `docker-compose.yml`。

## 核心边界

- 不建立内容候选、稿件版本等中间业务表或文件；研究资料包作为跨轮只读依据持久化。
- 不抓取搜索结果 URL 的网页正文；研究只依据搜索标题、摘要和链接。
- 会话创建时固定 AgentVersion，后续筛选、研究、确认和写稿不自动切换版本。
- 最终稿件是普通 Assistant 消息。
- checkpoint、执行状态与不含正文的审计仅用于持续对话与可靠性恢复。

完整文档导航见 [docs/README.md](docs/README.md)；产品与技术事实源分别是
[产品说明](docs/PRODUCT.md) 和 [技术设计](docs/DESIGN.md)。

## 本地开发

```powershell
tools/setup.ps1
tools/dev-api.ps1
tools/dev-web.ps1
```

- Web：`http://127.0.0.1:5180/`
- API 文档：`http://127.0.0.1:8000/docs`

运行验收：

```powershell
tools/review.ps1
```

## 容器化启动与重启

完整服务（PostgreSQL、Redis、迁移、API、Dispatcher、Worker、Beat、Web）使用：

```powershell
tools/restart.ps1
```

或直接运行：

```powershell
docker compose up --detach --build --wait
```

Web 容器以 Nginx 提供前端静态文件，并将 `/api/` 反向代理到 API 服务。
