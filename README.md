# ContentAI

ContentAI 是一个开放式对话 Agent 工作台，后端使用 FastAPI、LangChain 和 LangGraph，前端使用 Vue 3、TypeScript 和 Vite。

## 代码语言约定

这个仓库不是单一语言项目，按职责分层：

- `backend/`：Python，承载 API、Agent Runtime、LangGraph、记忆、工具和数据库访问。
- `frontend/`：Vue 3 + TypeScript，承载浏览器工作台界面。
- `frontend/src/styles.css`：前端样式，属于前端源码的一部分。
- `scripts/`：PowerShell 开发脚本，只是本地启动、安装和验收入口，不是业务运行代码。
- `frontend/scripts/`：前端专项验收脚本，不是浏览器端业务代码。

GitHub 的 Languages 面板会按文件字节数统计源码语言，所以会看到 Python、Vue、TypeScript、CSS、PowerShell、JavaScript 和 HTML。这里的主语言仍然是 Python 后端 + Vue/TypeScript 前端；PowerShell 和 JavaScript 脚本已通过 `.gitattributes` 从语言统计中排除，避免工具入口干扰项目语言占比。

## 主要结构

- `backend/src/agent/graph`：LangGraph ReAct 风格循环。
- `backend/src/agent/runtime`：运行执行、事件、checkpoint/store 编排。
- `backend/src/agent/memory`：短期记忆和长期记忆。
- `backend/src/agent/prompts`：系统提示词、工具提示词和提示词注册。
- `backend/src/agent/tools`：模型可调用工具。
- `backend/src/agent/infrastructure/llm`：底层模型网关。
- `frontend/src`：Vue 工作台界面、状态管理和 API 客户端。

## 根目录 scripts 的作用

`scripts/` 是 Windows PowerShell 开发入口，目的是减少手敲命令：

- `scripts/setup.ps1`：创建 `.venv`，安装后端依赖；默认也安装前端依赖，可传 `-SkipFrontend` 跳过。
- `scripts/dev-api.ps1`：启动后端 FastAPI，默认监听 `http://127.0.0.1:8000/`，可用 `CONTENTAI_API_PORT` 覆盖端口。
- `scripts/dev-web.ps1`：启动前端 Vite，监听 `http://127.0.0.1:5180/`。
- `scripts/test.ps1`：运行后端 pytest。
- `scripts/review.ps1`：依次运行 Ruff、pytest 和前端 build，用作提交前检查。

## 初始化

```powershell
scripts/setup.ps1
```

只安装后端依赖：

```powershell
scripts/setup.ps1 -SkipFrontend
```

## 本地开发

后端：

```powershell
scripts/dev-api.ps1
```

前端：

```powershell
scripts/dev-web.ps1
```

打开：

- 前端工作台：`http://127.0.0.1:5180/`
- 后端文档：`http://127.0.0.1:8000/docs`

## 验证

完整检查：

```powershell
scripts/review.ps1
```

分开运行：

```powershell
python -m ruff check backend/src backend/tests
python -m pytest backend/tests
cd frontend
npm run build
```
