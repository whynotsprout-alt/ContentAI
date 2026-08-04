from enum import StrEnum


class MessageRole(StrEnum):
    user = "user"
    assistant = "assistant"


class MessageType(StrEnum):
    text = "text"
    markdown = "markdown"


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


class MemoryKind(StrEnum):
    semantic = "semantic"
    episodic = "episodic"
    procedural = "procedural"
    summary = "summary"
    profile = "profile"
    preference = "preference"
    goal = "goal"


class MemorySourceType(StrEnum):
    manual = "manual"
    user_message = "user_message"
    turn_summary = "turn_summary"
    summary = "summary"
    tool = "tool"
    system = "system"
