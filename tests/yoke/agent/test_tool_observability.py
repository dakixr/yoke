from __future__ import annotations

# ruff: noqa: D100, D103, S101

from typing import cast

from yoke.agent.models import Message
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.observability.tool_trace import ToolTraceContext
from yoke.agent.observability.tool_trace import ToolTraceEntry
from yoke.agent.observability.tool_trace import ToolTraceStore
from yoke.agent.observability.tool_transcript import entries_from_messages
from yoke.agent.observability.tool_transcript import merge_trace_entries


def test_tool_transcript_does_not_attach_next_turn_answer_to_previous_tool() -> None:
    tool_call = ToolCall(
        id="call-1",
        function=ToolFunction(name="read", arguments='{"path":"README.md"}'),
    )
    messages = [
        Message.user("Read the file"),
        Message(role="assistant", tool_calls=[tool_call]),
        Message.tool("call-1", '{"ok":true}'),
        Message.assistant("The file says hello."),
        Message.user("Thanks"),
        Message.assistant("You're welcome."),
    ]

    entries = entries_from_messages(messages)

    assert len(entries) == 1
    assert entries[0].after_context == [
        ToolTraceContext(role="assistant", text="The file says hello.")
    ]


def test_tool_trace_store_owns_event_data_and_returned_entries() -> None:
    executed_options = {"paths": ["one.txt"]}
    executed_arguments: dict[str, object] = {"options": executed_options}
    result_data = {"matches": [1]}
    result: dict[str, object] = {"data": result_data}
    store = ToolTraceStore()
    store.record_end(
        {
            "tool_call_id": "call-1",
            "tool_name": "rg",
            "executed_arguments": executed_arguments,
            "result": result,
            "ok": True,
        }
    )
    store._entries["call-1"].context = [ToolTraceContext(role="user", text="find it")]

    executed_options["paths"].append("two.txt")
    result_data["matches"].append(2)
    snapshot = store.snapshot()
    assert snapshot[0].executed_arguments is not None
    snapshot_options = cast(
        dict[str, list[str]], snapshot[0].executed_arguments["options"]
    )
    snapshot_options["paths"].append("snapshot.txt")
    assert snapshot[0].result is not None
    snapshot_data = cast(dict[str, list[int]], snapshot[0].result["data"])
    snapshot_data["matches"].append(3)
    assert snapshot[0].context is not None
    snapshot[0].context[0].text = "changed"

    stored = store.get("call-1")

    assert stored is not None
    assert stored.executed_arguments == {"options": {"paths": ["one.txt"]}}
    assert stored.result == {"data": {"matches": [1]}}
    assert stored.context == [ToolTraceContext(role="user", text="find it")]


def test_merge_trace_entries_does_not_alias_update_data() -> None:
    update = ToolTraceEntry(
        tool_call_id="call-1",
        tool_name="rg",
        result={"data": {"matches": [1]}},
        context=[ToolTraceContext(role="user", text="find it")],
    )

    merged = merge_trace_entries(
        [ToolTraceEntry(tool_call_id="call-1", tool_name="tool")],
        [update],
    )
    assert update.result is not None
    cast(dict[str, list[int]], update.result["data"])["matches"].append(2)
    assert update.context is not None
    update.context[0].text = "changed"

    assert merged[0].result == {"data": {"matches": [1]}}
    assert merged[0].context == [ToolTraceContext(role="user", text="find it")]
