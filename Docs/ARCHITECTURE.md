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
  -> PostgreSQL queued AgentRun
  -> agent/runtime/worker.py claims run with a lease
  -> agent/runtime/executor.py
  -> context assembler + memory recall
  -> LangGraph: agent -> tools -> agent -> end
  -> persist new messages/events
  -> validate assistant output format via runtime schema
  -> refresh short-term summary and long-term memory
```

## Memory

Short-term memory follows LangGraph's thread-level pattern:

- graph compiled with a checkpointer
- each chat session uses `thread_id=session_id`
- PostgreSQL-backed LangGraph checkpoint state survives restarts and multi-worker runs
- summaries are stored under a session namespace

Long-term memory follows LangGraph store semantics:

- graph compiled with a store
- memories are JSON documents under namespace `("accounts", account_id, "long_term")`
- `remember` and `recall_memory` tools write/read long-term memory
- records are persisted in `MemoryRecord` and mirrored to the PostgreSQL LangGraph store

The runtime is PostgreSQL-only: application tables are managed by Alembic migrations, and LangGraph checkpoint/store tables are initialized by `agent/runtime/checkpoint.py`.
