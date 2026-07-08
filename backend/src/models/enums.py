from enum import StrEnum


class MessageRole(StrEnum):
    user = "user"
    assistant = "assistant"
    system = "system"
    tool = "tool"


class MessageType(StrEnum):
    text = "text"
    markdown = "markdown"
    json = "json"


class RunStatus(StrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    interrupted = "interrupted"


class ToolExecutionStatus(StrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"


class MemoryKind(StrEnum):
    semantic = "semantic"
    episodic = "episodic"
    procedural = "procedural"
    summary = "summary"
    profile = "profile"
    preference = "preference"
    goal = "goal"


class MemoryScope(StrEnum):
    short_term = "short_term"
    long_term = "long_term"
