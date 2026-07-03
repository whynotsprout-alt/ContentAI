# LangGraph Node / Tool / State Map

## State

`AgentState` contains only serializable graph state:

- `messages`
- `system_prompt`
- `iterations`
- `max_iterations`

Runtime-only objects such as DB sessions, event writers and memory managers are kept outside state.

## Nodes

- `agent`: invokes the unified model gateway's chat model.
- `tools`: executes LangChain tools through `ToolNode`.

## Edges

- `START -> agent`
- `agent -> tools` when the last AI message has tool calls and the iteration limit is not reached
- `agent -> END` otherwise
- `tools -> agent`

## Tools

- `remember`: persist long-term memory.
- `recall_memory`: recall long-term memory.
- `fetch_hotspots`: retrieve latest trend/heat candidates (with account hotspot source controls).
- `search_topic_sources`: collect evidentiary references and background materials for confirmed topics.
- `current_datetime`: return current date/time for date-sensitive replies.
