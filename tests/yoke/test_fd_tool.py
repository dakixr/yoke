from __future__ import annotations

# ruff: noqa: D100,D103,S101

from pathlib import Path
from typing import Any
from typing import cast

from yoke.agent.capabilities import CapabilityContext
from yoke.agent.capabilities import FileSearchCapability
from yoke.agent.models import Message
from yoke.agent.tools import FdTool
from yoke.ai.providers.base import Provider


class ProviderStub(Provider):
    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del messages, tools
        return Message.assistant("done")


def execute_fd(tmp_path: Path, **arguments: object) -> dict[str, Any]:
    tool = FdTool.bind(root=tmp_path)
    return cast(dict[str, Any], tool.parse_arguments(arguments).execute())


def test_fd_finds_paths_and_respects_root_dir(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("", encoding="utf-8")
    (project / "notes.txt").write_text("", encoding="utf-8")
    monkeypatch.setattr(FdTool, "_find_fd_binary", staticmethod(lambda: "fd"))

    class Completed:
        returncode = 0
        stdout = "main.py\n"
        stderr = ""

    seen: dict[str, object] = {}

    def fake_run(command, *, cwd, **kwargs):
        del kwargs
        seen.update(command=command, cwd=cwd)
        return Completed()

    monkeypatch.setattr("yoke.agent.tools.fd.subprocess.run", fake_run)
    result = execute_fd(tmp_path, raw_args="main -e py", root_dir="project")

    assert result["ok"] is True
    assert result["output"] == ["main.py"]
    assert seen == {"command": ["fd", "main", "-e", "py"], "cwd": project}


def test_fd_bounds_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(FdTool, "_find_fd_binary", staticmethod(lambda: "fd"))

    class Completed:
        returncode = 0
        stdout = "one.txt\ntwo.txt\nthree.txt\n"
        stderr = ""

    monkeypatch.setattr(
        "yoke.agent.tools.fd.subprocess.run", lambda *args, **kwargs: Completed()
    )
    result = execute_fd(tmp_path, max_output_chars=12)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["output"] == ["one.txt"]


def test_file_search_capability_registers_fd_and_rg_when_available(
    tmp_path: Path, monkeypatch
) -> None:
    context = CapabilityContext.from_provider(
        root=tmp_path,
        home=tmp_path,
        provider=ProviderStub(),
    )
    monkeypatch.setattr(
        CapabilityContext,
        "executable",
        lambda self, name: f"/bin/{name}" if name in {"fd", "rg"} else None,
    )

    registration = FileSearchCapability().register(context)

    assert [tool.name for tool in registration.tools] == ["rg", "fd"]
