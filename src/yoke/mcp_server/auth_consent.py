"""Consent form rendering and bounded password-attempt throttling."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time


@dataclass
class _Failures:
    count: int = 0
    last_failure: float = 0
    blocked_until: float = 0


class ConsentAttempts:
    """Throttle clients after three failures and all clients after ten.

    Lockouts double from one second to five minutes. Ten minutes without a
    failed attempt clears the counter. Call while holding the provider lock.
    """

    def __init__(self) -> None:
        self._clients: dict[str, _Failures] = {}
        self._global = _Failures()

    def retry_after(self, client_id: str) -> int:
        now = time.monotonic()
        self._clients = {
            key: value
            for key, value in self._clients.items()
            if now - value.last_failure < 600
        }
        if now - self._global.last_failure >= 600:
            self._global = _Failures()
        client = self._clients.get(client_id, _Failures())
        return max(
            0, math.ceil(max(client.blocked_until, self._global.blocked_until) - now)
        )

    def failed(self, client_id: str) -> None:
        now = time.monotonic()
        for state, threshold in (
            (self._clients.setdefault(client_id, _Failures()), 3),
            (self._global, 10),
        ):
            state.count += 1
            state.last_failure = now
            if state.count >= threshold:
                state.blocked_until = now + min(
                    300, 2 ** min(state.count - threshold, 9)
                )

    def succeeded(self, client_id: str) -> None:
        self._clients.pop(client_id, None)


def consent_html(
    client_name: str,
    scopes: str,
    request_id: str,
    *,
    error: bool = False,
) -> str:
    error_html = (
        '<p class="error">That authorization password was not valid.</p>'
        if error
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Authorize Yoke MCP</title><style>
body{{font:16px system-ui;background:#111827;color:#f9fafb;display:grid;place-items:center;min-height:100vh;margin:0}}
main{{width:min(440px,calc(100% - 40px));background:#1f2937;padding:28px;border-radius:16px}}
h1{{margin-top:0}} input,button{{box-sizing:border-box;width:100%;padding:12px;margin-top:12px;border-radius:8px}}
button{{background:#f97316;color:white;border:0;font-weight:700;cursor:pointer}} .muted{{color:#9ca3af}} .error{{color:#fca5a5}}
</style></head><body><main><h1>Authorize Yoke MCP</h1>
<p><strong>{client_name}</strong> is requesting access to <strong>{scopes}</strong>.</p>
<p class="muted">This permits ChatGPT to read, modify, and run commands as the Yoke MCP service account.</p>
{error_html}<form method="post" action="/oauth/consent">
<input type="hidden" name="request_id" value="{request_id}">
<label>Authorization password<input type="password" name="password" required autofocus autocomplete="current-password"></label>
<button type="submit">Authorize ChatGPT</button></form></main></body></html>"""
