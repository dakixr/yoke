from __future__ import annotations

# ruff: noqa: D100, D103, S101

from pathlib import Path
from typing import Any
from typing import cast

import pytest

from yoke.agent.tools import FindTool
from yoke.agent.tools import GrepTool
from yoke.agent.tools import LsTool
from yoke.agent.tools.base import WorkspaceTool


@pytest.mark.parametrize("tool_class", [LsTool, FindTool, GrepTool])
@pytest.mark.parametrize(
    ("file_count", "expected_truncated"),
    [(1, False), (2, False), (3, True)],
    ids=["under-limit", "exact-limit", "over-limit"],
)
def test_search_fallback_reports_truncation_only_for_omitted_results(
    tmp_path: Path,
    tool_class: type[WorkspaceTool],
    file_count: int,
    expected_truncated: bool,
) -> None:
    for index in range(file_count):
        (tmp_path / f"item-{index}.txt").write_text("needle\n", encoding="utf-8")
    arguments: dict[str, object] = {"limit": 2}
    if tool_class is FindTool:
        arguments["pattern"] = "*.txt"
    elif tool_class is GrepTool:
        arguments["pattern"] = "needle"
        arguments["glob"] = "*.txt"

    prototype = tool_class.bind(root=tmp_path)
    result = cast(dict[str, Any], prototype.parse_arguments(arguments).execute())

    assert result["ok"] is True
    assert result.get("truncated", False) is expected_truncated
    expected_count = min(file_count, 2)
    if tool_class is LsTool:
        returned = result["entries"]
        assert returned == [f"item-{index}.txt" for index in range(expected_count)]
    elif tool_class is FindTool:
        returned = result.get("matches", [])
        assert returned == [f"item-{index}.txt" for index in range(expected_count)]
    else:
        returned = [
            (file_result["path"], match["line"])
            for file_result in result.get("files", [])
            for match in file_result["matches"]
        ]
        assert result["match_count"] == expected_count
        assert returned == [(f"item-{index}.txt", 1) for index in range(expected_count)]
    assert len(returned) == expected_count
