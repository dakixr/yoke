"""Parser helpers for the apply_patch tool."""

from __future__ import annotations

from yoke.agent.tools.apply_patch.types import AddFileOp
from yoke.agent.tools.apply_patch.types import ApplyPatchError
from yoke.agent.tools.apply_patch.types import DeleteFileOp
from yoke.agent.tools.apply_patch.types import PatchChunk
from yoke.agent.tools.apply_patch.types import PatchOperation
from yoke.agent.tools.apply_patch.types import UpdateFileOp


class PatchParser:
    """Small parser for the codex-style apply_patch language."""

    def __init__(self, patch_text: str) -> None:
        self._lines = normalize_newlines(patch_text).split("\n")
        self._index = 0

    def parse(self) -> list[PatchOperation]:
        """Parse the full patch body into file operations."""
        if not self._lines or self._lines[0] != "*** Begin Patch":
            raise ApplyPatchError("Patch must start with '*** Begin Patch'")
        self._index = 1
        operations: list[PatchOperation] = []
        while self._index < len(self._lines):
            line = self._lines[self._index]
            if line == "":
                self._index += 1
                continue
            if line == "*** End Patch":
                self._consume_end_patch()
                if not operations:
                    raise ApplyPatchError(
                        "Patch must contain at least one file operation"
                    )
                return operations
            if line.startswith("*** Add File: "):
                operations.append(self._parse_add_file())
                continue
            if line.startswith("*** Delete File: "):
                operations.append(self._parse_delete_file())
                continue
            if line.startswith("*** Update File: "):
                operations.append(self._parse_update_file())
                continue
            raise ApplyPatchError(f"Unexpected patch header: {line}")
        raise ApplyPatchError("Patch is missing '*** End Patch'")

    def _consume_end_patch(self) -> None:
        self._index += 1
        while self._index < len(self._lines) and self._lines[self._index] == "":
            self._index += 1
        if self._index != len(self._lines):
            raise ApplyPatchError("Patch contains content after '*** End Patch'")

    def _parse_add_file(self) -> AddFileOp:
        path = self._path_from_header("*** Add File: ")
        self._index += 1
        lines: list[str] = []
        while self._index < len(self._lines):
            line = self._lines[self._index]
            if line == "*** End Patch" or is_file_operation_header(line):
                break
            if not line.startswith("+"):
                raise ApplyPatchError(
                    f"Add file content lines must start with '+': {line}"
                )
            lines.append(line[1:])
            self._index += 1
        if not lines:
            raise ApplyPatchError(f"Add file operation requires content: {path}")
        return AddFileOp(path=path, lines=tuple(lines))

    def _parse_delete_file(self) -> DeleteFileOp:
        path = self._path_from_header("*** Delete File: ")
        self._index += 1
        return DeleteFileOp(path=path)

    def _parse_update_file(self) -> UpdateFileOp:
        path = self._path_from_header("*** Update File: ")
        self._index += 1
        move_to = None
        if self._index < len(self._lines) and self._lines[self._index].startswith(
            "*** Move to: "
        ):
            move_to = self._lines[self._index][len("*** Move to: ") :].strip()
            if not move_to:
                raise ApplyPatchError("Move target must be a non-empty path")
            self._index += 1
        chunks: list[PatchChunk] = []
        while self._index < len(self._lines):
            line = self._lines[self._index]
            if line == "*** End Patch" or is_file_operation_header(line):
                break
            if not line.startswith("@@"):
                raise ApplyPatchError(
                    f"Expected hunk header for update file {path}: {line}"
                )
            chunks.append(self._parse_chunk())
        if not chunks:
            raise ApplyPatchError(
                f"Update file operation requires at least one hunk: {path}"
            )
        return UpdateFileOp(path=path, move_to=move_to, chunks=tuple(chunks))

    def _parse_chunk(self) -> PatchChunk:
        header = self._lines[self._index]
        context = header[2:].strip() or None
        self._index += 1
        old_lines: list[str] = []
        new_lines: list[str] = []
        saw_body = False
        end_of_file = False
        while self._index < len(self._lines):
            line = self._lines[self._index]
            if line == "*** End of File":
                end_of_file = True
                self._index += 1
                break
            if (
                line.startswith("@@")
                or line == "*** End Patch"
                or is_file_operation_header(line)
            ):
                break
            if not line or line[0] not in {" ", "+", "-"}:
                raise ApplyPatchError(f"Invalid hunk line: {line}")
            text = line[1:]
            if line[0] != "+":
                old_lines.append(text)
            if line[0] != "-":
                new_lines.append(text)
            saw_body = True
            self._index += 1
        if not saw_body:
            raise ApplyPatchError("Update hunks must contain at least one body line")
        return PatchChunk(
            context=context,
            old_lines=tuple(old_lines),
            new_lines=tuple(new_lines),
            end_of_file=end_of_file,
        )

    def _path_from_header(self, prefix: str) -> str:
        line = self._lines[self._index]
        path = line[len(prefix) :].strip()
        if not path:
            raise ApplyPatchError("Patch headers require a non-empty path")
        return path


def normalize_newlines(text: str) -> str:
    """Normalize mixed newline conventions to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def is_file_operation_header(line: str) -> bool:
    """Return whether the line starts a new file operation."""
    return line.startswith(("*** Add File: ", "*** Delete File: ", "*** Update File: "))
