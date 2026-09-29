"""Sanitized HTTP status failures for optional native API capabilities."""

from acp import RequestError


class NativeHttpError(RequestError):
    """Retain the status code without retaining a response body or credentials."""

    def __init__(self, status: int) -> None:
        super().__init__(-32001, f"Native HTTP error {status}")
        self.status = status
