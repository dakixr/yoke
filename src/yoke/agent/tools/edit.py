"""Tool for editing text files with precise replacements."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import AliasChoices, BaseModel, Field, model_validator

from .base import WorkspaceTool


class ReplaceEdit(BaseModel):
    """A single old-text to new-text replacement operation."""

    old_text: str = Field(
        min_length=1,
        validation_alias=AliasChoices("oldText", "old_text"),
        serialization_alias="oldText",
    )
    new_text: str = Field(
        validation_alias=AliasChoices("newText", "new_text"),
        serialization_alias="newText",
    )
    occurrence: int | None = Field(default=None, ge=1)
    replace_all: bool = Field(
        default=False,
        validation_alias=AliasChoices("replaceAll", "replace_all"),
        serialization_alias="replaceAll",
    )

    @model_validator(mode="after")
    def validate_disambiguation(self) -> ReplaceEdit:
        """Validate replacement disambiguation settings."""
        if self.occurrence is not None and self.replace_all:
            raise ValueError("occurrence and replaceAll cannot be used together.")
        return self


@dataclass(frozen=True, slots=True)
class ResolvedMatch:
    """A resolved exact-text match in the current file content."""

    start: int
    end: int
    old_text: str
    new_text: str


class EditTool(WorkspaceTool):
    """Tool that edits a text file using exact text replacements."""

    name = "edit"
    description = (
        "Edit a text file. Use oldText/newText for one exact replacement, "
        "edits for multiple exact replacements applied in order, or newText "
        "without oldText to replace the entire file. Use occurrence to "
        "target a specific repeated match or replaceAll for every exact "
        "match. Missing files are created only for whole-file replacement."
    )

    path: str = Field(min_length=1)
    old_text: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("oldText", "old_text"),
        serialization_alias="oldText",
    )
    new_text: str | None = Field(
        default=None,
        validation_alias=AliasChoices("newText", "new_text"),
        serialization_alias="newText",
    )
    occurrence: int | None = Field(default=None, ge=1)
    replace_all: bool = Field(
        default=False,
        validation_alias=AliasChoices("replaceAll", "replace_all"),
        serialization_alias="replaceAll",
    )
    edits: list[ReplaceEdit] = Field(default_factory=list)
    delete_file: bool = Field(
        default=False,
        validation_alias=AliasChoices("deleteFile", "delete_file"),
        serialization_alias="deleteFile",
    )

    @model_validator(mode="after")
    def validate_edit_mode(self) -> EditTool:
        """Validate that edit mode fields are mutually consistent."""
        has_single_args = self.old_text is not None or self.new_text is not None
        has_multi_args = bool(self.edits)
        uses_top_level_disambiguation = self.occurrence is not None or self.replace_all

        if self.delete_file:
            if has_single_args or has_multi_args or uses_top_level_disambiguation:
                raise ValueError(
                    "deleteFile cannot be true when providing edit content."
                )
            return self

        if has_single_args and has_multi_args:
            raise ValueError("Use either oldText/newText or edits, not both.")
        if not has_single_args and not has_multi_args:
            raise ValueError("Provide newText, oldText/newText, or edits.")
        if has_single_args and self.new_text is None:
            raise ValueError("Edit mode requires newText.")
        if self.occurrence is not None and self.replace_all:
            raise ValueError("occurrence and replaceAll cannot be used together.")
        if has_multi_args and uses_top_level_disambiguation:
            raise ValueError(
                "Top-level occurrence and replaceAll require oldText/newText."
            )
        if self.old_text is None and uses_top_level_disambiguation:
            raise ValueError("occurrence and replaceAll require oldText.")
        return self

    @staticmethod
    def _normalize_json(content: str) -> tuple[str, bool]:
        try:
            parsed = json.loads(content)
            normalized = json.dumps(parsed, indent=2, ensure_ascii=False) + "\n"
            return normalized, normalized != content
        except json.JSONDecodeError:
            return content, False

    def normalized_edits(self) -> list[ReplaceEdit]:
        """Return edits, normalizing single old/new text into a list."""
        if self.edits:
            return self.edits
        assert self.old_text is not None  # noqa: S101
        assert self.new_text is not None  # noqa: S101
        return [
            ReplaceEdit(
                old_text=self.old_text,
                new_text=self.new_text,
                occurrence=self.occurrence,
                replace_all=self.replace_all,
            )
        ]

    def _find_occurrences(self, content: str, needle: str) -> list[tuple[int, int]]:
        matches: list[tuple[int, int]] = []
        start = 0
        while True:
            start = content.find(needle, start)
            if start == -1:
                return matches
            end = start + len(needle)
            matches.append((start, end))
            start = end

    def _preview_matches(
        self, content: str, matches: list[tuple[int, int]]
    ) -> list[dict[str, object]]:
        previews: list[dict[str, object]] = []
        for index, (start, end) in enumerate(matches[:3], start=1):
            line_number = content.count("\n", 0, start) + 1
            line_start = content.rfind("\n", 0, start)
            line_start = 0 if line_start == -1 else line_start + 1
            line_end = content.find("\n", end)
            line_end = len(content) if line_end == -1 else line_end
            line_text = content[line_start:line_end].strip()
            if len(line_text) > 120:
                line_text = line_text[:117].rstrip() + "..."
            previews.append(
                {
                    "occurrence": index,
                    "line": line_number,
                    "preview": line_text,
                }
            )
        return previews

    def _resolve_single_edit(
        self, content: str, edit: ReplaceEdit
    ) -> list[ResolvedMatch] | dict[str, object]:
        matches = self._find_occurrences(content, edit.old_text)
        if not matches:
            return self._error(
                f"Text not found in {self.path}",
                path=self.path,
                match_count=0,
                suggestion=("Re-read the file and retry with the exact current text."),
            )
        if edit.replace_all:
            return [
                ResolvedMatch(start, end, edit.old_text, edit.new_text)
                for start, end in matches
            ]
        if edit.occurrence is not None:
            if edit.occurrence > len(matches):
                return self._error(
                    (f"Occurrence {edit.occurrence} is out of range in {self.path}"),
                    path=self.path,
                    match_count=len(matches),
                    previews=self._preview_matches(content, matches),
                )
            start, end = matches[edit.occurrence - 1]
            return [ResolvedMatch(start, end, edit.old_text, edit.new_text)]
        if len(matches) > 1:
            return self._error(
                f"Text to replace is ambiguous in {self.path}",
                path=self.path,
                match_count=len(matches),
                previews=self._preview_matches(content, matches),
                suggestion=(
                    "Provide occurrence to target one match or replaceAll "
                    "to edit every exact match."
                ),
            )
        start, end = matches[0]
        return [ResolvedMatch(start, end, edit.old_text, edit.new_text)]

    def _apply_resolved_matches(
        self, content: str, resolved: list[ResolvedMatch]
    ) -> str | dict[str, object]:
        updated = content
        for match in reversed(resolved):
            if updated[match.start : match.end] != match.old_text:
                return self._error(
                    f"Edit no longer matches file content in {self.path}",
                    path=self.path,
                )
            updated = updated[: match.start] + match.new_text + updated[match.end :]
        return updated

    def _apply_edits_in_order(
        self, content: str
    ) -> tuple[str, int] | dict[str, object]:
        updated = content
        edits_applied = 0
        for edit in self.normalized_edits():
            resolution = self._resolve_single_edit(updated, edit)
            if isinstance(resolution, dict):
                return resolution
            updated = self._apply_resolved_matches(updated, resolution)
            if isinstance(updated, dict):
                return updated
            edits_applied += len(resolution)
        return updated, edits_applied

    def execute(self) -> dict[str, object]:
        """Apply the edit operation and return the result dict."""
        try:
            if self.delete_file:
                path = self._resolve_path(self.path)
                if not path.exists():
                    return self._error(f"File not found: {self.path}", path=self.path)
                if not path.is_file():
                    return self._error(
                        f"Path is not a regular file: {self.path}",
                        path=self.path,
                    )
                previous_content = self._read_existing_text(path)
                bytes_removed = (
                    len(previous_content.encode("utf-8"))
                    if previous_content is not None
                    else path.stat().st_size
                )
                path.unlink()
                return self._success(
                    deleted=True,
                    bytes_removed=bytes_removed,
                )

            if not self.edits and self.old_text is None:
                assert self.new_text is not None  # noqa: S101
                return self._replace_entire_file(self.new_text)

            path = self._resolve_path(self.path)
            self._ensure_text_file(path)
            original = path.read_text(encoding="utf-8")
            updated = self._apply_edits_in_order(original)
            if isinstance(updated, dict):
                return updated

            updated_content, edits_applied = updated
            path.write_text(updated_content, encoding="utf-8")
            return self._success(
                edits_applied=edits_applied,
                bytes_written=len(updated_content.encode("utf-8")),
            )
        except Exception as exc:
            return self._error(str(exc), path=self.path)

    def _replace_entire_file(self, content: str) -> dict[str, object]:
        path = self._resolve_path(self.path, allow_missing=True)
        existed = path.exists()
        if existed and not path.is_file():
            raise ValueError(f"Path is not a regular file: {self.path}")
        json_normalized = False
        if path.suffix.lower() == ".json":
            content, json_normalized = self._normalize_json(content)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        result = self._success(
            bytes_written=len(content.encode("utf-8")),
            created=not existed,
            edits_applied=1,
        )
        if json_normalized:
            result["json_normalized"] = True
        return result
