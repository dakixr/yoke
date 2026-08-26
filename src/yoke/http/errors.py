"""HTTP error types and FastAPI exception handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi import status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from yoke.http.models.common import ErrorBody
from yoke.http.models.common import ErrorEnvelope


@dataclass(slots=True)
class ApiError(Exception):
    """Expected transport-facing application error."""

    status_code: int
    code: str
    message: str
    details: dict[str, Any] | None = None


def request_id(request: Request) -> str:
    """Return the request trace identity assigned by middleware."""
    return str(getattr(request.state, "request_id", "unknown"))


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build the stable public error envelope."""
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=message,
            details=details or {},
            request_id=request_id(request),
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json", by_alias=True),
    )


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """Render a known application error without leaking implementation details."""
    return error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )


async def validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Normalize FastAPI validation failures into the Yoke envelope."""
    return error_response(
        request,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="validation_error",
        message="Request validation failed.",
        details={"errors": exc.errors()},
    )
