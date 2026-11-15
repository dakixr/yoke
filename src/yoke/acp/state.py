"""Typed ACP bridge state shared by lifecycle and turn handlers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias, overload

ModelCatalogEntry: TypeAlias = dict[str, Any]
ModelCatalog: TypeAlias = dict[str, ModelCatalogEntry]


@dataclass(slots=True)
class AcpSession:
    """Selected model state for one loaded native session."""

    model: str
    effort: str | None


@dataclass(slots=True)
class ActiveTurn:
    """Cancellation and completion signals owned by one ACP prompt."""

    cancel: bool
    wake: asyncio.Event
    done: asyncio.Event
    task: asyncio.Task[object] | None

    @overload
    def __getitem__(self, key: Literal["wake", "done"]) -> asyncio.Event: ...

    @overload
    def __getitem__(self, key: Literal["cancel"]) -> bool: ...

    @overload
    def __getitem__(self, key: Literal["task"]) -> asyncio.Task[object] | None: ...

    def __getitem__(self, key: str) -> object:
        """Compatibility view for older integrations during the transition."""
        return getattr(self, key)
