"""Compatibility adapters for the authoritative session-tree module."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.models import ConversationEntry
from yoke.agent.session_tree import ConversationProjection
from yoke.agent.session_tree import ConversationView
from yoke.agent.session_tree import SessionTree
from yoke.agent.session_tree import memory_message_has_continuation_note
from yoke.agent.session_tree import parse_memory_message
from yoke.agent.session_tree import render_memory_message


def project_conversation(
    entries: Sequence[ConversationEntry] | None,
    *,
    leaf_id: str | None = None,
) -> ConversationView:
    """Project a legacy entry sequence through the session-tree seam."""
    tree = SessionTree.restore(entries, leaf_id=leaf_id)
    return tree.project(ConversationProjection())


__all__ = [
    "ConversationProjection",
    "ConversationView",
    "memory_message_has_continuation_note",
    "parse_memory_message",
    "project_conversation",
    "render_memory_message",
]
