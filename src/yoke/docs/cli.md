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
yoke --model codex:gpt-5.4-mini "..."
yoke --model opencode-go:kimi-k2.6 "..."
yoke --model opencode-go:minimax-m3 "Review this repository and suggest refactors"
```

**Built-in providers**

| Provider | Auth |
|----------|------|
| `codex` | `~/.codex/auth.json` with account-vault selection from `~/.codex-auth/accounts` |
| `codex-websockets` | Same Codex auth as `codex`, using the Responses WebSocket transport |
| `copilot` | `~/.yoke/auth.json` or `YOKE_COPILOT_AUTH_PATH` |
| `opencode-go` | `OPENCODE_API_KEY` env var |
| `zai` | `ZAI_API_KEY` env var |

If you omit the provider prefix and pass only `--model model-name`, yoke detects
the provider from available credentials.

Codex first tries the best usable account under `~/.codex-auth/accounts`. If no
account there works, it falls back to `~/.codex/auth.json`. If that token is
missing, expired, or later rejected by the API, yoke refreshes or re-prompts
login against `~/.codex/auth.json`.

Use `--model codex-websockets:gpt-5.4` to opt into Codex's persistent Responses
WebSocket transport. It uses the same auth and account selection as `codex` and
accepts `YOKE_CODEX_WEBSOCKETS_*` overrides for model, base URL, timeout,
retries, reasoning effort, text verbosity, and logs.

The WebSocket transport disables library-level idle keepalive pings by default,
which avoids background ping timeouts while yoke is waiting for your next prompt.
Set `YOKE_CODEX_WEBSOCKETS_PING_INTERVAL_SECONDS` and optionally
`YOKE_CODEX_WEBSOCKETS_PING_TIMEOUT_SECONDS` to enable explicit keepalive pings.

Outside a session you can inspect and configure models directly:

```bash
yoke models list
yoke models set codex:gpt-5.4-mini
yoke models set zai:glm-5.1
yoke models set codex:gpt-5.4-mini --reasoning-effort high
yoke models set
yoke models set --repo
```

`yoke models list` includes each model's advertised image-input support. For
providers such as `opencode-go`, this is model-specific rather than a single
provider-wide guarantee.

If you omit the model argument from `yoke models set`, yoke opens an interactive
selector when running in a TTY and otherwise falls back to a numbered prompt.
By default `yoke models set` writes to `~/.yoke/config.json`; use `--repo` to write
to `.yoke/config.json` in the current workspace instead. This sets the default
model for future new sessions. You can also persist a default reasoning effort
with `--reasoning-effort`.

You can also set a config default in `~/.yoke/config.json` or `.yoke/config.json`:

```json
{
  "default_model": "codex:gpt-5.4-mini",
  "default_reasoning_effort": "high"
}
```

`default_model` is only used when you do not pass `--model`.
`default_reasoning_effort` is only used when you do not pass
`--reasoning-effort`.
An explicit CLI flag wins, and `yoke resume` still prefers the last provider/model
saved in that session.

Image input support depends on the selected provider and model. If you
attach an image while using a provider that does not support image inputs, yoke
will stop the turn with an error instead of sending an invalid request.

---

## Images In Interactive Mode

In prompt-toolkit mode, yoke can keep pending image attachments for the next user
turn.

- Press `Ctrl+V` to attach an image from the clipboard when one is available.
- Press `Ctrl+U` to remove the last pending image attachment.
- Press `Ctrl+O` to open the fullscreen tool inspector. It shows complete
  tool call arguments, executed arguments, results, status, and duration.
- Press `Ctrl+Q` or run `/queue` to open the fullscreen queue manager. It can
  edit, delete, promote, reorder, pause, or mark pending prompts as steering.
- Press `Enter` to steer/send immediately while a turn is running.
- Press `Tab` to queue the prompt behind the current turn. Queued prompts and
  pending image attachments are persisted in a per-session sidecar and restored
  on resume/restart.
- While slash-command completions are open, use `Up`/`Down` to move between
  options; `Left`/`Right` keep moving the cursor in the prompt text.
- Press `Esc Esc` to stop the current turn; yoke waits for the turn to record
  the user prompt, completed/cancelled tool calls, and interruption marker
  before processing queued prompts or saving the session.
- Press `Ctrl+J` to insert a newline.
- Pasting multiline text keeps the entire paste in the current prompt; press
  `Enter` after the paste to submit it.
- Dragging a local image file into the terminal on macOS usually inserts an
  escaped path. If that path is on its own prompt line, yoke attaches it
  automatically when you submit.
- Use `/image path/to/file.png` to attach a local image file explicitly.
- Use `/tree` to navigate the current session tree, fork from an older point,
  label entries, search/filter history, and optionally summarize the branch you
  are leaving.
- Use `/title new-title` to rename the active session shown in resume/session
  lists and on the right side of the prompt-toolkit bottom toolbar.
- Use `/shortcuts` or `?` to print the interactive keyboard shortcuts in scrollback.

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
```

Sessions are stored under `~/.yoke/sessions/` as append-oriented `.jsonl` event
streams and auto-expire after 30 days. The CLI owns session files, indexes,
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

In a terminal, `yoke resume` opens a keyboard-driven selector with aligned
columns for the session title, last activity, and session id. Use `Up`/`Down`
or `j`/`k` to move, `PgUp`/`PgDn` to scroll faster, `Home`/`End` to jump, and
`Enter` to resume. Press `q` or `Esc` to cancel. Pass `--all` to list saved
sessions across every workspace root instead of only the current root; that
view adds a root-path column before the session id.

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

Yoke ships with a built-in `create-skill` skill in the codebase under
`yoke/agent/skills/built_in/create-skill/SKILL.md`. It instructs the
agent to ask where the skill should be created first (repo-local, global, or
custom location), then scaffold it with `yoke skills init`.

---

## Adding extra tools

Place Python files in `.yoke/` (workspace) or `~/.yoke/` (global) and yoke will load your tools automatically alongside the built-ins.

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

If you need runtime configuration (credentials, feature flags, …) return tools from a `register_tools` function. When this function is present, yoke uses it instead of scanning for decorated classes.

```python
# .yoke/tools.py
def register_tools(context):
    api_key = context.get("env", {}).get("MY_API_KEY")
    return [MyApiTool.bind(root=context.root, api_key=api_key)]
```

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

Control which tools the agent can use via `config.json`.

Use `yoke tools list` to inspect the current tool inventory and surface tool-loading or config problems. It exits non-zero when discovery fails and warns about unmatched tool policy patterns.

Use `/tools` in interactive mode to toggle tools. After selecting tools, yoke asks whether to apply the change only to the current session, persist it to this workspace root's `.yoke/config.json`, or persist it globally to `~/.yoke/config.json`.

- Built-in defaults — applied even when no config file exists
- `~/.yoke/config.json` — global policy
- `.yoke/config.json` — workspace policy (workspace overrides global and defaults)

The effective precedence order is: built-in defaults, then `~/.yoke/config.json`, then `.yoke/config.json`.

By default yoke starts from this curated tool baseline:

```json
{
  "tools": {
    "*": "deny",
    "apply_patch": "allow",
    "extract_file_context": "allow",
    "find": "allow",
    "grep": "allow",
    "ls": "allow",
    "read": "allow",
    "web_research": "allow"
  }
}
```

```json
{
  "tools": {
    "command": "deny",
    "web_fetch": "deny"
  }
}
```

Values are `"allow"` or `"deny"`. Patterns use glob syntax (`*`, `?`). Tools not listed follow the effective merged policy; under the built-in defaults, unspecified tools are denied unless a global or workspace config allows them.

If a `config.json`, tool plugin, or skill file is malformed, yoke now reports the file path and a short plain-English reason such as invalid JSON syntax, missing `SKILL.md` frontmatter, or a plugin import failure.

**Example: read-only agent**

```json
{
  "tools": {
    "*": "deny",
    "read": "allow",
    "grep": "allow",
    "find": "allow",
    "ls": "allow"
  }
}
```

**Always-available built-in tool names:** `read`, `edit`, `apply_patch`, `ls`, `find`, `grep`, `command`, `web_fetch`, `web_research`, `extract_file_context`, `attach_image`

The `command`/`bash` tool result mirrors `python_exec` metadata for the agent: `python_executable`, `returncode`, `timeout`, `timed_out`, `elapsed_seconds`, combined `output`, and `outputTruncationDetails`.

`skill` is added when yoke discovers one or more skill directories.

---

## Workspace root

By default yoke uses the current directory as the workspace root — all file tools operate relative to it.

```bash
yoke --root /path/to/project "..."
```

---

## Environment variables

| Variable | Description |
|----------|-------------|
| `YOKE_COPILOT_AUTH_PATH` | Override GitHub Copilot auth JSON path |
| `OPENCODE_API_KEY` | OpenCode Go API key |
| `ZAI_API_KEY` | Z.ai API key |
| `YOKE_SESSION_DIR` | Override session storage directory |
| `YOKE_ZSH` | Override zsh used by the command tool |
