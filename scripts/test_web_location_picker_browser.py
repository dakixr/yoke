"""Run with uv run python scripts/test_web_location_picker_browser.py.

Drives the working location palette in Chrome against an in-browser filesystem
fixture, never a live daemon or the real filesystem.
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
  const {html, render, useState} = await import('/vendor/htm-preact.js');
  const {LocationPicker} = await import('/js/session/location-picker.js');
  const {api} = await import('/js/api/client.js');
  const tree = {
    '/': ['home'],
    '/home': ['dev'],
    '/home/dev': ['.cache', 'site-builder', 'yoke', 'yoke-notes'],
    '/home/dev/yoke': ['docs', 'src'],
    '/home/dev/yoke/src': [],
    '/home/dev/yoke/docs': [],
    '/home/dev/yoke-notes': [],
    '/home/dev/site-builder': [],
    '/home/dev/.cache': [],
  };
  window.audit = {chosen: [], browses: [], gate: null};
  api.browseLocations = async (path) => {
    audit.browses.push(path);
    if (audit.gate) await audit.gate;
    const expanded = path.startsWith('~') ? `/home${path.slice(1)}` : path;
    const directory = expanded.length > 1 ? expanded.replace(/\\/+$/, '') : expanded;
    if (!Object.hasOwn(tree, directory)) {
      throw new Error('The parent directory for this location was not found.');
    }
    return {data: {
      browseDirectory: directory,
      entries: tree[directory].map((name) => ({
        name,
        directory: directory === '/' ? `/${name}` : `${directory}/${name}`,
      })),
    }};
  };
  function Harness() {
    const [value, setValue] = useState('/home/dev/yoke');
    return html`<div style="padding:40px;width:620px">
      <${LocationPicker}
        value=${value}
        recentLocations=${[
          {directory: '/home/dev/yoke'},
          {directory: '/home/dev/yoke-notes'},
          {directory: '/home/dev/site-builder'},
        ]}
        onChange=${(directory) => { audit.chosen.push(directory); setValue(directory); }}
      />
    </div>`;
  }
  window.mount = () => {
    render(null, document.body);
    document.body.innerHTML = '';
    audit.chosen = [];
    audit.browses = [];
    audit.gate = null;
    render(html`<${Harness}/>`, document.body);
  };
  window.mount();
}"""


class QuietHandler(SimpleHTTPRequestHandler):
    """Serve the fixture assets without logging one line per module."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002, D102
        return


def rows(page):
    return page.locator(".location-palette__row")


def active_row(page):
    return page.locator(".location-palette__row.is-active")


def palette(page):
    return page.locator(".location-palette")


def footer(page):
    return page.locator(".command-footer")


def action_positions(page):
    cancel = page.get_by_role("button", name="Cancel").bounding_box()
    select = page.locator(".location-palette__actions .primary").bounding_box()
    assert cancel is not None and select is not None
    return cancel["x"], select["x"]


def open_palette(page):
    page.locator("#draft-working-location").click()
    expect(palette(page)).to_be_visible()


def type_query(page, text):
    """Replace the palette query with real keystrokes, as a person would."""
    field = page.get_by_role("textbox", name="Working location")
    field.press("ControlOrMeta+a")
    field.press_sequentially(text, delay=10)
    expect(field).to_have_value(text)


def reset(page):
    """Remount so each check starts from one committed location and no history."""
    page.evaluate("window.mount()")


def check_the_trigger_shows_the_committed_location(page):
    trigger = page.locator("#draft-working-location")
    expect(trigger).to_contain_text("yoke")
    expect(trigger).to_contain_text("/home/dev/yoke")
    expect(palette(page)).to_have_count(0)


def check_the_palette_opens_in_the_home_directory(page):
    open_palette(page)
    # Reaching the filesystem must not cost a typed "~/" first.
    expect(page.get_by_role("textbox", name="Working location")).to_have_value("~/")
    expect(page.locator(".command-group__label")).to_have_text("Directories")
    expect(rows(page)).to_have_count(2)
    expect(rows(page).nth(1)).to_contain_text("dev")
    assert page.evaluate("audit.browses") == ["~/"]


def check_the_parent_row_uses_the_resolved_path(page):
    open_palette(page)
    expect(rows(page)).to_have_count(2)
    # "~/" is its own parent as plain text, so the row must come from the
    # directory the server resolved instead of stranding the user on itself.
    expect(rows(page).nth(0)).to_contain_text("..")
    expect(rows(page).nth(0)).not_to_contain_text("~")
    page.get_by_role("button", name="Go to parent folder").click()
    expect(page.get_by_role("textbox", name="Working location")).to_have_value("/")


def check_clearing_the_field_searches_recent_projects(page):
    open_palette(page)
    field = page.get_by_role("textbox", name="Working location")
    field.press("ControlOrMeta+a")
    field.press("Backspace")
    expect(page.locator(".command-group__label")).to_have_text("Recent projects")
    expect(rows(page)).to_have_count(3)


def check_nothing_is_highlighted_until_an_arrow_key(page):
    open_palette(page)
    expect(rows(page)).to_have_count(2)
    expect(active_row(page)).to_have_count(0)
    page.keyboard.press("ArrowDown")
    expect(active_row(page)).to_contain_text("dev")
    page.keyboard.press("ArrowUp")
    expect(active_row(page)).to_have_count(1)


def check_a_project_name_filters_and_enter_uses_it(page):
    open_palette(page)
    type_query(page, "site")
    expect(rows(page)).to_have_count(1)
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(palette(page)).to_have_count(0)
    assert page.evaluate("audit.chosen").pop() == "/home/dev/site-builder"


def check_a_typed_path_is_used_on_enter(page):
    open_palette(page)
    type_query(page, "/home/dev/yoke-notes")
    # Nothing is highlighted, so Enter takes the typed path rather than a row.
    expect(active_row(page)).to_have_count(0)
    page.keyboard.press("Enter")
    expect(palette(page)).to_have_count(0)
    assert page.evaluate("audit.chosen").pop() == "/home/dev/yoke-notes"


def check_enter_on_a_highlighted_folder_selects_it(page):
    open_palette(page)
    type_query(page, "/home/dev/")
    expect(rows(page)).to_have_count(4)
    page.keyboard.press("ArrowDown")
    expect(active_row(page)).to_contain_text("site-builder")
    page.keyboard.press("ArrowDown")
    expect(active_row(page)).to_contain_text("yoke")
    page.keyboard.press("Enter")
    expect(palette(page)).to_have_count(0)
    assert page.evaluate("audit.chosen").pop() == "/home/dev/yoke"


def check_right_arrow_opens_a_highlighted_folder(page):
    open_palette(page)
    type_query(page, "/home/dev/")
    expect(rows(page)).to_have_count(4)
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    expect(active_row(page)).to_contain_text("yoke")
    page.keyboard.press("ArrowRight")
    expect(page.get_by_role("textbox", name="Working location")).to_have_value(
        "/home/dev/yoke/"
    )
    expect(palette(page)).to_be_visible()
    assert page.evaluate("audit.chosen") == []


def check_mouse_can_select_or_open_a_folder(page):
    open_palette(page)
    type_query(page, "/home/dev/")
    page.get_by_role("button", name="Select folder yoke", exact=True).click()
    expect(active_row(page)).to_contain_text("yoke")
    expect(footer(page)).to_contain_text("/home/dev/yoke")
    page.get_by_role("button", name="Select folder", exact=True).click()
    expect(palette(page)).to_have_count(0)
    assert page.evaluate("audit.chosen").pop() == "/home/dev/yoke"

    reset(page)
    open_palette(page)
    type_query(page, "/home/dev/")
    page.get_by_role("button", name="Open folder yoke", exact=True).click()
    expect(page.get_by_role("textbox", name="Working location")).to_have_value(
        "/home/dev/yoke/"
    )
    assert page.evaluate("audit.chosen") == []


def check_the_up_row_navigates_to_the_parent(page):
    open_palette(page)
    type_query(page, "/home/dev/yoke/")
    page.get_by_role("button", name="Go to parent folder").click()
    expect(page.get_by_role("textbox", name="Working location")).to_have_value(
        "/home/dev/"
    )
    expect(rows(page)).to_have_count(4)


def check_a_partial_segment_filters_by_prefix(page):
    open_palette(page)
    type_query(page, "/home/dev/yoke")
    # The parent stays reachable while a leaf segment is being typed, because
    # the listing is still the one for the directory the leaf sits in.
    expect(rows(page)).to_have_count(3)
    expect(rows(page).nth(0)).to_contain_text("..")
    expect(rows(page).nth(1)).to_contain_text("yoke")
    expect(rows(page).nth(2)).to_contain_text("yoke-notes")
    # One listing per directory, not per keystroke.
    assert page.evaluate("audit.browses") == ["/home/dev/"]


def check_hidden_directories_appear_only_on_a_dot(page):
    open_palette(page)
    type_query(page, "/home/dev/")
    expect(rows(page)).to_have_count(4)
    expect(page.get_by_role("button", name="Select folder .cache")).to_have_count(0)
    type_query(page, "/home/dev/.")
    expect(rows(page)).to_have_count(2)
    expect(rows(page).nth(1)).to_contain_text(".cache")


def check_the_footer_exposes_the_selection_action(page):
    open_palette(page)
    initial_positions = action_positions(page)
    expect(page.get_by_role("button", name="Select folder", exact=True)).to_be_enabled()
    field = page.get_by_role("textbox", name="Working location")
    field.press("ControlOrMeta+a")
    field.press("Backspace")
    expect(
        page.get_by_role("button", name="Select project", exact=True)
    ).to_be_disabled()
    assert action_positions(page) == initial_positions
    type_query(page, "/home/dev/")
    expect(page.get_by_role("button", name="Select folder", exact=True)).to_be_enabled()
    expect(rows(page)).to_have_count(4)
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    expect(active_row(page)).to_contain_text("yoke")
    expect(footer(page)).to_contain_text("/home/dev/yoke")
    assert action_positions(page) == initial_positions


def check_escape_closes_without_choosing(page):
    open_palette(page)
    type_query(page, "/home/dev/")
    page.keyboard.press("Escape")
    expect(palette(page)).to_have_count(0)
    assert page.evaluate("audit.chosen") == []
    expect(page.locator("#draft-working-location")).to_contain_text("/home/dev/yoke")


def check_an_unreadable_path_reports_the_failure(page):
    open_palette(page)
    type_query(page, "/nowhere/at/all/")
    expect(page.locator(".command-empty")).to_contain_text(
        "The parent directory for this location was not found."
    )


def check_a_slow_listing_never_blanks_the_panel(page):
    open_palette(page)
    type_query(page, "/home/dev/")
    expect(rows(page)).to_have_count(4)
    page.evaluate(
        "audit.gate = new Promise((resolve) => { audit.release = resolve; }); null"
    )
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    expect(active_row(page)).to_contain_text("yoke")
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(250)
    # The pending listing has not replaced the visible one.
    expect(rows(page)).to_have_count(4)
    page.evaluate("audit.gate = null; audit.release(); null")
    expect(rows(page)).to_have_count(3)


def main():
    handler = partial(QuietHandler, directory=str(ASSETS))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Thread(target=server.serve_forever, daemon=True).start()
    checks = [
        check_the_trigger_shows_the_committed_location,
        check_the_palette_opens_in_the_home_directory,
        check_the_parent_row_uses_the_resolved_path,
        check_clearing_the_field_searches_recent_projects,
        check_nothing_is_highlighted_until_an_arrow_key,
        check_a_project_name_filters_and_enter_uses_it,
        check_a_typed_path_is_used_on_enter,
        check_enter_on_a_highlighted_folder_selects_it,
        check_right_arrow_opens_a_highlighted_folder,
        check_mouse_can_select_or_open_a_folder,
        check_the_up_row_navigates_to_the_parent,
        check_a_partial_segment_filters_by_prefix,
        check_hidden_directories_appear_only_on_a_dot,
        check_the_footer_exposes_the_selection_action,
        check_escape_closes_without_choosing,
        check_an_unreadable_path_reports_the_failure,
        check_a_slow_listing_never_blanks_the_panel,
    ]
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
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.set_content("""<meta name="viewport" content="width=device-width, initial-scale=1"><script type="importmap">{"imports":{"preact":"/vendor/preact.module.js","preact/hooks":"/vendor/hooks.module.js"}}</script>
                <link rel="stylesheet" href="/css/base.css"><link rel="stylesheet" href="/css/layout.css"><link rel="stylesheet" href="/css/session.css">""")
            page.evaluate(FIXTURE)
            for check in checks:
                reset(page)
                check(page)
                print(f"PASS {check.__name__.removeprefix('check_').replace('_', ' ')}")
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    print("Location palette browser checks passed")


if __name__ == "__main__":
    main()
