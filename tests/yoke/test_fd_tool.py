from __future__ import annotations

# ruff: noqa: D100, D101, D102, D103, D106, S101

from pathlib import Path
import shutil
from typing import Any, cast

import pytest
from pydantic import ValidationError

from yoke.agent.capabilities import create_builtin_capabilities
from yoke.agent.models import Message
from yoke.agent.tools import FdTool
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools.context import ModelIdentity
from yoke.ai.providers.base import Provider

requires_fd = pytest.mark.skipif(
    shutil.which("fd") is None, reason="fd is not installed"
)


class ProviderStub(Provider):
    provider_name = "test"
    max_images_per_message = None
    supports_image_inputs = False

    class Config:
        model = "test-model"

    config = Config()

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        del messages, tools
        return Message.assistant("done")


def execute_fd(tmp_path: Path, **arguments: object) -> dict[str, Any]:
    tool = FdTool.bind(root=tmp_path)
    return cast(dict[str, Any], tool.parse_arguments(arguments).execute())


def test_fd_is_first_class_file_search_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "yoke.agent.capabilities.builtins.shutil.which",
        lambda name: f"/test-bin/{name}" if name in {"rg", "fd"} else None,
    )
    provider = ProviderStub()
    context = ToolRegistrationContext(
        root=tmp_path,
        home=tmp_path,
        provider=provider,
        model=ModelIdentity(provider_name="test", model_id="test-model"),
    )

    registrations = create_builtin_capabilities(context)
    search_tools = {
        tool.name
        for registration in registrations
        if registration.capability_id == "file.search"
        for tool in registration.tools
    }

    assert search_tools == {"fd", "rg"}


@requires_fd
def test_fd_finds_paths_with_real_fd_arguments(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "notes.txt").write_text("notes\n", encoding="utf-8")

    result = execute_fd(tmp_path, pattern="main", extensions=["py"])

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert any(
        str(path).replace("\\", "/").endswith("src/main.py")
        for path in result["output"]
    )
    assert not any(str(path).endswith("notes.txt") for path in result["output"])


@requires_fd
def test_fd_root_dir_and_ignore_semantics(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (project / "visible.py").write_text("", encoding="utf-8")
    (project / "ignored.py").write_text("", encoding="utf-8")

    result = execute_fd(tmp_path, extensions=["py"], root_dir="project")

    assert result["ok"] is True
    assert any(str(path).endswith("visible.py") for path in result["output"])
    assert not any(str(path).endswith("ignored.py") for path in result["output"])


@requires_fd
def test_fd_bounds_output(tmp_path: Path) -> None:
    for index in range(20):
        (tmp_path / f"file-{index:02}.txt").write_text("", encoding="utf-8")

    result = execute_fd(tmp_path, extensions=["txt"], max_output_chars=25)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert len(result["output"]) < 20


def test_fd_rejects_non_directory_root(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("", encoding="utf-8")

    result = execute_fd(tmp_path, root_dir="file.txt")

    assert result["ok"] is False
    assert "not a directory" in result["error"]


def test_fd_rejects_raw_args_contract() -> None:
    with pytest.raises(ValidationError):
        FdTool.model_validate({"raw_args": "-t f | head -20"})


def test_fd_typed_command_uses_search_paths_and_limit(tmp_path: Path) -> None:
    tool = cast(
        FdTool,
        FdTool.bind(root=tmp_path).parse_arguments(
            {
                "pattern": "test",
                "paths": ["src", "tests"],
                "types": ["file"],
                "extensions": ["py"],
                "hidden": True,
                "max_depth": 4,
                "limit": 20,
            }
        ),
    )

    command = tool._build_command("fd", tmp_path)

    assert ["--type", "file"] == command[1:3]
    assert "--print0" in command
    assert ["--max-results", "20"] == command[
        command.index("--max-results") : command.index("--max-results") + 2
    ]
    assert command.count("--search-path") == 2
    assert command[-2:] == ["--", "test"]
    assert "|" not in command


def test_fd_filter_sort_and_limit_shape_results(tmp_path: Path) -> None:
    tool = cast(
        FdTool,
        FdTool.bind(root=tmp_path).parse_arguments(
            {
                "filter_pattern": r"\.py$",
                "sort": "path",
                "limit": 2,
            }
        ),
    )

    result = tool._render_output(
        "z.py\x00notes.txt\x00a.py\x00b.py\x00",
        "",
        ["fd"],
        0,
    )

    assert result["ok"] is True
    assert result["output"] == ["a.py", "b.py"]


def test_fd_details_limit_is_applied_after_execution(tmp_path: Path) -> None:
    tool = cast(
        FdTool,
        FdTool.bind(root=tmp_path).parse_arguments(
            {"pattern": "rg.py", "details": True, "limit": 2}
        ),
    )

    command = tool._build_command("fd", tmp_path)

    assert "--list-details" not in command
    assert "--print0" in command


def test_fd_details_are_structured_and_sort_by_path(tmp_path: Path) -> None:
    (tmp_path / "z.py").write_text("z", encoding="utf-8")
    (tmp_path / "a.py").write_text("aa", encoding="utf-8")
    tool = cast(
        FdTool,
        FdTool.bind(root=tmp_path).parse_arguments({"details": True, "sort": "path"}),
    )

    result = tool._render_output("z.py\x00a.py\x00", "", ["fd"], 0)

    output = cast(list[dict[str, object]], result["output"])
    assert [item["path"] for item in output] == ["a.py", "z.py"]
    assert output[0]["type"] == "file"
    assert output[0]["size_bytes"] == 2


def test_fd_nul_parser_preserves_newlines_in_paths(tmp_path: Path) -> None:
    tool = cast(FdTool, FdTool.bind(root=tmp_path).parse_arguments({}))

    result = tool._render_output("plain\x00line\nname\x00", "", ["fd"], 0)

    assert result["output"] == ["plain", "line\nname"]


def test_fd_bounds_failure_diagnostics(tmp_path: Path) -> None:
    tool = cast(
        FdTool,
        FdTool.bind(root=tmp_path).parse_arguments({"max_output_chars": 20}),
    )

    result = tool._render_output("", "x" * 200, ["fd"], 2)

    assert result["ok"] is False
    assert len(cast(str, result["error"])) == 20
    assert result["truncated"] is True
