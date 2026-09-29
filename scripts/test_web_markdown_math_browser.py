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

The quadratic roots are $x=\frac{-b\pm\sqrt{b^2-4ac}}{2a}$.

Euler's identity is $e^{i\pi}+1=0$.

    \(literal code stays literal\)

Prices stay literal: $25 and $30, or $5-$10. So do \$x\$ and <code>$x$</code>.

<img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==" onerror="window.__mathXss = true">`;
  const root = document.querySelector('#fixture');
  const rendered = markdownHTML(source);
  root.innerHTML = rendered;
  renderMarkdownMath(root);
}"""

REGRESSIONS = r"""async () => {
  const {markdownHTML, renderMarkdownMath} = await import('/js/session/markdown.js');
  const check = (ok, message) => { if (!ok) throw Error(message); };
  const render = source => {
    const root = document.createElement('div');
    root.innerHTML = markdownHTML(source);
    renderMarkdownMath(root);
    return root;
  };
  const examples = [
    [String.raw`$x=\frac{1}{2}$`, [String.raw`x=\frac{1}{2}`]],
    ['$25 and $30, then $x$.', ['x']],
    ['$5-$10 and $2+2=4$', ['2+2=4']],
    [String.raw`\$x$ and $y$`, ['y']],
    [String.raw`\$x\$`, []],
    ['&#36;x&#36;', []],
    ['`$x$` and $y$', ['y']],
    ['```math\n$x$\n```\n\n$y$', ['y']],
    ['```\n$unclosed\n```\n\n$y$', ['y']],
    ['$unclosed `code$` and $y$', ['y']],
    ['$25 and <code>$x$</code>, then $y$', ['y']],
    ['$25 [price](https://example.test/$x$), then $y$', ['y']],
    ['<code>$x$</code>', []],
    ['<pre>$x$</pre>', []],
    ['[price](https://example.test/$x$)', []],
    ['[$x$](https://example.test/$y$)', ['x']],
    ['**$a*b*c < d$**', ['a*b*c < d']],
    [String.raw`$$x$$, \(y\), \[z\] and $w$`, ['x', 'y', 'z', 'w']],
    [String.raw`$\text{cost: \$25}$`, [String.raw`\text{cost: \$25}`]],
    [String.raw`$\text{<img src=x onerror="alert(1)">}$`, [String.raw`\text{<img src=x onerror="alert(1)">}`]],
  ];
  for (const [source, expected] of examples) {
    const root = render(source);
    const actual = [...root.querySelectorAll('annotation')].map(node => node.textContent);
    check(JSON.stringify(actual) === JSON.stringify(expected), `Math changed: ${source}: ${JSON.stringify(actual)}`);
    const first = root.innerHTML;
    renderMarkdownMath(root);
    check(root.innerHTML === first, `Rendering twice changed: ${source}`);
    check(!root.querySelector('img[onerror], script'), `Unsafe HTML: ${source}`);
  }
  for (const source of [String.raw`$\unknownCommand{x}$`, String.raw`\[\unknownCommand{x}\]`, '$x + y', '$x $', '$x$2']) {
    const root = render(source);
    check(!root.querySelector('.katex'), `Invalid input rendered: ${source}`);
    check(root.textContent.trim() === source, `Invalid input lost text: ${source}`);
  }
  const escaped = render(String.raw`\$x\$ and &#36;y&#36;`);
  check(escaped.textContent.trim() === '$x$ and $y$', 'Escaped dollars did not stay literal');
  const code = render('```math\n$x$\n```');
  check(code.querySelector('code').textContent === '$x$\n', 'Code changed');
  const unsafe = render(String.raw`$x$ <img src=x onerror="alert(1)"><style>body{display:none}</style><iframe src=x></iframe> $\href{javascript:alert(1)}{click}$ $\htmlStyle{position:fixed}{x}$`);
  check(!unsafe.querySelector('script, style, iframe, img[onerror], a[href]'), 'Untrusted content gained privileges');
  return examples.length + 9;
}"""

TIMELINE = """async () => {
  const {html, render} = await import('/vendor/htm-preact.js');
  const {Timeline} = await import('/js/session/timeline.js');
  const root = document.createElement('div');
  root.id = 'timeline-fixture';
  root.style.height = '640px';
  document.body.append(root);
  window.showMathTimeline = async (text, saved = false) => {
    const data = saved
      ? {loaded: true, messages: [{id: 'saved', type: 'assistant', phase: 'final', content: [{type: 'text', text}]}]}
      : {loaded: true, messages: [], liveAssistants: {live: {id: 'live', phase: 'final', content: text}}};
    render(html`<${Timeline} sessionID="math-qa" data=${data} runtime=${{state: 'idle'}}/>`, root);
    await new Promise(requestAnimationFrame);
  };
}"""


def check_timeline(page) -> None:
    page.evaluate(TIMELINE)
    formula = r"The roots are $x=\frac{-b\pm\sqrt{b^2-4ac}}{2a}$."
    page.evaluate(
        """async source => {
          for (let length = 1; length <= source.length; length++) {
            await showMathTimeline(source.slice(0, length));
            const actual = document.querySelectorAll('#timeline-fixture .katex').length;
            const expected = length > source.lastIndexOf('$') ? 1 : 0;
            if (actual !== expected) throw Error(`Streaming prefix ${length}: ${actual} math nodes`);
          }
        }""",
        formula,
    )
    for saved in (False, True, True):
        page.evaluate(
            "([text, saved]) => showMathTimeline(text, saved)", [formula, saved]
        )
        expect(page.locator("#timeline-fixture .katex")).to_have_count(1)
        expect(page.locator("#timeline-fixture .katex-display")).to_have_count(0)
    mixed = r"Cost $25 and $30; literal \$x\$; formula $y^2$."
    page.evaluate("text => showMathTimeline(text, true)", mixed)
    expect(page.locator("#timeline-fixture annotation")).to_have_text("y^2")
    expect(page.locator("#timeline-fixture .markdown")).to_contain_text(
        "Cost $25 and $30"
    )
    expect(page.locator("#timeline-fixture .markdown")).to_contain_text("literal $x$")
    page.evaluate("document.querySelector('#timeline-fixture').remove()")


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
            errors: list[str] = []
            for name, width, height in [("desktop", 1000, 760), ("mobile", 390, 844)]:
                page = browser.new_page(
                    viewport={"width": width, "height": height}, color_scheme="dark"
                )
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                page.set_content(
                    """<!doctype html><html><head>
                    <meta name="viewport" content="width=device-width, initial-scale=1">
                    <script type="importmap">{"imports":{"preact":"/vendor/preact.module.js","preact/hooks":"/vendor/hooks.module.js"}}</script>
                    <link rel="stylesheet" href="/css/base.css">
                    <link rel="stylesheet" href="/vendor/katex/katex.min.css">
                    <link rel="stylesheet" href="/css/session.css">
                    </head><body><main style="width:min(820px,calc(100% - 32px));margin:32px auto">
                      <div id="fixture" class="markdown turn__body"></div>
                    </main></body></html>"""
                )
                page.evaluate(FIXTURE)

                expect(page.locator("#fixture .katex")).to_have_count(8)
                expect(page.locator("#fixture .katex-display")).to_have_count(2)
                expect(page.locator("#fixture pre code")).to_have_text(
                    r"\(literal code stays literal\)"
                )
                expect(page.locator("#fixture")).to_contain_text("$25 and $30")
                expect(page.locator("#fixture")).to_contain_text("$5-$10")
                expect(page.locator("#fixture")).to_contain_text("$x$")
                assert page.locator("#fixture img").get_attribute("onerror") is None
                assert page.evaluate("window.__mathXss") is None
                assert page.evaluate(REGRESSIONS) == 29
                check_timeline(page)
                page.evaluate("document.fonts.ready")
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                page.screenshot(
                    path=f"/tmp/yoke-inline-math-{name}.png", full_page=True
                )
                page.close()
                print(
                    f"PASS {name}: math, currency, code, sanitization, streaming, saved turns"
                )
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    print("Markdown math browser checks passed")


if __name__ == "__main__":
    main()
