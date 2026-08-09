"""Typed projection specifications and immutable views."""

from __future__ import annotations

from dataclasses import dataclass

from yoke.agent.models import ConversationEntry
from yoke.agent.models import ConversationEntryKind
from yoke.agent.models import MemorySnapshot
from yoke.agent.models import Message

from .values import EntryRef
from .values import MessageView


@dataclass(frozen=True, slots=True)
class RuntimeProjection:
    """Request the active model-runtime context."""


@dataclass(frozen=True, slots=True)
class ProviderProjection:
    """Request semantic messages for a provider adapter."""


@dataclass(frozen=True, slots=True)
class ScrollbackProjection:
    """Request bounded human-readable active history."""

    limit: int = 400

    def __post_init__(self) -> None:
        """Validate the requested visible-message limit."""
        if self.limit < 0:
            raise ValueError("Scrollback limit cannot be negative.")


@dataclass(frozen=True, slots=True)
class AuditProjection:
    """Request a flat preorder view of all lineages."""


@dataclass(frozen=True, slots=True)
class ConversationProjection:
    """Request structured runtime state for compatibility adapters."""


@dataclass(frozen=True, slots=True)
class CheckpointView:
    """The checkpoint selected for the active branch."""

    ref: EntryRef
    summary_text: str
    generation: int
    mid_turn: bool


@dataclass(frozen=True, slots=True)
class RuntimeView:
    """Runtime messages and selected checkpoint."""

    current: EntryRef | None
    messages: tuple[MessageView, ...]
    checkpoint: CheckpointView | None


@dataclass(frozen=True, slots=True)
class ProviderView:
    """Provider messages derived through the shared checkpoint resolver."""

    messages: tuple[MessageView, ...]
    checkpoint_generation: int | None


@dataclass(frozen=True, slots=True)
class ScrollbackView:
    """Bounded active audit history for terminal replay."""

    messages: tuple[MessageView, ...]
    omitted_count: int
    notice: str | None


@dataclass(frozen=True, slots=True)
class AuditItem:
    """One entry in an iterative flat tree layout."""

    ref: EntryRef
    kind: ConversationEntryKind
    message: MessageView | None
    label: str | None
    created_at: str
    depth: int
    lineage: int
    sibling_index: int
    child_count: int
    on_active_path: bool
    current: bool


@dataclass(frozen=True, slots=True)
class AuditView:
    """All canonical entries in stable preorder."""

    items: tuple[AuditItem, ...]


@dataclass(frozen=True, slots=True)
class BranchEntryView:
    """Immutable branch content for navigation decisions and summaries."""

    ref: EntryRef
    kind: ConversationEntryKind
    message: MessageView | None
    summary_text: str | None


@dataclass(frozen=True, slots=True)
class NavigationPreview:
    """The effect of navigation before the tree selection changes."""

    target: EntryRef
    current: bool
    editor_text: str | None
    abandoned: tuple[BranchEntryView, ...]


@dataclass(frozen=True, slots=True)
class NavigationOutcome:
    """The result of an accepted navigation intent."""

    current: EntryRef | None
    editor_text: str | None
    summary_appended: bool


@dataclass(frozen=True, slots=True)
class ConversationView:
    """Structured active and compacted views from one tree decision."""

    active_entries: tuple[ConversationEntry, ...]
    runtime_entries: tuple[ConversationEntry, ...]
    checkpoint: MemorySnapshot | None
    provider_messages: tuple[Message, ...]
    runtime_messages: tuple[Message, ...]

    @property
    def transcript_messages(self) -> tuple[Message, ...]:
        """Return visible messages from the runtime branch."""
        return tuple(
            entry.message.model_copy(deep=True)
            for entry in self.runtime_entries
            if entry.kind not in {"instruction", "memory_snapshot"}
            and entry.message is not None
        )


@dataclass(slots=True)
class RuntimeContextSeed:
    """Owned canonical state prepared for one runtime context."""

    entries: list[ConversationEntry]
    leaf_id: str | None
    messages: list[Message]


type ProjectionSpec = (
    RuntimeProjection
    | ProviderProjection
    | ScrollbackProjection
    | AuditProjection
    | ConversationProjection
)
type ProjectionView = (
    RuntimeView | ProviderView | ScrollbackView | AuditView | ConversationView
)
