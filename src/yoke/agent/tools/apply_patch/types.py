"""Typed models for the apply_patch tool."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class PatchChunk:
    """One update hunk within an apply_patch update operation."""

    context: str | None
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    end_of_file: bool = False


@dataclass(slots=True, frozen=True)
class AddFileOp:
    """An add-file operation parsed from the patch body."""

    path: str
    lines: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class DeleteFileOp:
    """A delete-file operation parsed from the patch body."""

    path: str


@dataclass(slots=True, frozen=True)
class UpdateFileOp:
    """An update-file operation parsed from the patch body."""

    path: str
    move_to: str | None
    chunks: tuple[PatchChunk, ...]


PatchOperation = AddFileOp | DeleteFileOp | UpdateFileOp


@dataclass(slots=True, frozen=True)
class AddChange:
    """A verified file-addition ready to apply."""

    path: Path
    content: str


@dataclass(slots=True, frozen=True)
class DeleteChange:
    """A verified file-deletion ready to apply."""

    path: Path
    old_content: str


@dataclass(slots=True, frozen=True)
class UpdateChange:
    """A verified file-update or move ready to apply."""

    path: Path
    old_content: str
    new_content: str
    move_to: Path | None = None


VerifiedChange = AddChange | DeleteChange | UpdateChange


class ApplyPatchError(ValueError):
    """Raised when an apply_patch input cannot be parsed or verified."""
