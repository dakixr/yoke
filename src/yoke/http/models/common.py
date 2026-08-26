"""Shared HTTP transport values."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


def to_camel(value: str) -> str:
    """Convert snake_case to the API's acronym-preserving camelCase."""
    head, *tail = value.split("_")
    return head + "".join("ID" if part == "id" else part.capitalize() for part in tail)


class ApiModel(BaseModel):
    """Base transport model with a stable browser-friendly field convention."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class LocationInfo(ApiModel):
    """Resolved workspace location."""

    directory: str


class CursorInfo(ApiModel):
    """Opaque list pagination cursors."""

    previous: str | None = None
    next: str | None = None


class ErrorBody(ApiModel):
    """Machine-readable API error."""

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str


class ErrorEnvelope(ApiModel):
    """Stable error response envelope."""

    error: ErrorBody
