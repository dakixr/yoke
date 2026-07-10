# ruff: noqa: D100, D103, S101

from __future__ import annotations

from pathlib import Path

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.ai.providers.base import Provider
from yoke.cli.runtime.base import ActiveSession
from yoke.cli.runtime.tree import navigate_session_tree
from yoke.cli.session import SessionStore


def test_tree_navigation_selecting_user_moves_to_parent_and_editor_text(
    tmp_path: Path,
) -> None:
    first = ConversationEntry(kind="user", message=Message.user("first"))
    answer = ConversationEntry(
        kind="assistant",
        message=Message.assistant("answer"),
        parent_id=first.id,
    )
    retry = ConversationEntry(
        kind="user",
        message=Message.user("retry this"),
        parent_id=answer.id,
    )
    retry_answer = ConversationEntry(
        kind="assistant",
        message=Message.assistant("retry answer"),
        parent_id=retry.id,
    )
    store = SessionStore(directory=tmp_path)
    store.save(
        "tree-nav",
        [],
        conversation_entries=[first, answer, retry, retry_answer],
        leaf_id=retry_answer.id,
    )
    active_session = ActiveSession(
        id="tree-nav",
        root=tmp_path,
        store=store,
        record=store.load("tree-nav"),
    )

    result = navigate_session_tree(active_session, object(), retry.id)

    assert result.editor_text == "retry this"
    assert result.active_session.record.leaf_id == answer.id
    assert [message.text_content() for message in result.messages] == [
        "first",
        "answer",
    ]


def test_tree_navigation_continues_when_branch_summary_provider_fails(
    tmp_path: Path,
) -> None:
    class FailingProvider(Provider):
        def complete(
            self,
            messages: list[Message],
            tools: list[dict[str, object]],
        ) -> Message:
            del messages, tools
            raise RuntimeError("summary unavailable")

    first = ConversationEntry(kind="user", message=Message.user("first"))
    answer = ConversationEntry(
        kind="assistant",
        message=Message.assistant("answer"),
        parent_id=first.id,
    )
    retry = ConversationEntry(
        kind="user",
        message=Message.user("retry"),
        parent_id=answer.id,
    )
    retry_answer = ConversationEntry(
        kind="assistant",
        message=Message.assistant("retry answer"),
        parent_id=retry.id,
    )
    store = SessionStore(directory=tmp_path)
    store.save(
        "tree-summary-failure",
        [],
        conversation_entries=[first, answer, retry, retry_answer],
        leaf_id=retry_answer.id,
    )
    active_session = ActiveSession(
        id="tree-summary-failure",
        root=tmp_path,
        store=store,
        record=store.load("tree-summary-failure"),
    )
    agent = RuntimeAgent(provider=FailingProvider(), tools=[])

    result = navigate_session_tree(
        active_session,
        agent,
        retry.id,
        summarize=True,
    )

    assert result.summary_created is False
    assert result.summary_error == "summary unavailable"
    assert result.editor_text == "retry"
    assert result.active_session.record.leaf_id == answer.id
    assert [message.text_content() for message in result.messages] == [
        "first",
        "answer",
    ]
