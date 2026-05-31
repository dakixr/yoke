"""Session tree display and label helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from yoke.agent.models import ConversationEntry
from yoke.agent.state import migrate_conversation_tree
from yoke.agent.state import transcript_messages_from_entries
from yoke.cli.runtime.base import ActiveSession
from yoke.cli.runtime.session import save_active_session

TreeFilterMode = Literal[
    "default",
    "no-tools",
    "user-only",
    "labeled-only",
    "all",
]


@dataclass(slots=True)
class TreeNode:
    """A tree node for one conversation entry."""

    entry: ConversationEntry
    children: list[TreeNode]
    label: str | None = None


@dataclass(slots=True)
class TreeRow:
    """A flattened render row for the tree selector."""

    entry: ConversationEntry
    depth: int
    graph_prefix: str
    label: str | None
    active: bool
    current: bool
    has_children: bool
    folded: bool
    branch_index: int = 0


def get_session_tree(active_session: ActiveSession) -> list[TreeNode]:
    """Build the persisted session tree."""
    entries, leaf_id, changed = migrate_conversation_tree(
        active_session.record.conversation_entries,
        leaf_id=active_session.record.leaf_id,
    )
    if changed:
        save_active_session(
            active_session,
            transcript_messages_from_entries(entries, leaf_id=leaf_id),
            conversation_entries=entries,
            leaf_id=leaf_id,
        )
    nodes = {
        entry.id: TreeNode(
            entry=entry.model_copy(deep=True),
            children=[],
            label=_entry_label(entry),
        )
        for entry in entries
    }
    roots: list[TreeNode] = []
    for entry in entries:
        node = nodes[entry.id]
        parent = nodes.get(entry.parent_id or "")
        if parent is None or entry.parent_id == entry.id:
            roots.append(node)
        else:
            parent.children.append(node)
    for node in nodes.values():
        node.children.sort(key=lambda child: child.entry.created_at)
    roots.sort(key=lambda node: node.entry.created_at)
    return roots


def flatten_tree_rows(
    roots: list[TreeNode],
    *,
    current_leaf_id: str | None,
    filter_mode: TreeFilterMode = "default",
    search: str = "",
    folded_ids: set[str] | None = None,
) -> list[TreeRow]:
    """Return visible rows for selector rendering."""
    folded = folded_ids or set()
    active_path = _active_path_ids(roots, current_leaf_id)
    query_tokens = [token for token in search.lower().split() if token]
    rows: list[TreeRow] = []
    next_branch_index = 1

    def push_visit(
        stack: list[tuple[TreeNode, int, str, str, int]],
        node: TreeNode,
        depth: int,
        graph_prefix: str,
        child_prefix: str,
        branch_index: int,
    ) -> None:
        stack.append((node, depth, graph_prefix, child_prefix, branch_index))

    stack: list[tuple[TreeNode, int, str, str, int]] = []
    pending_roots: list[tuple[TreeNode, int, str, str, int]] = []
    visible_roots = [
        root
        for root in _active_first(roots, active_path)
        if _subtree_matches(root, filter_mode, query_tokens)
    ]
    for index, root in enumerate(visible_roots):
        root_active = root.entry.id in active_path
        root_has_siblings = len(visible_roots) > 1
        root_is_last = index == len(visible_roots) - 1
        if root_active or not root_has_siblings:
            root_graph_prefix = ""
            root_child_prefix = ""
        else:
            root_graph_prefix = "└─ " if root_is_last else "├─ "
            root_child_prefix = "   " if root_is_last else "│  "
        pending_roots.append((root, 0, root_graph_prefix, root_child_prefix, index))
    for root_args in reversed(pending_roots):
        push_visit(stack, *root_args)

    while stack:
        node, depth, graph_prefix, child_prefix, branch_index = stack.pop()
        visible_children = [
            child
            for child in _active_first(node.children, active_path)
            if _subtree_matches(child, filter_mode, query_tokens)
        ]
        if _node_matches(node, filter_mode, query_tokens):
            rows.append(
                TreeRow(
                    entry=node.entry,
                    depth=depth,
                    graph_prefix=graph_prefix,
                    label=node.label,
                    active=node.entry.id in active_path,
                    current=node.entry.id == current_leaf_id,
                    has_children=bool(visible_children),
                    folded=node.entry.id in folded,
                    branch_index=branch_index,
                )
            )
            child_depth = depth + 1
        else:
            child_depth = depth
        if node.entry.id in folded:
            continue
        pending_children: list[tuple[TreeNode, int, str, str, int]] = []
        for index, child in enumerate(visible_children):
            child_has_siblings = len(visible_children) > 1
            child_active = child.entry.id in active_path
            child_is_last = index == len(visible_children) - 1
            if child_active:
                next_graph_prefix = ""
                next_child_prefix = ""
            elif child_has_siblings:
                connector = "└─ " if child_is_last else "├─ "
                lane = "   " if child_is_last else "│  "
                next_graph_prefix = f"{child_prefix}{connector}"
                next_child_prefix = f"{child_prefix}{lane}"
            else:
                next_graph_prefix = child_prefix
                next_child_prefix = child_prefix
            if len(visible_children) == 1:
                child_branch_index = branch_index
            elif child.entry.id in active_path:
                child_branch_index = branch_index
            else:
                child_branch_index = next_branch_index
                next_branch_index += 1
            pending_children.append(
                (
                    child,
                    child_depth,
                    next_graph_prefix,
                    next_child_prefix,
                    child_branch_index,
                )
            )
        for child_args in reversed(pending_children):
            push_visit(stack, *child_args)
    return rows


def set_entry_label(
    active_session: ActiveSession,
    entry_id: str,
    label: str | None,
) -> None:
    """Persist a selector label on an entry's metadata."""
    entries = [
        entry.model_copy(deep=True)
        for entry in active_session.record.conversation_entries
    ]
    for entry in entries:
        if entry.id != entry_id:
            continue
        normalized = " ".join((label or "").split()).strip()
        metadata = dict(entry.metadata)
        if normalized:
            metadata["label"] = normalized
        else:
            metadata.pop("label", None)
        entry.metadata = metadata
        save_active_session(
            active_session,
            transcript_messages_from_entries(
                entries,
                leaf_id=active_session.record.leaf_id,
            ),
            conversation_entries=entries,
            leaf_id=active_session.record.leaf_id,
        )
        return
    raise ValueError(f"Tree entry not found: {entry_id}")


def _entry_label(entry: ConversationEntry) -> str | None:
    label = entry.metadata.get("label")
    return label if isinstance(label, str) and label.strip() else None


def _node_matches(
    node: TreeNode,
    filter_mode: TreeFilterMode,
    query_tokens: list[str],
) -> bool:
    if not _filter_matches(node, filter_mode):
        return False
    if not query_tokens:
        return True
    text = _search_text(node).lower()
    return all(token in text for token in query_tokens)


def _subtree_matches(
    node: TreeNode,
    filter_mode: TreeFilterMode,
    query_tokens: list[str],
) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if _node_matches(current, filter_mode, query_tokens):
            return True
        stack.extend(current.children)
    return False


def _filter_matches(node: TreeNode, filter_mode: TreeFilterMode) -> bool:
    entry = node.entry
    if entry.kind == "instruction":
        return False
    if filter_mode == "all":
        return True
    if filter_mode == "labeled-only":
        return bool(node.label)
    if filter_mode == "user-only":
        return entry.kind == "user"
    if filter_mode == "default":
        return entry.kind in {"user", "assistant"}
    if entry.kind in {"memory_snapshot", "skill_event"}:
        return False
    if filter_mode == "no-tools" and entry.kind == "tool_result":
        return False
    if entry.kind == "assistant_tool_calls":
        text = entry.message.display_text_content() if entry.message else None
        return bool(text and text.strip())
    return True


def _search_text(node: TreeNode) -> str:
    entry = node.entry
    parts = [entry.kind, node.label or ""]
    if entry.message is not None:
        parts.append(entry.message.display_text_content() or "")
        parts.append(entry.message.role)
    summary = entry.metadata.get("summary")
    if isinstance(summary, str):
        parts.append(summary)
    return " ".join(parts)


def _active_path_ids(
    roots: list[TreeNode],
    current_leaf_id: str | None,
) -> set[str]:
    if current_leaf_id is None:
        return set()
    nodes = {node.entry.id: node for node in _walk_nodes(roots)}
    active: set[str] = set()
    current_id: str | None = current_leaf_id
    while current_id is not None and current_id not in active:
        node = nodes.get(current_id)
        if node is None:
            break
        active.add(current_id)
        current_id = node.entry.parent_id
    return active


def _active_first(
    nodes: Iterable[TreeNode],
    active_path: set[str],
) -> list[TreeNode]:
    return sorted(
        nodes,
        key=lambda node: (
            1 if _contains_active(node, active_path) else 0,
            node.entry.created_at,
        ),
    )


def _contains_active(node: TreeNode, active_path: set[str]) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.entry.id in active_path:
            return True
        stack.extend(current.children)
    return False


def _walk_nodes(nodes: Iterable[TreeNode]) -> Iterable[TreeNode]:
    stack = list(reversed(list(nodes)))
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))
