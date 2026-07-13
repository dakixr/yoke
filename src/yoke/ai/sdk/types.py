"""Public SDK value types and input helpers."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from yoke.agent.context import CompactionPolicy
from yoke.agent.loop.types import (
    AfterToolCallHook,
    BeforeToolCallHook,
    ToolExecutionMode,
)
from yoke.agent.loop.types import ConversationHistory
from yoke.agent.models import (
    ConversationEntry,
    Message,
    MessageImageURLContentPart,
    MessageLocalImageContentPart,
    MessageTextContentPart,
)
from yoke.agent.skills import (
    ActiveSkill,
    SkillSpec,
    load_skill_registry,
)
from yoke.agent.skills.discovery import load_skill
from yoke.ai.sdk.helpers import image_part, remote_image_part, text_part

if TYPE_CHECKING:
    from yoke.agent.capabilities import CapabilityInput
    from yoke.agent.tools import LocalTool
    from yoke.agent.tools import RegisterTools

    type AgentTool = LocalTool | type[LocalTool]
else:
    type AgentTool = object
    type CapabilityInput = object


class StructuredOutputError(ValueError):
    """Raised when a structured output cannot be parsed."""

    def __init__(self, message: str, *, output: str) -> None:
        super().__init__(message)
        self.output = output


@dataclass(slots=True)
class Context:
    """Conversation state used for SDK completions and agent runs."""

    sys_prompt: str | None = None
    messages: list[Message] = field(default_factory=list)

    @classmethod
    def from_prompt(
        cls,
        prompt: str,
        *,
        sys_prompt: str | None = None,
    ) -> Context:
        """Create a context from one prompt and an optional system prompt."""
        messages = [Message.user(prompt)]
        if sys_prompt is not None:
            messages.insert(0, Message.system(sys_prompt))
        return cls(sys_prompt=sys_prompt, messages=messages)


@dataclass(slots=True, frozen=True)
class Image:
    """Image input for `complete()` and `Agent.prompt()`."""

    content: MessageImageURLContentPart | MessageLocalImageContentPart

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        label: str | None = None,
        detail: str | None = None,
    ) -> Image:
        """Create an image input from a local path."""
        return cls(image_part(path, label=label, detail=detail))

    @classmethod
    def from_url(cls, url: str, *, detail: str | None = None) -> Image:
        """Create an image input from a remote URL or data URL."""
        return cls(remote_image_part(url, detail=detail))


@dataclass(slots=True, frozen=True)
class Skill:
    """SDK-native skill value."""

    name: str
    description: str
    content: str | None = None
    source_path: str = "<inline>"
    file_paths: tuple[str, ...] = ()

    @classmethod
    def inline(
        cls,
        name: str,
        sys_prompt: str,
        *,
        description: str | None = None,
    ) -> Skill:
        """Create an inline skill from Python code."""
        return cls(
            name=name,
            description=description or f"Inline skill: {name}.",
            content=sys_prompt,
            source_path="<inline>",
        )

    @classmethod
    def from_dir(cls, path: str | Path) -> Skill:
        """Load a skill from a directory containing `SKILL.md`."""
        spec = load_skill(Path(path).resolve())
        return cls(
            name=spec.name,
            description=spec.description,
            content=spec.load_content(),
            source_path=str(spec.skill_md_path),
            file_paths=tuple(spec.file_paths),
        )

    @classmethod
    def load_many(
        cls,
        names: Sequence[str] | None = None,
        *,
        dirs: Sequence[str | Path],
    ) -> list[Skill]:
        """Load named skills from skill directories."""
        registry = load_skill_registry(dirs)
        if names is None:
            names = [skill.name for skill in registry.skills]
        return [cls.from_active_skill(registry.activate(name)) for name in names]

    @classmethod
    def from_active_skill(cls, skill: ActiveSkill) -> Skill:
        """Create an SDK skill from a runtime active skill."""
        return cls(
            name=skill.name,
            description=skill.description,
            content=skill.content,
            source_path=skill.source_path,
            file_paths=tuple(skill.file_paths),
        )

    def to_active_skill(self) -> ActiveSkill:
        """Convert this SDK skill into runtime active skill state."""
        return ActiveSkill(
            name=self.name,
            description=self.description,
            source_path=self.source_path,
            content=self.content,
            file_paths=list(self.file_paths),
            reload_on_next_use=True,
        )

    def to_skill_spec(self) -> SkillSpec:
        """Convert this SDK skill into available skill metadata."""
        source_path = Path(self.source_path)
        root = source_path.parent if source_path.name == "SKILL.md" else Path()
        return SkillSpec(
            name=self.name,
            description=self.description,
            root=root,
            skill_md_path=source_path,
            file_paths=list(self.file_paths),
        )


@dataclass(slots=True)
class RunConfig:
    """Configuration for the public SDK `Agent` facade."""

    root: str | Path
    sys_prompt: str | None = None
    capabilities: Sequence[CapabilityInput] | None = None
    tools: Sequence[AgentTool] = ()
    register_tools: RegisterTools | None = None
    skills: Sequence[Skill] = ()
    include_agents_file: bool = True
    max_iterations: int = 10
    compaction: CompactionPolicy | None = None
    tool_execution: ToolExecutionMode = "parallel"
    before_tool_call: BeforeToolCallHook | None = None
    after_tool_call: AfterToolCallHook | None = None
    history: ConversationHistory | None = None


@dataclass(slots=True)
class CompletionResult[StructuredT]:
    """Result returned by `complete()`."""

    message: Message
    output: str
    messages: list[Message]
    structured: StructuredT | None = None


@dataclass(slots=True)
class AgentResult[StructuredT]:
    """Result returned by the public SDK `Agent.prompt()` method."""

    message: Message
    output: str
    messages: list[Message]
    iterations: int
    status: str = "completed"
    conversation_entries: list[ConversationEntry] | None = None
    structured: StructuredT | None = None


def normalize_image_inputs(
    *,
    images: Sequence[Image | str | Path],
    image_urls: Sequence[str],
) -> tuple[list[Image], list[str]]:
    """Normalize explicit Image values and path shortcuts."""
    normalized_images: list[Image] = []
    for image in images:
        if isinstance(image, Image):
            normalized_images.append(image)
        else:
            normalized_images.append(Image.from_path(image))
    return normalized_images, list(image_urls)


def build_user_message_from_images(
    text: str = "",
    *,
    images: Sequence[Image] = (),
    image_urls: Sequence[str] = (),
) -> Message:
    """Build a multimodal user message from SDK image inputs."""
    if not images and not image_urls:
        return Message.user(text)
    content = []
    if text:
        content.append(text_part(text))
    image_index = 1
    for image in images:
        part = image.content
        if isinstance(part, MessageLocalImageContentPart):
            copied = part.model_copy(deep=True)
            if copied.label is None:
                copied.label = f"[Image #{image_index}]"
            content.append(copied)
        else:
            content.append(part)
        image_index += 1
    for image_url in image_urls:
        content.append(remote_image_part(image_url))
        image_index += 1
    return Message.user(content)


def parse_structured_output[StructuredT](
    output: str,
    *,
    output_type: type[StructuredT] | None,
) -> StructuredT | None:
    """Parse final text into a structured output value."""
    if output_type is None:
        return None
    try:
        return TypeAdapter(output_type).validate_json(output)
    except Exception as exc:
        raise StructuredOutputError(
            f"Failed to parse structured output as {output_type.__name__}.",
            output=output,
        ) from exc


def structured_output_instructions(output_type: type[object]) -> str:
    """Build model-facing instructions for structured SDK outputs."""
    schema = TypeAdapter(output_type).json_schema()
    schema_json = json.dumps(schema, ensure_ascii=False, indent=2)
    return (
        "Return exactly one valid JSON object matching this JSON Schema. "
        "Do not include markdown fences, prose, comments, or extra keys. "
        "Use the exact field names and required fields from the schema.\n\n"
        f"JSON Schema:\n{schema_json}"
    )


def append_structured_output_instructions(
    message: Message,
    *,
    output_type: type[object],
) -> Message:
    """Return a user message with structured-output instructions appended."""
    instruction = structured_output_instructions(output_type)
    copied = message.model_copy(deep=True)
    if isinstance(copied.content, list):
        copied.content.append(MessageTextContentPart(text=instruction))
    elif isinstance(copied.content, str):
        copied.content = f"{copied.content}\n\n{instruction}"
    else:
        copied.content = instruction
    return copied
