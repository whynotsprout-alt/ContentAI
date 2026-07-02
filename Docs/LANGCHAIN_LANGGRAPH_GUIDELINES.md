# LangChain / LangGraph Guidelines

Use LangChain and LangGraph as first-class runtime primitives.

- Model calls go through `agent/infrastructure/llm/gateway.py`.
- Agent control flow lives in `agent/graph`.
- Tool execution uses LangChain `@tool` functions and LangGraph `ToolNode`.
- Short-term memory uses a LangGraph checkpointer and `thread_id`.
- Long-term memory uses a LangGraph store namespace/key model.
- Prompts live only in `agent/prompts`.

Do not add:

- another model gateway,
- direct HTTP model calls in API/services,
- business orchestration inside infrastructure,
- fixed content pipeline states,
- duplicate prompt directories.

Official documentation used for this design:

- https://docs.langchain.com/oss/python/langgraph/add-memory
- https://docs.langchain.com/oss/python/langchain/short-term-memory
- https://docs.langchain.com/oss/python/langchain/long-term-memory
