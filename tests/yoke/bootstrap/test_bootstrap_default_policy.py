from __future__ import annotations

# ruff: noqa: D100, D103, F405, S101

from .support import *  # noqa: F403


def test_default_builtin_policy_uses_curated_allowlist(tmp_path: Path) -> None:
    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
        home=tmp_path,
    )

    active_names = {entry.tool.name for entry in resolved.tool_report.active_tools}
    denied_names = {entry.tool.name for entry in resolved.tool_report.denied_tools}

    assert active_names == {
        "attach_image",
        "edit",
        "extract_file_context",
        "fd",
        "read",
        "rg",
        "web_fetch",
        "web_research",
        "web_search",
        "write",
    }
    assert "apply_patch" not in active_names
    assert "exec_command" in denied_names
    assert "python_exec" in denied_names
    assert "write_stdin" in denied_names


def test_shell_capability_controls_shell_and_python_tools(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".yoke"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        '{"capabilities": {"shell": "allow"}}\n',
        encoding="utf-8",
    )

    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
    )

    active_by_name = {
        entry.tool.name: entry.capability_id
        for entry in resolved.tool_report.active_tools
    }
    assert active_by_name["exec_command"] == "shell"
    assert active_by_name["python_exec"] == "shell"
    assert active_by_name["write_stdin"] == "shell"


def test_gpt_models_use_apply_patch_for_file_write(tmp_path: Path) -> None:
    class Config:
        model = "gpt-5.5"

    class GptProvider(StaticProvider):
        provider_name = "test"
        config = Config()

    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
        provider=GptProvider(Message.assistant("done")),
    )

    active_names = {entry.tool.name for entry in resolved.tool_report.active_tools}
    assert "apply_patch" in active_names
    assert "edit" not in active_names
    assert "write" not in active_names
    assert resolved.tool_system_messages
    instructions = resolved.tool_system_messages[0].text_content() or ""
    assert "*** Begin Patch" in instructions


def test_non_gpt_models_do_not_receive_apply_patch_instructions(
    tmp_path: Path,
) -> None:
    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
    )

    assert resolved.tool_system_messages == []
