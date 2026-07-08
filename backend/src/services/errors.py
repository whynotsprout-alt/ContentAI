
class AccountError(Exception):
    """Base class for account domain errors."""


class AccountNotFoundError(AccountError):
    pass


class AccountAlreadyExistsError(AccountError):
    pass


class AccountValidationError(AccountError):
    pass


class AccountInUseError(AccountError):
    pass


class AccountPermissionError(AccountError):
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


class ExecutionNotFoundError(ConversationError):
    pass


class InvalidCursorError(ConversationError):
    pass


class ExecutionNotResumableError(ConversationError):
    pass


class ExecutionResumeValueRequiredError(ConversationError):
    pass
