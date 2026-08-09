"""Pydantic response models for OpenAI-compatible providers."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

from yoke.agent.models import Message
from yoke.agent.models import MessagePhase
from yoke.agent.models import Role
from yoke.agent.models import ToolCall


class OpenAICompatibleResponseMessage(BaseModel):
    """Assistant message item returned by a chat-completions endpoint."""

    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    phase: MessagePhase | None = None
    reasoning_content: str | None = None

    @field_validator("phase", mode="before")
    @classmethod
    def normalize_phase(cls, value: object) -> MessagePhase | None:
        """Normalize provider phase aliases when present."""
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower().replace("-", "_")
        if normalized in {"commentary", "preamble"}:
            return "commentary"
        if normalized in {"final_answer", "final"}:
            return "final_answer"
        return None

    def to_message(self) -> Message:
        """Convert the response item to yoke's message model."""
        return Message(
            role=self.role,
            content=self.content,
            tool_calls=self.tool_calls,
            phase=self.phase,
            reasoning_content=self.reasoning_content,
        )


class OpenAICompatibleChoice(BaseModel):
    """One chat-completions choice."""

    message: OpenAICompatibleResponseMessage


class OpenAICompatibleChatCompletionResponse(BaseModel):
    """Top-level chat-completions response payload."""

    choices: list[OpenAICompatibleChoice]
    usage: dict[str, object] | None = None
