# ruff: noqa: D100,D103,S101

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from yoke.agent.tools import ModelIdentity
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools import register_search_tools


def registration_context(tmp_path: Path) -> ToolRegistrationContext:
    return ToolRegistrationContext(
        root=tmp_path,
        home=tmp_path,
        provider=cast(Any, SimpleNamespace()),
        model=ModelIdentity(provider_name="demo", model_id="model"),
    )


@pytest.mark.parametrize(
    ("available", "expected"),
    [
        ({"rg"}, ["rg"]),
        ({"fd"}, ["fd"]),
        ({"rg", "fd"}, ["rg", "fd"]),
        (set(), ["grep", "find", "ls"]),
    ],
)
def test_search_registration_selects_native_tools_or_portable_fallbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available: set[str],
    expected: list[str],
) -> None:
    monkeypatch.setattr(
        "yoke.agent.capabilities.builtins.find_search_binary",
        lambda name: f"/test-bin/{name}" if name in available else None,
    )

    tools = register_search_tools(registration_context(tmp_path))

    assert [tool.name for tool in tools] == expected


def test_macos_search_binary_falls_back_to_homebrew_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yoke.agent.tools import search_common

    binary = tmp_path / "rg"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(search_common.sys, "platform", "darwin")
    monkeypatch.setattr(search_common.shutil, "which", lambda name: None)
    monkeypatch.setattr(search_common, "_DARWIN_BINARY_DIRS", (str(tmp_path),))

    assert search_common.find_search_binary("rg") == str(binary)
