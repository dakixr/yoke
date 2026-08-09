# ruff: noqa: D100, D103, S101

from __future__ import annotations

from pathlib import Path
from typing import Any, cast


from yoke.agent.tools import EditTool


def execute_edit(tmp_path: Path, arguments: dict[str, object]) -> dict[str, object]:
    tool = EditTool.bind(root=tmp_path)
    return cast(dict[str, Any], tool.parse_arguments(arguments).execute())


def test_edit_reports_ambiguous_exact_match_with_previews(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nalpha\n", encoding="utf-8")

    result = execute_edit(
        tmp_path,
        {
            "path": "sample.txt",
            "oldText": "alpha",
            "newText": "beta",
        },
    )

    assert result["ok"] is False
    assert result["error"] == "Text to replace is ambiguous in sample.txt"
    assert result["match_count"] == 2
    previews = cast(list[dict[str, Any]], result["previews"])
    assert previews[0]["occurrence"] == 1
    assert previews[1]["occurrence"] == 2


def test_edit_replace_all_updates_every_exact_match(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nalpha\n", encoding="utf-8")

    result = execute_edit(
        tmp_path,
        {
            "path": "sample.txt",
            "oldText": "alpha",
            "newText": "beta",
            "replaceAll": True,
        },
    )

    assert result["ok"] is True
    assert result["edits_applied"] == 2
    assert target.read_text(encoding="utf-8") == "beta\nbeta\n"


def test_edit_multi_mode_applies_edits_incrementally(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    result = execute_edit(
        tmp_path,
        {
            "path": "sample.txt",
            "edits": [
                {"oldText": "alpha", "newText": "gamma"},
                {"oldText": "gamma", "newText": "delta"},
            ],
        },
    )

    assert result["ok"] is True
    assert result["edits_applied"] == 2
    assert target.read_text(encoding="utf-8") == "delta\nbeta\n"
