from __future__ import annotations

# ruff: noqa: D100,D103,S101

import json
from pathlib import Path

from yoke.agent.context import ContextManager
from yoke.agent.loop import RuntimeAgent
from yoke.agent.loop.forking import promote_runtime_fork
from yoke.agent.loop.types import AfterToolCallResult
from yoke.agent.models import Message
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.agent.session_tree import SessionTree
from yoke.agent.tools import FdTool
from yoke.agent.tool_result_projection import project_tool_result_content
from yoke.agent.tool_result_projection import project_tool_results_for_provider
from yoke.ai.providers.base import Provider


def _call(tool_name: str, call_id: str = "call-1") -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolFunction(name=tool_name, arguments="{}"),
    )


def _canonical(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def test_session_tree_moves_runtime_projection_to_entry_metadata() -> None:
    canonical = _canonical(
        {
            "ok": True,
            "command": ["fd", "--print0"],
            "output": ["a.py"],
            "exit_code": 0,
        }
    )
    result = Message.tool("call-1", canonical)
    result._provider_result_projection = "fd"

    tree = SessionTree.from_messages(
        [
            Message(role="assistant", tool_calls=[_call("fd")]),
            result,
        ]
    )
    entry = next(entry for entry in tree.entries if entry.kind == "tool_result")

    assert entry.metadata["provider_result_projection"] == "fd"
    assert entry.message is not None
    assert entry.message._provider_result_projection is None
    assert entry.message == Message.tool("call-1", canonical)


def test_runtime_projection_marker_is_invisible_to_message_equality() -> None:
    canonical = Message.tool("call-1", '{"ok":true}')
    projected = canonical.model_copy(deep=True)

    projected._provider_result_projection = "fd"

    assert projected == canonical
    assert projected.model_dump() == canonical.model_dump()
    assert projected.model_copy(deep=True)._provider_result_projection == "fd"


def test_owned_runtime_rehydrates_projection_without_mutating_entries() -> None:
    canonical = _canonical(
        {
            "ok": True,
            "command": ["fd", "--print0"],
            "output": ["a.py"],
            "exit_code": 0,
        }
    )
    result = Message.tool("call-1", canonical)
    result._provider_result_projection = "fd"
    entries = list(
        SessionTree.from_messages(
            [
                Message(role="assistant", tool_calls=[_call("fd")]),
                result,
            ]
        ).entries
    )
    canonical_entry = next(entry for entry in entries if entry.kind == "tool_result")
    assert canonical_entry.message is not None
    assert canonical_entry.message._provider_result_projection is None

    seed = SessionTree.take_validated_runtime(entries)

    runtime_result = next(
        message for message in seed.messages if message.role == "tool"
    )
    assert runtime_result._provider_result_projection == "fd"
    assert canonical_entry.message._provider_result_projection is None
    assert canonical_entry.message == Message.tool("call-1", canonical)


def test_rg_flattens_single_submatches_into_toon_columns() -> None:
    content = _canonical(
        {
            "ok": True,
            "command": ["rg", "--json", "needle"],
            "output": [
                {
                    "kind": "match",
                    "path": "src/a.py",
                    "line": 12,
                    "text": "value, needle",
                    "submatches": [{"text": "needle", "start": 7, "end": 13}],
                },
                {
                    "kind": "match",
                    "path": "src/b.py",
                    "line": 3,
                    "text": "needle",
                    "submatches": [{"text": "needle", "start": 0, "end": 6}],
                },
            ],
            "exit_code": 0,
        }
    )

    projected = project_tool_result_content("rg", content)

    assert projected == (
        "ok: true\n"
        "matches[2]{path,line,text,match,start,end}:\n"
        '  src/a.py,12,"value, needle",needle,7,13\n'
        "  src/b.py,3,needle,needle,0,6"
    )
    assert "submatches" not in projected
    assert "command" not in projected


def test_rg_keeps_context_kind_and_only_matching_offsets_when_present() -> None:
    contextual = _canonical(
        {
            "ok": True,
            "command": ["rg", "--json", "needle"],
            "output": [
                {
                    "kind": "context",
                    "path": "a.py",
                    "line": 1,
                    "text": "before",
                    "submatches": [],
                },
                {
                    "kind": "match",
                    "path": "a.py",
                    "line": 2,
                    "text": "needle",
                    "submatches": [{"text": "needle", "start": 0, "end": 6}],
                },
            ],
            "exit_code": 0,
        }
    )
    only_matching = _canonical(
        {
            "ok": True,
            "command": ["rg", "--json", "--only-matching", "needle"],
            "output": [
                {
                    "kind": "match",
                    "path": "a.py",
                    "line": 2,
                    "text": "needle",
                    "start": 4,
                    "end": 10,
                }
            ],
            "exit_code": 0,
        }
    )

    assert project_tool_result_content("rg", contextual) == (
        "ok: true\n"
        "matches[2]{kind,path,line,text,match,start,end}:\n"
        "  context,a.py,1,before,null,null,null\n"
        "  match,a.py,2,needle,needle,0,6"
    )
    assert project_tool_result_content("rg", only_matching) == (
        "ok: true\nmatches[1]{path,line,text,start,end}:\n  a.py,2,needle,4,10"
    )


def test_rg_and_fd_keep_failure_diagnostics_but_drop_invocation_metadata() -> None:
    content = _canonical(
        {
            "ok": False,
            "error": "bad pattern",
            "command": ["rg", "--json", "("],
            "exit_code": 2,
            "stderr": "bad pattern",
        }
    )

    assert project_tool_result_content("rg", content) == (
        '{"ok":false,"error":"bad pattern","exit_code":2,"stderr":"bad pattern"}'
    )
    assert project_tool_result_content("fd", content) == (
        '{"ok":false,"error":"bad pattern","exit_code":2,"stderr":"bad pattern"}'
    )


def test_fd_projects_paths_and_details_without_command_metadata() -> None:
    paths = _canonical(
        {
            "ok": True,
            "command": ["fd", "--print0"],
            "output": ["src/a.py", "src/comma,name.py"],
            "exit_code": 0,
            "truncated": True,
            "summary": "showing 2 results",
        }
    )
    details = _canonical(
        {
            "ok": True,
            "command": ["fd", "--print0"],
            "output": [
                {
                    "path": "src/a.py",
                    "type": "file",
                    "size_bytes": 12,
                    "modified_at": "2026-09-08T12:00:00+00:00",
                }
            ],
            "exit_code": 0,
        }
    )

    assert project_tool_result_content("fd", paths) == (
        "ok: true\n"
        'paths[2]: src/a.py,"src/comma,name.py"\n'
        "truncated: true\n"
        "summary: showing 2 results"
    )
    assert project_tool_result_content("fd", details) == (
        "ok: true\n"
        "paths[1]{path,type,size_bytes,modified_at}:\n"
        '  src/a.py,file,12,"2026-09-08T12:00:00+00:00"'
    )


def test_command_process_projection_keeps_output_and_actionable_state_only() -> None:
    completed = _canonical(
        {
            "ok": True,
            "session_id": None,
            "exit_code": 0,
            "returncode": 0,
            "running": False,
            "timed_out": False,
            "chunk_id": "abc123",
            "wall_time_seconds": 0.5,
            "elapsed_seconds": 0.5,
            "original_token_count": 25,
            "output": "done\n",
            "outputTruncationDetails": {"truncated": False},
        }
    )
    running = _canonical(
        {
            "ok": True,
            "session_id": 42,
            "exit_code": None,
            "returncode": None,
            "running": True,
            "timed_out": False,
            "chunk_id": "def456",
            "wall_time_seconds": 30.0,
            "elapsed_seconds": 30.0,
            "original_token_count": 2000,
            "output": "tail",
            "outputTruncationDetails": {
                "truncated": True,
                "truncatedBy": "bytes",
                "outputLines": 1,
                "totalLines": 300,
                "lastLinePartial": True,
            },
        }
    )

    assert project_tool_result_content("command", completed) == (
        '{"ok":true,"output":"done\\n"}'
    )
    assert project_tool_result_content("command", running) == (
        '{"ok":true,"output":"tail","running":true,"session_id":42,'
        '"truncated":true,"kept":"tail","truncated_by":"bytes",'
        '"visible_lines":1,"total_lines":300,"first_visible_line_partial":true,'
        '"original_token_count":2000}'
    )


def test_python_exec_projection_retains_selected_interpreter() -> None:
    content = _canonical(
        {
            "ok": True,
            "output": "ok",
            "exit_code": 0,
            "running": False,
            "python_executable": "/tmp/.venv/bin/python",
            "timeout": 180,
            "outputTruncationDetails": {"truncated": False},
        }
    )

    assert project_tool_result_content("command", content) == (
        '{"ok":true,"output":"ok","python_executable":"/tmp/.venv/bin/python"}'
    )


def test_apply_patch_projects_changed_files_as_toon_table() -> None:
    content = _canonical(
        {
            "ok": True,
            "changes": [
                {"action": "A", "path": "added.txt"},
                {"action": "M", "path": "renamed.txt", "move_from": "notes.txt"},
            ],
            "changes_applied": 2,
            "stdout": "Success. Updated the following files:\nA added.txt\nM renamed.txt\n",
            "stderr": "",
        }
    )

    assert project_tool_result_content("apply_patch", content) == (
        "ok: true\n"
        "changes[2\t]{action\tpath\tmove_from}:\n"
        "  A\tadded.txt\tnull\n"
        "  M\trenamed.txt\tnotes.txt"
    )


def test_web_search_projects_uniform_results_as_toon_table() -> None:
    content = _canonical(
        {
            "ok": True,
            "provider": "bing",
            "results": [
                {
                    "title": "Python, docs",
                    "url": "https://docs.python.org/3/",
                    "domain": "docs.python.org",
                    "sourceType": "docs",
                    "snippet": "Official docs",
                }
            ],
            "exhausted": True,
            "requestedResults": 3,
            "returnedResults": 1,
            "note": "Bing returned fewer results, use web_research.",
        }
    )

    assert project_tool_result_content("web_search", content) == (
        "ok: true\n"
        "provider: bing\n"
        "results[1]{title,url,domain,sourceType,snippet}:\n"
        '  "Python, docs","https://docs.python.org/3/",docs.python.org,docs,Official docs\n'
        "exhausted: true\n"
        "requestedResults: 3\n"
        "returnedResults: 1\n"
        'note: "Bing returned fewer results, use web_research."'
    )


def test_projection_quotes_toon_values_per_spec_4_1_rules() -> None:
    content = _canonical(
        {
            "ok": True,
            "command": ["fd", "--print0"],
            "output": [
                "true",
                "05",
                "-flag",
                "#hash",
                "a:b",
                "a\\b",
                "line\nname",
            ],
            "exit_code": 0,
        }
    )

    assert project_tool_result_content("fd", content) == (
        'ok: true\npaths[7]: "true","05","-flag","#hash","a:b","a\\\\b","line\\nname"'
    )


def test_provider_projection_changes_only_the_provider_copy() -> None:
    canonical = _canonical(
        {
            "ok": True,
            "command": ["fd", "--print0"],
            "output": ["src/a.py", "src/b.py"],
            "exit_code": 0,
        }
    )
    messages = [
        Message(role="assistant", tool_calls=[_call("fd")]),
        Message.tool("call-1", canonical),
    ]
    messages[1]._provider_result_projection = "fd"

    projected = project_tool_results_for_provider(messages)

    assert messages[1].content == canonical
    assert projected[1].content == "ok: true\npaths[2]: src/a.py,src/b.py"
    assert projected[1] is not messages[1]
    assert projected[0] is messages[0]


def test_context_manager_persists_json_but_projects_provider_tool_content() -> None:
    manager = ContextManager()
    context = manager.initialize("search")
    call = _call("exec_command")
    manager.append_message(context, Message(role="assistant", tool_calls=[call]))
    result: dict[str, object] = {
        "ok": True,
        "exit_code": 0,
        "returncode": 0,
        "running": False,
        "timed_out": False,
        "output": "done",
        "outputTruncationDetails": {"truncated": False},
    }
    manager.append_tool_result(
        context,
        tool_call_id=call.id,
        result=result,
        provider_result_projection="command",
    )

    transcript = manager.transcript_messages(context)
    provider = manager.messages_for_provider(context)

    assert transcript[-1].content == _canonical(result)
    assert provider[-1].content == '{"ok":true,"output":"done"}'
    tool_entries = [
        entry
        for entry in context.conversation_log.entries
        if entry.kind == "tool_result"
    ]
    assert len(tool_entries) == 1
    assert tool_entries[0].message is not None
    assert tool_entries[0].message.content == _canonical(result)
    assert tool_entries[0].metadata["provider_result_projection"] == "command"


def test_custom_transform_sees_canonical_json_before_provider_projection() -> None:
    canonical = _canonical(
        {
            "ok": True,
            "command": ["fd", "--print0"],
            "output": ["a.py"],
            "exit_code": 0,
        }
    )
    seen: list[str] = []

    def transform(messages: list[Message]) -> list[Message]:
        for message in messages:
            if message.role == "tool" and isinstance(message.content, str):
                seen.append(message.content)
        return messages

    manager = ContextManager(transform_messages=transform)
    context = manager.initialize("", append_prompt=False)
    manager.append_message(context, Message(role="assistant", tool_calls=[_call("fd")]))
    manager.append_tool_result(
        context,
        tool_call_id="call-1",
        result=json.loads(canonical),
        provider_result_projection="fd",
    )

    projected = manager.messages_for_provider(context)

    assert seen == [canonical]
    assert projected[-1].content == "ok: true\npaths[1]: a.py"


def test_unknown_tools_and_non_json_results_are_unchanged() -> None:
    assert project_tool_result_content("custom_tool", '{"ok":true}') == '{"ok":true}'
    assert project_tool_result_content("rg", "plain text") == "plain text"


def test_projection_preserves_hook_enriched_builtin_results() -> None:
    content = _canonical(
        {
            "ok": True,
            "output": "done",
            "exit_code": 0,
            "returncode": 0,
            "running": False,
            "outputTruncationDetails": {"truncated": False},
            "custom_metadata": "keep me",
        }
    )

    assert project_tool_result_content("command", content) == content


def test_projection_preserves_hook_enriched_nested_results() -> None:
    rg_content = _canonical(
        {
            "ok": True,
            "command": ["rg", "--json", "needle"],
            "output": [
                {
                    "kind": "match",
                    "path": "a.py",
                    "line": 1,
                    "text": "needle",
                    "submatches": [
                        {"text": "needle", "start": 0, "end": 6, "score": 1.0}
                    ],
                }
            ],
            "exit_code": 0,
        }
    )
    patch_content = _canonical(
        {
            "ok": True,
            "changes": [{"action": "M", "path": "a.py", "reviewed": True}],
            "changes_applied": 1,
            "stdout": "updated",
            "stderr": "",
        }
    )

    assert project_tool_result_content("rg", rg_content) == rg_content
    assert project_tool_result_content("apply_patch", patch_content) == patch_content


def test_rg_multiple_submatches_fall_back_to_canonical_json() -> None:
    content = _canonical(
        {
            "ok": True,
            "command": ["rg", "--json", "needle|other"],
            "output": [
                {
                    "kind": "match",
                    "path": "a.py",
                    "line": 1,
                    "text": "needle other",
                    "submatches": [
                        {"text": "needle", "start": 0, "end": 6},
                        {"text": "other", "start": 7, "end": 12},
                    ],
                }
            ],
            "exit_code": 0,
        }
    )

    assert project_tool_result_content("rg", content) == content


def test_persisted_projection_provenance_survives_registry_changes() -> None:
    fd_result = _canonical(
        {
            "ok": True,
            "command": ["fd", "--print0"],
            "output": ["a.py"],
            "exit_code": 0,
        }
    )
    manager = ContextManager()
    original = manager.initialize("", append_prompt=False)
    manager.append_message(
        original, Message(role="assistant", tool_calls=[_call("fd")])
    )
    manager.append_tool_result(
        original,
        tool_call_id="call-1",
        result=json.loads(fd_result),
        provider_result_projection="fd",
    )
    resumed = ContextManager().initialize(
        "",
        append_prompt=False,
        conversation_entries=original.conversation_log.entries,
    )
    assert ContextManager().messages_for_provider(resumed)[-1].content == (
        "ok: true\npaths[1]: a.py"
    )

    custom_result = _canonical(
        {
            "ok": True,
            "output": "keep",
            "custom_metadata": "must survive",
        }
    )
    legacy = ContextManager().initialize(
        "",
        append_prompt=False,
        messages=[
            Message(role="assistant", tool_calls=[_call("exec_command")]),
            Message.tool("call-1", custom_result),
        ],
    )
    assert ContextManager().messages_for_provider(legacy)[-1].content == custom_result


def test_reused_tool_call_id_keeps_projection_per_result_occurrence() -> None:
    manager = ContextManager()
    context = manager.initialize("", append_prompt=False)

    manager.append_message(context, Message(role="assistant", tool_calls=[_call("fd")]))
    manager.append_tool_result(
        context,
        tool_call_id="call-1",
        result={
            "ok": True,
            "command": ["fd", "--print0"],
            "output": ["first.py"],
            "exit_code": 0,
        },
        provider_result_projection="fd",
    )
    manager.append_message(context, Message.assistant("first done"))

    manager.append_message(
        context,
        Message(role="assistant", tool_calls=[_call("fd")]),
    )
    second: dict[str, object] = {
        "ok": True,
        "command": ["fd", "--print0"],
        "output": ["second.py"],
        "exit_code": 0,
    }
    manager.append_tool_result(
        context,
        tool_call_id="call-1",
        result=second,
        provider_result_projection=None,
    )

    resumed = ContextManager().initialize(
        "",
        append_prompt=False,
        conversation_entries=context.conversation_log.entries,
    )
    tool_messages = [
        message
        for message in ContextManager().messages_for_provider(resumed)
        if message.role == "tool"
    ]

    assert [message.content for message in tool_messages] == [
        "ok: true\npaths[1]: first.py",
        _canonical(second),
    ]
    assert [message._provider_result_projection for message in tool_messages] == [
        "fd",
        None,
    ]


def test_after_tool_call_replacement_bypasses_projection(tmp_path: Path) -> None:
    replacement: dict[str, object] = {
        "ok": True,
        "command": ["fd", "--print0"],
        "output": ["hooked.py"],
        "exit_code": 0,
    }

    class RecordingProvider(Provider):
        max_images_per_message = None
        supports_image_inputs = False

        def __init__(self) -> None:
            self.requests: list[list[Message]] = []

        def complete(self, messages, tools):
            del tools
            self.requests.append(
                [message.model_copy(deep=True) for message in messages]
            )
            if len(self.requests) == 1:
                return Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id="call-hook",
                            function=ToolFunction(
                                name="fd",
                                arguments='{"pattern":"a"}',
                            ),
                        )
                    ],
                )
            return Message.assistant("done")

    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    provider = RecordingProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[FdTool.bind(root=tmp_path)],
        after_tool_call=lambda _: AfterToolCallResult(result=dict(replacement)),
    )
    try:
        result = agent.run("find a")
    finally:
        agent.close()

    assert provider.requests[1][-1].content == _canonical(replacement)
    assert result.conversation_entries is not None
    tool_entry = next(
        entry for entry in result.conversation_entries if entry.kind == "tool_result"
    )
    assert "provider_result_projection" not in tool_entry.metadata


def test_builtin_subclass_does_not_inherit_projection_implicitly(
    tmp_path: Path,
) -> None:
    custom_result: dict[str, object] = {
        "ok": True,
        "command": ["fd", "--print0"],
        "output": ["custom.py"],
        "exit_code": 0,
    }

    class CustomFdTool(FdTool):
        execute_in_process = True

        def execute(self) -> dict[str, object]:
            return dict(custom_result)

    class RecordingProvider(Provider):
        max_images_per_message = None
        supports_image_inputs = False

        def __init__(self) -> None:
            self.requests: list[list[Message]] = []

        def complete(self, messages, tools):
            del tools
            self.requests.append(
                [message.model_copy(deep=True) for message in messages]
            )
            if len(self.requests) == 1:
                return Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id="call-custom",
                            function=ToolFunction(name="fd", arguments="{}"),
                        )
                    ],
                )
            return Message.assistant("done")

    provider = RecordingProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[CustomFdTool.bind(root=tmp_path)],
    )
    try:
        result = agent.run("custom")
    finally:
        agent.close()

    assert provider.requests[1][-1].content == _canonical(custom_result)
    assert result.conversation_entries is not None
    tool_entry = next(
        entry for entry in result.conversation_entries if entry.kind == "tool_result"
    )
    assert "provider_result_projection" not in tool_entry.metadata


def test_fork_and_promotion_preserve_projection_provenance() -> None:
    class DoneProvider(Provider):
        max_images_per_message = None
        supports_image_inputs = False

        def complete(self, messages, tools):
            del messages, tools
            return Message.assistant("done")

    manager = ContextManager()
    context = manager.initialize("", append_prompt=False)
    manager.append_message(context, Message(role="assistant", tool_calls=[_call("fd")]))
    manager.append_tool_result(
        context,
        tool_call_id="call-1",
        result={
            "ok": True,
            "command": ["fd", "--print0"],
            "output": ["a.py"],
            "exit_code": 0,
        },
        provider_result_projection="fd",
    )
    agent = RuntimeAgent(provider=DoneProvider(), tools=[])
    agent.load_conversation(conversation_entries=context.conversation_log.entries)
    forked = agent.fork()
    try:
        assert forked._context is not None
        forked_tool = next(
            message for message in forked._context.messages if message.role == "tool"
        )
        assert forked_tool._provider_result_projection == "fd"
        promote_runtime_fork(agent, forked)
        assert agent._context is not None
        promoted_tool = next(
            message for message in agent._context.messages if message.role == "tool"
        )
        assert promoted_tool._provider_result_projection == "fd"
        assert agent.context_manager.messages_for_provider(agent._context)[
            -1
        ].content == ("ok: true\npaths[1]: a.py")
    finally:
        forked.close()
        agent.close()
