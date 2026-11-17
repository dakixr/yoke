# Web inspectors

This page is for contributors working on the inspector modal in the browser
app. It records how each view behaves, which keys it uses, and how to test it.
The rest of the app is described in [web-ui.md](web-ui.md).

Open Inspect from a saved session, from a tool call in the conversation, from
the command palette or with a slash command. The inspector belongs to that
session. Its views are Tool activity, Tree, Processes, Agents, Context and
Configuration, which holds Tools, Skills and MCP servers. Selection, filters
and reading position stay with each session and view while the app is open.
They survive switching views and closing the inspector, not a page reload, and
they never reach provider messages or saved config.

## Tool activity

The first load requests the latest 100 calls with `order=latest` and shows them
oldest at the top, in canonical start order. The first open selects the latest
call. Later opens restore the previous selection. Load earlier follows the
cursor toward older calls without changing the selection. Counts distinguish
loaded calls from the total. Search and status filters apply to the loaded
window only.

Canonical sequence decides order, not call ID or completion time, and
completing a call never moves it. Start time and duration are separate. A saved
call without a timestamp says the time was not recorded instead of guessing one.

Reading older history pauses following. New calls update the list and a
new-call count but never replace the selected detail. Latest clears the filters
and returns to the newest window. A call opened from chat can stay selected
outside the loaded page or the filters, and appears in a separate highlighted
row that explains why. Stale responses cannot overwrite a newer selection.
Detail requests for the same call are merged, and saved calls never request the
live output endpoint.

The list shows the tool name and a compact argument signature. The detail has
two open sections:

- Call shows the arguments the model sent as a function-style signature.
  Multiline strings and structured values stay readable. Argument
  normalization done at execution is internal and not shown.
- Result shows `resultProjection` when the call has one. That is the result as
  the model saw it, after the same projection the provider context used for
  that call. Commands omit runtime bookkeeping, and `rg`, `fd`, `apply_patch`
  and web search can show their TOON projections. JSON projections render as
  structured fields. TOON and other text stay preformatted. Copy returns the
  exact projected text. Without a projection the view shows the canonical
  redacted `result`. A running call shows captured live output until the
  result exists.

There is no intent summary and no raw or pretty toggle. The view labels output
retention and partial pages. Following the loaded output and reading earlier
lines are separate states, and a partial page never claims its last line is the
live end.

Open file reads the current file on disk, not the file as the tool saw it. Open
process resolves an HTTP process ID or a retained process number. Back returns
to the originating call. A process that is no longer retained, or a file the
session cannot read, produces an error instead of an unrelated item.

## Tree

Tree is for choosing where the next prompt continues. Selecting a node never
moves the session's leaf. The destination pane shows enough of the node to
confirm the choice and explains what leaves active context.

| Control | Action |
| --- | --- |
| Click a row, node or time | Select that destination and load its preview. |
| Up, Down | Select the previous or next shown row in time order. |
| Left, Right | Select a visible parent or child. |
| Home, End | Select the first or last loaded row. |
| Page Up, Page Down | Move through loaded rows in larger steps. |
| Space | Select the focused row without moving. |
| Enter | Continue from the selected destination once its preview has loaded. |
| Escape | Clear the destination. With none selected, close the inspector. |
| Current | Reveal the real leaf, loading earlier pages if needed. |
| Latest | Reveal the newest node in time, which may be on another branch. |

The Continue from here button sits in a fixed footer. It stays disabled while
the preview is loading or stale. A held Enter key cannot repeat a move.
Choosing another destination invalidates the old preview at once, and a
revision conflict requires a fresh preview.

Messages mode shows user messages and final assistant messages. When the leaf
is a tool or control node, it also shows the current position as an anchor.
All nodes shows commentary, tools and controls too. Hidden technical parents
are bridged so the graph stays connected. The graph marks the current
position, the selected destination and the latest node separately. Search finds
text, labels and IDs in shown rows without hiding the connecting history. Load
older pages to search further back. Loading older history keeps the visible
position, and timestamps stay visible on narrow screens.

Continuing from an ancestor removes its later descendants from active context
without deleting them. Moving to a user message can put its prompt back in the
composer for editing. The pane labels that case, and the branch handoff note is
optional. Labels are edited inline. After a move the graph returns to the new
leaf and clears the destination.

The bundled view requests 80 nodes per page to keep the DOM small. The API
default is 200.

## Processes

Processes are ordered newest started first and are labelled separately from
tool history. The initial filter is Running when anything runs, otherwise All
or recent. An explicit filter choice is remembered. A watched process stays
selected after it exits, even if it no longer matches Running, so its final
output stays visible. When the selection falls out of the filter otherwise, the
newest visible process is selected.

The right pane is mostly the selected process's output tail. While the process
runs, the view reads `GET /process/{id}/output` with an exclusive sequence
cursor and appends only new chunks. Process events trigger quick reads, with a
short polling fallback so a chatty process cannot starve its own updates. Full
metadata refreshes more slowly and restores the retained tail if the cursor
falls behind truncation. Output follows the live end until the user scrolls up,
then shows Jump to live. Completed processes without output say so.

Status, working directory, wrapping, stdin, Interrupt and Terminate sit next to
the output. Terminate needs an inline confirmation. Input and actions show
their own pending and error states, and an action finishing for one process
must not replace a different process selected in the meantime.

Process IDs and retained output live only in daemon memory. A tool call's final
result can survive a restart, but its process cannot.

## Agents

Agents lists the Yoke SDK agents started in this session, one row per agent
with its latest run. Open it from the Inspect menu, the inspector tabs, the
command palette or `/agents`. The session view has no agent panel. The view
reads `GET /agent-run?sessionID=` and refreshes on selection, on
`session.agent.updated` and after reconnect. A pending refresh is merged
rather than cancelling every read during sustained activity.

The initial filter is Running when an agent runs, otherwise All or recent, and
an explicit choice is remembered. Search covers names, models, last tools, task
IDs, errors and agent or run IDs. Running agents come first, then the newest
started. A sub-agent is indented under its parent when the parent is shown, and
otherwise appears as its own row. Reusing an agent keeps its row and adds a
run. An agent that has not started does not count as running.

The detail shows status, observation state, model, start and finish times,
duration, last tool, error class, attempt and task. Parent and sub-agent links
select the related agent. Open process shows the process that hosts the agent,
and Back to origin in Processes returns to the agent. A selected agent stays
visible outside the filter. Stale or lost observations never show as running or
completed, and after a failed refresh the previous rows are labelled as not
current.

## Configuration

Tools are grouped by source, with search, enabled and disabled filters and
expandable descriptions. Toggles apply to this session and show saving and
error feedback.

Skills list active skills before available ones. Preview opens the current
instruction file without activating the skill. File access still follows the
session's filesystem boundary, so a skill outside the location can be listed
while its preview is unavailable. Activation applies to the session.

MCP shows connection health and effective enabled state as separate facts.
Inventories start collapsed and can be searched. When a server's inventory is
truncated, counts describe the inspected page. Session changes save at once.
Repository and global changes wait for Apply or Discard. Changing scope
discards the unsaved draft for the old scope. Existing allow and deny rules
stay intact.

## Context and files

Context separates session properties from recent model-visible messages. It
reads the bounded `/context` window, not the full provider prompt. It reports
retained counts and truncation, shows messages oldest first, and labels content
blocks it does not render.

File detail reads the current file once when opened. It has line numbers,
literal find with previous and next match, wrapping, copy and Back. It is not a
historical snapshot or a live editor.

## Focus and narrow screens

Tab stays inside the modal, and the background is inert while it is open.
Escape closes the innermost interaction first. Closing returns focus to the
element that opened the inspector. The command palette can open above the
inspector and returns focus to it. View tabs move focus with the arrow keys and
activate with Enter or Space. Each view shows its own key help instead of a
permanent shortcut footer.

On small screens each view uses a full-screen list or detail with a Back
control instead of two stacked scrolling panes. Back keeps the selection and
reading position. Expanding diagnostic payloads does not shrink the main text.

## Tests

Run the inspector state suites and the trace chronology test:

```sh
node --experimental-default-type=module scripts/test_web_optimistic_updates.mjs
node --experimental-default-type=module scripts/test_web_inspector_state.mjs
node --experimental-default-type=module scripts/test_web_tool_activity.mjs
node --experimental-default-type=module scripts/test_web_tree_navigation.mjs
node --experimental-default-type=module scripts/test_web_inspector_support.mjs
node --experimental-default-type=module scripts/test_web_agent_runs.mjs
uv run pytest tests/yoke/http/test_tool_trace_chronology.py
```

With Chrome installed, run the assembled desktop and narrow-screen fixture:

```sh
node --experimental-default-type=module scripts/test_web_inspector_browser.mjs
```

Set `CHROME_BIN` when the browser is not called `google-chrome`. The test
starts a private loopback fixture server and an isolated headless browser, and
renders the real inspector components against in-memory API fixtures. It does
not run agents, open real sessions, signal real processes or change real tool or
MCP configuration. It writes results and screenshots under the ignored
`.agents_local/inspector-ux/browser` directory and removes the temporary
browser profile on exit.
