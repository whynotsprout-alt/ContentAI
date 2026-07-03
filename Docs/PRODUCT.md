# Product

ContentAI is now an account-configured conversational content Agent, not a fixed autonomous content-production workflow.

The product surface is intentionally small:

- Manage content accounts with positioning, topic scoring prompts, content creation prompts and hotspot source selections.
- Create chat sessions.
- Send messages into a LangGraph Agent loop.
- Persist assistant replies, tool calls and run events.
- Maintain short-term thread memory and long-term account memory.
- Fetch account-limited hotspot candidates when the user asks for trends or topic planning.
- Let the model score topics and create content from the active account's database-backed prompts.

Out of scope after this refactor:

- Deep search orchestration.
- Code-driven topic scoring.
- Autonomous draft-generation pipelines.
- Backward compatibility with old content-state-machine code.
