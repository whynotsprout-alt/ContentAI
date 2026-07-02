# ContentAI

ContentAI is an open-ended conversational Agent workbench built with FastAPI, Vue 3, LangChain and LangGraph.

The backend is intentionally structured around one Agent runtime:

- `agent/graph`: LangGraph ReAct-style loop
- `agent/runtime`: run execution, events, checkpoint/store wiring
- `agent/memory`: short-term and long-term memory
- `agent/prompts`: single prompt registry and prompt files
- `agent/tools`: model-callable tools
- `agent/infrastructure/llm`: single lower-level model gateway

Old content-pipeline, hotspot and deep-search capabilities were removed.

## Run

```powershell
python -m venv .venv
. .venv/Scripts/Activate.ps1
pip install -e ".[dev]"
uvicorn main:app --reload --host 127.0.0.1 --port 8000 --app-dir backend/src
```

```powershell
cd frontend
npm install
npm run dev
```

## Verify

```powershell
python -m ruff check backend/src backend/tests
python -m pytest backend/tests
cd frontend
npm run build
```
