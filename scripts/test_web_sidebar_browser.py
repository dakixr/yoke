"""Run with uv run --with playwright python scripts/test_web_sidebar_browser.py.

Uses installed Chrome and isolated in-browser fixtures, never live sessions.
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
  const {html, render} = await import('/vendor/htm-preact.js');
  const {Sidebar} = await import('/js/components/sidebar.js');
  const {MainView} = await import('/js/session/session-view.js');
  const {store} = await import('/js/state/store.js');
  const {controller} = await import('/js/state/controller.js');
  const {installSessionSummary} = await import('/js/state/optimistic-projections.js');
  window.audit = {store, controller, patches: []};
  const session = (id, archivedAt) => ({id, title: id, archivedAt,
    location: {directory: '/fixture'}, selection: {}, queue: {total: 0},
    time: {created: '2026-01-01T12:00:00Z'}});
  store.setState(s => ({...s,
    connection: {...s.connection, current: true},
    capabilities: {features: {sessionArchive: true}},
    sessions: {active: session('active', null), settled: session('settled', '2026-02-01T12:00:00Z')},
    sessionOrder: ['active'], archivedOrder: ['settled'], archivedTotal: 1,
    locations: {'/fixture': {name: 'Fixture'}},
    ui: {...s.ui, selectedSessionID: null},
  }));
  controller.patchSession = async (id, patch) => {
    audit.patches.push({id, patch});
    store.setState(s => installSessionSummary(s, {...s.sessions[id],
      archivedAt: patch.archived ? '2026-02-01T12:00:00Z' : null}));
  };
  controller.searchSessions = async query => store.setState(s => ({...s,
    ui: {...s.ui, search: query, searchResults: query ? ['active', 'settled'] : []}}));
  render(html`<div class="app-shell"><${Sidebar}/><${MainView}/></div>`, document.body);
}"""


def main():
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
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="UTC",
            )
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route(
                "**/api/v1/provider?*", lambda route: route.fulfill(json={"data": []})
            )
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.set_content("""<meta name="viewport" content="width=device-width, initial-scale=1"><script type="importmap">{"imports":{"preact":"/vendor/preact.module.js","preact/hooks":"/vendor/hooks.module.js"}}</script>
                <link rel="stylesheet" href="/css/base.css"><link rel="stylesheet" href="/css/layout.css"><link rel="stylesheet" href="/css/session.css">""")
            page.evaluate(FIXTURE)
            chevron = page.locator(".section-toggle__chevron")
            expect(chevron).to_have_attribute("viewBox", "0 0 16 16")
            closed_box = chevron.bounding_box()
            page.get_by_role("button", name="Settled (1)").click()
            page.wait_for_timeout(150)
            assert chevron.bounding_box() == closed_box
            assert (
                chevron.evaluate("el => getComputedStyle(el).transformOrigin")
                == "8px 8px"
            )
            undo = page.get_by_role("button", name="Unsettle settled", exact=True)
            page.locator(".is-settled").hover()
            expect(undo).to_have_attribute("title", "Unsettle session")
            undo.click()
            expect(page.locator(".is-settled")).to_have_count(0)
            page.get_by_role("button", name="Settle settled", exact=True).focus()
            page.get_by_role("button", name="Settle settled", exact=True).press("Enter")
            expect(page.locator(".is-settled")).to_have_count(1)
            page.get_by_role("searchbox").fill("fixture")
            expect(page.locator(".session-settled-label")).to_have_text("Settled")
            expect(
                page.get_by_role("button", name="Settle settled", exact=True)
            ).to_have_count(0)
            expect(undo).to_have_count(1)
            expect(page.locator(".session-settled-label")).to_have_attribute(
                "title", "Settled: 2/1/2026, 12:00:00 PM"
            )
            page.locator(".session-card").filter(has_text="settled").click(
                button="right"
            )
            expect(
                page.get_by_role("menuitem", name="Unsettle session")
            ).to_be_visible()
            page.keyboard.press("Escape")
            page.locator(".session-card").filter(has_text="settled").click()
            expect(page.locator(".session-header .session-settled-label")).to_have_text(
                "· Settled"
            )
            expect(
                page.get_by_role("button", name="Unsettle", exact=True)
            ).to_be_visible()
            page.evaluate(
                "audit.store.setState(s => ({...s, connection: {...s.connection, current: false}}))"
            )
            expect(undo).to_be_disabled()
            page.screenshot(path="/tmp/yoke-sidebar-fixed-search.png")
            assert page.evaluate("audit.patches") == [
                {"id": "settled", "patch": {"archived": False}},
                {"id": "settled", "patch": {"archived": True}},
            ]
            page.evaluate(
                "audit.store.setState(s => ({...s, connection: {...s.connection, current: true}, sessions: {...s.sessions, active: {...s.sessions.active, queue: {total: 1, paused: 1}}}}))"
            )
            expect(
                page.get_by_role("button", name="Settle active", exact=True)
            ).to_have_count(0)
            mobile = browser.new_page(
                viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
            )
            mobile.on("pageerror", lambda error: errors.append(str(error)))
            mobile.route(
                "**/api/v1/provider?*", lambda route: route.fulfill(json={"data": []})
            )
            mobile.goto(f"http://127.0.0.1:{server.server_port}/")
            mobile.set_content(page.locator("head").inner_html())
            mobile.evaluate(FIXTURE)
            mobile.get_by_role("button", name="Settled (1)").tap()
            mobile_undo = mobile.get_by_role(
                "button", name="Unsettle settled", exact=True
            )
            assert mobile_undo.evaluate("el => getComputedStyle(el).opacity") == "1"
            mobile.screenshot(path="/tmp/yoke-sidebar-fixed-mobile.png")
            mobile_undo.tap()
            expect(mobile.locator(".is-settled")).to_have_count(0)
            assert mobile.evaluate("audit.patches[0].patch.archived") is False
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    print("Sidebar browser checks passed")


if __name__ == "__main__":
    main()
