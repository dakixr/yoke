# Web UI

This page is for contributors working on the browser application that the
daemon serves. It records the behaviour the app is expected to keep: routes,
composer and keyboard rules, Markdown and math rendering, the sidebar,
optimistic updates, the transcript and workspace recovery, plus how to test
them. The inspector modal has its own page, [web-inspectors.md](web-inspectors.md).
The HTTP API the app uses is in [http.md](http.md).

## Shell and assets

The app is a no-build Preact and HTM application under `src/yoke/web`. The
daemon serves the app shell for `GET /`, `/new`, `/session/<id>` and
`/settings`, and the modules, CSS, vendored libraries and licenses under
`/assets/*`, all with `Cache-Control: no-store`. `/settings` is an alias for
the home view. These routes are public and are not in the OpenAPI schema.
Production needs no Node.js, npm, CDN or separate frontend server.

The app sends the bearer token on every JSON and SSE request. It keeps the
token in session storage, never in local storage. `yoke serve --open` passes
the token in the URL fragment, which the browser does not send to the server.
A `?token=` query is also accepted. The app removes either form from the
address bar and history as soon as it reads it.

## Markdown and math

The app sanitizes assistant Markdown with the bundled DOMPurify before display
and renders TeX with the bundled KaTeX assets, with no network requests.

- `$...$` and `\(...\)` are inline math. `\[...\]` and `$$...$$` are display
  math.
- Single-dollar math stays on one source line and has no whitespace just
  inside either delimiter. Its closing dollar cannot be followed by a digit.
- The first unescaped dollar must be a valid closer, so prices such as
  `$25 and $30` or `$5-$10` do not swallow a later formula. `\$` is a literal
  dollar.
- Code spans, code fences and link destinations stay literal.
- Math is tokenized before Markdown unescapes text, so escaped dollars are not
  reinterpreted after sanitizing.
- Incomplete or invalid formulas stay readable as text.

## Composer

Composer keys follow the interactive CLI:

| Key | Action |
| --- | --- |
| Enter | Send, or steer when a turn is running. |
| Tab | Queue. |
| Shift+Tab | Cycle the model's reasoning effort. |
| Esc Esc | Interrupt. |
| Shift+Enter, Ctrl+J, Esc then Enter | Insert a newline. |
| Ctrl+U | Remove the last pending image. |
| Cmd+Enter (macOS), Ctrl+Enter | From a new-session draft, create and start the session in the background, then open a fresh draft with the same location, provider, model and effort. |

Global keys:

| Key | Action |
| --- | --- |
| Shift+Cmd+O (macOS), Shift+Ctrl+O | New session. |
| Cmd+K, Ctrl+K | Command palette. |
| Cmd+B (macOS), Ctrl+B | Toggle the sessions sidebar. The Shift variant stays with the browser. |
| Alt+Up, Alt+Down | Previous or next session in sidebar order. |

The app does not capture the terminal's Ctrl+X chords. Alt+V is a terminal
key. The browser requires Cmd+V or Ctrl+V for clipboard images. `/shortcuts`
and `?` show the browser shortcut summary.

Typing or pasting while nothing editable has focus writes into the composer and
focuses it, in a saved session and in a new-session draft. Space and Enter are
left alone so they still scroll the transcript and activate the focused
control. Modified keys stay with their shortcuts, and an open dialog keeps the
keyboard.

When image attachments are enabled, the composer accepts images from the file
picker, drag and drop, and paste. The composer grows with its text up to a
compact height. A control at its top right switches to a taller editor, which
changes only the height limit.

Completion menus open above the composer in saved sessions and drafts:

- `/` at the start of an empty prompt opens slash-command completion from
  `GET /api/v1/command`. Keyboard navigation keeps the active option inside the
  menu's own scroll area. `/skill <name> [prompt]` and `/mcp <server>` complete
  from the current workspace catalogs. Supported commands run the matching HTTP
  operation instead of going to the model. Commands that need a saved session
  are disabled in a draft.
- `$` anywhere opens skill completion at the caret. Choosing a skill inserts
  `$skill-name` without sending. A prompt can mention several skills, also with
  images attached. Skills activate when the prompt starts running. The prompt
  API stays plain text. Tokens that cannot name a skill, such as `$HOME` or
  `$5`, open nothing.

Each saved session has its own draft. Text and pending images stay with the
session while the user goes elsewhere, and they survive a reload because they
live in per-tab session storage. A successful admission clears the draft. A
failed admission restores the exact text and images. Clearing a new-session
draft removes its saved text and images without closing the composer.

## Model picker and context ring

Provider and model selection uses one searchable picker, not native selects.
It loads every provider's models, groups them by provider, marks the current
model, and shows context window and thinking metadata. Arrow keys, Home, End,
PageUp and PageDown move the active row and keep it visible. Escape closes the
picker and returns focus to its trigger. Reasoning effort sits next to it as
compact buttons.

Switching to a model with a smaller context first tries automatic compaction.
If the conversation still does not fit, the daemon returns
`model_context_too_small`, and the picker stays open, restores the previous
selection and shows the error inline.

Changing provider, model or effort is a configuration change, not agent
activity. It does not publish a `running` state, so the send controls stay and
the session is not marked Working.

Above the composer, a ring shows the latest model-visible input usage as a
percentage. Its tooltip gives input and maximum tokens, remaining capacity, and
explains that Yoke may compact before the raw limit to leave room for output.
The daemon saves the latest provider-reported usage in session metadata when
the model responds, so the ring survives reloads during long tool runs.
Changing provider or model, forking or checking out another branch clears it.

## Sessions sidebar

The sidebar requests `order=lastUserDesc`, so agent activity, title edits and
model changes do not move a session above one the user touched more recently.
Sessions without a user message fall back to creation time. Pinned sessions come
first, then the rest, each group keeping that order. `Alt+Up` and `Alt+Down`
follow the same order. The list response's `total` drives the counters, and the
app keeps it across refreshes so the settled-session shelf is available at
once.

Status shows current work before past results. `Done` means an unreviewed
completed turn, or a session the user marked unread from its context menu.
Opening the session clears it. Running, stopping, attention, error and pending
queue states replace `Done` at once. Queue counts separate runnable steer and
queued inputs from paused ones. List and single-session refreshes keep a newer
locally known queue revision rather than letting an older summary overwrite the
card.

The UI calls archiving "Settle", and the reverse "Unsettle", in the sidebar,
session header, mobile menu and context menu. Settling is refused while the
session runs or has queued inputs, including paused ones. Settled rows have a
quick Unsettle action that appears on hover and keyboard focus and is always
visible on touch devices. Search results keep a `Settled` label and offer
Unsettle. Settled ages use the settlement time. Sending a prompt to a settled
session reopens it in the same admission. The app shows that at once and
restores the settled state if admission fails.

When the layout collapses the sidebar, fine-pointer devices can reveal it from
the left screen edge after a short hover delay. It stays open while the pointer
is inside and closes after a short grace period. Touch-only devices get no
invisible edge target. The connection dot follows the event stream's state,
including reconnects.

## Working location palette

The new-session draft and the workspace relocation notice share one location
control. The trigger shows the chosen directory and opens a palette whose path
parsing and browsing follow T3 Code's project picker.

The input has focus, and no row is selected until an arrow key or click picks
one. Enter or **Select folder** confirms the selected directory. **Open** or
Right Arrow enters a directory, and Left Arrow goes to its parent. The `..` row
only navigates. Recent projects use the same model with **Select project**.
Escape or **Cancel** closes without choosing.

A query that starts with `/`, `~/`, a drive letter or a UNC prefix browses the
filesystem. Anything else searches recent projects from
`GET /api/v1/location/recent`. A trailing separator lists the whole directory.
Without one, the last segment filters that listing by prefix, and hidden
directories appear only when the segment starts with a dot. Opening a folder
appends its name and a separator.

`GET /api/v1/location/browse` lists one directory, including hidden entries,
and the app filters locally. One listing serves every keystroke in a directory,
and a listing already read is reused when navigating back. The listing limit
must stay above the number of entries a directory can hold, because local
filtering cannot find an entry the server cut off. The app discards stale
browse responses. A draft's location changes only when the user picks a recent
project or an existing folder, so typing a partial path never triggers catalog
requests against an invalid directory.

## Optimistic updates

The app applies a change locally when its result is predictable, then
reconciles with the response or the event stream:

- Prompt admission paints the input at once. The draft moves from the composer
  to the transcript before the response, and the composer locks briefly so the
  text never shows in both places. On failure the row disappears and the
  composer gets the exact text and images back.
- A new session switches into the created session and paints the prompt
  before admission finishes.
- Queue edits keep pending local operations across stale snapshots.
- Title, pin, archive, model, effort, tool, skill and MCP changes apply first
  and roll back on failure.
- Permission and question replies disappear at once and refresh on failure.
- Compaction shows its activity immediately.
- Tree label edits are serialized against revisions while the newest local
  label stays visible.

Generation counters and mutation chains stop older responses or durable events
from undoing a newer click or bringing back a prompt that is already saved.
Operations that need the server's answer show a spinner and block duplicate
submissions instead of pretending they finished. This covers transcript and
tree paging, tree previews and checkout, and manual compaction.

## Loading and the transcript

Session lists and the selected transcript become usable without waiting for
provider readiness, Git or location metadata. Queue and human-input snapshots
load in parallel after the transcript. A newly created session tracks whether
it has received an authoritative message page, so returning to it reloads the
transcript instead of trusting an empty local list. While a large session's
runtime loads, its activity reads `Loading session`, then `Thinking`.

Transcript paging treats the cursor as recoverable state. If a reconnect or
branch change invalidates the cursor, the app reloads the newest page and
retries. If a page holds only messages already loaded, it keeps paging until it
reaches older history or the start. Paging failures show a notice.

While a turn runs, a status row at the bottom of the chat shows the runtime
activity, such as `Thinking`, `Running tool` or `Compacting`, falling back to
`Working`. Live tool rows do not hide it, and it disappears when the runtime is
idle.

Transcript rendering rules:

- Each tool call is one row. A saved result folds into its call row when both
  are loaded. A result whose call is on an unloaded page stays visible.
- Chat rows use the same outcome checks as Tool activity for live and saved
  results. Failures include `ok: false`, MCP `isError`, timeouts and nonzero
  exit codes. Exit code 1 from `rg`, `grep` and `fd` counts as success unless
  the result reports failure.
- Consecutive tool-only assistant batches join into one dense run, and
  commentary that starts tools stays joined to the batch that follows.
  Text-only commentary and final text keep normal spacing.
- A checkpointed assistant message with text and tool calls replaces its live
  commentary row as soon as it arrives, even when the provider saved it with a
  null phase.
- Messages that tools inject, such as the image message from `attach_image` or
  `image_generation`, are saved as `tool_context` entries. The model still sees
  them, but chat scrollback hides them and Tree shows them as technical nodes.
  Older sessions that saved them as `user` entries are recognized from their
  tool-call ancestry.
- User turns sit on the right and assistant turns on the left. User messages
  always show role and time. In an assistant turn, only the last row with text
  shows them.
- Turns that ran for at least a minute end with a summary such as
  `Worked for 1m23s · 7 tools`, taken from the saved turn's `turnSummary`.
  Parallel tool calls count individually.

## Workspace recovery

When a session's directory is unavailable, the timeline and draft stay
visible. A notice offers **Retry workspace** after the user restores the
directory, or **Relocate workspace**, which opens the location palette. Picking
a folder does not move the session until the user confirms
**Relocate this session**. Execution, model and compaction controls stay
disabled until the workspace is available. Queue removal stays available. A
`session_workspace_unavailable` response after the directory disappears shows
the same notice, and the rejected prompt's text and images return to the
composer.

After a relocation, the app clears directory-dependent caches and refreshes
tools, skills, MCP, models, providers and location metadata. Late catalog
responses for the old directory are ignored. Other tabs react to
`session.workspace.relocated` the same way. Delayed rename or pin responses
keep the new workspace and request a fresh summary. A failed catalog refresh
does not turn a successful relocation into a load failure. Reopening the
inspector or model picker retries it.

## Tests

Run the Node state suites from the repository root. The `HTTP contract` GitHub
Actions workflow runs all of them except `test_web_location_picker.mjs`.

```bash
node --experimental-default-type=module scripts/test_web_optimistic_updates.mjs
node --experimental-default-type=module scripts/test_web_agent_runs.mjs
node --experimental-default-type=module scripts/test_web_workspace_recovery.mjs
node --experimental-default-type=module scripts/test_web_workspace_races.mjs
node --experimental-default-type=module scripts/test_web_location_picker.mjs
node --experimental-default-type=module scripts/test_web_markdown_math.mjs
```

These scripts drive an installed Chrome against in-browser fixtures, never live
sessions. CI does not run them. Run each with `uv run python scripts/<name>.py`:

| Script | Covers |
| --- | --- |
| `test_web_location_picker_browser.py` | The location palette. |
| `test_web_type_to_focus_browser.py` | Typing into the composer from anywhere. |
| `test_web_composer_popover_browser.py` | Completion menus above both composers. |
| `test_web_markdown_math_browser.py` | Sanitized Markdown, math, code blocks and bundled KaTeX fonts. |
| `test_web_sidebar_browser.py` | The sessions sidebar. |

The inspector suites are listed in [web-inspectors.md](web-inspectors.md#tests).
