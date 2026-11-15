"""Shared types and low-level helpers for HTTP session message indexing."""

from __future__ import annotations

from dataclasses import dataclass

from yoke.agent.models import ConversationEntry


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
