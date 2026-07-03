# ContentAI

ContentAI is an open-ended conversational Agent workbench built with FastAPI, Vue 3, LangChain and LangGraph.

The backend is intentionally structured around one Agent runtime:

- `agent/graph`: LangGraph ReAct-style loop
- `agent/runtime`: run execution, events, checkpoint/store wiring
- `agent/memory`: short-term and long-term memory
- `agent/prompts`: single prompt registry, system prompts and tool descriptions
- `agent/tools`: model-callable tools
- `agent/infrastructure/llm`: single lower-level model gateway

The current behavior is conversational and tool-driven. Legacy fixed content-state machine orchestration and deep-search pipeline automation were removed, while account-bound chat flow and hotspot/content tooling remain supported.

## Run

### 后端（FastAPI）

```powershell
python -m venv .venv
. .venv/Scripts/Activate.ps1
pip install -e ".[dev]"
uvicorn main:app --reload --host 127.0.0.1 --port 8000 --app-dir backend/src
```

### 前端（Vue）

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5180 --strictPort
```

说明：

- 前端默认监听 `http://127.0.0.1:5180/`，与当前运行环境保持一致。
- 后端监听 `http://127.0.0.1:8000/`，前端接口请按该地址配置。

## Verify

```powershell
python -m ruff check backend/src backend/tests
python -m pytest backend/tests
cd frontend
npm run build
```

```powershell
# 后端快速可达性检测
python -c "import requests; print(requests.get('http://127.0.0.1:8000/docs').status_code)"
# 预期输出 200
```
