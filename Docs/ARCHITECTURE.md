# Architecture

## Current Shape

```text
backend/src/
  api/                 HTTP adapters only
  services/            application use cases
  models/              SQLModel tables and Pydantic schemas
  db/                  engine/session lifecycle
  core/                settings and paths
  agent/
    graph/             LangGraph state, nodes, edges, factory
    runtime/           executor, events, checkpointer/store/context
    context/           prompt/context assembly
    memory/            short-term and long-term memory
    prompts/           single prompt registry
    tools/             LangChain tools exposed to the model
    infrastructure/
      llm/             single LangChain/OpenAI-compatible model gateway
```

## Dependency Direction

```text
api -> services -> agent/runtime -> agent/graph
                         |          -> agent/tools
                         |          -> agent/memory
                         |          -> agent/context
                         -> agent/infrastructure/llm
```

Infrastructure does not call graph, graph does not perform HTTP or database work, and API does not call lower-level model clients directly.

## Runtime Flow

```text
user message
  -> api/chat.py
  -> services/chat_service.py
  -> agent/runtime/executor.py
  -> context assembler + memory recall
  -> LangGraph: agent -> tools -> agent -> end
  -> persist new messages/events
  -> refresh short-term summary and long-term memory
```

## Memory

Short-term memory follows LangGraph's thread-level pattern:

- graph compiled with a checkpointer
- each chat session uses `thread_id=session_id`
- DB chat history is used to rehydrate when the in-memory checkpointer is empty
- summaries are stored under a session namespace

Long-term memory follows LangGraph store semantics:

- graph compiled with a store
- memories are JSON documents under namespace `("accounts", account_id, "long_term")`
- `remember` and `recall_memory` tools write/read long-term memory
- records are persisted in `MemoryRecord` and mirrored to `InMemoryStore`

The code currently uses `InMemorySaver` and `InMemoryStore` because those packages are already installed. A database-backed LangGraph checkpointer/store can replace them in `agent/runtime/checkpoint.py` without changing graph or API contracts.
