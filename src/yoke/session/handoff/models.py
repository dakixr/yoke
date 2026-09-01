"""Typed values for portable Yoke session handoffs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DEFAULT_HANDOFF_MAX_CHARS = 240_000


class SessionHandoffImage(BaseModel):
    """One image reference retained in a portable handoff."""

    label: str
    source: str | None = None


class SessionHandoffToolCall(BaseModel):
    """One assistant tool-call request retained in a portable handoff."""

    id: str
    name: str
    arguments: str
    truncated: bool = False


class SessionHandoffMessage(BaseModel):
    """One readable message in a portable session handoff."""

    role: str
    content: str = ""
    phase: Literal["commentary", "final_answer"] | None = None
    tool_call_id: str | None = None
    tool_calls: list[SessionHandoffToolCall] = Field(default_factory=list)
    images: list[SessionHandoffImage] = Field(default_factory=list)
    source: Literal["conversation", "compaction_summary", "compaction_retained"] = (
        "conversation"
    )
    truncated: bool = False


class SessionHandoff(BaseModel):
    """Portable active-branch context for continuing work in another agent."""

    session_id: str
    title: str | None = None
    root: str | None = None
    provider_name: str | None = None
    model_id: str | None = None
    reasoning_effort: str | None = None
    updated_at: str | None = None
    leaf_id: str | None = None
    active_skills: list[str] = Field(default_factory=list)
    total_entries: int = 0
    retained_entries: int = 0
    omitted_messages: int = 0
    truncated: bool = False
    max_chars: int = DEFAULT_HANDOFF_MAX_CHARS
    messages: list[SessionHandoffMessage] = Field(default_factory=list)
