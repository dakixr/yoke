"""An uncertain receipt is replayed by input ID; rejections reach the UI."""

import httpx
import pytest

from tests.yoke.provider_projection.recording import answer

from .interactive_daemon_support import interactive_daemon as interactive_daemon


@pytest.mark.parametrize("mode", ["basic", "prompt"])
def test_interactive_lost_admission_receipt_executes_and_renders_once(
    interactive_daemon, monkeypatch, mode
):
    d = interactive_daemon
    d.tape.expect(answer("receipt answer"))
    request = d.connection.session_request
    attempts = []

    def lose_receipt(method, suffix="", **kwargs):
        result = request(method, suffix, **kwargs)
        if suffix == "/prompt":
            attempts.append(kwargs["json"]["id"])
            if len(attempts) == 1:
                raise httpx.ReadError("Lost admission response")
        return result

    monkeypatch.setattr(d.connection, "session_request", lose_receipt)

    def script(ui):
        ui.send("receipt prompt")
        ui.wait_text("receipt answer")
        ui.idle()

    text = d.run(mode, script, monkeypatch)
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert text.count("receipt prompt") == 1
    assert text.count("receipt answer") == 1
    assert len(d.tape.calls) == 1
