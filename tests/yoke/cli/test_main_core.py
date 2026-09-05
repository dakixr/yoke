from __future__ import annotations

# ruff: noqa: D100,D103,S101

import pytest

from yoke.cli.main_core import _inject_prompt_flag


@pytest.mark.parametrize(
    "argv",
    [
        ["serve", "--host", "127.0.0.1", "--port", "0"],
        ["mcp", "demo"],
        ["--root", "/tmp/repo", "serve", "--port", "0"],
    ],
)
def test_prompt_injection_preserves_subcommands(argv: list[str]) -> None:
    assert _inject_prompt_flag(argv) == argv


def test_prompt_injection_still_converts_bare_prompt() -> None:
    assert _inject_prompt_flag(["fix the tests"]) == ["--prompt", "fix the tests"]
