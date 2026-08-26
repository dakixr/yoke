from __future__ import annotations

# ruff: noqa: D100,D103,S101

from yoke.cli.main_core import _inject_prompt_flag


def test_prompt_injection_preserves_serve_subcommand_options() -> None:
    argv = ["serve", "--host", "127.0.0.1", "--port", "0"]
    assert _inject_prompt_flag(argv) == argv


def test_prompt_injection_preserves_mcp_subcommand() -> None:
    argv = ["mcp", "demo"]
    assert _inject_prompt_flag(argv) == argv


def test_prompt_injection_recognizes_subcommand_after_top_level_option() -> None:
    argv = ["--root", "/tmp/repo", "serve", "--port", "0"]
    assert _inject_prompt_flag(argv) == argv


def test_prompt_injection_still_converts_bare_prompt() -> None:
    assert _inject_prompt_flag(["fix the tests"]) == ["--prompt", "fix the tests"]
