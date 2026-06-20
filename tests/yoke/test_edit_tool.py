# ruff: noqa: D100, D103, S101

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from yoke.agent.tools import EditTool
from yoke.agent.tools import WriteTool


def execute_edit(tmp_path: Path, arguments: dict[str, object]) -> dict[str, object]:
    tool = EditTool.bind(root=tmp_path)
    return cast(dict[str, Any], tool.parse_arguments(arguments).execute())


def execute_write(tmp_path: Path, arguments: dict[str, object]) -> dict[str, object]:
    tool = WriteTool.bind(root=tmp_path)
    return cast(dict[str, Any], tool.parse_arguments(arguments).execute())


def test_edit_reports_ambiguous_exact_match(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nalpha\n", encoding="utf-8")

    result = execute_edit(
        tmp_path,
        {
            "path": "sample.txt",
            "oldString": "alpha",
            "newString": "beta",
        },
    )

    assert result["ok"] is False
    assert result["error"] == "Text to replace is ambiguous in sample.txt"
    assert result["match_count"] == 2


def test_edit_replace_all_updates_every_exact_match(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nalpha\n", encoding="utf-8")

    result = execute_edit(
        tmp_path,
        {
            "path": "sample.txt",
            "oldString": "alpha",
            "newString": "beta",
            "replaceAll": True,
        },
    )

    assert result["ok"] is True
    assert result["replacements"] == 2
    assert target.read_text(encoding="utf-8") == "beta\nbeta\n"


def test_edit_rejects_identical_replacement(tmp_path: Path) -> None:
    tool = EditTool.bind(root=tmp_path)

    with pytest.raises(ValueError, match="oldString and newString must differ"):
        tool.parse_arguments(
            {
                "path": "sample.txt",
                "oldString": "alpha",
                "newString": "alpha",
            }
        )


def test_edit_preserves_crlf_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(b"alpha\r\nbeta\r\n")

    result = execute_edit(
        tmp_path,
        {
            "path": "sample.txt",
            "oldString": "alpha\nbeta",
            "newString": "one\ntwo",
        },
    )

    assert result["ok"] is True
    assert target.read_bytes() == b"one\r\ntwo\r\n"


def test_edit_preserves_utf8_bom(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(b"\xef\xbb\xbfalpha\n")

    result = execute_edit(
        tmp_path,
        {
            "path": "sample.txt",
            "oldString": "alpha",
            "newString": "beta",
        },
    )

    assert result["ok"] is True
    assert target.read_bytes() == b"\xef\xbb\xbfbeta\n"


def test_write_creates_file(tmp_path: Path) -> None:
    result = execute_write(
        tmp_path,
        {
            "path": "nested/sample.txt",
            "content": "hello\n",
        },
    )

    assert result["ok"] is True
    assert result["created"] is True
    assert (tmp_path / "nested" / "sample.txt").read_text(encoding="utf-8") == "hello\n"


def test_write_preserves_existing_utf8_bom(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_bytes(b"\xef\xbb\xbfold\n")

    result = execute_write(
        tmp_path,
        {
            "path": "sample.txt",
            "content": "new\n",
        },
    )

    assert result["ok"] is True
    assert result["created"] is False
    assert target.read_bytes() == b"\xef\xbb\xbfnew\n"
