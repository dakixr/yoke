"""Tests for yoke CLI startup helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from yoke.cli.main import _inject_prompt_flag
from yoke.cli.main import _load_source_dotenv
from yoke.cli.main import main


def test_load_source_dotenv_sets_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loads key-value pairs from a source-local `.env` file."""
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        """
# comment
PLAIN=value
QUOTED="two words"
export EXPORTED='three words'
KEEP_EQUALS=a=b=c
INVALID_LINE
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.delenv("PLAIN", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)
    monkeypatch.delenv("EXPORTED", raising=False)
    monkeypatch.delenv("KEEP_EQUALS", raising=False)

    _load_source_dotenv(tmp_path)

    if os.environ["PLAIN"] != "value":
        pytest.fail("PLAIN was not loaded from .env")
    if os.environ["QUOTED"] != "two words":
        pytest.fail("QUOTED was not unwrapped correctly")
    if os.environ["EXPORTED"] != "three words":
        pytest.fail("EXPORTED was not loaded from export syntax")
    if os.environ["KEEP_EQUALS"] != "a=b=c":
        pytest.fail("KEEP_EQUALS lost content after the first equals sign")


def test_load_source_dotenv_skips_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaves the environment unchanged when no `.env` file exists."""
    monkeypatch.delenv("MISSING_ENV", raising=False)

    _load_source_dotenv(tmp_path)

    if "MISSING_ENV" in os.environ:
        pytest.fail("Missing .env should not introduce new environment keys")


def test_continue_is_not_treated_as_prompt() -> None:
    """Leaves the continue command as a subcommand during prompt injection."""
    assert _inject_prompt_flag(["continue"]) == ["continue"]
    assert _inject_prompt_flag(["continue", "--global"]) == ["continue", "--global"]


@pytest.mark.parametrize("global_flag", ["--global", "-g"])
def test_continue_command_passes_global_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    global_flag: str,
) -> None:
    """Parses continue global aliases and forwards the resolved root."""
    fake_runtime = ModuleType("yoke.cli.runtime")
    calls: list[tuple[Any, bool]] = []

    def fake_run_continue_cli(args: Any, *, all_sessions: bool = False) -> int:
        calls.append((args, all_sessions))
        return 7

    setattr(fake_runtime, "run_continue_cli", fake_run_continue_cli)
    monkeypatch.setitem(sys.modules, "yoke.cli.runtime", fake_runtime)

    assert main(["continue", global_flag, "--root", str(tmp_path)]) == 7
    assert len(calls) == 1
    args, all_sessions = calls[0]
    assert all_sessions is True
    assert args.root == str(tmp_path.resolve())
