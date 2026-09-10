# Web inspectors

Open Inspect from a saved session, a tool call in the conversation, or the
command palette. The inspector belongs to that session. View preferences stay
in application memory, separate from provider messages and persistent config.
They survive view switches and closing the inspector, not a page reload.

## Tool activity

The initial window contains the latest 100 calls in canonical start order,
oldest at the top. The first open selects the latest call; subsequent opens
restore the previous selection. Load earlier retrieves preceding pages without
changing the selected call. Counts distinguish loaded calls from total calls.
Search covers loaded calls only.

Reading older history pauses following. New calls update the list and its
new-call count but never replace the selected detail. Latest clears the list's
filters and returns to the newest window. A selected call outside that window
or outside a filter appears separately, with an explanation rather than an
unhighlighted detail disconnected from its list.

The list shows the actual tool name and a compact argument signature. The detail
has two open sections: Call and Result. Call renders the model-sent arguments as
a function-style signature. Multiline strings and structured values stay
readable instead of becoming escaped one-line JSON. Execution normalization is
internal state and is not shown in the inspector.
Result uses the same opt-in projection that provider context uses for that
specific tool-result occurrence. Commands omit runtime bookkeeping, while rg,
fd, apply_patch, and web search can use TOON 4.1 where their provider projection
does. If no projection applied to that occurrence, the canonical public result
is shown unchanged. JSON projection text is parsed for presentation and rendered
as structured fields; TOON and other text projections stay preformatted. Copy
still returns the exact projected text. Running calls can show captured live
output until a result exists. There is no semantic "intent" summary and no
raw/pretty or status-filter dropdown. Time unavailable in a saved trace stays
unavailable rather than being guessed from its ID.

Output retention and partial pages are labelled. Following the currently loaded
output and inspecting earlier lines are distinct states. A partial output page
does not claim that its last line is the live end.

Open file reads the current file on disk, not the historical result of the tool
call. Open process resolves an HTTP process ID or a retained runtime process
number. Back restores the originating call. A no-longer-retained process or an
unauthorized file produces an error without substituting an unrelated item.

## Tree navigation

Tree is for choosing where the next prompt should continue. Selecting a node
does not move HEAD. The stable destination pane supplies enough context to
confirm the destination and explains its effect on active context.

| Control | Action |
| --- | --- |
| Click a row, node, or time | Select that destination and load its preview. |
| Up / Down | Select the preceding or following shown row. |
| Left / Right | Select a visible parent or child. |
| Home / End | Select the first or last loaded row. |
| Page Up / Page Down | Move through the loaded rows in larger steps. |
| Space | Select the focused row without moving HEAD. |
| Enter | Continue from the selected destination after its matching preview loads. |
| Escape | Clear the destination first; close the inspector when none is selected. |
| Current | Reveal the actual current HEAD, loading earlier pages when needed. |
| Latest | Reveal the latest chronological node, independent of HEAD. |

The Continue from here button stays in a fixed footer. It remains disabled while
the preview is pending or stale. A held Enter key cannot repeat a move. Changing
destinations immediately invalidates the prior preview, and revision conflicts
require a fresh preview rather than silently moving on new information.

Messages mode hides technical detail but retains the current position as an
explicit anchor when HEAD is a tool or control node. All nodes reveals the full
loaded graph. Search finds text, labels, and IDs in shown rows without hiding
the connecting history. Load older pages to search more history.

Continuing from an ancestor removes later descendants from active context. It
does not delete them. Moving to a user message can restore its prompt to the
composer so it can be edited. The pane labels this case and keeps branch
handoff notes optional. Existing paths remain available for later navigation.

## Processes

Processes are ordered newest-started first, labelled separately from tool-call
history. The initial filter is Running when active processes exist, otherwise
All / recent. Explicit filter choices are remembered. A watched process stays
selected after it exits, even if it no longer matches Running, so its final
output remains visible.

Scroll upward to stop following output; use Jump to live to resume. Completed
processes without output say so instead of waiting indefinitely. Interrupt and
Terminate are separate actions; termination requires an inline confirmation.
Input and actions display their own pending and error states. An action finishing
for one process must not replace a different process selected in the meantime.

## Configuration

Tools use source groups, search, enabled/disabled filters, and expandable
descriptions. Toggles apply to this session, with local saving and error feedback.

Skills show active instructions before available skills. Preview opens the
actual current instruction file without activating it. File access still obeys
the session's filesystem boundary, so an out-of-location skill can be listed
but its file preview may be unavailable. Activation applies to the session.

MCP connection health and effective enabled state are separate facts. Inventories
are collapsed until needed and can be searched. Capability counts describe the
inspected page when the server inventory is truncated. Session changes save
immediately. Repository and global changes are staged behind Apply and Discard.
Changing scope discards the unsaved draft for the old scope. Existing allowlist
and denylist rules remain intact.

## Context and files

Context separates session properties from recent model-visible messages. It is
a bounded window, not the full provider prompt. The view reports retained counts
and truncation, presents messages oldest to newest, and explicitly labels
non-text blocks it does not render.

File detail reads the current disk contents once when opened. It has line
numbers, literal find, previous/next matching line, wrapping, copy, and Back.
It is neither a historical file snapshot nor a live editor.

## Focus and narrow screens

Tab remains inside the modal. The background is inert while it is open. Escape
closes the innermost active interaction first, and closing restores the opener's
focus. The command palette can open above the inspector and returns focus to it.
Primary and configuration view tabs use arrow keys to move focus and Enter or
Space to activate.

On narrow screens, list/detail Back controls preserve the selection and reading
position. Tree timestamps remain visible. Expanding diagnostic payloads does
not require shrinking the interface's primary text.

## Verification

Run the existing web state suite and the focused inspector suites:

```sh
node --experimental-default-type=module scripts/test_web_optimistic_updates.mjs
node --experimental-default-type=module scripts/test_web_inspector_state.mjs
node --experimental-default-type=module scripts/test_web_tool_activity.mjs
node --experimental-default-type=module scripts/test_web_tree_navigation.mjs
node --experimental-default-type=module scripts/test_web_inspector_support.mjs
uv run pytest tests/yoke/http/test_tool_trace_chronology.py
```

With Chrome installed, run the assembled desktop/narrow-screen browser fixture:

```sh
node --experimental-default-type=module scripts/test_web_inspector_browser.mjs
```

Set `CHROME_BIN` when the browser executable is not named `google-chrome`. This
test starts a private loopback fixture server and an isolated headless browser.
It renders the actual inspector components with in-memory API fixtures. It does
not start real agent work, navigate a real session, signal a real process, or
change real tool/MCP configuration. Results and screenshots are written under
the ignored `.agents_local/inspector-ux/browser` directory, and the temporary
browser profile is removed on exit.
