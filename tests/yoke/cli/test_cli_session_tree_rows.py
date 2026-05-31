# ruff: noqa: D100, D103, S101

from __future__ import annotations

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.cli.runtime.tree import TreeNode
from yoke.cli.runtime.tree import collect_abandoned_branch_entries
from yoke.cli.runtime.tree import flatten_tree_rows


def test_tree_rows_show_sibling_branch_connectors() -> None:
    root = ConversationEntry(kind="user", message=Message.user("root"))
    first = ConversationEntry(
        kind="assistant",
        message=Message.assistant("first"),
        parent_id=root.id,
    )
    first_child = ConversationEntry(
        kind="user",
        message=Message.user("first child"),
        parent_id=first.id,
    )
    second = ConversationEntry(
        kind="assistant",
        message=Message.assistant("second"),
        parent_id=root.id,
    )
    roots = [
        TreeNode(
            entry=root,
            children=[
                TreeNode(
                    entry=first,
                    children=[TreeNode(entry=first_child, children=[])],
                ),
                TreeNode(entry=second, children=[]),
            ],
        )
    ]

    rows = flatten_tree_rows(
        roots,
        current_leaf_id=second.id,
        filter_mode="default",
    )

    assert [(row.entry.id, row.graph_prefix) for row in rows] == [
        (root.id, ""),
        (first.id, "├─ "),
        (first_child.id, "│  "),
        (second.id, ""),
    ]

    from yoke.cli.interactive.tree_selector import _format_row

    rendered_first_child = "".join(
        fragment[1] for fragment in _format_row(rows[2], 120)
    )
    assert rendered_first_child.startswith("│  user")


def test_tree_rows_keep_active_branch_flat_after_fork() -> None:
    root = ConversationEntry(kind="user", message=Message.user("root"))
    side = ConversationEntry(
        kind="assistant",
        message=Message.assistant("side"),
        parent_id=root.id,
    )
    active = ConversationEntry(
        kind="assistant",
        message=Message.assistant("active"),
        parent_id=root.id,
    )
    active_child = ConversationEntry(
        kind="user",
        message=Message.user("active child"),
        parent_id=active.id,
    )
    roots = [
        TreeNode(
            entry=root,
            children=[
                TreeNode(entry=side, children=[]),
                TreeNode(
                    entry=active,
                    children=[TreeNode(entry=active_child, children=[])],
                ),
            ],
        )
    ]

    rows = flatten_tree_rows(
        roots,
        current_leaf_id=active_child.id,
        filter_mode="default",
    )

    assert [(row.entry.id, row.graph_prefix) for row in rows] == [
        (root.id, ""),
        (side.id, "├─ "),
        (active.id, ""),
        (active_child.id, ""),
    ]


def test_tree_rows_do_not_indent_single_child_chain() -> None:
    first = ConversationEntry(kind="user", message=Message.user("first"))
    second = ConversationEntry(
        kind="assistant",
        message=Message.assistant("second"),
        parent_id=first.id,
    )
    third = ConversationEntry(
        kind="user",
        message=Message.user("third"),
        parent_id=second.id,
    )
    roots = [
        TreeNode(
            entry=first,
            children=[
                TreeNode(
                    entry=second,
                    children=[TreeNode(entry=third, children=[])],
                )
            ],
        )
    ]

    rows = flatten_tree_rows(
        roots,
        current_leaf_id=third.id,
        filter_mode="default",
    )

    assert [row.graph_prefix for row in rows] == ["", "", ""]


def test_tree_rows_handle_deep_single_child_chain() -> None:
    entries = [ConversationEntry(kind="user", message=Message.user("0"))]
    root = TreeNode(entry=entries[0], children=[])
    current = root
    for index in range(1, 1200):
        entry = ConversationEntry(
            kind="assistant",
            message=Message.assistant(str(index)),
            parent_id=current.entry.id,
        )
        child = TreeNode(entry=entry, children=[])
        current.children.append(child)
        entries.append(entry)
        current = child

    rows = flatten_tree_rows(
        [root],
        current_leaf_id=entries[-1].id,
        filter_mode="default",
    )

    assert len(rows) == len(entries)
    assert rows[-1].entry.id == entries[-1].id
    assert rows[-1].current is True


def test_tree_rows_indent_only_nested_branch_lanes() -> None:
    root = ConversationEntry(kind="user", message=Message.user("root"))
    side = ConversationEntry(
        kind="assistant",
        message=Message.assistant("side"),
        parent_id=root.id,
    )
    side_child = ConversationEntry(
        kind="user",
        message=Message.user("side child"),
        parent_id=side.id,
    )
    nested_first = ConversationEntry(
        kind="assistant",
        message=Message.assistant("nested first"),
        parent_id=side_child.id,
    )
    nested_answer = ConversationEntry(
        kind="user",
        message=Message.user("nested answer"),
        parent_id=nested_first.id,
    )
    nested_second = ConversationEntry(
        kind="assistant",
        message=Message.assistant("nested second"),
        parent_id=side_child.id,
    )
    active = ConversationEntry(
        kind="assistant",
        message=Message.assistant("active"),
        parent_id=root.id,
    )
    roots = [
        TreeNode(
            entry=root,
            children=[
                TreeNode(
                    entry=side,
                    children=[
                        TreeNode(
                            entry=side_child,
                            children=[
                                TreeNode(
                                    entry=nested_first,
                                    children=[
                                        TreeNode(
                                            entry=nested_answer,
                                            children=[],
                                        )
                                    ],
                                ),
                                TreeNode(entry=nested_second, children=[]),
                            ],
                        )
                    ],
                ),
                TreeNode(entry=active, children=[]),
            ],
        )
    ]

    rows = flatten_tree_rows(
        roots,
        current_leaf_id=active.id,
        filter_mode="default",
    )

    assert [(row.entry.id, row.graph_prefix) for row in rows] == [
        (root.id, ""),
        (side.id, "├─ "),
        (side_child.id, "│  "),
        (nested_first.id, "│  ├─ "),
        (nested_answer.id, "│  │  "),
        (nested_second.id, "│  └─ "),
        (active.id, ""),
    ]


def test_tree_row_format_marks_only_current_leaf() -> None:
    first = ConversationEntry(kind="user", message=Message.user("first"))
    second = ConversationEntry(
        kind="assistant",
        message=Message.assistant("second"),
        parent_id=first.id,
    )
    roots = [
        TreeNode(
            entry=first,
            children=[TreeNode(entry=second, children=[])],
        )
    ]

    rows = flatten_tree_rows(
        roots,
        current_leaf_id=second.id,
        filter_mode="default",
    )

    from yoke.cli.interactive.tree_selector import _format_row

    rendered_first = _format_row(rows[0], 120)
    rendered_second = _format_row(rows[1], 120)

    assert "".join(fragment[1] for fragment in rendered_first).startswith("user: first")
    assert rendered_second[0] == ("ansiyellow", "● ")
    assert "".join(fragment[1] for fragment in rendered_second).startswith(
        "● assistant: second"
    )


def test_collect_abandoned_branch_entries_uses_old_side_of_fork() -> None:
    root = ConversationEntry(kind="user", message=Message.user("root"))
    old = ConversationEntry(
        kind="assistant",
        message=Message.assistant("old"),
        parent_id=root.id,
    )
    target = ConversationEntry(
        kind="assistant",
        message=Message.assistant("target"),
        parent_id=root.id,
    )

    abandoned = collect_abandoned_branch_entries(
        [root, old, target],
        old_leaf_id=old.id,
        target_id=target.id,
    )

    assert [entry.id for entry in abandoned] == [old.id]
