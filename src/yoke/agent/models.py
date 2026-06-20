"""Core data models for agent messages, tool calls, and conversation state."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence
from datetime import UTC
from datetime import datetime
import secrets
from typing import Annotated
from typing import Literal
from typing import cast

from pydantic import BaseModel
from pydantic import Field

from yoke.agent.compaction.types import CompactionBoundary
from yoke.agent.compaction.types import CompactionReason
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec

Role = Literal["system", "user", "assistant", "tool"]
MessagePhase = Literal["commentary", "final_answer"]
ConversationEntryKind = Literal[
    "instruction",
    "user",
    "assistant",
    "assistant_tool_calls",
    "tool_result",
    "control",
    "memory_snapshot",
    "skill_event",
    "compaction_summary",
    "branch_summary",
]


class ToolFunction(BaseModel):
    """Describes the function name and arguments of a tool call."""

    name: str
    arguments: str


class ToolCall(BaseModel):
    """Represents a single tool call issued by the model."""

    id: str
    type: str = "function"
    function: ToolFunction


class TokenUsageDetails(BaseModel):
    """Provider-reported token usage details."""

    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    audio_tokens: int | None = None
    accepted_prediction_tokens: int | None = None
    rejected_prediction_tokens: int | None = None


class TokenUsage(BaseModel):
    """Normalized provider token usage for one assistant response."""

    provider_name: str | None = None
    model_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    input_details: TokenUsageDetails = Field(default_factory=TokenUsageDetails)
    output_details: TokenUsageDetails = Field(default_factory=TokenUsageDetails)
    estimated_input_tokens: int | None = None
    estimated_total_with_reserve: int | None = None
    raw: dict[str, object] = Field(default_factory=dict)


class MessageTextContentPart(BaseModel):
    """A text content part for multimodal user messages."""

    type: Literal["text"] = "text"
    text: str


class MessageImageURL(BaseModel):
    """Provider-compatible image URL payload."""

    url: str


class MessageImageURLContentPart(BaseModel):
    """A remote or pre-encoded image content part."""

    type: Literal["image_url"] = "image_url"
    image_url: MessageImageURL
    detail: str | None = None


class MessageLocalImageContentPart(BaseModel):
    """A local image attachment resolved during provider serialization."""

    type: Literal["local_image"] = "local_image"
    path: str
    detail: str | None = None
    label: str | None = None

    @property
    def filename(self) -> str:
        """Return the attachment filename for display."""
        return Path(self.path).name

    @property
    def display_label(self) -> str:
        """Return the stable model-facing label for this image."""
        return self.label or self.filename


type MessageContentPart = Annotated[
    MessageTextContentPart | MessageImageURLContentPart | MessageLocalImageContentPart,
    Field(discriminator="type"),
]
type MessageContent = str | list[MessageContentPart] | None


class Message(BaseModel):
    """A single message in the conversation history."""

    role: Role
    content: MessageContent = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    phase: MessagePhase | None = None
    reasoning_content: str | None = None
    reasoning_signature: str | None = None
    usage: TokenUsage | None = None

    @classmethod
    def system(cls, content: str) -> Message:
        """Create a system message."""
        return cls(role="system", content=content)

    @classmethod
    def user(
        cls,
        content: str | Sequence[MessageContentPart | dict[str, object]],
    ) -> Message:
        """Create a user message."""
        normalized: MessageContent
        if isinstance(content, str):
            normalized = content
        else:
            normalized = cast(MessageContent, list(content))
        return cls(role="user", content=normalized)

    @classmethod
    def assistant(cls, content: str, *, phase: MessagePhase | None = None) -> Message:
        """Create an assistant message."""
        return cls(role="assistant", content=content, phase=phase)

    @classmethod
    def commentary(cls, content: str) -> Message:
        """Create a mid-turn assistant commentary message."""
        return cls(role="assistant", content=content, phase="commentary")

    @classmethod
    def tool(cls, tool_call_id: str, content: str) -> Message:
        """Create a tool result message."""
        return cls(role="tool", tool_call_id=tool_call_id, content=content)

    @property
    def plain_text_content(self) -> str | None:
        """Return plain text content when the message is text-only."""
        return self.content if isinstance(self.content, str) else None

    def text_content(self) -> str | None:
        """Return a readable text projection of the message content."""
        if isinstance(self.content, str) or self.content is None:
            return self.content
        text_parts = [
            part.text
            for part in self.content
            if isinstance(part, MessageTextContentPart) and part.text
        ]
        if text_parts:
            return "\n".join(text_parts)
        image_labels = [
            part.display_label
            if isinstance(part, MessageLocalImageContentPart)
            else "[Image]"
            for part in self.content
            if isinstance(
                part,
                MessageImageURLContentPart | MessageLocalImageContentPart,
            )
        ]
        if not image_labels:
            return ""
        return " ".join(image_labels)

    def display_text_content(self) -> str | None:
        """Return transcript-friendly text preserving user prompt text parts."""
        return self.text_content()

    def final_text_content(self) -> str | None:
        """Return assistant text that should count as final answer output."""
        if self.role == "assistant" and self.phase == "commentary":
            return None
        text = self.display_text_content()
        if text:
            return text
        if self.role == "assistant" and not self.tool_calls:
            return self.reasoning_content
        return text

    def commentary_text_content(self) -> str | None:
        """Return assistant text that should be shown as mid-turn commentary."""
        if self.role != "assistant":
            return None
        text = self.text_content()
        if not text:
            return None
        if self.phase == "commentary":
            return text
        if self.phase is None and self.tool_calls:
            return text
        return None

    def has_image_inputs(self) -> bool:
        """Return whether this message contains any image input parts."""
        if not isinstance(self.content, list):
            return False
        return any(
            isinstance(
                part,
                MessageImageURLContentPart | MessageLocalImageContentPart,
            )
            for part in self.content
        )

    def to_api_dict(self) -> dict[str, object]:
        """Serialize this message to the dict shape expected by providers."""
        payload: dict[str, object] = {"role": self.role}
        if self.content is not None or self.role == "assistant":
            if isinstance(self.content, list):
                payload["content"] = [
                    part.model_dump(mode="json") for part in self.content
                ]
            else:
                payload["content"] = self.content
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            payload["tool_calls"] = [
                tool_call.model_dump() for tool_call in self.tool_calls
            ]
        if self.reasoning_content is not None:
            payload["reasoning_content"] = self.reasoning_content
        return payload


class ConversationEntry(BaseModel):
    """A single entry in the structured conversation log."""

    kind: ConversationEntryKind
    message: Message | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
    id: str = Field(default_factory=lambda: secrets.token_hex(8))
    parent_id: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class ConversationLog(BaseModel):
    """Ordered log of all conversation entries."""

    entries: list[ConversationEntry] = Field(default_factory=list)


class CompactionHandoff(BaseModel):
    """A typed handoff produced when older context is compacted."""

    summary_text: str
    reason: CompactionReason
    boundary: CompactionBoundary
    summarized_messages: int
    retained_user_messages: int
    retained_messages: list[Message] = Field(default_factory=list)
    generation: int = 1
    input_tokens: int | None = None
    total_tokens: int | None = None


class MemorySnapshot(BaseModel):
    """A persisted summary snapshot of the conversation used for compaction."""

    id: str
    summary_text: str
    compaction_handoff: CompactionHandoff | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class WorkingMemory(BaseModel):
    """Holds the current memory snapshot for the active agent context."""

    current_snapshot: MemorySnapshot | None = None


class AgentContext(BaseModel):
    """Runtime context for a single agent run including messages and memory."""

    system_prompt: str | None = None
    messages: list[Message] = Field(default_factory=list)
    instructions: list[Message] = Field(default_factory=list)
    conversation_log: ConversationLog = Field(default_factory=ConversationLog)
    memory: WorkingMemory = Field(default_factory=WorkingMemory)
    available_skills: list[SkillSpec] = Field(default_factory=list)
    active_skills: list[ActiveSkill] = Field(default_factory=list)
