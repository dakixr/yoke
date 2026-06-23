# ruff: noqa: D100, D103, S101

from __future__ import annotations

from contextlib import contextmanager


def test_list_selector_uses_fullscreen_alternate_buffer(monkeypatch) -> None:
    from yoke.cli.runtime.selector import ui

    captured: dict[str, object] = {}

    class FakeApplication:
        def __init__(self, *, full_screen: bool, **kwargs: object) -> None:
            captured["full_screen"] = full_screen
            captured["kwargs"] = kwargs

        def run(self) -> str:
            captured["ran"] = True
            return "alpha"

    @contextmanager
    def fake_suppress_terminal_output_for_fullscreen():
        captured["suppressed"] = True
        yield

    monkeypatch.setattr(
        "prompt_toolkit.application.Application",
        FakeApplication,
    )
    monkeypatch.setattr(
        ui,
        "suppress_terminal_output_for_fullscreen",
        fake_suppress_terminal_output_for_fullscreen,
    )

    result = ui.select_list_item_interactive(
        ["alpha"],
        title="Choose",
        render_item=lambda item, _index, _selected, _width: item,
        footer="enter select",
    )

    assert result == "alpha"
    assert captured["full_screen"] is True
    assert captured["suppressed"] is True
    assert captured["ran"] is True
