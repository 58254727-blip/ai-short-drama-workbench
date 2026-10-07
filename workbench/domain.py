"""Public data-layer error contract."""


class DomainError(Exception):
    def __init__(self, code: str, status: int, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.details = details or {}


def require(condition: bool, code: str, status: int, message: str) -> None:
    if not condition:
        raise DomainError(code, status, message)
