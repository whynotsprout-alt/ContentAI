# Product

ContentAI is now a continuous conversation Agent, not a fixed content-production workflow.

The product surface is intentionally small:

- Manage Agent accounts with description and long-term behavior instructions.
- Create chat sessions.
- Send messages into a LangGraph Agent loop.
- Persist assistant replies, tool calls and run events.
- Maintain short-term thread memory and long-term account memory.

Out of scope after this refactor:

- Hotspot collection.
- Deep search orchestration.
- Topic scoring.
- Draft-generation pipelines.
- Backward compatibility with old content-state-machine code.
