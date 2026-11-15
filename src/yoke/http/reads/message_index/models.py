"""Shared types and low-level helpers for HTTP session message indexing."""

from __future__ import annotations

from dataclasses import dataclass

from yoke.agent.models import ConversationEntry
from yoke.http.services.projectors import PUBLIC_CHAT_EXCLUDED_KINDS

# Kept as an import-compatible alias for the index helpers. The policy itself
# belongs to the public chat projector used by indexed and fallback reads.
PUBLIC_EXCLUDED_KINDS = PUBLIC_CHAT_EXCLUDED_KINDS


@dataclass(frozen=True, slots=True)
class MessageIndexSnapshot:
    """Entry topology at one stored session signature.

    ``entries`` maps id -> [parent_id, kind, stored position]; positions follow
    stored order. Payloads stay in the database until ``read_entries``.
    """

    signature: tuple[int, int]
    leaf_id: str | None
    entries: dict[str, list[object]]


@dataclass(frozen=True, slots=True)
class MessagePage:
    entries: list[ConversationEntry]
    has_more: bool


@dataclass(frozen=True, slots=True)
class TreeIndexPage:
    entries: list[ConversationEntry]
    leaf_id: str | None
    active_ids: frozenset[str]
    child_counts: dict[str, int]
    total_entries: int
    has_more: bool
    legacy_tool_context_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ContextIndexWindow:
    entries: list[ConversationEntry]
    total_entries: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class RuntimeIndexSeed:
    """Compaction-bounded active entries sufficient to rebuild model state."""

    entries: list[ConversationEntry]
    leaf_id: str
    total_entries: int


@dataclass(frozen=True, slots=True)
class NavigationIndexPreview:
    target: ConversationEntry
    current: bool
    abandoned: list[ConversationEntry]
    abandoned_total: int
    abandoned_truncated: bool


def parent_id(location: list[object]) -> str | None:
    value = location[0]
    return value if isinstance(value, str) else None


def kind(location: list[object]) -> str:
    value = location[1]
    return value if isinstance(value, str) else "control"
