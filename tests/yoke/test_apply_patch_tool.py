# ruff: noqa: D100, D103, S101

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from yoke.agent.tools import ApplyPatchTool
from yoke.agent.tools import EditTool
from yoke.agent.tools import ModelIdentity
from yoke.agent.tools import ToolRegistrationContext
from yoke.ai.providers.base import ProviderModelInfo
from yoke.cli.bootstrap.tools import create_builtin_tools


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


def test_builtin_tools_select_apply_patch_for_gpt_models(tmp_path: Path) -> None:
    provider = SimpleNamespace()
    context = ToolRegistrationContext(
        root=tmp_path,
        home=tmp_path,
        provider=cast(Any, provider),
        model=ModelIdentity(provider_name="demo", model_id="my-gpt-coder"),
    )
    tools = create_builtin_tools(context)
    names = [tool.name for tool in tools]

    assert "apply_patch" in names
    assert "edit" not in names
    assert isinstance(tools[1], ApplyPatchTool)


def test_builtin_tools_select_edit_for_non_gpt_models(tmp_path: Path) -> None:
    provider = SimpleNamespace()
    context = ToolRegistrationContext(
        root=tmp_path,
        home=tmp_path,
        provider=cast(Any, provider),
        model=ModelIdentity(provider_name="demo", model_id="kimi-k2.7-code"),
    )
    tools = create_builtin_tools(context)
    names = [tool.name for tool in tools]

    assert "edit" in names
    assert "apply_patch" not in names
    assert isinstance(tools[1], EditTool)


def test_builtin_tools_skip_attach_image_for_text_only_models(tmp_path: Path) -> None:
    class TextOnlyProvider:
        supports_image_inputs = True

        def current_model_info(self) -> ProviderModelInfo:
            return ProviderModelInfo(
                id="text-only",
                display_name="Text Only",
                context_window_tokens=1000,
                supports_image_inputs=False,
            )

    provider = TextOnlyProvider()
    context = ToolRegistrationContext(
        root=tmp_path,
        home=tmp_path,
        provider=cast(Any, provider),
        model=ModelIdentity(provider_name="demo", model_id="text-only"),
    )

    names = [tool.name for tool in create_builtin_tools(context)]

    assert "attach_image" not in names


def test_builtin_tools_include_attach_image_for_image_models(tmp_path: Path) -> None:
    class ImageProvider:
        supports_image_inputs = False

        def current_model_info(self) -> ProviderModelInfo:
            return ProviderModelInfo(
                id="vision",
                display_name="Vision",
                context_window_tokens=1000,
                supports_image_inputs=True,
            )

    provider = ImageProvider()
    context = ToolRegistrationContext(
        root=tmp_path,
        home=tmp_path,
        provider=cast(Any, provider),
        model=ModelIdentity(provider_name="demo", model_id="vision"),
    )

    names = [tool.name for tool in create_builtin_tools(context)]

    assert "attach_image" in names


def test_builtin_tools_include_image_generation_only_for_codex_providers(
    tmp_path: Path,
) -> None:
    class CodexProvider:
        provider_name = "codex"
        supports_image_inputs = True
        supports_image_generation = True

        def generate_image(self, *, prompt: str) -> str:
            del prompt
            return ""

    class VisionProvider:
        provider_name = "demo"
        supports_image_inputs = True

    codex_context = ToolRegistrationContext(
        root=tmp_path,
        home=tmp_path,
        provider=cast(Any, CodexProvider()),
        model=ModelIdentity(provider_name="codex", model_id="gpt-5.4"),
    )
    vision_context = ToolRegistrationContext(
        root=tmp_path,
        home=tmp_path,
        provider=cast(Any, VisionProvider()),
        model=ModelIdentity(provider_name="demo", model_id="vision"),
    )

    codex_names = [tool.name for tool in create_builtin_tools(codex_context)]
    vision_names = [tool.name for tool in create_builtin_tools(vision_context)]

    assert "image_generation" in codex_names
    assert "image_generation" not in vision_names


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


def test_apply_patch_accepts_absolute_paths_inside_workspace(
    tmp_path: Path,
) -> None:
    tool = ApplyPatchTool.bind(root=tmp_path)
    notes_path = tmp_path / "notes.txt"
    delete_path = tmp_path / "delete.txt"
    added_path = tmp_path / "added.txt"
    notes_path.write_text("alpha\nbeta\n", encoding="utf-8")
    delete_path.write_text("remove me\n", encoding="utf-8")
    patch = f"""*** Begin Patch
*** Add File: {added_path}
+first
*** Update File: {notes_path}
@@
-beta
+gamma
*** Delete File: {delete_path}
*** End Patch
"""

    result = as_dict(tool.parse_arguments({"input": patch}).execute())

    assert result["ok"] is True
    assert added_path.read_text(encoding="utf-8") == "first\n"
    assert notes_path.read_text(encoding="utf-8") == "alpha\ngamma\n"
    assert not delete_path.exists()


def test_apply_patch_accepts_absolute_paths_outside_workspace(
    tmp_path: Path,
) -> None:
    outside_path = tmp_path.parent / "outside.txt"
    outside_path.write_text("outside\n", encoding="utf-8")
    tool = ApplyPatchTool.bind(root=tmp_path)
    patch = f"""*** Begin Patch
*** Delete File: {outside_path}
*** End Patch
"""

    result = as_dict(tool.parse_arguments({"input": patch}).execute())

    assert result["ok"] is True
    assert not outside_path.exists()
