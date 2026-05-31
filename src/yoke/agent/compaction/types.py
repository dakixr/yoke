"""Shared compaction type aliases."""

from __future__ import annotations

from typing import Literal

CompactionBoundary = Literal["user", "assistant", "split_turn"]
CompactionReason = Literal["threshold", "overflow_retry", "forced", "manual"]
