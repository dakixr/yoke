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


def test_search_registration_prefers_rg_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "yoke.agent.capabilities.core.shutil.which",
        lambda name: "/usr/bin/rg" if name == "rg" else None,
    )

    tools = register_search_tools(registration_context(tmp_path))

    assert [tool.name for tool in tools] == ["rg"]


def test_search_registration_uses_fallback_when_rg_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "yoke.agent.capabilities.core.shutil.which",
        lambda _name: None,
    )

    tools = register_search_tools(registration_context(tmp_path))

    assert [tool.name for tool in tools] == ["grep", "find", "ls"]
