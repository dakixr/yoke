"""Public SDK value types and input helpers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Literal


from yoke.agent.compaction.core import CompactionPolicy
from yoke.agent.loop.types import (
    AfterToolCallHook,
    BeforeToolCallHook,
    ToolExecutionMode,
)
from yoke.agent.models import (
    ConversationEntry,
    Message,
    MessageImageURLContentPart,
    MessageLocalImageContentPart,
)
from yoke.agent.skills import (
    ActiveSkill,
    SkillSpec,
    load_skill_registry,
)
from yoke.agent.skills.discovery import load_skill
from yoke.ai.sdk.helpers import image_part, remote_image_part

if TYPE_CHECKING:
    from yoke.agent.tools import LocalTool

    type AgentTool = LocalTool | type[LocalTool] | str
else:
    type AgentTool = object


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
            source_path=str(spec.skill_md_path),
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
        )

    def to_active_skill(self) -> ActiveSkill:
        """Convert this SDK skill into runtime active skill state."""
        return ActiveSkill(
            name=self.name,
            description=self.description,
            source_path=self.source_path,
            content=self.content,
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
        )


@dataclass(slots=True)
class RunConfig:
    """Configuration for the public SDK `Agent` facade."""

    root: str | Path
    sys_prompt: str | None = None
    tools: Sequence[AgentTool] = ()
    skills: Sequence[Skill] = ()
    include_agents_file: bool = True
    compaction: CompactionPolicy | None = None
    tool_execution: ToolExecutionMode = "parallel"
    before_tool_call: BeforeToolCallHook | None = None
    after_tool_call: AfterToolCallHook | None = None
    messages: Sequence[Message] | None = None
    conversation_entries: Sequence[ConversationEntry] | None = None


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


@dataclass(slots=True, frozen=True)
class BatchTask:
    """One independent prompt submitted to `run_many()`."""

    id: str
    prompt: str
    images: Sequence[Image | str | Path] = ()
    image_urls: Sequence[str] = ()


@dataclass(slots=True)
class BatchUsage:
    """Provider-reported usage aggregated across batch attempts."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass(slots=True)
class BatchItemResult[StructuredT]:
    """Terminal outcome for one batch task."""

    task: BatchTask
    status: Literal["completed", "error", "timed_out"]
    attempts: int
    duration_seconds: float
    result: AgentResult[StructuredT] | None = None
    error: BaseException | None = None
    usage: BatchUsage = field(default_factory=BatchUsage)


@dataclass(slots=True, frozen=True)
class BatchProgress:
    """Progress event emitted after one batch task reaches a terminal state."""

    task_id: str
    index: int
    completed: int
    total: int
    status: Literal["completed", "error", "timed_out"]
    attempts: int
    duration_seconds: float


@dataclass(slots=True)
class BatchResult[StructuredT]:
    """Input-ordered outcomes and aggregate metrics returned by `run_many()`."""

    items: list[BatchItemResult[StructuredT]]
    usage: BatchUsage
    duration_seconds: float
    progress_errors: list[Exception] = field(default_factory=list)

    @property
    def completed_count(self) -> int:
        """Return the number of successfully completed tasks."""
        return sum(item.status == "completed" for item in self.items)

    @property
    def failed_count(self) -> int:
        """Return the number of errored or timed-out tasks."""
        return len(self.items) - self.completed_count
