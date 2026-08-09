"""Branch navigation and annotation intents for SessionTree."""

from __future__ import annotations

from typing import TYPE_CHECKING

from yoke.agent.models import Message

from .errors import InvalidMessageError
from .projections import BranchEntryView
from .projections import NavigationOutcome
from .projections import NavigationPreview
from .values import EntryRef
from .values import MessageView
from ._mutations import _validate_role_tool_fields
from ._topology import active_path

if TYPE_CHECKING:
    from yoke.agent.models import ConversationEntry
    from yoke.agent.models import ConversationEntryKind


class SessionTreeNavigation:
    """Implement navigation and annotation intents on canonical topology."""

    _entries: list[ConversationEntry]
    _leaf_id: str | None
    _scope: str

    if TYPE_CHECKING:

        @property
        def current(self) -> EntryRef | None: ...

        def _resolve_ref(self, target: EntryRef) -> str: ...

        def _append_entry(
            self,
            kind: ConversationEntryKind,
            *,
            message: Message | None = None,
            metadata: dict[str, object] | None = None,
        ) -> EntryRef: ...

    def preview_navigation(
        self,
        target: EntryRef,
        *,
        include_abandoned: bool = True,
    ) -> NavigationPreview:
        """Describe editor and abandoned-branch effects without mutation."""
        target_id = self._resolve_ref(target)
        target_entry = next(entry for entry in self._entries if entry.id == target_id)
        abandoned: tuple[BranchEntryView, ...] = ()
        if include_abandoned:
            old_path = active_path(self._entries, self._leaf_id)
            target_path = active_path(self._entries, target_id)
            common_count = 0
            for old, selected in zip(old_path, target_path, strict=False):
                if old.id != selected.id:
                    break
                common_count += 1
            abandoned = tuple(
                self._branch_view(entry) for entry in old_path[common_count:]
            )
        editor_text = (
            target_entry.message.display_text_content() or ""
            if target_entry.kind == "user" and target_entry.message is not None
            else None
        )
        return NavigationPreview(
            target=target,
            current=target_id == self._leaf_id,
            editor_text=editor_text,
            abandoned=abandoned,
        )

    def navigate(
        self,
        target: EntryRef,
        *,
        branch_summary: str | None = None,
    ) -> NavigationOutcome:
        """Select a point, with before-user editing and optional handoff."""
        preview = self.preview_navigation(target, include_abandoned=False)
        target_id = self._resolve_ref(target)
        if preview.current:
            return NavigationOutcome(self.current, None, False)
        selected = next(entry for entry in self._entries if entry.id == target_id)
        old_leaf_id = self._leaf_id
        self._leaf_id = (
            selected.parent_id if preview.editor_text is not None else target_id
        )
        normalized_summary = (branch_summary or "").strip()
        if normalized_summary:
            self._append_entry(
                "branch_summary",
                message=Message.user(
                    f"Branch summary from the path you left:\n\n{normalized_summary}"
                ),
                metadata={
                    "from_leaf_id": old_leaf_id,
                    "target_id": target_id,
                    "summary": normalized_summary,
                },
            )
        return NavigationOutcome(
            self.current,
            preview.editor_text,
            bool(normalized_summary),
        )

    def set_label(self, target: EntryRef, label: str | None) -> None:
        """Set or clear one normalized selector label."""
        entry_id = self._resolve_ref(target)
        normalized = " ".join((label or "").split()).strip()
        index, entry = next(
            (index, item)
            for index, item in enumerate(self._entries)
            if item.id == entry_id
        )
        metadata = dict(entry.metadata)
        if normalized:
            metadata["label"] = normalized
        else:
            metadata.pop("label", None)
        self._entries[index] = entry.model_copy(
            update={"metadata": metadata},
            deep=True,
        )

    def append_interrupted_turn(
        self,
        *,
        user_message: Message | None,
        notice: str,
    ) -> tuple[EntryRef, ...]:
        """Append the accepted user input and its interrupted continuation."""
        appended: list[EntryRef] = []
        if user_message is not None:
            if user_message.role != "user":
                raise InvalidMessageError(
                    "An interrupted turn requires a user message."
                )
            _validate_role_tool_fields(user_message)
            appended.append(self._append_entry("user", message=user_message))
        appended.append(
            self._append_entry("assistant", message=Message.assistant(notice))
        )
        return tuple(appended)

    def _branch_view(self, entry: ConversationEntry) -> BranchEntryView:
        summary = entry.metadata.get("summary")
        return BranchEntryView(
            ref=EntryRef(self._scope, entry.id),
            kind=entry.kind,
            message=(
                MessageView._from_message(entry.message)
                if entry.message is not None
                else None
            ),
            summary_text=summary if isinstance(summary, str) else None,
        )
