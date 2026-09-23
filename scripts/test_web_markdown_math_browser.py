"""Run with uv run python scripts/test_web_markdown_math_browser.py.

Uses installed Chrome and the packaged browser assets, never live sessions.
"""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from pathlib import Path
from threading import Thread

playwright_api = import_module("playwright.sync_api")
expect = playwright_api.expect
sync_playwright = playwright_api.sync_playwright

ASSETS = Path(__file__).resolve().parents[1] / "src/yoke/web/assets"

FIXTURE = r"""async () => {
  const {markdownHTML, renderMarkdownMath} = await import('/js/session/markdown.js');
  const source = String.raw`Model predictable dilution as a known discrete downward adjustment to the per-share price.

* Let the current share count be \(N_0\). At a known date, the theoretical adjustment is \(S_{t_i^+}=S_{t_i^-}\frac{N_{i-1}}{N_i}\).
* In a risk-neutral simulation, apply the dilution jump \(S_{t_i^+}=S_{t_i^-}(1-d_i)\).

\[
d_i = 1 - \frac{N_{i-1}}{N_i}
\]

A markdown-sensitive formula stays math: \(a*b*c < d\).

$$
P = \frac{E[X]}{(1+r)^T}
$$

    \(literal code stays literal\)

A price like $25 stays plain text.

<img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==" onerror="window.__mathXss = true">`;
  const root = document.querySelector('#fixture');
  const rendered = markdownHTML(source);
  root.innerHTML = rendered;
  renderMarkdownMath(root);
}"""


def main() -> None:
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(ASSETS))
    )
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path="/usr/bin/google-chrome", headless=True
            )
            page = browser.new_page(
                viewport={"width": 1000, "height": 760}, color_scheme="dark"
            )
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.set_content(
                """<!doctype html><html><head>
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <link rel="stylesheet" href="/css/base.css">
                <link rel="stylesheet" href="/vendor/katex/katex.min.css">
                <link rel="stylesheet" href="/css/session.css">
                </head><body><main style="width:min(820px,calc(100% - 40px));margin:40px auto">
                  <div id="fixture" class="markdown turn__body"></div>
                </main></body></html>"""
            )
            page.evaluate(FIXTURE)

            expect(page.locator("#fixture .katex")).to_have_count(6)
            expect(page.locator("#fixture .katex-display")).to_have_count(2)
            expect(page.locator("#fixture pre code")).to_have_text(
                r"\(literal code stays literal\)"
            )
            expect(page.locator("#fixture")).to_contain_text("$25")
            assert page.locator("#fixture img").get_attribute("onerror") is None
            assert page.evaluate("window.__mathXss") is None
            assert not errors, errors

            page.screenshot(path="/tmp/yoke-markdown-math.png", full_page=True)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    print("Markdown math browser checks passed")


if __name__ == "__main__":
    main()
