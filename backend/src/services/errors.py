
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
