from __future__ import annotations

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
from yoke.session import SessionStore


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
