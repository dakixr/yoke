"""Identity-first reconciliation of persistence-compatible branches."""

from __future__ import annotations

import secrets

from yoke.agent.models import ConversationEntry

from ._topology import active_path
from ._topology import copy_and_validate


def reconcile_entries(
    existing_entries: list[ConversationEntry],
    incoming: list[ConversationEntry],
    incoming_leaf: str | None,
) -> tuple[list[ConversationEntry], str | None]:
    """Merge by shared canonical identity and select the incoming branch."""
    existing_by_id = {entry.id: entry for entry in existing_entries}
    mapping: dict[str, str] = {}
    for source in incoming:
        if source.parent_id is not None and source.parent_id not in mapping:
            continue
        existing = existing_by_id.get(source.id)
        expected_parent = (
            mapping.get(source.parent_id, source.parent_id)
            if source.parent_id is not None
            else None
        )
        if existing is None or existing.parent_id != expected_parent:
            continue
        candidate = source.model_copy(
            update={"parent_id": expected_parent},
            deep=True,
        )
        if existing == candidate:
            mapping[source.id] = existing.id
    return _merge_with_mapping(
        existing_entries,
        incoming,
        incoming_leaf,
        mapping=mapping,
    )


def reconcile_legacy_entries_by_message(
    existing_entries: list[ConversationEntry],
    existing_leaf: str | None,
    incoming: list[ConversationEntry],
    incoming_leaf: str | None,
) -> tuple[list[ConversationEntry], str | None]:
    """Merge an identity-free legacy transcript by equal active prefixes."""
    source_path = active_path(incoming, incoming_leaf)
    destination_path = active_path(existing_entries, existing_leaf)
    mapping: dict[str, str] = {}
    for source, destination in zip(source_path, destination_path, strict=False):
        if not _same_legacy_intent(source, destination):
            break
        mapping[source.id] = destination.id
    shared_ids = {entry.id for entry in existing_entries}
    for source in incoming:
        if source.id in shared_ids:
            mapping[source.id] = source.id
    return _merge_with_mapping(
        existing_entries,
        incoming,
        incoming_leaf,
        mapping=mapping,
    )


def _merge_with_mapping(
    existing_entries: list[ConversationEntry],
    incoming: list[ConversationEntry],
    incoming_leaf: str | None,
    *,
    mapping: dict[str, str],
) -> tuple[list[ConversationEntry], str | None]:
    merged = [entry.model_copy(deep=True) for entry in existing_entries]
    existing = {entry.id: entry for entry in merged}
    for source in incoming:
        if source.id in mapping:
            continue
        copied = source.model_copy(deep=True)
        if copied.id in existing:
            copied.id = _unique_id(existing)
        if copied.parent_id is not None:
            copied.parent_id = mapping.get(copied.parent_id, copied.parent_id)
        merged.append(copied)
        existing[copied.id] = copied
        mapping[source.id] = copied.id
    selected = (
        mapping.get(incoming_leaf, incoming_leaf) if incoming_leaf is not None else None
    )
    return copy_and_validate(merged, selected, assume_linear=False)


def _same_legacy_intent(left: ConversationEntry, right: ConversationEntry) -> bool:
    if left.message is not None or right.message is not None:
        return left.kind == right.kind and left.message == right.message
    return left.kind == right.kind and left.metadata == right.metadata


def _unique_id(existing: dict[str, ConversationEntry]) -> str:
    while True:
        candidate = secrets.token_hex(8)
        if candidate not in existing:
            return candidate
