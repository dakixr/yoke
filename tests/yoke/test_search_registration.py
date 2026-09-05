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
        "yoke.agent.capabilities.builtins.shutil.which",
        lambda name: f"/test-bin/{name}" if name in available else None,
    )

    tools = register_search_tools(registration_context(tmp_path))

    assert [tool.name for tool in tools] == expected
