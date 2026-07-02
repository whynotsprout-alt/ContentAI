# Development Requirements

## Backend

- Keep API handlers thin.
- Put application use cases in `services`.
- Keep LangGraph state serializable.
- Persist user/assistant/tool messages through `ChatMessage`.
- Persist run events through `AgentRunEvent`.
- Add new tools under `agent/tools` only.
- Add prompt files under `agent/prompts` only.

## Frontend

- Treat the app as a conversation workbench.
- Do not reintroduce hotspot, topic scoring or content-pipeline controls.
- Do not call removed endpoints such as `/api/hotspot-platforms`.

## Verification

Run:

```powershell
python -m ruff check backend/src backend/tests
python -m pytest backend/tests
cd frontend
npm run build
```
