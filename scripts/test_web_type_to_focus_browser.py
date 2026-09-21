"""Run with uv run python scripts/test_web_type_to_focus_browser.py.

Drives composer type-to-focus in Chrome against a fixture that mounts the hook
beside the surfaces it must not steal from.
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

FIXTURE = """async () => {
  const {html, render, useRef, useState} = await import('/vendor/htm-preact.js');
  const {useTypeToFocus} = await import('/js/session/composer/type-to-focus.js');
  function Harness() {
    const [text, setText] = useState('');
    const [dialogOpen, setDialogOpen] = useState(false);
    const [enabled, setEnabled] = useState(true);
    const promptInput = useRef(null);
    useTypeToFocus({
      enabled,
      inputRef: promptInput,
      appendText: (chunk) => setText((current) => `${current}${chunk}`),
    });
    window.harness = {
      openDialog: () => setDialogOpen(true),
      closeDialog: () => setDialogOpen(false),
      disable: () => setEnabled(false),
      enable: () => setEnabled(true),
    };
    return html`<div id="harness" data-enabled=${String(enabled)}>
      <button id="resting-button" type="button">Change</button>
      <input id="other-field" aria-label="Other field" />
      <div id="transcript" tabindex="-1">transcript</div>
      <textarea ref=${promptInput} id="prompt" aria-label="Prompt"
        value=${text} onInput=${(event) => setText(event.currentTarget.value)}></textarea>
      ${dialogOpen ? html`<div class="modal-backdrop">
        <div role="dialog" aria-modal="true" aria-label="Overlay"><button type="button">In dialog</button></div>
      </div>` : null}
    </div>`;
  }
  window.mount = () => {
    render(null, document.body);
    document.body.innerHTML = '';
    render(html`<${Harness}/>`, document.body);
  };
  window.mount();
}"""


class QuietHandler(SimpleHTTPRequestHandler):
    """Serve the fixture assets without logging one line per module."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002, D102
        return


def prompt(page):
    return page.locator("#prompt")


def focus_elsewhere(page):
    page.locator("#transcript").click()
    assert page.evaluate("document.activeElement.id") != "prompt"


def expect_composer_focused(page):
    page.wait_for_function("document.activeElement.id === 'prompt'", timeout=5000)


def check_typing_outside_the_composer_writes_into_it(page):
    focus_elsewhere(page)
    page.keyboard.press("h")
    expect(prompt(page)).to_have_value("h")
    expect_composer_focused(page)
    # Once focused the composer receives the rest directly, spaces included.
    page.keyboard.type("ello world")
    expect(prompt(page)).to_have_value("hello world")


def check_a_resting_button_does_not_swallow_the_keystroke(page):
    page.locator("#resting-button").focus()
    assert page.evaluate("document.activeElement.id") == "resting-button"
    page.keyboard.press("y")
    expect(prompt(page)).to_have_value("y")


def check_space_and_enter_stay_with_the_page(page):
    focus_elsewhere(page)
    page.keyboard.press("Space")
    page.keyboard.press("Enter")
    expect(prompt(page)).to_have_value("")


def check_shortcuts_are_not_captured(page):
    focus_elsewhere(page)
    page.keyboard.press("ControlOrMeta+k")
    page.keyboard.press("ControlOrMeta+b")
    page.keyboard.press("Alt+n")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Escape")
    expect(prompt(page)).to_have_value("")


def check_another_field_keeps_its_own_typing(page):
    page.locator("#other-field").click()
    page.keyboard.type("abc")
    expect(page.locator("#other-field")).to_have_value("abc")
    expect(prompt(page)).to_have_value("")


def check_an_open_dialog_keeps_the_keyboard(page):
    focus_elsewhere(page)
    page.evaluate("window.harness.openDialog()")
    expect(page.locator(".modal-backdrop")).to_be_visible()
    page.keyboard.press("z")
    expect(prompt(page)).to_have_value("")
    page.evaluate("window.harness.closeDialog()")
    expect(page.locator(".modal-backdrop")).to_have_count(0)
    focus_elsewhere(page)
    page.keyboard.press("z")
    expect(prompt(page)).to_have_value("z")


def check_pasted_text_lands_in_the_composer(page):
    focus_elsewhere(page)
    page.evaluate(
        """() => {
          const data = new DataTransfer();
          data.setData('text/plain', 'pasted note');
          document.getElementById('transcript').dispatchEvent(
            new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true}),
          );
        }"""
    )
    expect(prompt(page)).to_have_value("pasted note")


def check_a_disabled_composer_is_not_written_to(page):
    focus_elsewhere(page)
    page.evaluate("window.harness.disable()")
    expect(page.locator("#harness")).to_have_attribute("data-enabled", "false")
    page.keyboard.press("q")
    expect(prompt(page)).to_have_value("")
    page.evaluate("window.harness.enable()")
    expect(page.locator("#harness")).to_have_attribute("data-enabled", "true")
    focus_elsewhere(page)
    page.keyboard.press("q")
    expect(prompt(page)).to_have_value("q")


def main():
    handler = partial(QuietHandler, directory=str(ASSETS))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Thread(target=server.serve_forever, daemon=True).start()
    checks = [
        check_typing_outside_the_composer_writes_into_it,
        check_a_resting_button_does_not_swallow_the_keystroke,
        check_space_and_enter_stay_with_the_page,
        check_shortcuts_are_not_captured,
        check_another_field_keeps_its_own_typing,
        check_an_open_dialog_keeps_the_keyboard,
        check_pasted_text_lands_in_the_composer,
        check_a_disabled_composer_is_not_written_to,
    ]
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path="/usr/bin/google-chrome", headless=True
            )
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.set_content(
                """<script type="importmap">{"imports":{"preact":"/vendor/preact.module.js","preact/hooks":"/vendor/hooks.module.js"}}</script>"""
            )
            page.evaluate(FIXTURE)
            for check in checks:
                page.evaluate("window.mount()")
                check(page)
                print(f"PASS {check.__name__.removeprefix('check_').replace('_', ' ')}")
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    print("Type-to-focus browser checks passed")


if __name__ == "__main__":
    main()
