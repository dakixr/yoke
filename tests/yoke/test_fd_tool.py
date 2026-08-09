from __future__ import annotations

# ruff: noqa: D100, D101, D102, D103, D106, S101

from pathlib import Path
from typing import Any, cast

from yoke.agent.capabilities import create_builtin_capabilities
from yoke.agent.models import Message
from yoke.agent.tools import FdTool
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools.context import ModelIdentity
from yoke.agent.tools.fd import _resolve_fd_binary
from yoke.agent.tools.rg import _resolve_rg_binary
from yoke.ai.providers.base import Provider


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


def test_fd_and_rg_are_available_on_path() -> None:
    fd_binary = Path(_resolve_fd_binary())
    rg_binary = Path(_resolve_rg_binary())

    assert fd_binary.is_file()
    assert rg_binary.is_file()


def test_fd_is_first_class_file_search_tool(tmp_path: Path) -> None:
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


def test_fd_finds_paths_with_real_fd_arguments(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "notes.txt").write_text("notes\n", encoding="utf-8")

    result = execute_fd(tmp_path, raw_args="main -e py")

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert any(
        str(path).replace("\\", "/").endswith("src/main.py")
        for path in result["output"]
    )
    assert not any(str(path).endswith("notes.txt") for path in result["output"])


def test_fd_root_dir_and_ignore_semantics(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (project / "visible.py").write_text("", encoding="utf-8")
    (project / "ignored.py").write_text("", encoding="utf-8")

    result = execute_fd(tmp_path, raw_args="-e py", root_dir="project")

    assert result["ok"] is True
    assert any(str(path).endswith("visible.py") for path in result["output"])
    assert not any(str(path).endswith("ignored.py") for path in result["output"])


def test_fd_bounds_output(tmp_path: Path) -> None:
    for index in range(20):
        (tmp_path / f"file-{index:02}.txt").write_text("", encoding="utf-8")

    result = execute_fd(tmp_path, raw_args="-e txt", max_output_chars=25)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert len(result["output"]) < 20


def test_fd_rejects_non_directory_root(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("", encoding="utf-8")

    result = execute_fd(tmp_path, root_dir="file.txt")

    assert result["ok"] is False
    assert "not a directory" in result["error"]
