# CLI

At startup, yoke automatically loads environment variables from a `.env` file
located next to the yoke source package at `src/yoke/.env`, if present.
The CLI entrypoint keeps package startup lightweight for commands such as
`yoke --help` and `yoke version`; provider, tool, model, skills, and interactive
runtime modules are imported only when the selected command needs them.
On Windows, interactive mode also guards prompt-toolkit's console-input watcher
against a benign late reschedule during Python asyncio executor shutdown, so
exiting yoke does not print an unrelated callback traceback.

## Basic usage

```bash
# Start an interactive session
yoke

# Start interactive mode with an initial prompt
yoke "explain this codebase"

# Run headless (one turn, print output, exit)
yoke --headless "run the tests and summarize failures"

# Pipe input
echo "what does utils.py do?" | yoke --headless

# Attach one or more local images to the initial prompt
yoke --image screenshot.png "describe this screenshot"
yoke --headless --image chart.png --image legend.png "summarize these charts"
```

Interactive startup prints only the tool-loading summary and version banner.
Run `/shortcuts` or `?` when you want the keyboard shortcut reference.

Interactive mode keeps one prompt-toolkit application alive for the complete
session. Prompt submissions are serialized away from the terminal event loop,
and fullscreen selectors temporarily suspend and then restore the editor,
including any text that was present before opening them. Agent, tool, and
commentary output is queued in order, rendered in worker threads, and committed
to native terminal scrollback in short batches. Idle sessions do not redraw on
a timer; the spinner redraws only while a turn is active. This keeps cursor
movement and typing responsive even when Markdown rendering, tool output, or
session persistence is busy.

---

## Providers and models

Select models with `provider:model` or `provider:model:thinking_effort`:

```bash
yoke --model codex:gpt-5.5:medium "..."
yoke --model codex:gpt-5.6-terra:max "..."
yoke --model opencode-go:gpt-5.6-luna:high "..."
yoke --model zai:glm-5.3:max "..."
```

Yoke's built-in providers are:

| Provider | Authentication |
| --- | --- |
| `codex` | `yoke login codex`, `~/.codex/auth.json`, the account vault under `~/.codex-auth/accounts/`, or `YOKE_CODEX_API_KEY` |
| `opencode-go` | `OPENCODE_API_KEY` or `yoke login opencode-go` |
| `zai` | `ZAI_API_KEY` or `yoke login zai` |

Codex uses a persistent Responses WebSocket transport and keeps response
continuity, encrypted replay state, prompt-cache affinity, and routing metadata
in memory. Session IDs provide stable cache scope across provider
reconstruction and resume. New and forked sessions receive distinct scopes.
The advertised catalog currently includes `gpt-5.6-sol`,
`gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`, and
`gpt-5.4-mini`.

OpenCode Go advertises its maintained model catalog, including its Responses
path for `gpt-5.6-luna` and OpenAI-compatible chat-completions paths for
other models. Both Z.ai and OpenCode Go expose GLM-5.3 with `low`, `high`, and
`max` reasoning efforts (default: `max`). GLM-5.2 supports `none`, `high`, and
`max` (default: `max`) through both providers. Provider catalogs also declare context
windows, image-input support, and model-specific system messages.

Built-in provider response and stream-idle timeouts default to 15 minutes.
Connection-establishment and WebSocket health-check timeouts remain shorter so
unreachable services fail promptly.

Use these commands to inspect or change defaults:

```bash
yoke models list
yoke models set codex:gpt-5.5 --reasoning-effort high
yoke models set zai:glm-5.3 --reasoning-effort max
yoke models set
yoke models set --repo

yoke providers list
yoke providers doctor
yoke providers login codex
yoke providers init my-provider
```

Without `--repo`, model defaults are written to
`~/.yoke/config.json`; workspace defaults live in `.yoke/config.json`.
Explicit CLI flags win, while resumed sessions prefer their saved provider,
model, and thinking effort. Every model selection is reconciled with the
destination provider's catalog. A supported explicit effort is retained;
otherwise Yoke uses that model's advertised default. This applies at startup,
session resume, SDK construction, and `/model` switches, so an effort saved for
one model or provider cannot make another model unavailable.

Global custom providers are Python modules under `~/.yoke/providers/`. A
plugin defines `register_provider(context)` and may define
`list_provider_models(context)`; the alternate `CONFIG_CLASS` plus
`PROVIDER_CLASS` convention is also supported. CLI and SDK provider
resolution use the same plugin registry, credential store, readiness checks,
and model metadata.

Image attachment is available only when the selected provider/model accepts
image input. Codex additionally exposes provider-hosted image generation.
Codex web research uses hosted Responses web search with external access and a
high search-context budget. Other providers retain Yoke's local keyless search,
fetch, and multi-source synthesis workflow.

Provider retries, rate limits, connection recovery, and stale-continuity
fallbacks appear as visible warning events in interactive and observed SDK
runs. Provider and web clients use the operating system's standard TLS
certificate validation. Every completed response also writes an attributed local usage record
under `~/.yoke/usage-metric-logs/<provider>/`.

---

## Mermaid Diagrams

In terminal output, fenced `mermaid` blocks render as width-aware Unicode art.
Yoke supports nine diagram families: flowchart, state, class, ER, sequence, pie,
mindmap, timeline, and git graph. A malformed diagram, or one wider than the
available terminal columns, remains readable in a framed source fallback.
Redirected and other non-TTY output preserves the original Markdown instead of
replacing it with terminal art, so transcripts and downstream automation keep
the source form.

Rendering happens in-process and requires no Node.js installation, browser,
subprocess, or network access. Set `YOKE_MERMAID=0` (also `false`, `no`, or
`off`) to show Mermaid fences as ordinary highlighted code blocks.

## Images In Interactive Mode

In prompt-toolkit mode, yoke can keep pending image attachments for the next user
turn.

- Press `Ctrl+V` or `Alt+V` to paste text or attach an image from the
  clipboard when one is available. Use `Alt+V` when the terminal intercepts
  `Ctrl+V`. Clipboard image detection and PNG encoding run in the background,
  so a large clipboard image does not freeze prompt editing.
- Press `Ctrl+U` to remove the last pending image attachment.
- Press `Enter` to steer/send immediately while a turn is running.
- Press `Tab` to queue the prompt behind the current turn.
- Press `Shift+Tab` to cycle thinking effort through the active model's supported levels.
- Press `Ctrl+X` then `Q`, or run `/queue`, to open the fullscreen queue manager.
- In the queue manager, you can edit, delete, promote, pause, reorder, or mark queued prompts as steering prompts. While the queue manager or its item editor is open, output from an active turn is deferred until you close the manager, so tool calls and response text cannot overwrite your edit.
- Queued prompts and pending image attachments are persisted with the session, so they survive exit/resume. Steering prompts run before normal queued prompts, and active steering requests stop the current turn first.
- Starting a queued prompt removes it from persisted queue state immediately, so consumed prompts cannot reappear after a crash or later resume.
- A `/skill` command queued with `Tab` stays inactive until it reaches its queue position. Yoke then activates the skill once and does not send the command to the model.
- While slash-command completions are open, use `Up`/`Down` to move between
  options; `Left`/`Right` keep moving the cursor through the whole prompt,
  including across newline boundaries.
- Press `Esc Esc` to stop the current turn immediately; yoke records the user
  prompt and interruption marker while retiring in-flight tool calls in the
  background.
- Press `Ctrl+J` to insert a newline.
- Press `Ctrl+X` then `O` to open the fullscreen tool inspector. It shows complete
  tool-call arguments, validated/executed arguments when available, and full
  tool results in an alternate-buffer view without adding noise to scrollback.
  When opened during a running turn, the inspector refreshes live, supports
  arrow-key navigation, mouse selection/scrolling, and shows streamed tool
  output before the final result arrives. Stopping or steering a turn marks its
  in-flight tool calls as cancelled and ignores late events from retired work.
  Terminal control bytes are shown as visible control pictures so ANSI-bearing
  output and malformed Unicode cannot break inspector navigation. Live redraws
  are rate-limited, unchanged trace snapshots and detail layouts are reused,
  and streamed output is compacted within its bounded history so large or
  noisy tool sessions do not block navigation.
- Run `/ps` or press `Ctrl+X` then `Ctrl+P` to open the
  fullscreen process inspector. It lists running and
  recently completed `exec_command` sessions for this live yoke runtime and
  shows each command's PID, working directory, timing, exit status, and bounded
  output history without consuming output needed by `write_stdin`. The view
  refreshes while processes produce output. Notifications are coalesced and
  decoded/wrapped output is reused until the process bytes, terminal width, or
  wrapping mode changes, so large retained output does not get rebuilt for
  every small update. Process state is ephemeral and is
  not restored when a persisted conversation session is resumed. The basic CLI
  prints the same process metadata as a table instead of opening a fullscreen
  view.
- In basic interactive mode, `Ctrl+C`, `exit`, and `quit` also request active
  turn cancellation before yoke saves the resumable session state.
- Pasting multiline text keeps the entire paste in the current prompt; press
  `Enter` after the paste to submit it.
- Use `/image path/to/file.png` to attach a local image file explicitly.
- Use `/tree` to navigate the current session tree, fork from an older point,
  label entries, search/filter history, and optionally summarize the branch you
  are leaving.
- Use `/title new-title` to rename the active session shown in resume/session
  lists and on the right side of the prompt-toolkit bottom toolbar.
- Use `/pin` to pin or unpin the active session from inside the session.
- Use `/info` to print the active session id, title, pin state, storage path,
  provider/model, message counts, timestamps, and context-window metadata.
- Use `/fork` to copy the current saved session into a new persisted session and
  continue future turns in that fork.
- Use `/shortcuts` or `?` to print the interactive keyboard shortcuts in scrollback.

Pending image attachments are shown in the bottom toolbar and are sent with the
next submitted prompt. Submitted CLI and `attach_image` tool images are stored
as compact data-URL snapshots in the session, so resumed conversations do not
depend on temporary clipboard files or deleted local paths. Older sessions that
still reference a missing local image path degrade that image to a text
placeholder instead of failing the provider request.
`attach_image` keeps that snapshot only in the appended image message; its tool
result contains compact path and label metadata rather than a duplicate base64
payload, so large images do not consume the context window twice.
The prompt-toolkit bottom toolbar uses the same accent palette and status flow
as yoke: `Thinking`, `Streaming`, `Running tool`, `Compacting`, and
`Recovering`. Set `YOKE_BAR_TIMER`, `YOKE_BAR_TOKENS`, `YOKE_BAR_GAUGE`,
`YOKE_BAR_TOOLS`, or `YOKE_BAR_TURN` to `0`/`false`/`off` to hide optional
segments, or to a truthy value to force-enable them where applicable.
Turns that run for more than 60 seconds also print a dim completion summary in
scrollback, such as `Worked for 1m02s · 6 tools`.

Shell command tool timeouts terminate the full subprocess tree and bound final
pipe collection, so timed-out commands finish as tool results instead of staying
pending behind child processes that keep stdout or stderr open. Steering or
stopping a turn logically retires it immediately and targets a UI handoff under
100 ms; a replacement steering turn starts without waiting for the retired
provider request or tool to physically exit. Retired generations are fenced so
late output cannot replace the accepted conversation or render into the active
turn.

Most tool calls run in isolated child processes created with the cross-platform
`spawn` start method, avoiding unsafe `fork` state in the multithreaded CLI.
Their termination and kill
escalation continue in a background reaper after cancellation. Tools that cannot
be spawned or explicitly require in-process resources run in supervised daemon
threads: cooperative tools observe the cancellation callback, while
non-cooperative tools are detached from the UI and retain their resources until
they return. The sub-100 ms target is therefore a logical handoff guarantee, not
a guarantee that every remote request, thread, or kernel process has exited.
Runtime shutdown signals all remaining in-process tools and waits for a bounded
cleanup window before releasing their resources. If a non-cooperative tool
outlives that window, shutdown fails explicitly and leaves the runtime open for
a later cleanup attempt instead of falsely reporting a completed close.
Steering keeps the original turn timer and accumulated tool count while the
replacement model run continues. An explicit `Esc`, `Esc` stop prints the same
elapsed-time and tool-count summary as a completed turn, including for turns
shorter than one minute. Both paths continue from the last accepted tool-result
checkpoint. The in-memory continuation branch is available immediately, while
per-session write serialization prevents a retired stop checkpoint from racing
with or replacing a newer turn.

Yoke checkpoints accepted session state to the same session JSONL file after tool
results and records a synthetic interruption checkpoint immediately when a turn
is stopped or steered. A steering turn starts from the latest checkpoint,
including activated skills, while retired turns cannot overwrite a newer
generation's state. Later metadata changes, such as switching models, preserve
that checkpoint instead of restoring the runtime's older pre-interruption branch.
Checkpoint writes use unique temporary files with short retries for transient
Windows file-lock races, and a failed checkpoint is reported as a warning rather
than crashing the active turn.

Use `/model` in interactive mode to open a fullscreen table of advertised models
across providers and switch to the selected row.
Choosing a row uses that model's advertised default thinking effort instead of
carrying the previous provider's effort across the switch.
Context budgeting follows the selected model's advertised window, and yoke may
compact the provider working context before a switch to a smaller context
window. The canonical session remains append-only. The switch rolls back when
handoff generation fails or the reduced epoch still does not fit.
When providers report token usage, yoke stores normalized input, output,
reasoning, cached-input, cache-creation, and total token counts on the
assistant response for session diagnostics and future budgeting improvements.
Provider-reported counts are also used only when their provider and model match
the active model.
They also fall back to the current conservative estimate when persisted provider
usage understates a rebuilt or resumed prompt. Handoff generation is one normal
continuation request, not a flattened or chunked side request. The current epoch
must therefore still fit the provider for the handoff request to succeed.
The manual `/compact` command appends a synthetic handoff instruction to the
current provider epoch and stores the assistant response as the checkpoint. It
then starts a reduced epoch containing prior real user intent, the newest
handoff, and the normal post-checkpoint tail. Pre-handoff user intent is bounded
newest-first by `recent_user_tokens` (20,000 tokens by default); one boundary
message may be tail-truncated to keep the limit strict. The handoff prompt asks
for at most `handoff_target_tokens` (12,000 tokens by default). Automatic
threshold and overflow paths use the same flow; overflow retries at most once
after a successful reduction.

---

## Sessions

Sessions save your conversation so you can pick up where you left off.

```bash
# Start or continue a named session
yoke --session my-project "let's keep working on the auth module"

# Resume interactively (pick from a list)
yoke resume

# Resume interactively across all roots
yoke resume --all

# Resume a specific session by id
yoke resume 20240421-143022-abc1

# Start directly from a forked session
yoke --fork 20240421-143022-abc1
```

Sessions are stored under `~/.yoke/sessions/` as append-only `.jsonl` files and
auto-expire after 30 days. The CLI owns session files, indexes, ids, and resume
selection; the stored agent state uses structured conversation entries so
memory snapshots, typed compaction handoffs, and branched session trees can be
restored without flattening to transcript text. Yoke streams the typed JSONL
events from disk and uses an entry-id index to replace repeated entry events.
The streaming decoder uses Pydantic's native JSON parser. This avoids a second
complete text copy, reduces temporary decode memory, and avoids repeated
duplicate-entry scans at startup. Yoke reads only the current typed JSONL event
format. It does not carry migration adapters for obsolete session formats.
Metadata-only changes, including `/model`, append only changed metadata fields
and update the loaded record in memory. They do not rebuild or reload the
conversation. Interactive shutdown also trusts the latest accepted turn
checkpoint. It saves only changed provider metadata and queue content instead
of capturing and reconciling the complete conversation again. Context-usage
estimates run outside the prompt-critical path and coalesce pending requests so
only one scan runs at a time. Large sessions do not delay input after a command.
Initial prompt titles use a local fallback and do not require a provider request.
OpenAI-compatible providers create their HTTP transport on the first model
request instead of importing and initializing it during CLI startup.

The `SessionTree` module is the authoritative seam for session topology. It owns
parent assignment, active selection, legacy repair, branch reconciliation,
compaction checkpoints, and typed runtime, provider, audit, and scrollback
projections. Callers append intents and request projections instead of editing
entry IDs or rebuilding paths. An active CLI session validates the complete
topology once and retains entry and parent indexes. Prompt, stop, steer, and
context-estimate paths then walk and copy only the selected branch. The JSONL
session store remains a persistence adapter at the `SessionTree` seam.

An isolated CLI turn takes ownership of its already-copied, validated active
path. It does not copy the primary runtime context before replacing it. Message
appends use a `SessionTree` append delta, and provider prompt assembly reuses the
runtime projection until a mutation changes it. Accepted turns transfer their
owned context back to the primary runtime instead of rebuilding it.

Normal saves prove that the runtime branch extends the retained active path.
They sanitize only the new suffix, append that suffix with its metadata delta,
and update the loaded record and indexes in memory. The writer trusts this proof
instead of comparing every persisted entry. Branch changes and legacy inputs
still use complete `SessionTree` reconciliation. Retention cleanup runs when
sessions are listed instead of delaying each turn checkpoint.

The model and live runtime receive the current provider epoch, while `/tree`
retains the complete active message path. Detached checkpoints remain audit
state and are not inserted into another active branch's provider prompt.

Startup scrollback is independently bounded to the latest 400 user-visible audit
messages. Internal compaction handoff messages do not replace or hide the
assistant and tool activity around a compaction boundary. When older messages
are omitted, yoke prints their count, explains whether a compaction summary
remains in model context, and points to `/tree`.
Scrollback rendering skips an internal handoff marker without discarding any
real messages that occur before it.
Legacy handoff recovery also removes exact copies of provider-retained messages,
including nested handoffs, before it applies the scrollback limit.
Resuming an unchanged session is read-only until the conversation or its
metadata changes, and subsequent saves reuse the record already loaded by the
runtime instead of decoding the session repeatedly. Normal resume projections
copy only active-path values; detached legacy handoffs retain the complete-tree
recovery path.

`/tree` is available in the prompt-toolkit TUI. It opens a fullscreen navigator
over the session entries. Selecting a user entry rewinds to that entry's parent
and puts the selected user text back in the editor, so submitting it creates a
new branch. Selecting an assistant, tool, compaction, or summary entry continues
after that entry. Navigation never deletes abandoned history; future turns are
built only from the active branch. The selector supports search, filter cycling,
local folding, entry labels stored as metadata, and distinct colored headers for
message types such as user, assistant, tool, and summaries. Inactive branches
start folded at their first visible entry. Search temporarily expands them and
uses retained per-filter candidate indexes, so large inactive branches do not
delay normal navigation. Before moving branches, yoke asks whether to create a
branch summary; `No summary` is the default, while custom summary guidance is
appended to the standard summary prompt when chosen.
After navigation, yoke reprints the active branch transcript before showing the
next prompt so the visible scrollback matches the selected point.

In a terminal, `yoke resume` opens a keyboard-driven selector with aligned
columns for pin status, session title, last activity, and session id. Use
`Up`/`Down` or `j`/`k` to move, `PgUp`/`PgDn` to scroll faster, `Home`/`End` to
jump, and `Enter` to resume. Press `/` to fuzzy-search by title, `p` to pin or
unpin the highlighted session, and `q` or `Esc` to cancel. Pinned sessions are
shown at the top of the table and are protected from normal session-retention
cleanup. Pass `--all` to list saved sessions across every workspace root
instead of only the current root; that view adds a root-path column before the
session id.
If an external index edit has newer metadata than the JSONL stream, loading the
session appends that metadata back to the JSONL file so a manual title or pin
state survives resume.
Use `--fork <session-id>` to copy an existing session into a new session id and
continue there without appending to the original; `--fork` cannot be combined
with `--session` because one selects a source session and the other names the
target session.

---

## Skills

A skill is a Markdown file that tells the agent how to approach a type of task. You create skills once and activate them by name.

### Creating a skill

Create a directory with a `SKILL.md` file. The directory name must match the skill name:

```
my-skills/
└── code-review/
    └── SKILL.md
```

```markdown
---
name: code-review
description: Detailed code review focusing on security and correctness
---

When reviewing code, always check for:
- Security vulnerabilities
- Missing error handling
- Unclear variable names

Format findings as a prioritized list.
```

Skill name rules: lowercase kebab-case, directory name must match the `name` field.
Skill files may use UTF-8 with or without a byte order mark (BOM).

Yoke also ships with a built-in `create-skill` skill that helps the agent create
new skills correctly. It tells the agent to ask where the skill should be
created first (repo-local, global, or custom directory) and then use
`yoke skills init` to scaffold it.

### Using skills from the CLI

```bash
# Activate a skill at startup
yoke --skill code-review "review the changes in src/auth.py"

# Activate a skill during an interactive session, then submit a prompt
/skill code-review review the current diff with this workflow

# Inspect and scaffold skills
yoke skills list
yoke skills show code-review
yoke skills init repo-style
```

`yoke skills list` and `yoke skills show` use the same built-in, global, and
repo-local discovery paths as normal CLI sessions. Pass `--root` when you want
repo-local discovery or scaffolding to target a different workspace.

### Skill directories

Yoke auto-discovers skills from:
- built-in yoke skills under `src/yoke/agent/skills/built_in/`
- `~/.yoke/skills/` — your personal skills, available in every project
- `.yoke/skills/` — skills for the current repo

Place skill folders inside these directories and they'll be available by name.

During a session the agent can also activate skills itself when the `skill`
tool is available. Each activation reads the skill's current `SKILL.md` content
and appends it as a system message at the end of the conversation context. That
message also includes a recursive list of full file paths in the skill
directory, so skills can reference supporting files next to `SKILL.md`.
The compact `skill` tool result contains active skill metadata only because the
complete instructions are already in that system message.
Activating a skill that is already active simply appends a fresh skill system
message; there is no separate refresh path. Yoke stores the loaded `SKILL.md`
content in session history, so resumed sessions keep working even if the
original skill file is later moved or deleted. Interactive skill activation
preserves the structured active branch, including compaction memory snapshots,
and appends the activated skill after that snapshot instead of rebuilding the
branch from the rendered message transcript.

You can manually activate a skill with `/skill <name>`. If you include text
after the skill name, yoke activates the skill and submits the remaining text as
the next normal user prompt, so multiline prompts are supported in the
interactive editor.

Yoke ships with a built-in `create-skill` skill in the codebase under
`src/yoke/agent/skills/built_in/create-skill/SKILL.md`. It instructs the
agent to ask where the skill should be created first (repo-local, global, or
custom location), then scaffold it with `yoke skills init`.

---

## MCP servers

Configure global MCP servers in `~/.yoke/mcp.json` and workspace servers in
`.yoke/mcp.json`. Streamable HTTP servers verify TLS certificates by default. For
an internal server with a self-signed certificate, set `"verify": false` on that
server. This disables TLS certificate verification only for that MCP server.

## Adding extra tools

Place Python files in `.yoke/` (workspace) or `~/.yoke/` (global) and yoke will load your tools automatically alongside the built-ins. Yoke skips state and content subdirectories such as `skills/` and `sessions/`, so Python helper files bundled with skills are not imported as tool plugins.

There are three ways to define tools in these files.

### `@function_tool` — quickest option

Decorate a typed function and yoke turns it into a tool. The function name becomes the tool name, the docstring becomes the description, and every parameter becomes an argument the agent can pass.

```python
# .yoke/tools.py
from yoke.cli.tools.decorators import function_tool

@function_tool
def notify(message: str, title: str = "yoke") -> dict:
    """Send a desktop notification."""
    import subprocess
    subprocess.run(["notify-send", title, message])
    return {"ok": True}
```

Override the name or description when the function name isn't ideal:

```python
@function_tool(name="send_notification", description="Pop up a desktop alert.")
def notify(message: str) -> dict:
    import subprocess
    subprocess.run(["notify-send", message])
    return {"ok": True}
```

Rules:
- Every parameter must have a type annotation.
- No `*args` or `**kwargs`.
- Must return a `dict`.

### `@class_tool` — full control

For tools that need more logic, workspace access, or Pydantic validation, write a `LocalTool` subclass and mark it with `@class_tool`.

```python
# .yoke/tools.py
from pydantic import Field
from yoke.cli.tools.decorators import class_tool
from yoke.agent.tools import WorkspaceTool

@class_tool
class AppendFileTool(WorkspaceTool):
    name = "append_file"
    description = "Append text to a file, creating it if it doesn't exist."

    path: str = Field(description="File path relative to the workspace.")
    content: str = Field(description="Text to append.")

    def execute(self) -> dict:
        target = self._resolve_path(self.path, allow_missing=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as f:
            f.write(self.content)
        return {"ok": True, "path": self._display_path(target)}
```

Use `@class_tool(name=..., description=...)` to override the class-level attributes without editing the class body.

### `register_tools(context)` — explicit registration

If you need runtime configuration (credentials, feature flags, provider/model selection, …) return tools from a `register_tools` function. When this function is present, yoke uses it instead of scanning for decorated classes.

```python
# .yoke/tools.py
def register_tools(context):
    api_key = context.home.joinpath(".my_tool_key").read_text().strip()
    if context.model_id and context.model_id.startswith("gpt"):
        return [FastApiTool.bind(root=context.root, api_key=api_key)]
    return [MyApiTool.bind(root=context.root, api_key=api_key)]
```

`context` exposes `root`, `home`, `provider`, `provider_name`, `model_id`,
`model_name`, `model_key`, `reasoning_effort`, and `cancel_requested`. In CLI
sessions yoke refreshes provider-aware tool registration when the active provider
or model changes, so a tool can expose different capabilities per model.

While fullscreen modal screens such as `/tools`, `/queue`, `/model`, `/tree`,
and the tool inspector are open, background agent output is deferred and replayed
after the modal exits to avoid alternate-buffer bleed-through.

Tools added any of these ways appear alongside the built-ins. To restrict which tools are active, use a tool policy (see below).

---

## System instructions (`AGENTS.md`)

`AGENTS.md` is loaded into the agent's system prompt automatically.

- `~/.yoke/AGENTS.md` — applies to all your yoke sessions
- `AGENTS.md` in the repo root — applies when running yoke inside that repo

```markdown
# AGENTS.md

This is a FastAPI project. Use async functions throughout.
The test suite is run with `make test`.
Never edit migration files directly.
```

---

## Tool policy

Control which capabilities and exact tool overrides the agent can use via `config.json`.

Use `yoke tools list` to inspect the current inventory. It exits non-zero when discovery fails, shows each tool's capability slice, warns about unknown capability IDs, and warns when an exact tool override does not match a loaded tool.

You can also scaffold and update policy from the CLI:

```bash
yoke tools init
yoke tools activate file.write --repo
yoke tools deactivate shell --global
yoke tools deactivate repo_echo --tool --repo
```

`yoke tools init` creates `.yoke/example_tools.py` in the selected workspace. `yoke tools activate` and `yoke tools deactivate` write capability policy by default; pass `--tool` to write an exact concrete tool override for custom tools or debugging. Config is written to `.yoke/config.json` by default; pass `--global` for `~/.yoke/config.json` or `--repo` to make the workspace target explicit.

Use `/tools` in interactive mode to toggle concrete tools for the current run. After selecting tools, yoke asks whether to apply the change only to the current session, persist it to this workspace root's `.yoke/config.json`, or persist it globally to `~/.yoke/config.json`. Runtime-injected tools that are not displayed in the menu, such as `skill`, are preserved and are not written as disabled merely because they were absent from the selection table. Persisting a built-in capability selection also clears older exact-tool overrides for the concrete tools in that row, so stale overrides cannot contradict the selected state. When the current root is the home directory, the duplicate root scope is omitted because it resolves to the global config path.

- Built-in defaults — applied even when no config file exists
- `~/.yoke/config.json` — global policy
- `.yoke/config.json` — workspace policy (workspace overrides global and defaults)

The effective precedence order is: built-in capability defaults, then global config, then workspace config. Capability policy gates whole tool slices, then exact `tools` entries can override individual concrete implementations. Built-in capabilities live in `yoke.agent.capabilities`, where each `BaseCapability` resolves a high-level ability into one or more provider/model-specific concrete tools.

By default yoke allows these built-in capabilities: `file.read`, `file.write`, `file.search`, `image.attach`, `image.generate`, `web.fetch`, `web.search`, and `web.research`. Provider-aware capabilities can resolve to no concrete tool when the selected provider or model cannot support them. The `file.read` capability registers both `read` for UTF-8 text and `extract_file_context` for best-effort document and image extraction. `shell` is a known capability for shell and Python execution tools, but denied unless enabled by config.

```json
{
  "capabilities": {
    "file.write": "deny",
    "shell": "deny",
    "web.fetch": "deny"
  },
  "tools": {
    "repo_echo": "allow",
    "write": "deny"
  }
}
```

Values are `"allow"` or `"deny"`. Capability IDs and tool names are exact strings; glob patterns are not supported. If yoke sees legacy glob keys in `tools`, such as `"*"`, it replaces that config file with the current default capability policy. Built-in `file.write` is model-aware: GPT/OpenAI-style models receive `apply_patch` and its patch-format system instructions, while other models receive `edit` plus `write`. Disabling the capability also removes its contributed instructions from the context.

If a `config.json`, tool plugin, or skill file is malformed, yoke reports the file path and a short plain-English reason such as invalid JSON syntax, missing `SKILL.md` frontmatter, or a plugin import failure.

**Example: read-only agent**

```json
{
  "capabilities": {
    "file.write": "deny",
    "file.search": "allow",
    "file.read": "allow",
    "web.fetch": "deny",
    "web.research": "deny",
    "shell": "deny",
  }
}
```

**Built-in capability IDs:** `file.read`, `file.write`, `file.search`, `image.attach`, `image.generate`, `web.fetch`, `web.search`, `web.research`, `shell`, `mcp`

**Built-in tool names:** `read`, `edit`, `write`, `apply_patch`, `fd`, `rg`, `find`, `grep`, `ls`, `exec_command`, `write_stdin`, `python_exec`, `web_fetch`, `web_search`, `web_research`, `extract_file_context`, `attach_image`, `image_generation`, `mcp_inspect`, `mcp_call`

The `exec_command` tool runs through the platform shell, defaulting to PowerShell on Windows and Bash elsewhere. It returns output immediately when the command exits, or a `session_id` when the command is still running after `yield_time_ms`; the default wait is 30,000 ms (30 seconds). Use `write_stdin` with that `session_id` to poll for more output or send interactive input, and `/ps` to inspect all command sessions owned by the current live runtime. `write_stdin` polls can wait up to 3,600,000 ms (1 hour). Results include `exit_code`/`returncode`, `running`, `wall_time_seconds`, combined `output`, and `outputTruncationDetails`. On Windows, bash-style Python heredocs such as `python - <<'PY'` are rewritten to PowerShell pipelines while preserving stdin through yoke's `python`/`python3` shims. Native PowerShell pipelines use UTF-8 without a BOM so rewritten Python stdin starts at the first script character.

The `python_exec` tool uses yoke's current interpreter by default, preferring the parent shell's active `VIRTUAL_ENV` or `CONDA_PREFIX`. Pass `python_executable` to run a single call with a specific interpreter, for example a worktree-local `.venv` Python. Child subprocesses launched by that code inherit `YOKE_PYTHON_EXECUTABLE`; use that environment variable or `sys.executable` when a nested process must use the same interpreter. It waits 30 seconds by default, then returns a session ID for code that is still running. Use `write_stdin` with that session ID to poll incremental unbuffered output.

`skill` is added when yoke discovers one or more skill directories.

---

## Workspace root

By default yoke uses the current directory as the workspace root — all file tools operate relative to it.

```bash
yoke --root /path/to/project "..."
```
