"""Tool for applying codex-style patches within the workspace."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from pathlib import PureWindowsPath

from pydantic import Field

from yoke.agent.tools.apply_patch.parser import PatchParser
from yoke.agent.tools.apply_patch.types import AddChange
from yoke.agent.tools.apply_patch.types import AddFileOp
from yoke.agent.tools.apply_patch.types import ApplyPatchError
from yoke.agent.tools.apply_patch.types import DeleteChange
from yoke.agent.tools.apply_patch.types import DeleteFileOp
from yoke.agent.tools.apply_patch.types import PatchChunk
from yoke.agent.tools.apply_patch.types import PatchOperation
from yoke.agent.tools.apply_patch.types import UpdateChange
from yoke.agent.tools.apply_patch.types import UpdateFileOp
from yoke.agent.tools.apply_patch.types import VerifiedChange

from ..base import WorkspaceTool


class ApplyPatchTool(WorkspaceTool):
    """Tool that applies codex-style file patches inside the workspace."""

    name = "apply_patch"
    description = (
        "Apply a codex-style patch to files in the workspace. "
        "Provide the full patch text in `input` using the "
        "`*** Begin Patch` / `*** End Patch` envelope."
    )

    input: str = Field(min_length=1)

    def execute(self) -> dict[str, object]:
        """Parse, verify, and apply the patch atomically per file write."""
        try:
            operations = PatchParser(self.input).parse()
            changes = self._verify_changes(operations)
            changed_files, stdout = self._apply_changes(changes)
            return self._success(
                changes=changed_files,
                changes_applied=len(changed_files),
                stdout=stdout,
                stderr="",
            )
        except Exception as exc:
            return self._error(str(exc), stderr=str(exc))

    def _verify_changes(self, operations: list[PatchOperation]) -> list[VerifiedChange]:
        state: dict[Path, str | None] = {}
        verified: list[VerifiedChange] = []
        for operation in operations:
            if isinstance(operation, AddFileOp):
                verified.append(self._verify_add(state, operation))
                continue
            if isinstance(operation, DeleteFileOp):
                verified.append(self._verify_delete(state, operation))
                continue
            verified.append(self._verify_update(state, operation))
        return verified

    def _verify_add(
        self,
        state: dict[Path, str | None],
        operation: AddFileOp,
    ) -> AddChange:
        path = self._resolve_patch_path(operation.path, allow_missing=True)
        current = self._load_text_state(state, path)
        if current is not None:
            raise ApplyPatchError(f"File already exists: {self._display_path(path)}")
        content = _join_patch_lines(operation.lines)
        state[path] = content
        return AddChange(path=path, content=content)

    def _verify_delete(
        self,
        state: dict[Path, str | None],
        operation: DeleteFileOp,
    ) -> DeleteChange:
        path = self._resolve_patch_path(operation.path, allow_missing=False)
        current = self._load_text_state(state, path)
        if current is None:
            raise ApplyPatchError(f"File not found: {self._display_path(path)}")
        state[path] = None
        return DeleteChange(path=path, old_content=current)

    def _verify_update(
        self,
        state: dict[Path, str | None],
        operation: UpdateFileOp,
    ) -> UpdateChange:
        path = self._resolve_patch_path(operation.path, allow_missing=False)
        current = self._load_text_state(state, path)
        if current is None:
            raise ApplyPatchError(f"File not found: {self._display_path(path)}")
        updated = self._compute_updated_text(
            self._display_path(path), current, operation.chunks
        )
        move_to = self._resolve_move_target(state, path, operation.move_to, updated)
        if move_to is None:
            state[path] = updated
        return UpdateChange(
            path=path,
            old_content=current,
            new_content=updated,
            move_to=move_to,
        )

    def _resolve_move_target(
        self,
        state: dict[Path, str | None],
        path: Path,
        raw_move_to: str | None,
        updated: str,
    ) -> Path | None:
        if raw_move_to is None:
            return None
        move_to = self._resolve_patch_path(raw_move_to, allow_missing=True)
        if move_to == path:
            return None
        target_state = self._load_text_state(state, move_to)
        if target_state is not None:
            raise ApplyPatchError(
                f"Move target already exists: {self._display_path(move_to)}"
            )
        state[path] = None
        state[move_to] = updated
        return move_to

    def _load_text_state(self, state: dict[Path, str | None], path: Path) -> str | None:
        if path in state:
            return state[path]
        if not path.exists():
            state[path] = None
            return None
        if not path.is_file():
            raise ApplyPatchError(
                f"Path is not a regular file: {self._display_path(path)}"
            )
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ApplyPatchError(
                f"File is not valid UTF-8: {self._display_path(path)}"
            ) from exc
        state[path] = content
        return content

    def _resolve_patch_path(self, raw_path: str, *, allow_missing: bool) -> Path:
        path_text = raw_path.strip()
        if not path_text:
            raise ApplyPatchError("Patch paths must be non-empty")
        candidate_path = Path(path_text)
        windows_path = PureWindowsPath(path_text)
        if candidate_path.is_absolute():
            resolved = candidate_path.resolve()
        elif windows_path.is_absolute() or bool(windows_path.drive):
            resolved = Path(windows_path).resolve()
        else:
            resolved = (self.root / candidate_path).resolve()
        if not allow_missing and not resolved.exists():
            raise ApplyPatchError(f"File not found: {path_text}")
        return resolved

    def _compute_updated_text(
        self,
        display_path: str,
        original: str,
        chunks: tuple[PatchChunk, ...],
    ) -> str:
        lines = _split_file_lines(original)
        replacements: list[tuple[int, int, list[str]]] = []
        cursor = 0
        for chunk in chunks:
            cursor = self._queue_replacement(
                display_path,
                lines,
                chunk,
                cursor,
                replacements,
            )
        for start, old_length, replacement in reversed(replacements):
            lines[start : start + old_length] = replacement
        if not lines or lines[-1] != "":
            lines.append("")
        return "\n".join(lines)

    def _queue_replacement(
        self,
        display_path: str,
        lines: list[str],
        chunk: PatchChunk,
        cursor: int,
        replacements: list[tuple[int, int, list[str]]],
    ) -> int:
        if chunk.context is not None:
            context_index = _seek_sequence(lines, [chunk.context], cursor, eof=False)
            if context_index is None:
                raise ApplyPatchError(
                    f"Failed to find context {chunk.context!r} in {display_path}"
                )
            cursor = context_index + 1
        old_lines = list(chunk.old_lines)
        new_lines = list(chunk.new_lines)
        if not old_lines:
            replacements.append((len(lines), 0, new_lines))
            return cursor
        start, old_lines, new_lines = self._resolve_update_match(
            lines, old_lines, new_lines, cursor, chunk.end_of_file
        )
        if start is None:
            raise ApplyPatchError(f"Failed to find expected lines in {display_path}")
        replacements.append((start, len(old_lines), new_lines))
        return start + len(old_lines)

    def _resolve_update_match(
        self,
        lines: list[str],
        old_lines: list[str],
        new_lines: list[str],
        cursor: int,
        end_of_file: bool,
    ) -> tuple[int | None, list[str], list[str]]:
        start = _seek_sequence(lines, old_lines, cursor, eof=end_of_file)
        if start is not None or old_lines[-1:] != [""]:
            return start, old_lines, new_lines
        trimmed_old = old_lines[:-1]
        trimmed_new = new_lines[:-1] if new_lines[-1:] == [""] else new_lines
        start = _seek_sequence(lines, trimmed_old, cursor, eof=end_of_file)
        if start is None:
            return None, old_lines, new_lines
        return start, trimmed_old, trimmed_new

    def _apply_changes(
        self, changes: list[VerifiedChange]
    ) -> tuple[list[dict[str, str]], str]:
        changed_files: list[dict[str, str]] = []
        for change in changes:
            if isinstance(change, AddChange):
                self._write_text_atomically(change.path, change.content)
                changed_files.append(
                    _change_entry("A", self._display_path(change.path))
                )
                continue
            if isinstance(change, DeleteChange):
                change.path.unlink()
                changed_files.append(
                    _change_entry("D", self._display_path(change.path))
                )
                continue
            target = change.move_to or change.path
            self._write_text_atomically(target, change.new_content)
            if change.move_to is not None:
                change.path.unlink()
                changed_files.append(
                    _change_entry(
                        "M",
                        self._display_path(target),
                        move_from=self._display_path(change.path),
                    )
                )
                continue
            changed_files.append(_change_entry("M", self._display_path(target)))
        stdout = "Success. Updated the following files:\n" + "".join(
            f"{entry['action']} {entry['path']}\n" for entry in changed_files
        )
        return changed_files, stdout

    def _write_text_atomically(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            os.replace(temp_name, path)
        except Exception:
            Path(temp_name).unlink(missing_ok=True)
            raise


def _join_patch_lines(lines: tuple[str, ...]) -> str:
    return "\n".join(lines) + "\n"


def _split_file_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _seek_sequence(
    lines: list[str],
    needle: list[str],
    start: int,
    *,
    eof: bool,
) -> int | None:
    if not needle:
        return len(lines) if eof else start
    limit = len(lines) - len(needle) + 1
    if limit < start:
        return None
    for index in range(start, limit):
        if lines[index : index + len(needle)] != needle:
            continue
        if eof and index + len(needle) != len(lines):
            continue
        return index
    return None


def _change_entry(
    action: str, path: str, *, move_from: str | None = None
) -> dict[str, str]:
    entry: dict[str, str] = {"action": action, "path": path}
    if move_from is not None:
        entry["move_from"] = move_from
    return entry
