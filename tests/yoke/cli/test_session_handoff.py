from __future__ import annotations

import json
from pathlib import Path

from yoke.agent.models import CompactionHandoff
from yoke.agent.models import MemorySnapshot
from yoke.agent.models import Message
from yoke.agent.models import MessageImageURL
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.session_tree import SessionTree
from yoke.cli.main import main
from yoke.cli.session.writer import append_session_tree_delta
from yoke.session import SessionStore
from yoke.session.handoff import build_session_handoff


def test_session_handoff_cli_continues_from_compacted_active_context(
    tmp_path: Path,
    capsys,
) -> None:
    tree = SessionTree.from_messages(
        [
            Message.user("superseded old request"),
            Message.assistant("superseded old answer"),
        ]
    )
    handoff = CompactionHandoff(
        summary_text="The implementation already moved auth into middleware.",
        reason="threshold",
        boundary="assistant",
        summarized_messages=2,
        retained_user_messages=1,
        retained_messages=[Message.user("Keep the middleware behavior unchanged.")],
    )
    tree.append_snapshot(
        MemorySnapshot(
            id="checkpoint-1",
            summary_text=handoff.summary_text,
            compaction_handoff=handoff,
        )
    )
    tree.append_message(Message.user("Now finish the login redirect."))
    tree.append_message(Message.assistant("Redirect implementation is half done."))

    store = SessionStore()
    store.save(
        "handoff-demo",
        [],
        conversation_entries=list(tree.entries),
        leaf_id=tree.leaf_id,
        root=tmp_path,
        title="Auth continuation",
        provider_name="codex",
        model_id="gpt-5.6-sol",
        reasoning_effort="medium",
    )

    exit_code = main(["session-handoff", "handoff-demo"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "# Yoke session handoff" in captured.out
    assert f"- Working directory: `{tmp_path}`" in captured.out
    assert "### Compacted history" in captured.out
    assert "The implementation already moved auth into middleware." in captured.out
    assert "Keep the middleware behavior unchanged." in captured.out
    assert "Now finish the login redirect." in captured.out
    assert "Redirect implementation is half done." in captured.out
    assert "superseded old answer" not in captured.out
    assert captured.err == ""


def test_session_handoff_keeps_tool_state_without_dumping_inline_image_bytes(
    tmp_path: Path,
    capsys,
) -> None:
    tree = SessionTree.from_messages(
        [
            Message.user(
                [
                    MessageTextContentPart(text="Match this screenshot."),
                    MessageImageURLContentPart(
                        image_url=MessageImageURL(
                            url="data:image/png;base64," + ("A" * 100_000)
                        ),
                        label="[Image #4]",
                    ),
                ]
            ),
            Message(
                role="assistant",
                content="I will inspect it.",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=ToolFunction(
                            name="inspect_page",
                            arguments='{"path":"/settings"}',
                        ),
                    )
                ],
            ),
            Message.tool("call-1", "Settings page is missing the save button."),
            Message.assistant("The save button still needs to be restored."),
        ]
    )
    SessionStore().save(
        "handoff-image-tool",
        [],
        conversation_entries=list(tree.entries),
        leaf_id=tree.leaf_id,
        root=tmp_path,
    )

    exit_code = main(["session-handoff", "handoff-image-tool"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Match this screenshot." in captured.out
    assert "[Image #4]" in captured.out
    assert "data:image/png;base64" not in captured.out
    assert "`inspect_page` (`call-1`)" in captured.out
    assert '"path":"/settings"' in captured.out
    assert "Settings page is missing the save button." in captured.out
    assert "The save button still needs to be restored." in captured.out
    assert captured.err == ""


def test_session_handoff_falls_back_when_jsonl_is_newer_than_index(
    tmp_path: Path,
) -> None:
    session_id = "handoff-stale-index"
    tree = SessionTree.from_messages([Message.user("Original request")])
    store = SessionStore(tmp_path / "sessions")
    store.save(
        session_id,
        [],
        conversation_entries=list(tree.entries),
        leaf_id=tree.leaf_id,
        root=tmp_path,
    )
    index_path = store.directory / "index.json"
    stale_index = index_path.read_bytes()

    tree.append_message(Message.assistant("Answer appended before index update"))
    appended = tree.entries[-1]
    append_session_tree_delta(
        store.directory / f"{session_id}.jsonl",
        session_changes={"leaf_id": tree.leaf_id},
        appended_entries=(appended,),
    )

    handoff = build_session_handoff(
        session_id,
        store=SessionStore(store.directory),
    )

    assert handoff.leaf_id == appended.id
    assert handoff.total_entries == 2
    assert [message.content for message in handoff.messages] == [
        "Original request",
        "Answer appended before index update",
    ]
    assert index_path.read_bytes() == stale_index


def test_session_handoff_compacts_tool_detail_by_default_and_full_opts_in(
    tmp_path: Path,
) -> None:
    arguments = '{"query":"' + ("A" * 1_000) + 'TAIL"}'
    tool_result = "RESULT-BEGIN\n" + ("B" * 3_000) + "\nRESULT-TAIL"
    tree = SessionTree.from_messages(
        [
            Message.user("Inspect the failing command."),
            Message(
                role="assistant",
                content="I will inspect it.",
                tool_calls=[
                    ToolCall(
                        id="call-long",
                        function=ToolFunction(
                            name="inspect_command",
                            arguments=arguments,
                        ),
                    )
                ],
            ),
            Message.tool("call-long", tool_result),
            Message.assistant("The failure is understood."),
        ]
    )
    store = SessionStore(tmp_path / "sessions")
    store.save(
        "handoff-tool-detail",
        [],
        conversation_entries=list(tree.entries),
        leaf_id=tree.leaf_id,
        root=tmp_path,
    )

    compact = build_session_handoff("handoff-tool-detail", store=store)
    full = build_session_handoff(
        "handoff-tool-detail",
        store=store,
        tool_detail="full",
    )

    compact_call = compact.messages[1].tool_calls[0]
    compact_result = compact.messages[2]
    assert compact.tool_detail == "compact"
    assert compact_call.truncated is True
    assert len(compact_call.arguments) <= 256
    assert compact_call.arguments.startswith('{"query":"')
    assert compact_call.arguments.endswith('TAIL"}')
    assert compact_result.truncated is True
    assert len(compact_result.content) <= 512
    assert compact_result.content.startswith("RESULT-BEGIN")
    assert compact_result.content.endswith("RESULT-TAIL")

    full_call = full.messages[1].tool_calls[0]
    full_result = full.messages[2]
    assert full.tool_detail == "full"
    assert full_call.arguments == arguments
    assert full_call.truncated is False
    assert full_result.content == tool_result
    assert full_result.truncated is False


def test_session_handoff_tail_keeps_whole_recent_user_turns_and_summary(
    tmp_path: Path,
) -> None:
    tree = SessionTree.from_messages(
        [Message.user("Old request"), Message.assistant("Old answer")]
    )
    handoff = CompactionHandoff(
        summary_text="Authentication was moved into middleware.",
        reason="threshold",
        boundary="assistant",
        summarized_messages=2,
        retained_user_messages=1,
        retained_messages=[Message.user("Preserve the redirect contract.")],
    )
    tree.append_snapshot(
        MemorySnapshot(
            id="checkpoint-tail",
            summary_text=handoff.summary_text,
            compaction_handoff=handoff,
        )
    )
    tree.append_message(Message.user("Run the login tests."))
    tree.append_message(
        Message(
            role="assistant",
            content="Running them.",
            tool_calls=[
                ToolCall(
                    id="call-tests",
                    function=ToolFunction(
                        name="exec_command", arguments="pytest login"
                    ),
                )
            ],
        )
    )
    tree.append_message(Message.tool("call-tests", "2 failed, 10 passed"))
    tree.append_message(Message.assistant("Two redirect tests fail."))
    tree.append_message(Message.user("Fix only the callback test."))
    tree.append_message(Message.assistant("The callback test is fixed."))

    store = SessionStore(tmp_path / "sessions")
    store.save(
        "handoff-tail",
        [],
        conversation_entries=list(tree.entries),
        leaf_id=tree.leaf_id,
        root=tmp_path,
    )

    result = build_session_handoff("handoff-tail", store=store, tail=1)
    contents = [message.content for message in result.messages]

    assert result.tail == 1
    assert "Authentication was moved into middleware." in contents
    assert "Fix only the callback test." in contents
    assert "The callback test is fixed." in contents
    assert "Preserve the redirect contract." not in contents
    assert "Run the login tests." not in contents
    assert "Running them." not in contents
    assert "2 failed, 10 passed" not in contents
    assert "Two redirect tests fail." not in contents


def test_session_handoff_cli_exposes_tail_and_tool_detail(
    tmp_path: Path,
    capsys,
) -> None:
    tree = SessionTree.from_messages(
        [Message.user("First"), Message.assistant("One"), Message.user("Second")]
    )
    SessionStore().save(
        "handoff-cli-options",
        [],
        conversation_entries=list(tree.entries),
        leaf_id=tree.leaf_id,
        root=tmp_path,
    )

    exit_code = main(
        [
            "session-handoff",
            "handoff-cli-options",
            "--tail",
            "1",
            "--tool-detail",
            "full",
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["tail"] == 1
    assert payload["tool_detail"] == "full"
    assert [message["content"] for message in payload["messages"]] == ["Second"]
    assert captured.err == ""
