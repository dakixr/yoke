"""Verify prompt completion menus stay above both composer variants."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from pathlib import Path
from threading import Thread


playwright_api = import_module("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

ASSETS = Path(__file__).resolve().parents[1] / "src/yoke/web/assets"


class QuietHandler(SimpleHTTPRequestHandler):
    """Serve web assets without request logs."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002, D102
        return


def assert_menu_above_composer(page, wrapper: str) -> None:
    menu = page.locator(f"{wrapper} .slash-menu").bounding_box()
    composer = page.locator(f"{wrapper} .composer-shell").bounding_box()
    assert menu is not None
    assert composer is not None
    assert menu["y"] + menu["height"] <= composer["y"] - 5, (menu, composer)


def main() -> None:
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(QuietHandler, directory=str(ASSETS)),
    )
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path="/usr/bin/google-chrome",
                headless=True,
            )
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.set_content(
                """
                <link rel="stylesheet" href="/css/base.css">
                <link rel="stylesheet" href="/css/session.css">
                <div style="height: 360px"></div>
                <div id="chat" class="composer-shell-wrap">
                  <div class="slash-menu" style="height: 120px"></div>
                  <div class="composer-shell" style="height: 120px"></div>
                </div>
                <div style="height: 260px"></div>
                <div class="draft-composer-wrap" style="margin: 0; padding: 0; width: 720px">
                  <div id="draft" class="composer-shell-wrap">
                    <div class="slash-menu" style="height: 120px"></div>
                    <div class="composer-shell composer-shell--draft" style="height: 180px"></div>
                  </div>
                </div>
                """
            )

            assert_menu_above_composer(page, "#chat")
            assert_menu_above_composer(page, "#draft")
            print("PASS saved-session completion menu opens above composer")
            print("PASS new-session completion menu opens above composer")
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
