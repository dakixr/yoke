"""Sanitized failures for native API requests and optional capabilities."""

from acp import RequestError
import httpx


class NativeHttpError(RequestError):
    """Retain the status code without retaining a response body or credentials."""

    def __init__(self, status: int) -> None:
        super().__init__(-32001, f"Native HTTP error {status}")
        self.status = status


def native_request_failure(
    error: httpx.HTTPError | ValueError,
    *,
    method: str,
    path: str,
    elapsed: float,
) -> RequestError:
    """Describe a failed operation without echoing untrusted exception text."""
    kinds = (
        (httpx.ConnectTimeout, "connection timeout"),
        (httpx.ReadTimeout, "read timeout"),
        (httpx.WriteTimeout, "write timeout"),
        (httpx.PoolTimeout, "connection pool timeout"),
        (httpx.TimeoutException, "timeout"),
        (httpx.ConnectError, "connection failure"),
        (httpx.RemoteProtocolError, "remote protocol failure"),
        (httpx.HTTPError, "transport failure"),
        (ValueError, "invalid JSON response"),
    )
    reason = next(label for kind, label in kinds if isinstance(error, kind))
    # Paths can contain session IDs, query values, or absolute URLs. Only emit
    # known route names, never arbitrary path segments or exception messages.
    parts = path.split("?", 1)[0].split("/")
    operation = "native request"
    if len(parts) in (2, 3) and parts[0] == "session":
        operation = "session"
        if len(parts) == 3 and parts[2] in {
            "prompt",
            "wait",
            "drain",
            "interrupt",
            "history",
            "message",
        }:
            operation += "/" + parts[2]
    elif path in {"skill", "upload", "process", "agent-run", "openapi.json"}:
        operation = path
    verb = method if method in {"GET", "POST", "PATCH", "DELETE"} else "HTTP"
    return RequestError(
        -32001,
        f"Native HTTP {reason} during {verb} {operation} after {elapsed:.1f}s",
    )
