"""Durable-state behavior shared by the public SDK agent."""

from __future__ import annotations

import os
from pathlib import Path
import threading
from typing import Any, Self

from yoke.agent.persistence import restore_agent_state
from yoke.agent.persistence import save_agent_state


class DurableAgentMixin:
    """Provide synchronized durable-state operations to an SDK agent."""

    _autosave: bool
    _prompt_lock: threading.RLock
    _prompt_owner: int | None
    _runtime: Any
    _state_path: Path | None

    @property
    def state_path(self) -> Path | None:
        """Return the durable state path bound to this agent, if any."""
        with self._prompt_lock:
            return self._state_path

    @property
    def autosave(self) -> bool:
        """Return whether this agent saves after each successful prompt."""
        return self._autosave

    def save(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        metadata: dict[str, Any] | None = None,
        atomic: bool = True,
    ) -> Path:
        """Save the current portable agent state to a durable snapshot file."""
        with self._prompt_lock:
            self._ensure_open()
            self._ensure_not_prompt_callback("save")
            target = (
                normalize_state_path(path) if path is not None else self._state_path
            )
            if target is None:
                raise ValueError("Agent.save() requires a path or bound state_path.")
            return self._save_unlocked(
                target,
                metadata=metadata,
                atomic=atomic,
            )

    def restore(
        self,
        path: str | os.PathLike[str],
        *,
        strict: bool = True,
    ) -> Self:
        """Replace this agent's portable state from a durable snapshot file."""
        target = normalize_state_path(path)
        if target is None:
            raise ValueError("Agent.restore() requires a path.")
        with self._prompt_lock:
            self._ensure_open()
            self._ensure_not_prompt_callback("restore")
            restore_agent_state(self._runtime, target, strict=strict)
            self._state_path = target
            return self

    def _save_unlocked(
        self,
        target: Path,
        *,
        metadata: dict[str, Any] | None = None,
        atomic: bool = True,
    ) -> Path:
        """Save state while the caller owns the prompt lock."""
        saved_path = save_agent_state(
            self._runtime,
            target,
            metadata=metadata,
            atomic=atomic,
        )
        self._state_path = saved_path
        return saved_path

    def _ensure_open(self) -> None:
        raise NotImplementedError

    def _ensure_not_prompt_callback(self, operation: str) -> None:
        raise NotImplementedError


def normalize_state_path(
    path: str | os.PathLike[str] | None,
) -> Path | None:
    """Resolve an optional durable-state path."""
    if path is None:
        return None
    return Path(path).expanduser().resolve()
