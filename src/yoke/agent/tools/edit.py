"""Tool for editing text files with one exact replacement."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import AliasChoices, Field, model_validator

from .base import WorkspaceTool


@dataclass(frozen=True, slots=True)
class DecodedText:
    text: str
    newline: str
    bom: bool


def decode_text(content: bytes) -> DecodedText:
    bom = content.startswith(b"\xef\xbb\xbf")
    text = content[3:].decode("utf-8") if bom else content.decode("utf-8")
    return DecodedText(
        text=text,
        newline="\r\n" if "\r\n" in text else "\n",
        bom=bom,
    )


def encode_text(text: str, *, bom: bool) -> bytes:
    content = text.encode("utf-8")
    return b"\xef\xbb\xbf" + content if bom else content


def convert_line_endings(text: str, newline: str) -> str:
    normalized = text.replace("\r\n", "\n")
    if newline == "\r\n":
        return normalized.replace("\n", "\r\n")
    return normalized


class EditTool(WorkspaceTool):
    """Tool that edits a text file using one exact text replacement."""

    name = "edit"
    description = (
        "Replace exact text in one file. Use oldString/newString for one exact "
        "replacement, or set replaceAll to true to replace every exact match. "
        "Use write to create or overwrite files. Re-read the file and retry with "
        "exact current text if a replacement fails."
    )

    path: str = Field(min_length=1)
    old_string: str = Field(
        min_length=1,
        validation_alias=AliasChoices("oldString", "old_string"),
        serialization_alias="oldString",
    )
    new_string: str = Field(
        validation_alias=AliasChoices("newString", "new_string"),
        serialization_alias="newString",
    )
    replace_all: bool = Field(
        default=False,
        validation_alias=AliasChoices("replaceAll", "replace_all"),
        serialization_alias="replaceAll",
    )

    @model_validator(mode="after")
    def validate_edit(self) -> EditTool:
        """Reject exact no-op replacements."""
        if self.old_string == self.new_string:
            raise ValueError("oldString and newString must differ.")
        return self

    def execute(self) -> dict[str, object]:
        """Apply the edit operation and return the result dict."""
        try:
            path = self._resolve_path(self.path)
            self._ensure_text_file(path)
            source = decode_text(path.read_bytes())
            old_string = convert_line_endings(self.old_string, source.newline)
            new_string = convert_line_endings(self.new_string, source.newline)
            replacements = source.text.count(old_string)
            if replacements == 0:
                return self._error(
                    f"Text not found in {self.path}",
                    path=self.path,
                    match_count=0,
                    suggestion=(
                        "Re-read the file and retry with the exact current text."
                    ),
                )
            if replacements > 1 and not self.replace_all:
                return self._error(
                    f"Text to replace is ambiguous in {self.path}",
                    path=self.path,
                    match_count=replacements,
                    suggestion=(
                        "Provide more surrounding context or set replaceAll "
                        "to edit every exact match."
                    ),
                )

            updated = (
                source.text.replace(old_string, new_string)
                if self.replace_all
                else source.text.replace(old_string, new_string, 1)
            )
            path.write_bytes(encode_text(updated, bom=source.bom))
            return self._success(
                replacements=replacements if self.replace_all else 1,
                bytes_written=len(encode_text(updated, bom=source.bom)),
            )
        except Exception as exc:
            return self._error(str(exc), path=self.path)
