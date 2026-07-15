class AgentDomainError(Exception):
    """Base class for agent profile domain errors."""


class AgentNotFoundError(AgentDomainError):
    pass


class AgentAlreadyExistsError(AgentDomainError):
    pass


class AgentValidationError(AgentDomainError):
    pass


class AgentInUseError(AgentDomainError):
    pass


class AgentPermissionError(AgentDomainError):
    pass


class PermissionError(Exception):
    """Base class for authorization failures in service runtime."""


class AgentError(Exception):
    """Base class for top-level agent execution failures."""


class ToolError(Exception):
    """Base class for tool execution failures."""


class ConversationError(Exception):
    """Base class for conversation domain errors."""


class ChatSessionNotFoundError(ConversationError):
    pass


class ActiveExecutionExistsError(ConversationError):
    pass


class SessionAgentMismatchError(ConversationError):
    pass


class ExecutionNotFoundError(ConversationError):
    pass


class InvalidCursorError(ConversationError):
    pass


class StreamReplayGapError(ConversationError):
    pass


class StreamReplayExpiredError(ConversationError):
    pass


class InvalidStreamCursorError(ConversationError):
    pass


class StreamingDegradedError(ConversationError):
    pass


class ExecutionNotResumableError(ConversationError):
    pass


class ExecutionResumeValueRequiredError(ConversationError):
    pass
