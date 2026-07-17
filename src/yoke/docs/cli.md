# CLI

At startup, yoke automatically loads environment variables from a `.env` file
located next to the yoke source package at `src/yoke/.env`, if present.

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

---

## Providers and models

```bash
yoke --model codex:gpt-5.6-sol "..."
yoke --model codex:gpt-5.6-terra "..."
yoke --model codex:gpt-5.6-luna "..."
yoke --model codex:gpt-5.4-mini "..."
yoke --model opencode-go:glm-5.2 "..."
yoke --model opencode-go:kimi-k2.7-code "..."
yoke --model opencode-go:deepseek-v4-pro "Review this repository and suggest refactors"
```

**Built-in providers**

| Provider | Auth |
|----------|------|
| `codex` | WebSocket Codex Responses transport using `~/.codex/auth.json`, account-vault selection from `~/.codex-auth/accounts`, or `YOKE_CODEX_API_KEY` for proxy/API-key auth |
| `opencode-go` | `OPENCODE_API_KEY` env var |
| `zai` | `ZAI_API_KEY` env var |

Run `yoke login codex` (or `yoke providers login codex`) to complete Codex
OAuth explicitly. For `opencode-go` and `zai`, the same command prompts for an
API key and saves it in the private user credential store at
`~/.yoke/providers/credentials.json`. Process environment variables take
precedence over values in that file.

If you omit the provider prefix and pass only `--model model-name`, yoke detects
the provider from available credentials.

Codex uses the persistent Responses WebSocket transport. By default it connects
to ChatGPT's Codex backend. Set `YOKE_CODEX_DOMAIN` to a proxy origin, such as
`https://codexlb.dakixr.dev`, to append `/backend-api` automatically and route
Codex through that proxy. Set `YOKE_CODEX_API_KEY` when the proxy uses bearer
API-key auth instead of local Codex OAuth.

Codex prompt-cache affinity follows the yoke session id. Reconstructing the
provider or resuming the same saved session therefore reuses its cache key,
while `/new`, `/fork`, and newly created sessions use a distinct key. Direct SDK
provider instances without a session id retain one generated key for that
provider instance.

Provider model catalogs can attach model-specific system messages. Yoke sends
those messages only for the active `provider:model` and refreshes them when a
session switches models. Custom provider plugins can do this by returning
`ProviderModelInfo(system_messages=(Message.system(...),))` from
`list_provider_models(context)`, or by implementing
`current_model_system_messages()` on the provider object.

Codex first tries the best usable account under `~/.codex-auth/accounts`. If
quota probing is temporarily unavailable, yoke can still use a locally fresh
account token from that vault instead of falling back immediately. If no account
there works, it falls back to `~/.codex/auth.json`. If that fallback token is
missing, expired, or later rejected by the API, yoke refreshes or re-prompts
login against `~/.codex/auth.json`.

The Codex provider accepts `YOKE_CODEX_*` overrides for model, domain/base URL,
timeout, retries, reasoning effort, text verbosity, logs, and optional API key.
Its GPT-5.6 catalog includes the official tier slugs `gpt-5.6-sol`,
`gpt-5.6-terra`, and `gpt-5.6-luna`. Visibility for each tier can depend on
plan, rollout, and workspace settings. The broader API `gpt-5.6` Sol alias is
not advertised here because the Codex ChatGPT transport rejects that alias.
Yoke intentionally budgets each GPT-5.6 tier at 400,000 context tokens, even
when the backend advertises a larger window, so compaction begins without using
the full one-million-token family window.
`gpt-5.6-luna` uses the backend's Responses Lite contract. Yoke marks each Luna
WebSocket request with the required per-request routing metadata and applies the
matching reasoning-context and tool-parallelism settings; the streaming HTTP
transport sends the equivalent Responses Lite header. Unless
`YOKE_CODEX_ORIGINATOR` explicitly selects a custom originator, Luna requests
also use the backend-recognized `codex_cli_rs` originator instead of Yoke's
normal `yoke` originator.

When resuming sessions, Codex request history is normalized to omit orphaned or
partially saved tool outputs before sending the next request. This prevents
Responses API errors about `function_call_output` entries whose function call is
no longer present in the active conversation branch.

The WebSocket transport disables library-level idle keepalive pings by default,
which avoids background ping timeouts while yoke is waiting for your next prompt.
Set `YOKE_CODEX_WEBSOCKETS_PING_INTERVAL_SECONDS` and optionally
`YOKE_CODEX_WEBSOCKETS_PING_TIMEOUT_SECONDS` to enable explicit keepalive pings.
`YOKE_CODEX_WEBSOCKETS_TIMEOUT_SECONDS` limits how long a response may produce no
events; active response streams reset this inactivity timeout. The default is
300 seconds, matching Codex CLI's stream idle timeout. A timed-out socket is
closed and retried according to `YOKE_CODEX_WEBSOCKETS_MAX_RETRIES`.

Outside a session you can inspect and configure models directly:

```bash
yoke models list
yoke models set codex:gpt-5.6-terra --reasoning-effort max
yoke models set codex:gpt-5.4-mini
yoke models set opencode-go:glm-5.2
yoke models set zai:glm-5.2
yoke models set codex:gpt-5.4-mini --reasoning-effort high
yoke models set
yoke models set --repo
```

`yoke models list` includes each model's advertised image-input support. For
providers such as `opencode-go`, this is model-specific rather than a single
provider-wide guarantee. The Thinking column reports selectable controls only:
for example, Z.ai GLM models expose `none` and `thinking`, which yoke maps to
Z.ai's documented `thinking.type` disabled/enabled request field. When thinking
is enabled, yoke sends `thinking.clear_thinking: true` and does not replay prior
`reasoning_content`, avoiding stale hidden reasoning after compaction or
transcript transforms.

Z.ai and OpenCode Go chat-completions models use standard OpenAI-compatible
tool-call history: assistant `tool_calls` are followed by `tool` messages with
matching `tool_call_id` values. Z.ai GLM models and some OpenCode Go models,
such as `kimi-k2.7-code`, can return intermediate `reasoning_content`; yoke
parses it from the response and preserves it on the assistant message. That
text is also used as fallback output if the visible response content is empty.

The Z.ai (`zai`) provider streams every chat-completion request via
Server-Sent Events so it can detect unresponsive servers quickly: an
idle-read-timeout (default `60s`) fires once the server stops sending
chunks for too long, which triggers an immediate retry with exponential
backoff. This is far faster than the previous non-streaming path, which
had no timeout at all and could hang indefinitely on a stalled server.
Each request also opens a fresh HTTP connection to avoid stale
keep-alive sockets.

OpenCode Go currently exposes maintained OpenAI-compatible models in yoke's
built-in catalog, including the image-capable `grok-4.5` with its 500K context
window. Deprecated OpenCode Go model entries such as GLM 5/5.1, Kimi K2.5/2.6,
MiMo, MiniMax, and Qwen have been removed from the selectable inventory.

If you omit the model argument from `yoke models set`, yoke opens an interactive
selector when running in a TTY and otherwise falls back to a numbered prompt.
By default `yoke models set` writes to `~/.yoke/config.json`; use `--repo` to write
to `.yoke/config.json` in the current workspace instead. This sets the default
model for future new sessions. You can also persist a default reasoning effort
with `--reasoning-effort`.

You can also set a config default in `~/.yoke/config.json` or `.yoke/config.json`:

```json
{
  "default_model": "codex:gpt-5.6-terra",
  "default_reasoning_effort": "max",
  "title_model": "codex:gpt-5.4-mini:medium"
}
```

`default_model` is only used when you do not pass `--model`.
`default_reasoning_effort` is only used when you do not pass
`--reasoning-effort`.
`title_model` controls the low-cost model used to generate new session titles.
It uses `provider:model:reasoning-effort`, defaults to
`codex:gpt-5.4-mini:medium`, and does not change the provider/model used for
conversation turns.
An explicit CLI flag wins, and `yoke resume` still prefers the last provider/model
saved in that session. If the saved provider is no longer available, resume falls
back to the configured default model (or normal automatic provider selection when
no default model is configured).

Image input support depends on the selected provider and model. If you
attach an image while using a provider that does not support image inputs, yoke
will stop the turn with an error instead of sending an invalid request.

Attached images are encoded as base64 data URLs and embedded directly in the
session data at attachment time. This means conversations stay intact even if
the original file on disk is later renamed, moved, or deleted. When an image is
attached through the `attach_image` or `image_generation` tools, yoke keeps the
embedded image in the appended multimodal message but strips the internal
handoff payload from the model-visible tool-result JSON so follow-up turns do
not also replay the same image as raw base64 text.

---

## Images In Interactive Mode

In prompt-toolkit mode, yoke can keep pending image attachments for the next user
turn.

- Press `Ctrl+V` to attach an image from the clipboard when one is available.
- Press `Ctrl+U` to remove the last pending image attachment.
- Press `Ctrl+O` to open the fullscreen tool inspector. It shows complete
  tool call arguments, executed arguments, results, status, and duration.
- The tool inspector updates while it is open, supports mouse click/scroll, and
  shows streamed output from `exec_command` and `python_exec` while commands run.
- The tool inspector escapes and sanitizes dynamic tool output before applying
  prompt-toolkit styling so markup-like text or control characters cannot break
  fullscreen rendering.
- While a fullscreen menu is open, live turn output is deferred and replayed
  after the menu closes so background tool updates do not overwrite the view.
- Press `Ctrl+Q` or run `/queue` to open the fullscreen queue manager. It can
  edit, delete, promote, reorder, pause, or mark pending prompts as steering.
- Press `Enter` to steer/send immediately while a turn is running.
- Press `Ctrl+X` then `M` or run `/model` to open the fullscreen model switcher.
- Press `Ctrl+X` then `T` or run `/tree` to open the session tree.
- Steering immediately retires the active generation and dispatches its
  replacement without waiting for the old provider request or tool to exit.
  Retired renderers and workers are generation-fenced, so they cannot publish
  output or replace the accepted conversation, provider, or skill state.
- The handoff target is under 100 ms. This is a logical cancellation guarantee,
  not a claim that every remote request or kernel process has physically exited:
  provider aborts, process TERM/KILL escalation, and resource cleanup continue
  asynchronously after the replacement turn starts.
- Codex WebSocket follow-up requests only reuse `previous_response_id` while the
  same Codex account profile remains selected; if account rotation changes the
  profile, yoke resends full context for that turn. If Codex reports that the
  previous response anchor is stale or missing, yoke automatically retries the
  same turn without `previous_response_id`, sending full context.
- Local tools run under cancellation supervision. Process-isolated tools receive
  TERM/KILL cleanup off the handoff path; tools that require in-process resources
  run in a supervised daemon thread and are logically detached if they do not
  cooperate. Detached tools retain their isolated resources until they return.
- MCP stdio and HTTP/SSE calls receive the same cancellation signal. Image
  generation publishes through an atomic temporary-file replacement and drops
  the pending output when cancellation is observed before publication.
- Press `Tab` to queue the prompt behind the current turn. Queued prompts and
  pending image attachments are persisted in a per-session sidecar and restored
  on resume/restart. Once a queued or steering prompt starts, yoke removes it
  from that sidecar so it does not reappear after a crash or restart.
- While slash-command completions are open, use `Up`/`Down` to move between
  options; `Left`/`Right` keep moving the cursor in the prompt text.
- Press `Esc Esc` to stop the current turn. Yoke immediately records a synthetic
  interrupted checkpoint containing the user prompt and interruption marker;
  the retired worker cannot append a late model response or tool result.
- Session state is persisted from the accepted turn outcome rather than from
  isolated mid-turn workers, preventing a retired generation from overwriting a
  newer steering result.
- `Shift+Tab` cycles only through the active model's advertised thinking
  levels. Models without advertised levels leave thinking effort at the default.
- OpenCode Go chat-completions requests include a high output-token cap so
  large tool calls are less likely to be truncated by provider defaults.
- Persisted provider reasoning effort is normalized on resume; provider configs
  accept saved effort values even when a model catalog omits explicit thinking
  levels.
- Press `Ctrl+J` or `Shift+Enter` to insert a newline when supported by the terminal.
- Press `Esc` then `Enter` to insert a newline when `Shift+Enter` is unavailable.
- Pasting multiline text keeps the entire paste in the current prompt; press
  `Enter` after the paste to submit it.
- Dragging a local image file into the terminal on macOS usually inserts an
  escaped path. If that path is on its own prompt line, yoke attaches it
  automatically when you submit; non-image text lines are left unchanged.
- Use `/image path/to/file.png` to attach a local image file explicitly.
- Use `/info` to print the current session id, title, root, session file path,
  provider/model, and saved conversation counts.
- Use `/fork` to copy the current saved session into a new persisted session and
  continue future turns in that fork.
- Use `/tree` to navigate the current session tree, fork from an older point,
  label entries, search/filter history, and optionally summarize the branch you
  are leaving.
- Use `/title new-title` to rename the active session shown in resume/session
  lists and on the right side of the prompt-toolkit bottom toolbar.
- Use `/shortcuts` or `?` to print the interactive keyboard shortcuts in scrollback.
- Use `/ps` to list background command sessions and `/stop [session-id]` to
  stop one session. `/stop` without an ID stops all background commands.

Commands that replace, branch, or persist mutable session state are rejected
while a turn is active. This includes `/new`, `/fork`, `/tree`, `/model`,
`/tools`, `/mcp`, `/compact`, `/skill`, `/title`, `/pin-session`, and
`/unpin-session`; stop the turn or let it finish before running them.

Pending image attachments are shown in the bottom toolbar and are sent with the
next submitted prompt.

Use `/model` in interactive mode to open a fullscreen table of advertised models
across providers and switch to the selected row.
Context budgeting follows the selected model's advertised window, and yoke may
refuse a switch with a compact-first note when the current conversation no
longer fits in the target model.
When providers report token usage, yoke stores normalized input, output,
reasoning, cached-input, and total token counts on the assistant response for
session diagnostics and future budgeting improvements.
Compaction decisions ignore provider-reported input counts from before the most
recent memory snapshot, so stale oversized usage from an earlier turn cannot
repeatedly trigger compaction after history has already been summarized.
The manual `/compact` command uses the same runtime compaction operation as
automatic threshold and overflow compaction, then updates both the saved session
and live in-memory agent state before the next turn.
If a provider still rejects a request as too large because its backend limit is
lower than the advertised metadata, yoke treats that as an overflow signal,
compacts older history, and retries the newest user turn once.

---

## Status bar

The prompt-toolkit bottom toolbar shows live session state with styled
fragments and a shared color palette (cyan accent, amber for warnings, red
for errors, dim gray for secondary info).

**When idle** the toolbar shows: `model · context gauge · root · session title`.

**When a turn is active** the toolbar shows:
- Spinner + phase status (`Thinking`, `Streaming`, `Running tool`, `Compacting`, `Recovering`)
- Elapsed time (`12s`)
- Tool count (`3 tools`)
- Context gauge: `% left` tinted by pressure (cyan < 70%, amber < 90%, red near auto-compact)
- Queue summary (steering/queued prompt counts)
- Model · root label · session title (right-aligned)

**Per-turn summary**: when a turn takes over 60 seconds, yoke emits a dim
summary line in scrollback on completion: `Worked for 1m23s · 2 tools`.

**Configurable segments**: set environment variables to hide individual
segments:

| Variable | Default | Hides |
|----------|---------|-------|
| `YOKE_BAR_TIMER` | on | Turn elapsed timer |
| `YOKE_BAR_TOKENS` | off | Token counts (set to `1` to show `↓in ↑out ⚡reasoning` and absolute gauge tokens) |
| `YOKE_BAR_GAUGE` | on | Context gauge bar |
| `YOKE_BAR_TOOLS` | on | Tool count |
| `YOKE_BAR_TURN` | off | Turn number (set to `1` to show) |

Set any to `0` or `false` to hide that segment.

---

## Sessions

Sessions save your conversation so you can pick up where you left off.

```bash
# Start or continue a named session
yoke --session my-project "let's keep working on the auth module"

# Resume interactively (pick from a list)
yoke resume

# Print saved sessions without resuming
yoke resume list

# Print saved sessions across all roots
yoke resume list --all

# Resume interactively across all roots
yoke resume --all

# Resume a specific session by id
yoke resume 20240421-143022-abc1

# Resume a session id that matches a reserved resume action
yoke resume --session-id list

# Pin the active session from inside the TUI
/pin-session

# Continue the most recent session for this directory
yoke continue

# Continue the most recent session across all directories
yoke continue --global

# Fork an existing session id and continue in the new session
yoke continue --fork 20240421-143022-abc1

# Start directly from a forked session
yoke --fork 20240421-143022-abc1
```

Sessions are stored under `~/.yoke/sessions/` as append-oriented `.jsonl` event
streams and auto-expire after 30 days. If the final event is truncated by an
interrupted write, resume ignores that partial event and recovers the earlier
complete events. The CLI owns session files, indexes,
ids, and resume selection; the stored agent state uses structured conversation
entries so memory snapshots, typed compaction handoffs, and branched session
trees can be restored without flattening to transcript text. Older `.json`
sessions are migrated automatically at startup, and older linear sessions are
migrated on load by assigning entry ids, parent links, timestamps, and an active
leaf.

`/tree` is available in the prompt-toolkit TUI. It opens a fullscreen navigator
over the session entries. Selecting a user entry rewinds to that entry's parent
and puts the selected user text back in the editor, so submitting it creates a
new branch. Selecting an assistant, tool, compaction, or summary entry continues
after that entry. Navigation never deletes abandoned history; future turns are
built only from the active branch. The selector supports search, filter cycling,
local folding, color-coded entry types, and entry labels stored as metadata.
Before moving branches, yoke asks whether to create a branch summary; `No
summary` is the default, while
custom summary guidance is appended to the standard summary prompt when chosen.
If the summary provider request fails, navigation still completes without a
summary and reports the provider error in scrollback.

In a terminal, `yoke resume` opens a keyboard-driven selector with aligned
columns for the session title, last activity, and session id. Use `Up`/`Down`
or `j`/`k` to move, `PgUp`/`PgDn` to scroll faster, `Home`/`End` to jump,
`p` to pin or unpin the selected session, and `Enter` to resume. Press `q` or
`Esc` to cancel. Pinned sessions sort first in `yoke resume` and
`yoke resume list` and display with a `★` before the title. Pass `--all` to list
saved sessions across every workspace root instead of only the current root;
that view adds a root-path column before the session id. Use `yoke continue` to
skip selection and immediately resume the most recent session for the current
root, or `yoke continue --global` / `yoke continue -g` to ignore root and
continue the most recent saved session overall.
Use `yoke resume list` to print matching sessions without opening the selector
or resuming one; add `--all` to include every root. If a session id collides
with a reserved resume action such as `list`, resume it with
`yoke resume --session-id list`.
Inside a session, `/pin-session` pins the active session and `/unpin-session`
removes the pin.
Use `--fork <session-id>` to copy an existing session into a new session id and
continue there without appending to the original; `--fork` cannot be combined
with `--session` because one selects a source session and the other names the
active destination.

---

## Skills

A skill is a Markdown file that tells the agent how to approach a type of task. You create skills once and activate them by name.

### Creating a skill

Create a directory with a `SKILL.md` file. The directory name must match the skill name:

```
my-skills/
└── code-review/
    ├── SKILL.md
    └── reference.md
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
When a skill is loaded, yoke tells the agent the absolute path of every file in
that skill directory, so skills may include reference files, examples, or
templates alongside `SKILL.md`.

Yoke also ships with a built-in `create-skill` skill that helps the agent create
new skills correctly. It tells the agent to ask where the skill should be
created first (repo-local, global, or custom directory) and then use
`yoke skills init` to scaffold it.

### Using skills from the CLI

```bash
# Activate a skill at startup
yoke --skill code-review "review the changes in src/auth.py"

# Point yoke at a custom skills directory
# (done via .yoke/skills/ or ~/.yoke/skills/ — see below)
```

### Skill directories

Yoke auto-discovers skills from:
- built-in yoke skills under `yoke/agent/skills/built_in/`
- `~/.yoke/skills/` — your personal skills, available in every project
- `.yoke/skills/` — skills for the current repo

Place skill folders inside these directories and they'll be available by name.

During a session the agent can also activate skills itself when the `skill`
tool is available. Manual activation with `/skill <name>` and model activation
through the `skill` tool use the same activation semantics: existing skills are
not duplicated, reloading marks the skill to send its canonical instructions on
the next model call, and active skills are preserved when additional skills are
loaded.

Activated skills store an instruction snapshot in the session. Resuming a
session therefore does not depend on the original skill directory still being
present: yoke refreshes a saved skill from the current registry when possible,
falls back to its snapshot after a move or deletion, and safely removes legacy
skill state that has neither a current source nor a snapshot. Invalid or
half-deleted skill directories are isolated during normal runtime startup and
reported in the tool discovery summary instead of preventing resume. Use
`yoke skills list` for strict validation and the full repairable error.

You can also activate a skill and send the next prompt in one line by placing
the prompt after the skill name. A semicolon separator is also supported:

```text
/skill create-skill create a repo-local skill called docs-review
/skill create-skill ; create a repo-local skill called docs-review
```

Yoke ships with a built-in `create-skill` skill in the codebase under
`yoke/agent/skills/built_in/create-skill/SKILL.md`. It instructs the
agent to ask where the skill should be created first (repo-local, global, or
custom location), then scaffold it with `yoke skills init`.

---

## Adding extra tools

Place Python files in `.yoke/tools/` (workspace) or `~/.yoke/tools/` (global) and yoke will load your tools automatically alongside the built-ins.

Tool and provider plugins are trusted Python code: importing them can execute
arbitrary code with your user account's permissions. Use repo-local plugins only
in repositories you trust, and review unfamiliar `.yoke/tools/` files before
starting yoke there.

There are three ways to define tools in these files.

### `@function_tool` — quickest option

Decorate a typed function and yoke turns it into a tool. The function name becomes the tool name, the docstring becomes the description, and every parameter becomes an argument the agent can pass.

```python
# .yoke/tools/tools.py
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
# .yoke/tools/tools.py
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

If you need runtime configuration (credentials, feature flags, …) return tools from a `register_tools` function. When this function is present, yoke uses it instead of scanning for decorated classes.

```python
# .yoke/tools/tools.py
from yoke.agent.models import Message
from yoke.agent.tools import ToolRegistrationResult


def register_tools(context):
    if context.model_key == "opencode-go:kimi-k2.7-code":
        return ToolRegistrationResult(
            tools=[KimiWriteTool.bind(root=context.root)],
            system_messages=[
                Message.system("Follow the Kimi write-tool instructions.")
            ],
        )
    return [SimpleEditTool.bind(root=context.root)]
```

The registration context is the same public `ToolRegistrationContext` used by
the SDK. It exposes the current raw `provider` plus stable `provider_name`,
`model_id`/`model_name`, `model_key`, and `reasoning_effort` strings. Tools can
access the corresponding execution-time values through `self.context`.
Changing model or provider re-runs registration so model-specific schemas stay
current. Returning `ToolRegistrationResult` also lets the registration
contribute tool-use system messages. Those messages are active only while at
least one tool from that registration remains enabled, and they are replaced on
re-registration. Returning a plain iterable of tools remains supported.
General provider/model steering should live on provider model metadata instead
of tool registration.

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

`web_fetch` saves each complete, converted page under `~/.yoke/tool-output/` and
returns its absolute `path`. Use `read`, `rg`, or shell commands on that path when
the model-facing result is truncated. At agent-session startup, Yoke deletes
outputs older than seven days.

---

## Tool policy

Control which tools the agent can use via `config.json`.

Use `yoke tools list` to inspect the current tool inventory and surface tool-loading or config problems. It exits non-zero when discovery fails and warns about unmatched exact-name tool policy entries.

Use `/tools` in interactive mode to toggle tools. After selecting tools, yoke asks whether to apply the change only to the current session, persist it to this workspace root's `.yoke/config.json`, or persist it globally to `~/.yoke/config.json`.

- `~/.yoke/config.json` — global policy
- `.yoke/config.json` — workspace policy (workspace overrides global)

The effective precedence order is: discovered tools are enabled by default,
then `~/.yoke/config.json`, then `.yoke/config.json`.

By default, discovered built-in, repo, and global tools are enabled. Add exact
tool-name entries to disable tools:

```json
{
  "tools": {
    "command_execution": "deny",
    "web_fetch": "deny"
  }
}
```

Use `"allow"` only to override a deny from a lower-priority config:

```json
{
  "tools": {
    "web_fetch": "allow"
  }
}
```

Values are `"allow"` or `"deny"`. Built-ins are keyed by exact capability name
such as `file.edit`, `file.search`, `command_execution`, or `web`; repo and
global custom tools are keyed by exact tool name. Targets not listed are enabled
by default after discovery. Use `"deny"` to disable a built-in capability,
repo tool, or global tool. Use `"allow"` only to override a deny from a
lower-priority config.

If a `config.json`, tool plugin, or skill file is malformed, the relevant
inspection command reports the file path and a short plain-English reason such
as invalid JSON syntax, missing `SKILL.md` frontmatter, or a plugin import
failure. Runtime skill discovery isolates malformed entries so they cannot
block session startup or resume.

**Example: read-only agent**

```json
{
  "tools": {
    "command_execution": "deny",
    "file.edit": "deny"
  }
}
```

**Built-in capability names:** `file.read`, `file.context`, `file.search`,
`file.edit`, `command_execution`, `web`, `image.input`, and
`image.generation`.
The writing capability is model-aware: model IDs containing `gpt` receive
`apply_patch`; all other models receive `edit` and `write`. `attach_image` is also model-aware and is only registered
when the active model advertises image input support. `image_generation` is only
registered for Codex-backed providers and saves/attaches generated PNG files;
it can also use `referenced_image_paths` or `num_last_images_to_include` for
image-edit/reference workflows. Codex image requests use the hosted Responses
`image_generation` tool rather than the removed direct `/images/*` subscription
endpoints. Search is environment-aware:
when ripgrep is installed, only `rg` is active; otherwise `grep`, `find`, and
`ls` are active as the fallback set.

Built-in writing instructions live in the active tool definitions. Yoke does
not also add them to the system prompt, avoiding duplicate tool guidance in the
model context. Custom tool registrations can still contribute separate system
messages when their instructions do not belong in a tool description.

Every model receives `exec_command` and `write_stdin`; command registration is
not provider-specific. Command results include `session_id`, `exit_code`,
`chunk_id`, `wall_time_seconds`, `original_token_count`, combined `output`, and
`outputTruncationDetails`. A non-null `session_id` means the command is still
running. Process-isolated tool failures report negative exit statuses as
terminating signals, for example status `-11` is `SIGSEGV`.
On POSIX systems the command tool prefers `YOKE_SHELL`, then a usable `$SHELL`,
then `zsh`, `bash`, or `sh`. `YOKE_ZSH` remains a compatibility alias. Zsh gets
the zsh profile wrapper, POSIX-family shells get a portable wrapper, and other
explicit shells receive the command directly instead of zsh syntax.

MCP text results use the same line/byte bounds as other tool output. When a
result exceeds those bounds, the response includes a truncated preview and a
`full_output_path` in a private, randomized temporary directory; files are
removed when the process exits. Oversized or non-JSON `structuredContent` is
omitted and marked with `structuredContentTruncated`.

`skill` is added when yoke discovers one or more skill directories.

---

## Workspace root

By default yoke uses the current directory as the workspace root, and relative
file-tool paths resolve from it. The root is a working-directory anchor, not a
sandbox: explicit absolute paths and relative paths containing `..` may access
locations outside the root. Use tool policy or an OS-level sandbox when paths
must be confined.

```bash
yoke --root /path/to/project "..."
```

---

## Environment variables

| Variable | Description |
|----------|-------------|
| `YOKE_CODEX_API_KEY` | Optional bearer token for a Codex-compatible proxy such as CodexLB |
| `YOKE_CODEX_DOMAIN` | Optional Codex-compatible proxy origin; yoke appends `/backend-api` |
| `OPENCODE_API_KEY` | OpenCode Go API key |
| `ZAI_API_KEY` | Z.ai API key |
| `YOKE_SESSION_DIR` | Override session storage directory |
| `YOKE_SHELL` | Override the shell used by the command tool |
| `YOKE_ZSH` | Legacy alias for `YOKE_SHELL` |
| `YOKE_BAR_TIMER` | Set to `0` to hide the turn elapsed timer in the toolbar |
| `YOKE_BAR_TOKENS` | Set to `1` to show token counts in the toolbar (off by default) |
| `YOKE_BAR_GAUGE` | Set to `0` to hide the context gauge bar in the toolbar |
| `YOKE_BAR_TOOLS` | Set to `0` to hide the tool count in the toolbar |
| `YOKE_BAR_TURN` | Set to `1` to show the turn number in the toolbar (off by default) |
