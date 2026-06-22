"""Formatting helpers for tool inspector details."""

from __future__ import annotations

import json

TEXT_KEYS: tuple[str, ...] = ("input", "stdout", "stderr", "output", "content", "error")
ERROR_KEYS: tuple[str, ...] = ("stderr", "error")


def format_arguments(raw_arguments: str | None) -> str:
    """Format raw tool-call arguments as readable sections."""
    if not raw_arguments:
        return "(empty)"
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return raw_arguments
    if isinstance(parsed, dict):
        return _format_tool_arguments(parsed)
    return pretty_json(parsed)


def format_result(result: dict[str, object] | None) -> str:
    """Format a tool result as readable sections."""
    if result is None:
        return "(pending)"
    return _format_mapping(result)


def pretty_json(value: object) -> str:
    """Return stable, readable JSON for non-text values."""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def section_header(title: str) -> str:
    """Return a visually distinct section header."""
    return f"╭─ {title} " + "─" * max(2, 34 - len(title))


def _format_mapping(mapping: dict[str, object]) -> str:
    text_fields = []
    for key in _ordered_text_keys(mapping):
        value = mapping.get(key)
        if isinstance(value, str) and value:
            text_fields.append(_format_text_block(key, value))
    remaining = {
        key: value for key, value in mapping.items() if key not in set(TEXT_KEYS)
    }
    if remaining:
        text_fields.append(_format_metadata(remaining))
    return "\n\n".join(text_fields) if text_fields else pretty_json(mapping)


def _format_tool_arguments(arguments: dict[str, object]) -> str:
    width = min(max((len(key) for key in arguments), default=0), 24)
    sections = [
        _format_argument_value(key, value, width) for key, value in arguments.items()
    ]
    return "\n\n".join(sections) if sections else "(empty)"


def _format_argument_value(key: str, value: object, width: int) -> str:
    label = _label(key, width)
    if isinstance(value, str):
        decoded = _decode_text_escapes(value)
        if "\n" in decoded:
            return f"{label}\n{_numbered_lines(decoded)}"
        return f"{label} {decoded}"
    if isinstance(value, bool | int | float) or value is None:
        return f"{label} {value}"
    return f"{label}\n{pretty_json(value)}"


def _format_text_block(key: str, value: str) -> str:
    label = key.upper()
    decoded = _decode_text_escapes(value)
    return f"[{label}]\n{_numbered_lines(decoded)}"


def _format_metadata(mapping: dict[str, object]) -> str:
    return f"[META]\n{pretty_json(mapping)}"


def _ordered_text_keys(mapping: dict[str, object]) -> list[str]:
    keys = [key for key in TEXT_KEYS if key in mapping]
    return [key for key in ERROR_KEYS if key in keys] + [
        key for key in keys if key not in ERROR_KEYS
    ]


def _label(key: str, width: int) -> str:
    clipped = f"{key[:23]}…" if len(key) > 24 else key
    return f"{clipped:<{width}} │"


def _numbered_lines(text: str) -> str:
    lines = text.splitlines() or [""]
    width = len(str(len(lines)))
    return "\n".join(
        f"{index:>{width}} │ {line}" for index, line in enumerate(lines, 1)
    )


def _decode_text_escapes(text: str) -> str:
    return text.replace("\\r\\n", "\n").replace("\\n", "\n")
