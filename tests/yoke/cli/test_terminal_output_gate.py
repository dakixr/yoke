from __future__ import annotations

from yoke.cli.runtime.terminal_output_gate import TerminalOutputGate


def test_terminal_output_gate_defers_until_fullscreen_exits() -> None:
    events: list[str] = []
    gate = TerminalOutputGate()

    with gate.suppressing():
        assert gate.active is True
        assert gate.defer(lambda: events.append("first")) is True
        events.append("during")
        assert gate.defer(lambda: events.append("second")) is True

    assert gate.active is False
    assert events == ["during", "first", "second"]


def test_terminal_output_gate_nested_fullscreen_flushes_once() -> None:
    events: list[str] = []
    gate = TerminalOutputGate()

    with gate.suppressing():
        assert gate.defer(lambda: events.append("outer")) is True
        with gate.suppressing():
            assert gate.defer(lambda: events.append("inner")) is True
        assert events == []
    assert events == ["outer", "inner"]


def test_terminal_output_gate_does_not_defer_when_inactive() -> None:
    events: list[str] = []
    gate = TerminalOutputGate()

    assert gate.defer(lambda: events.append("never")) is False
    assert events == []
