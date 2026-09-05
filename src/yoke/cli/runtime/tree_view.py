"""Session tree display and label helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from yoke.agent.models import ConversationEntry
from yoke.agent.session_tree import SessionTree
from yoke.cli.runtime.base import ActiveSession

TreeFilterMode = Literal[
    "default",
    "no-tools",
    "user-only",
    "labeled-only",
    "all",
]
_FILTER_BITS: dict[TreeFilterMode, int] = {
    "default": 1,
    "no-tools": 2,
    "user-only": 4,
    "labeled-only": 8,
    "all": 16,
}


@dataclass(slots=True)
class TreeViewIndex:
    """Reusable filter candidates and search generation for one tree."""

    candidates: dict[int, tuple[TreeNode, ...]]
    all_nodes: tuple[TreeNode, ...]
    generation: int = 0


@dataclass(slots=True)
class TreeNode:
    """A tree node for one conversation entry."""

    entry: ConversationEntry
    children: list[TreeNode]
    label: str | None = None
    active: bool = False
    filter_mask: int = 0
    subtree_filter_mask: int = 0
    search_text: str | None = None
    parent: TreeNode | None = None
    match_generation: int = 0
    subtree_generation: int = 0
    view_index: TreeViewIndex | None = None


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
    source = active_session.record.conversation_entries
    active_ids = {entry.id for entry in active_session.tree_index.active_entry_refs()}
    nodes_by_id: dict[str, TreeNode] = {}
    roots: list[TreeNode] = []
    for entry in source:
        label = entry.metadata.get("label")
        node = TreeNode(
            entry=entry,
            children=[],
            label=label if isinstance(label, str) else None,
            active=entry.id in active_ids,
        )
        nodes_by_id[entry.id] = node
        if entry.parent_id is None:
            roots.append(node)
        else:
            parent = nodes_by_id[entry.parent_id]
            node.parent = parent
            parent.children.append(node)
    for node in nodes_by_id.values():
        node.filter_mask = _filter_mask(node)
        if node.filter_mask & _FILTER_BITS["default"]:
            node.search_text = _search_text(node).lower()
        if len(node.children) > 1:
            node.children.sort(key=lambda child: child.entry.created_at)
    for node in reversed(nodes_by_id.values()):
        node.subtree_filter_mask = node.filter_mask
        for child in node.children:
            node.subtree_filter_mask |= child.subtree_filter_mask
    all_nodes = tuple(nodes_by_id.values())
    default_bit = _FILTER_BITS["default"]
    view_index = TreeViewIndex(
        candidates={
            default_bit: tuple(
                node for node in all_nodes if node.filter_mask & default_bit
            )
        },
        all_nodes=all_nodes,
    )
    for root in roots:
        root.view_index = view_index
    roots.sort(key=lambda node: node.entry.created_at)
    return roots


def flatten_tree_rows(  # noqa: C901
    roots: list[TreeNode],
    *,
    current_leaf_id: str | None,
    filter_mode: TreeFilterMode = "default",
    search: str = "",
    folded_ids: set[str] | None = None,
) -> list[TreeRow]:
    """Return visible rows for selector rendering."""
    folded = folded_ids or set()
    query_tokens = [token for token in search.lower().split() if token]
    filter_bit = _FILTER_BITS[filter_mode]
    search_generation = 0
    if query_tokens:
        search_generation = _mark_subtree_matches(
            roots,
            filter_mode,
            query_tokens,
        )

    def node_matches(node: TreeNode) -> bool:
        if node.entry.id == current_leaf_id:
            return True
        if query_tokens:
            return node.match_generation == search_generation
        return bool(node.filter_mask & filter_bit)

    def subtree_matches(node: TreeNode) -> bool:
        if query_tokens:
            return node.active or node.subtree_generation == search_generation
        return node.active or bool(node.subtree_filter_mask & filter_bit)

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
    visible_roots = [root for root in _active_last(roots) if subtree_matches(root)]
    for index, root in enumerate(visible_roots):
        root_active = root.active
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
            child for child in _active_last(node.children) if subtree_matches(child)
        ]
        if node_matches(node):
            rows.append(
                TreeRow(
                    entry=node.entry,
                    depth=depth,
                    graph_prefix=graph_prefix,
                    label=node.label,
                    active=node.active,
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
            child_active = child.active
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
            elif child.active:
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


def default_folded_tree_ids(roots: Iterable[TreeNode]) -> set[str]:
    """Fold inactive branches at their first default-visible entry."""
    folded: set[str] = set()
    stack = list(roots)
    while stack:
        node = stack.pop()
        if node.active:
            stack.extend(node.children)
            continue
        if node.filter_mask & _FILTER_BITS["default"]:
            if node.children:
                folded.add(node.entry.id)
            continue
        stack.extend(node.children)
    return folded


def set_entry_label(
    active_session: ActiveSession,
    entry_id: str,
    label: str | None,
) -> None:
    """Persist a selector label on an entry's metadata."""
    tree = SessionTree.borrow_validated(
        active_session.record.conversation_entries,
        active_session.record.leaf_id,
    )
    tree.set_label(tree.ref_from_persisted_id(entry_id), label)
    entry = tree.export_entry_for_persistence(entry_id)
    with active_session.save_lock:
        active_session.record = active_session.store.save_entry_metadata(
            active_session.id,
            entry,
            existing_record=active_session.record,
            tree_index=active_session.tree_index,
        )


def _node_matches(
    node: TreeNode,
    filter_mode: TreeFilterMode,
    query_tokens: list[str],
) -> bool:
    if not _filter_matches(node, filter_mode):
        return False
    if not query_tokens:
        return True
    if node.search_text is None:
        node.search_text = _search_text(node).lower()
    text = node.search_text
    return all(token in text for token in query_tokens)


def _mark_subtree_matches(
    roots: list[TreeNode],
    filter_mode: TreeFilterMode,
    query_tokens: list[str],
) -> int:
    view_index = roots[0].view_index if roots else None
    if view_index is None:
        return 0
    view_index.generation += 1
    generation = view_index.generation
    filter_bit = _FILTER_BITS[filter_mode]
    candidates = view_index.candidates.get(filter_bit)
    if candidates is None:
        candidates = tuple(
            node for node in view_index.all_nodes if node.filter_mask & filter_bit
        )
        view_index.candidates[filter_bit] = candidates
    for node in candidates:
        if not _node_matches(node, filter_mode, query_tokens):
            continue
        node.match_generation = generation
        ancestor: TreeNode | None = node
        while ancestor is not None and ancestor.subtree_generation != generation:
            ancestor.subtree_generation = generation
            ancestor = ancestor.parent
    return generation


def _filter_matches(node: TreeNode, filter_mode: TreeFilterMode) -> bool:
    return bool(node.filter_mask & _FILTER_BITS[filter_mode])


def _filter_mask(node: TreeNode) -> int:
    entry = node.entry
    if entry.kind == "instruction":
        return 0
    mask = _FILTER_BITS["all"]
    if node.label:
        mask |= _FILTER_BITS["labeled-only"]
    if entry.kind == "user":
        mask |= _FILTER_BITS["user-only"] | _FILTER_BITS["default"]
    elif entry.kind == "assistant":
        mask |= _FILTER_BITS["default"]
    if entry.kind in {"memory_snapshot", "skill_event", "tool_result"}:
        return mask
    if entry.kind == "assistant_tool_calls":
        text = entry.message.display_text_content() if entry.message else None
        if not text or not text.strip():
            return mask
    return mask | _FILTER_BITS["no-tools"]


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


def _active_last(
    nodes: Iterable[TreeNode],
) -> list[TreeNode]:
    inactive: list[TreeNode] = []
    active: list[TreeNode] = []
    for node in nodes:
        target = active if node.active else inactive
        target.append(node)
    return [*inactive, *active]
