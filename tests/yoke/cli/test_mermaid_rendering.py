"""Tests for Mermaid-aware Rich Markdown rendering."""

from __future__ import annotations

import io
import re

import pytest
from rich.console import Console

from yoke.cli.render import (
    build_console,
    print_agent_output,
    print_scrollback_agent,
)
from yoke.cli.render.markdown import YokeMarkdown

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class _PlainStream(io.StringIO):
    def isatty(self) -> bool:
        return False


def _terminal_render(markdown: str, *, width: int) -> str:
    stream = io.StringIO()
    console = Console(
        file=stream,
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
        highlight=False,
        width=width,
        height=25,
    )
    console.print(YokeMarkdown(markdown))
    return stream.getvalue()


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


def test_mermaid_fence_renders_as_terminal_art(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    output = _terminal_render(
        """```mermaid
flowchart TD
Idea((💡 Idea)) --> Build[⚙️ Build]
Build --> Ship([🚀 Ship])
```
""",
        width=80,
    )

    plain = _plain(output)
    assert "💡 Idea" in plain and "⚙️ Build" in plain and "🚀 Ship" in plain
    assert "▼" in plain
    assert "flowchart TD" not in plain
    assert "\x1b[38;2;137;189;181m" in output


def test_overwide_mermaid_uses_width_aware_source_fallback() -> None:
    output = _plain(
        _terminal_render(
            """```mermaid
flowchart LR
Idea((Idea)) --> Build[Build] --> Test{Tests pass?} --> Ship([Ship])
```
""",
            width=30,
        )
    )

    assert "mermaid: flowchart" in output
    assert "flowchart LR" in output


def test_invalid_mermaid_falls_back_without_raising() -> None:
    output = _plain(
        _terminal_render("```mermaid\nflowchart LR\n! invalid\n```", width=60)
    )
    assert "mermaid: flowchart" in output
    assert "! invalid" in output


def test_partial_flowchart_renders_and_surfaces_parser_warning() -> None:
    output = _plain(
        _terminal_render(
            "```mermaid\nflowchart LR\nA --> B\nnot valid !\n```",
            width=80,
        )
    )
    assert "A" in output and "B" in output
    assert "warning: dropped, expected a link" in output


def test_mermaid_rendering_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("YOKE_MERMAID", "off")
    output = _plain(
        _terminal_render("```mermaid\nflowchart TD\nA --> B\n```", width=80)
    )
    assert "flowchart TD" in output
    assert "▼" not in output


def test_non_mermaid_fences_keep_normal_rich_rendering() -> None:
    output = _plain(_terminal_render("```python\nprint('hello')\n```", width=80))
    assert "print" in output and "hello" in output


def test_non_tty_output_preserves_original_markdown() -> None:
    stream = _PlainStream()
    source = "```mermaid\nflowchart TD\nA --> B\n```"
    print_agent_output(build_console(stream), source)
    assert stream.getvalue() == source + "\n"


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        ("flowchart TD\nA --> B", "▼"),
        ("stateDiagram-v2\n[*] --> Ready", "Ready"),
        ("classDiagram\nAnimal <|-- Duck", "Animal"),
        ("erDiagram\nA ||--o{ B : has", "0..*"),
        ("sequenceDiagram\nAlice->>Bob: hello", "hello"),
        ('pie\n"Done" : 3\n"Open" : 1', "Done"),
        ("mindmap\n root\n  child", "child"),
        ("timeline\n2026 : shipped", "shipped"),
        ('gitGraph\ncommit id: "first"', "first"),
    ],
)
def test_all_nine_advertised_families_render(source: str, needle: str) -> None:
    output = _plain(_terminal_render(f"```mermaid\n{source}\n```", width=100))
    assert needle in output


def test_live_and_replayed_agent_output_share_mermaid_rendering() -> None:
    source = "```mermaid\nflowchart TD\nLive --> Replay\n```"
    outputs: list[str] = []
    for printer in (print_agent_output, print_scrollback_agent):
        stream = io.StringIO()
        console = Console(
            file=stream,
            force_terminal=True,
            width=60,
            height=25,
            no_color=True,
        )
        printer(console, source)
        outputs.append(stream.getvalue())
    assert all("▼" in output for output in outputs)
    assert all("flowchart TD" not in output for output in outputs)
