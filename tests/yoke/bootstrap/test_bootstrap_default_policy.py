from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: D100, D103, F405, S101

from .support import *  # noqa: F403, F405


def test_default_builtin_policy_allows_all_builtin_tools(
    tmp_path: Path,
) -> None:
    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
    )

    active_names = {entry.tool.name for entry in resolved.tool_report.active_tools}
    denied_names = {entry.tool.name for entry in resolved.tool_report.denied_tools}

    assert active_names == {
        "apply_patch",
        "attach_image",
        "bash",
        "edit",
        "extract_file_context",
        "find",
        "grep",
        "ls",
        "python_exec",
        "read",
        "rg",
        "subagent",
        "web_fetch",
        "web_research",
    }
    assert not denied_names


def test_global_config_can_override_builtin_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".yoke"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        """
{
  "tools": {
    "web_fetch": "allow"
  }
}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
    )

    active_names = {entry.tool.name for entry in resolved.tool_report.active_tools}
    assert "web_fetch" in active_names


def test_repo_config_overrides_global_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    global_config_dir = home / ".yoke"
    global_config_dir.mkdir(parents=True)
    (global_config_dir / "config.json").write_text(
        """
{
  "tools": {
    "web_fetch": "allow"
  }
}
""".strip(),
        encoding="utf-8",
    )
    repo_config_dir = tmp_path / ".yoke"
    repo_config_dir.mkdir(parents=True)
    (repo_config_dir / "config.json").write_text(
        """
{
  "tools": {
    "web_fetch": "deny"
  }
}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
    )

    active_names = {entry.tool.name for entry in resolved.tool_report.active_tools}
    denied_names = {entry.tool.name for entry in resolved.tool_report.denied_tools}
    assert "web_fetch" not in active_names
    assert "web_fetch" in denied_names
