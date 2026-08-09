"""Errors raised by the session tree domain."""

from __future__ import annotations


class SessionTreeError(Exception):
    """Base error for session tree operations."""


class TreeCorruptionError(SessionTreeError, ValueError):
    """Base error for invalid persisted topology."""


class DuplicateNodeError(TreeCorruptionError):
    """A node identifier occurs more than once."""


class DuplicateEntryError(DuplicateNodeError):
    """An entry identifier occurs more than once."""


class MissingParentError(TreeCorruptionError):
    """An entry refers to an entry that is not in the tree."""


class ForwardParentError(TreeCorruptionError):
    """An entry refers to a parent that occurs later in event order."""


class ParentCycleError(TreeCorruptionError):
    """One or more parent links form a cycle."""


class InvalidCurrentError(TreeCorruptionError):
    """The persisted current entry is not in the tree."""


class InvalidCommandError(SessionTreeError, ValueError):
    """Base error for a rejected tree operation."""


class InvalidMessageError(InvalidCommandError):
    """A message cannot be appended in its current form."""


class InvalidToolSequenceError(InvalidMessageError):
    """A message would create an invalid tool-call sequence."""


class InvalidCheckpointError(InvalidCommandError):
    """A checkpoint is incomplete or invalid."""


class EntryReferenceError(SessionTreeError, ValueError):
    """Base error for invalid opaque entry references."""


class UnknownEntryError(EntryReferenceError):
    """An entry reference is not present in its session tree."""


class ForeignEntryError(EntryReferenceError):
    """An entry reference belongs to a different session tree."""


class LegacyImportError(SessionTreeError, ValueError):
    """Base error for legacy session import failures."""


class AmbiguousLegacyTreeError(LegacyImportError):
    """A detached legacy handoff matches more than one checkpoint."""
