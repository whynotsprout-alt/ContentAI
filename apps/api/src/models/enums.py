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
    tool_call = "tool_call"
    tool_result = "tool_result"
    reasoning = "reasoning"
    artifact = "artifact"
    citation = "citation"
    image = "image"
    file = "file"


class RunStatus(StrEnum):
    pending = "pending"
    running = "running"
    waiting_input = "waiting_input"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class ExecutionAttemptKind(StrEnum):
    initial = "initial"
    resume = "resume"
    retry = "retry"


class ExecutionAttemptStatus(StrEnum):
    running = "running"
    waiting_input = "waiting_input"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    lease_lost = "lease_lost"


class ToolExecutionStatus(StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class AgentType(StrEnum):
    content = "content"
    customer_service = "customer_service"
    recruiting = "recruiting"
    custom = "custom"


class AgentStatus(StrEnum):
    draft = "draft"
    active = "active"
    disabled = "disabled"
    archived = "archived"


class SessionStatus(StrEnum):
    active = "active"
    archived = "archived"
    deleted = "deleted"


class TitleSource(StrEnum):
    default = "default"
    manual = "manual"
    llm_generated = "llm_generated"


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


class MemoryOwnerType(StrEnum):
    user = "user"
    agent = "agent"
    session = "session"


class MemorySourceType(StrEnum):
    manual = "manual"
    user_message = "user_message"
    turn_summary = "turn_summary"
    summary = "summary"
    tool = "tool"
    system = "system"
