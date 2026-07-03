# Development Requirements

## Backend

- Keep API handlers thin.
- Put application use cases in `services`.
- Keep LangGraph state serializable.
- Persist user/assistant/tool messages through `ChatMessage`.
- Persist run events through `AgentRunEvent`.
- Add new tools under `agent/tools` only.
- Add prompt files and model-visible tool descriptions under `agent/prompts` only.
- Keep tool runtime and assistant-response schemas in `agent/runtime` synchronized with prompt/config changes.

## Frontend

- Treat the app as a conversation-driven content account workbench.
- Do not reintroduce standalone hotspot, topic scoring or content-pipeline controls outside the chat flow.
- Do not call removed endpoints such as `/api/hotspot-platforms`.

## Verification

Run:

```powershell
python -m ruff check backend/src backend/tests
python -m pytest backend/tests
cd frontend
npm run build
```
