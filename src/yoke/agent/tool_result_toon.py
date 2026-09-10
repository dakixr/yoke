"""Small TOON 4.1 encoder for model-facing tool result projections."""

from __future__ import annotations

import json
import math
import re


TOON_SPEC_VERSION = "4.1"

_UNQUOTED_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_NUMERIC_LIKE = re.compile(r"^[+-]?[0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?$", re.I)


def toon_table(
    name: str,
    rows: list[dict[str, object]],
    fields: tuple[str, ...],
    *,
    delimiter: str = ",",
) -> str:
    """Encode homogeneous primitive rows as one TOON table."""
    if not rows:
        return f"{_toon_key(name)}: []"
    marker = "" if delimiter == "," else delimiter
    encoded_fields = delimiter.join(_toon_key(field) for field in fields)
    lines = [f"{_toon_key(name)}[{len(rows)}{marker}]{{{encoded_fields}}}:"]
    for row in rows:
        cells = delimiter.join(
            _toon_scalar(row.get(field), delimiter) for field in fields
        )
        lines.append(f"  {cells}")
    return "\n".join(lines)


def toon_array(name: str, values: list[object], *, delimiter: str = ",") -> str:
    """Encode a primitive list as one inline TOON array."""
    if not values:
        return f"{_toon_key(name)}: []"
    marker = "" if delimiter == "," else delimiter
    encoded = delimiter.join(_toon_scalar(value, delimiter) for value in values)
    return f"{_toon_key(name)}[{len(values)}{marker}]: {encoded}"


def toon_field(name: str, value: object) -> str:
    """Encode one primitive TOON field."""
    return f"{_toon_key(name)}: {_toon_scalar(value, ',')}"


def _toon_key(value: str) -> str:
    if _UNQUOTED_KEY.fullmatch(value):
        return value
    return _quote_toon(value)


def _toon_scalar(value: object, delimiter: str) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("TOON projection cannot encode non-finite numbers")
        if value == 0:
            return "0"
        return json.dumps(value, allow_nan=False, separators=(",", ":"))
    if not isinstance(value, str):
        raise TypeError(
            f"TOON projection only supports primitive cells, got {type(value)!r}"
        )
    if _toon_string_needs_quotes(value, delimiter):
        return _quote_toon(value)
    return value


def _toon_string_needs_quotes(value: str, delimiter: str) -> bool:
    if not value:
        return True
    if value[0] in " \t-#" or value[-1] in " \t":
        return True
    if value in {"true", "false", "null"} or _NUMERIC_LIKE.fullmatch(value):
        return True
    if delimiter in value or any(character in value for character in ':"\\[]{}'):
        return True
    return any(ord(character) < 0x20 for character in value)


def _quote_toon(value: str) -> str:
    escaped: list[str] = []
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError("TOON projection cannot encode lone surrogate codepoints")
        if character == "\\":
            escaped.append("\\\\")
        elif character == '"':
            escaped.append('\\"')
        elif character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif character == "\t":
            escaped.append("\\t")
        elif codepoint < 0x20:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(character)
    return '"' + "".join(escaped) + '"'
