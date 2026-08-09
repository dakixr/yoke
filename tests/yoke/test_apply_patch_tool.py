# ruff: noqa: D100, D103, S101

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from yoke.agent.tools import ApplyPatchTool


def as_dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value)


def test_apply_patch_can_add_move_update_and_delete_files(
    tmp_path: Path,
) -> None:
    tool = ApplyPatchTool.bind(root=tmp_path)
    (tmp_path / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (tmp_path / "delete.txt").write_text("remove me\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Add File: added.txt
+first
+second
*** Update File: notes.txt
*** Move to: renamed.txt
@@
-alpha
-beta
+alpha
+gamma
*** Delete File: delete.txt
*** End Patch
"""

    result = as_dict(tool.parse_arguments({"input": patch}).execute())

    assert result["ok"] is True
    assert result["changes_applied"] == 3
    assert cast(str, result["stdout"]).startswith(
        "Success. Updated the following files:"
    )
    assert (tmp_path / "added.txt").read_text(encoding="utf-8") == "first\nsecond\n"
    assert not (tmp_path / "notes.txt").exists()
    assert (tmp_path / "renamed.txt").read_text(encoding="utf-8") == "alpha\ngamma\n"
    assert not (tmp_path / "delete.txt").exists()


def test_apply_patch_verifies_all_changes_before_mutating_workspace(
    tmp_path: Path,
) -> None:
    tool = ApplyPatchTool.bind(root=tmp_path)
    original = "alpha\nbeta\n"
    (tmp_path / "notes.txt").write_text(original, encoding="utf-8")
    patch = """*** Begin Patch
*** Add File: added.txt
+hello
*** Update File: notes.txt
@@
-missing
+gamma
*** End Patch
"""

    result = as_dict(tool.parse_arguments({"input": patch}).execute())

    assert result["ok"] is False
    assert "Failed to find expected lines" in cast(str, result["error"])
    assert not (tmp_path / "added.txt").exists()
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == original
