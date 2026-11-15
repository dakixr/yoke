"""Utilities for truncating content by line count or byte size."""

from __future__ import annotations

from dataclasses import asdict, dataclass

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024
GREP_MAX_LINE_LENGTH = 500


@dataclass(slots=True, frozen=True)
class TruncationResult:
    """Result of a truncation operation with metadata about what was kept."""

    content: str
    truncated: bool
    truncated_by: str | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool
    first_line_exceeds_limit: bool
    max_lines: int
    max_bytes: int

    def to_dict(self) -> dict[str, object]:
        """Return the truncation result as a camelCase dict."""
        payload = asdict(self)
        payload["truncatedBy"] = payload.pop("truncated_by")
        payload["totalLines"] = payload.pop("total_lines")
        payload["totalBytes"] = payload.pop("total_bytes")
        payload["outputLines"] = payload.pop("output_lines")
        payload["outputBytes"] = payload.pop("output_bytes")
        payload["lastLinePartial"] = payload.pop("last_line_partial")
        payload["firstLineExceedsLimit"] = payload.pop("first_line_exceeds_limit")
        payload["maxLines"] = payload.pop("max_lines")
        payload["maxBytes"] = payload.pop("max_bytes")
        return payload

    def to_metadata_dict(self) -> dict[str, object]:
        """Return truncation metadata without repeating the retained content."""
        payload = self.to_dict()
        payload.pop("content", None)
        return payload


def format_size(bytes_count: int) -> str:
    """Format a byte count as a human-readable size string."""
    if bytes_count < 1024:
        return f"{bytes_count}B"
    if bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f}KB"
    return f"{bytes_count / (1024 * 1024):.1f}MB"


def truncate_head(
    content: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """Truncate content from the end, keeping the beginning within limits."""
    total_bytes = len(content.encode("utf-8"))
    lines = content.split("\n")
    total_lines = len(lines)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=content,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=total_lines,
            output_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    if lines and len(lines[0].encode("utf-8")) > max_bytes:
        return TruncationResult(
            content="",
            truncated=True,
            truncated_by="bytes",
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=0,
            output_bytes=0,
            last_line_partial=False,
            first_line_exceeds_limit=True,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    kept_lines: list[str] = []
    used_bytes = 0
    truncated_by = "lines"
    for index, line in enumerate(lines[:max_lines]):
        line_bytes = len(line.encode("utf-8")) + (1 if index > 0 else 0)
        if used_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        kept_lines.append(line)
        used_bytes += line_bytes

    output = "\n".join(kept_lines)
    return TruncationResult(
        content=output,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(kept_lines),
        output_bytes=len(output.encode("utf-8")),
        last_line_partial=False,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_line(
    line: str, *, max_chars: int = GREP_MAX_LINE_LENGTH
) -> tuple[str, bool]:
    """Truncate a single line to max_chars, returning the line and a flag."""
    if len(line) <= max_chars:
        return line, False
    return line[: max_chars - 14].rstrip() + " [truncated]", True
