"""Tests for yoke CLI startup helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from yoke.cli.main import _load_source_dotenv


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
