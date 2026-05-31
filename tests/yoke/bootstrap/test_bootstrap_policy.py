from __future__ import annotations

# ruff: noqa: F403, F405
from .support import *  # noqa: F403, F405


def test_workspace_config_can_keep_builtin_tools_enabled(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".yoke"
    config_dir.mkdir(parents=True)
    (tmp_path / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (config_dir / "config.json").write_text(
        """
{
  "tools": {
    "extract_file_context": "allow"
  }
}
""".strip(),
        encoding="utf-8",
    )

    resolved = resolve_agent_config(
        root=tmp_path,
        base_system_prompt=None,
        include_global_tools=False,
    )
    result = execute_tool(resolved.tools, "extract_file_context", {"path": "notes.txt"})

    assert result["ok"] is True
    assert result["extractor"] == "text"
