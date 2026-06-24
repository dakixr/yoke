"""Lightweight CLI argument data structures."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True)
class CLIArgs:
    """Parsed CLI arguments used to construct runtime state."""

    prompt: str | None = None
    headless: bool = False
    session: str | None = None
    fork_session_id: str | None = None
    model: str | None = None
    provider_name: str | None = None
    provider_from_default: bool = False
    reasoning_effort: str | None = None
    root: str = os.getcwd()
    skills: tuple[str, ...] = ()
    images: tuple[str, ...] = ()
