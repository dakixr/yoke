"""HTML styling helpers for the interactive tool inspector."""

from __future__ import annotations

from html import escape

DETAIL_DIM_OPEN = '<style fg="#777777">'
DETAIL_DIM_CLOSE = "</style>"


def fit_cell(
    text: str,
    width: int,
    *,
    html: bool,
    trusted_markup: bool = False,
) -> str:
    """Fit and style a cell for either plain text or prompt-toolkit HTML."""
    if html and trusted_markup and text:
        return text
    fitted = fit(text, width)
    if html:
        return _style_detail_line(fitted)
    return fitted


def _style_detail_line(text: str) -> str:
    body = text.rstrip(" ")
    padding = text[len(body) :]
    if body.startswith("╭─"):
        return f"<ansicyan>{escape_html(body)}</ansicyan>{padding}"
    if body.startswith("[STDERR]") or body.startswith("[ERROR]"):
        return f"<ansired><b>{escape_html(body)}</b></ansired>{padding}"
    if body.startswith("[STDOUT]") or body.startswith("[CONTENT]"):
        return f"<ansigreen><b>{escape_html(body)}</b></ansigreen>{padding}"
    if body.startswith("[META]"):
        return f"{DETAIL_DIM_OPEN}<b>{escape_html(body)}</b>{DETAIL_DIM_CLOSE}{padding}"
    if _looks_like_numbered_line(body):
        return _style_numbered_line(body, padding)
    if " │" in body:
        return _style_key_value_line(body, padding)
    if body.startswith(('"', "{", "}", "[", "]")):
        return f"{DETAIL_DIM_OPEN}{escape_html(body)}{DETAIL_DIM_CLOSE}{padding}"
    return _style_status_symbols(body, padding)


def _looks_like_numbered_line(text: str) -> bool:
    prefix, separator, _ = text.partition("│")
    return bool(separator) and prefix.strip().isdigit()


def _style_numbered_line(body: str, padding: str) -> str:
    number, _, value = body.partition("│")
    value = value[1:] if value.startswith(" ") else value
    return (
        f"{DETAIL_DIM_OPEN}{escape_html(number)} │{DETAIL_DIM_CLOSE} "
        f"{_style_status_symbols(value, '')}{padding}"
    )


def _style_key_value_line(body: str, padding: str) -> str:
    key, _, value = body.partition(" │")
    return (
        f"<ansicyan>{escape_html(key.rstrip())}</ansicyan>"
        f"{DETAIL_DIM_OPEN} │{DETAIL_DIM_CLOSE}"
        f"{escape_html(value)}{padding}"
    )


def _style_status_symbols(body: str, padding: str) -> str:
    styled = escape_html(body)
    styled = styled.replace("✓", "<ansigreen>✓</ansigreen>")
    styled = styled.replace("✗", "<ansired>✗</ansired>")
    styled = styled.replace("…", "<ansiyellow>…</ansiyellow>")
    return f"{styled}{padding}"


def escape_line(text: str, html: bool) -> str:
    """Escape a line when rendering prompt-toolkit HTML."""
    return escape_html(text) if html else text


def pane_label(label: str, width: int, *, active: bool) -> str:
    """Return a styled pane header label."""
    padded = fit(f" {label} ", width)
    if active:
        return f"<reverse><ansicyan>{escape_html(padded)}</ansicyan></reverse>"
    return f"<ansibrightblack>{escape_html(padded)}</ansibrightblack>"


def escape_html(text: str) -> str:
    """Escape text after replacing characters rejected by the HTML parser."""
    return escape("".join(_xml_safe_character(char) for char in text))


def _xml_safe_character(char: str) -> str:
    value = ord(char)
    if value <= 0x1F:
        return chr(0x2400 + value)
    if value == 0x7F:
        return "␡"
    if 0x80 <= value <= 0x9F:
        return f"\\x{value:02x}"
    if (
        0x20 <= value <= 0xD7FF
        or 0xE000 <= value <= 0xFFFD
        or 0x10000 <= value <= 0x10FFFF
    ):
        return char
    return "\N{REPLACEMENT CHARACTER}"


def fit(text: str, width: int) -> str:
    """Pad or truncate text to a fixed terminal cell width."""
    if len(text) <= width:
        return text.ljust(width)
    if width <= 1:
        return text[:width]
    return f"{text[: width - 1]}…"
