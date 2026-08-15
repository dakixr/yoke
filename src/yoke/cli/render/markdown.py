"""Rich Markdown rendering with width-aware Mermaid diagrams."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import ClassVar

from grok_mermaid import MermaidArt, render, source_box
from markdown_it.token import Token
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import CodeBlock, Markdown
from rich.text import Text

from yoke.cli.render.theme import ACCENT, AMBER, DIM

_MERMAID_LEXER = "mermaid"
_DISABLED_VALUES = {"0", "false", "no", "off"}
_SPAN_STYLES = {
    "border": f"dim {DIM}",
    "edge": ACCENT,
    "edgeLabel": f"italic {ACCENT}",
    "title": "bold",
    "text": "none",
    "none": "none",
}


def mermaid_rendering_enabled() -> bool:
    """Return whether terminal Mermaid rendering is enabled."""

    value = os.environ.get("YOKE_MERMAID")
    return value is None or value.strip().lower() not in _DISABLED_VALUES


class YokeCodeBlock(CodeBlock):
    """A normal Rich code block with special handling for Mermaid fences."""

    @classmethod
    def create(cls, markdown: Markdown, token: Token) -> YokeCodeBlock:
        node_info = token.info or ""
        lexer_name = node_info.partition(" ")[0]
        return cls(lexer_name or "text", markdown.code_theme)

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        if self.lexer_name.lower() != _MERMAID_LEXER or not mermaid_rendering_enabled():
            yield from super().__rich_console__(console, options)
            return

        source = str(self.text).rstrip()
        available_width = max(1, options.max_width)
        art = render(source)
        if art is None or art.width > available_width:
            art = source_box(source, available_width)
        yield _styled_mermaid(art)
        yield from _warning_lines(art)


class YokeMarkdown(Markdown):
    """Yoke's Markdown presentation, including Mermaid fenced blocks."""

    elements: ClassVar = {**Markdown.elements, "fence": YokeCodeBlock}


def _styled_mermaid(art: MermaidArt) -> Text:
    output = Text(no_wrap=True, overflow="crop")
    for row_index, row in enumerate(art.styled):
        if row_index:
            output.append("\n")
        for span in row:
            output.append(span.text, style=_SPAN_STYLES[span.cls])
    return output


def _warning_lines(art: MermaidArt) -> Iterator[Text]:
    for warning in art.warnings:
        yield Text(f"warning: {warning}", style=f"dim {AMBER}")
