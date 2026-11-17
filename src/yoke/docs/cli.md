# CLI

This page covers day-to-day use of the `yoke` command. Saved sessions, resume
and the session database are in [Sessions](sessions.md). Providers, config
files, tool policy, MCP servers and environment variables are in
[Configuration](configuration.md).

## How the CLI runs

Every `yoke` session runs in a shared per-user daemon. The terminal is a client.
The first `yoke` command starts the daemon if none is running, and the daemon
exits about five seconds after its last client leaves and its queued work is
done. There is no in-process fallback. If the daemon cannot start, the command
prints the error and exits with status 1.

When the CLI attaches, it sends its environment variables and working directory
to the daemon. Turns in that session use those values, so provider keys such as
`OPENCODE_API_KEY` come from the shell that ran `yoke`. The daemon also reads a
`.env` file in the workspace root. Values from the client environment win over
`.env` values.

See [The daemon](#the-daemon) for status, restart and running it under systemd.

## Start a session

```bash
# Open an interactive session in the current directory
yoke

# Open one with a first prompt
yoke "explain this codebase"

# Work in another directory
yoke --root /path/to/project

# Pick a model and preload a skill
yoke --model codex:gpt-6.1-sol:high --skill code-review "review src/auth.py"
```

A bare argument that is not a subcommand becomes the prompt, so
`yoke "explain this"` is the same as `yoke --prompt "explain this"`.

The workspace root defaults to the current directory. File tools resolve
relative paths against it. Yoke also reads `AGENTS.md` and the config, MCP
servers, skills and tool plugins under `.yoke/` from it.

Yoke picks one of two interactive modes:

| Mode | When | Notes |
| --- | --- | --- |
| Full-screen | stdout is a terminal | prompt-toolkit editor, bottom toolbar, inspectors, `/tree` and `/queue` |
| Basic | stdout is not a terminal | Reads lines. Lines sent during a turn are queued. `Ctrl+C`, `exit` or `quit` stops the turn and exits. |

Type `exit` or `quit` to leave either mode. Run `/help` for the slash commands
and `/shortcuts` or `?` for the keyboard shortcuts in the current mode.

## Run headless

Headless mode runs one turn, prints the final answer and exits.

```bash
yoke --headless "run the tests and summarize failures"
yoke -p "run the tests and summarize failures"

# Read the prompt from stdin
echo "what does utils.py do?" | yoke --headless

# Continue a saved session
yoke resume 20261117-143022-a1b2c3 -p "now fix the first failure"
```

The final assistant message goes to stdout. Progress and errors go to stderr.

| Exit status | Meaning |
| --- | --- |
| 0 | The turn completed. |
| 1 | The turn failed or stopped, or startup failed. |
| 130 | You pressed `Ctrl+C`. Yoke asks the daemon to interrupt the turn. |

When the prompt comes from stdin, Yoke reads all of it, including blank lines.

Headless runs are strict about models. An unknown model or an unsupported
thinking effort is an error, and Yoke never substitutes another model. See
[Choose a model](#choose-a-model).

## Choose a model

A model selection has the form `provider:model[:effort]`:

```bash
yoke --model codex:gpt-6.1-sol:medium
yoke --model opencode-go:deepseek-v4.1-flash
yoke --model zai:glm-5.3-flash:max
```

You can also pass only the model name, and Yoke picks a provider from the
credentials you have. If you leave out the effort, Yoke uses the model's
default. The model table and the provider defaults are in
[Configuration](configuration.md#models).

Yoke picks the model for a new session in this order:

1. `--model` on the command line.
2. `default_model` and `default_reasoning_effort` in `.yoke/config.json`.
3. The same keys in `~/.yoke/config.json`.
4. The provider's default model.

A resumed session keeps its saved provider, model and effort unless you pass
`--model`. This applies to `yoke resume`, an existing `--session` and `--fork`.

Yoke checks the effort against the provider's model catalog. An unsupported
effort, or an empty one such as `codex:gpt-6.1-sol:`, fails with a list of the
supported levels and changes nothing.

If the configured default model or a resumed session's model has left the
catalog, interactive mode retries once with the same provider's current default
model. It prints a warning naming both models. It does not rewrite your config.
Run `yoke models set` to save a new default. This recovery never applies to an
explicit `--model`, to headless runs, or to authentication and network errors,
and Yoke never switches to a different provider.

To change the model during a session:

- Run `/model` or press `Ctrl+X` then `M` to open a table of models from all
  ready providers. Choosing a row switches to that model at its default effort.
- Press `Shift+Tab` to cycle the thinking effort through the current model's
  levels.

If the new model has a smaller context window, Yoke may compact the working
context before the switch. If compaction fails or the context still does not
fit, Yoke rolls back the switch and keeps the previous model.

To save a default:

```bash
yoke models list                              # every model from every provider
yoke models set codex:gpt-6.1-sol:high        # save to ~/.yoke/config.json
yoke models set opencode-go:deepseek-v4.1-flash --repo   # save to .yoke/config.json
yoke models set                               # choose from a list
```

`yoke models set` writes `default_model` and `default_reasoning_effort` as
separate keys. Choosing from the list saves the model's default effort. It
rejects a model the provider's catalog does not list before writing anything.

## Attach images

```bash
yoke --image screenshot.png "describe this screenshot"
yoke --headless --image chart.png --image legend.png "summarize these charts"
yoke resume 20261117-143022-a1b2c3 -p "compare with this" --image new.png
```

In a full-screen session:

- Press `Ctrl+V` or `Alt+V` to paste an image from the clipboard. If the
  clipboard holds text, Yoke pastes the text. Use `Alt+V` when your terminal
  captures `Ctrl+V`.
- Run `/image path/to/file.png` to attach a file.
- Press `Ctrl+U` to remove the last pending image.

Pending images appear in the bottom toolbar and go with the next prompt you
send. Yoke offers image tools only when the current model accepts images.

Yoke stores a copy of each attached image in the session, so a resumed session
does not need the original file. It scales the copy to fit a 1568-pixel long
edge and 1,150,000 pixels, and never scales up. Opaque images are stored as JPEG
at quality 90 and images with transparency as PNG. An original that already
fits and is no larger is kept as is.

## Steer, queue and stop a turn

While a turn runs, you can keep typing.

| Action | Key |
| --- | --- |
| Steer the current turn with this prompt | `Enter` |
| Queue the prompt to run after the current turn | `Tab` |
| Stop the turn | `Esc` `Esc` |
| Resume a held queue | `Ctrl+R` |
| Open the queue manager | `Ctrl+X` then `Q`, or `/queue` |

Stopping or steering keeps everything up to the last completed tool result.
Yoke records your prompt and an interruption marker, and the next turn starts
from there. Yoke marks tool calls that were still running as cancelled. Command
timeouts kill the whole process tree.

The toolbar follows the daemon's session state, including turns started from
the queue or another terminal. The spinner and stop key follow whichever input
is actually active. Steering runs before earlier ordinary queued prompts.

After Stop, the queue stays held. The toolbar shows the reason and `Ctrl+R`
resumes eligible work. A new prompt also resumes the queue. Paused inputs stay
paused until you change them in the queue manager. Queue edits alone do not
resume a held queue.

The queue manager lets you edit, delete, reorder, pause and promote queued
prompts, and mark a prompt as a steering prompt. Steering prompts run before
normal ones. Output from a running turn waits while the manager is open, so it
cannot overwrite your edit.

The daemon saves queued prompts and pending images with the session, so they
survive exiting and resuming. A prompt leaves the queue when it starts. A
`/skill` command queued with `Tab` activates the skill when it reaches the
front of the queue.

Turns longer than 60 seconds end with a summary line such as
`Worked for 1m02s · 6 tools`.

## Compact the context

Yoke compacts the provider context automatically when it nears the model's
window, and once more if the provider reports an overflow. Run `/compact` to do
it now. Compaction asks the model for a handoff summary and starts a smaller
context from that summary, your recent prompts and the latest messages. The
saved session keeps the full history, and `/tree` still shows it.

## Use skills

A skill is a directory with a `SKILL.md` file that tells the agent how to
handle one kind of task. Yoke finds skills in three places:

| Location | Scope |
| --- | --- |
| Built in, under `src/yoke/agent/skills/built_in/` | `create-skill`, `yoke-sessions`, `yoke-subagents` |
| `.yoke/skills/` | the current workspace |
| `~/.yoke/skills/` | every workspace |

When two locations have a skill with the same name, the built-in skill wins,
then the workspace skill, then the global one.

Activate a skill in any of these ways:

```bash
# At startup
yoke --skill code-review "review the changes in src/auth.py"
```

```text
/skill code-review review the current diff
$code-review $repo-style review the current diff
```

`/skill name` activates the skill. Any text after the name is sent as a normal
prompt. A `$name` mention anywhere in a prompt activates that skill just before
the prompt runs. Typing `$` opens completion, and `Enter` or `Tab` inserts the
name without sending. Yoke activates mentions in order, once each per prompt,
and leaves the text visible to the model. Mentions inside Markdown code or
escaped with a backslash are plain text. The agent can also activate skills
itself through the `skill` tool.

Activation adds the current `SKILL.md` content and a list of the files in the
skill directory to the conversation. The session keeps that text, so a resumed
session still works if the skill file moves or is deleted. Activating a skill
again adds a fresh copy.

### Create a skill

```bash
yoke skills init repo-style          # creates .yoke/skills/repo-style/SKILL.md
yoke skills list
yoke skills show repo-style          # file list, then the instructions
```

A skill name is lowercase kebab-case and must match its directory name:

```text
.yoke/skills/
  code-review/
    SKILL.md
```

```markdown
---
name: code-review
description: Code review focused on security and correctness
---

Check for injection, missing error handling and unclear names.
Report findings as a prioritized list.
```

`SKILL.md` may start with a UTF-8 byte order mark. Pass `--root` to `yoke skills`
to work on another workspace. The built-in `create-skill` skill walks the agent
through the same steps and asks where the new skill should go.

## Inspect tools, processes and agents

| Command or key | Shows |
| --- | --- |
| `Ctrl+X` then `O` | The tool inspector: full arguments and results for every tool call, updated live during a turn. |
| `/ps`, or `Ctrl+X` then `Ctrl+P` | Running and recent `command_exec` processes: PID, directory, timing, exit status and output. Basic mode prints a table. |
| `/agents` | The latest run of each SDK agent started from this session's tools, with model, status and last tool. |
| `/info` | Session ID, title, pin state, database path, model, message counts and context window. |

The inspectors are read only. They do not consume output that `process_read`
still needs, and they do not stop work. Process state lives in the daemon, so
`/ps` does not show processes from before a daemon restart.

## Diagrams

In a terminal, fenced `mermaid` blocks render as text diagrams. Yoke supports
flowchart, state, class, ER, sequence, pie, mindmap, timeline and git graph
diagrams. A diagram that fails to parse or is wider than the terminal shows its
source in a frame instead. Redirected output keeps the original Markdown. Set
`YOKE_MERMAID=0` to show Mermaid blocks as ordinary code.

## The daemon

You rarely need to manage the daemon. These commands help when you do:

```bash
yoke daemon status               # pid, build, socket, port, leases; exit 1 if none
yoke daemon restart              # restart on the code installed now
yoke daemon restart --when-idle  # restart once running turns finish
yoke daemon stop                 # refuses while a turn runs
yoke daemon stop --force
```

`yoke daemon restart` refuses while a turn runs unless you pass `--when-idle`.
After you upgrade Yoke, the next client restarts an idle daemon that runs older
code. A busy daemon keeps running and the client attaches with a warning.

On Linux and macOS, clients connect over a Unix socket that only your user can
use. The daemon also listens on `127.0.0.1:8770` for the browser UI, with a
bearer token stored in `~/.yoke/daemon.json`. Windows clients use the loopback
port and that token. Detached daemons log to `~/.yoke/run/daemon.log`.

To keep the daemon running without clients, for example under systemd:

```bash
yoke serve --daemon --no-idle-exit
```

The repository has a sample user unit in
[`docs/yoke-daemon.service`](../../../docs/yoke-daemon.service). The [HTTP API](http.md)
and [daemon client](daemon-client.md) docs cover socket paths, port conflicts
and build checks.

A daemon that a client starts automatically gets a minimal environment. It
does not inherit arbitrary variables from the shell that started it. Settings it reads from its own
environment, such as `YOKE_DISABLE_TLS`, must be set where the daemon starts,
for example in the systemd unit.

## Other front ends

Yoke has three other entry points:

| Command | Use |
| --- | --- |
| `yoke serve` | Standalone HTTP API and web UI on `127.0.0.1:8765`. See [HTTP API](http.md). |
| `yoke acp` | Agent Client Protocol over stdio, for editors such as T3 Code. See [ACP and T3 Code](acp.md). |
| `yoke-mcp` | Tool-only MCP server with no agent. See [Tool-only MCP server](mcp-server.md). |

`yoke acp` uses the local daemon. To point it at another server, set
`YOKE_ACP_URL` or pass `--url`, and set `YOKE_ACP_TOKEN`.

## Reference

### Command-line options

These options apply when `yoke` runs without a subcommand.

| Option | Meaning |
| --- | --- |
| `--prompt TEXT` | First prompt. A bare argument does the same. |
| `--headless`, `-p` | Run one turn and exit. Needs a prompt or stdin. |
| `--session NAME` | Use `NAME` as the session ID. Continues that session if it exists, otherwise creates it. |
| `--fork ID` | Copy session `ID` into a new session and continue there. Cannot be combined with `--session`. |
| `--model SEL` | `provider:model[:effort]`, or just a model name. |
| `--root DIR` | Workspace root. Defaults to the current directory. |
| `--skill NAME` | Activate a skill at startup. Repeatable. |
| `--image PATH` | Attach an image to the first prompt. Repeatable. |

### Subcommands

| Command | Purpose |
| --- | --- |
| `yoke version` | Print the version. |
| `yoke resume [ID]` | Resume a session. See [Sessions](sessions.md). |
| `yoke session-handoff ID` | Print a portable summary of a session. See [Sessions](sessions.md#hand-off-a-session). |
| `yoke db ...` | Inspect, back up and maintain the session database. See [Sessions](sessions.md#maintain-the-database). |
| `yoke daemon status\|restart\|stop` | Manage the shared daemon. |
| `yoke login NAME` | Store credentials for a provider. Same as `yoke providers login`. |
| `yoke models list\|set` | List models and save a default. |
| `yoke providers list\|doctor\|login\|init` | Inspect providers, check credentials, scaffold a provider plugin. |
| `yoke tools list\|init\|activate\|deactivate` | Inspect tools and edit tool policy. See [Configuration](configuration.md#tool-policy). |
| `yoke skills list\|show\|init` | Inspect and scaffold skills. |
| `yoke mcp [SERVER]` | Show configured MCP servers and their tools. |
| `yoke serve` | Run the HTTP API. `--daemon` runs the shared daemon. |
| `yoke acp` | Serve Agent Client Protocol on stdio. |

Run any command with `--help` for its options.

### Slash commands

| Command | Action |
| --- | --- |
| `/help` | List commands. |
| `/shortcuts`, `?` | List keyboard shortcuts for the current mode. |
| `/new` | Start a new session in the same workspace. |
| `/model` | Open the model switcher. |
| `/skill <name> [prompt]` | Activate a skill, then send the rest as a prompt. |
| `/image <path>` | Attach an image to the next prompt. |
| `/compact` | Compact the context now. |
| `/tree` | Navigate and branch the session tree. Full-screen only. |
| `/queue` | Open the queue manager. Full-screen only. |
| `/fork` | Copy this session and continue in the copy. |
| `/title <new-title>` | Rename the session. |
| `/regenerate-title` | Ask the model for a new title. |
| `/pin` | Pin or unpin the session. |
| `/info` | Show session metadata. |
| `/tools` | Turn tools on or off for this session, this workspace or globally. |
| `/mcp [server]` | Manage MCP servers and their tools. |
| `/ps` | Open the process inspector. |
| `/agents` | Show the latest SDK agent runs. |

Text that starts with `/` but is not a command goes to the model as a prompt.

### Keyboard shortcuts

Full-screen mode:

| Key | Action |
| --- | --- |
| `Enter` | Send. During a turn, stop it and send this prompt. |
| `Tab` | Queue behind the current turn. Inserts the selected completion when one is open. |
| `Esc` `Esc` | Stop the current turn, including a turn started from the queue. |
| `Ctrl+R` | Resume a held queue. |
| `Shift+Tab` | Cycle thinking effort. |
| `Ctrl+J`, or `Shift+Enter` where the terminal supports it, otherwise `Esc` then `Enter` | Insert a newline. |
| `Ctrl+V`, `Alt+V` | Paste text or attach a clipboard image. |
| `Ctrl+U` | Remove the last pending image. |
| `Ctrl+X` then `O` | Tool inspector. |
| `Ctrl+X` then `Ctrl+P` | Process inspector. |
| `Ctrl+X` then `Q` | Queue manager. |
| `Ctrl+X` then `M` | Model switcher. |
| `Ctrl+X` then `T` | Session tree. |
| `Up`, `Down` | Move through completions when the menu is open. |

A multiline paste stays in the editor. Press `Enter` to send it.

Basic mode has two keys. `Enter` sends a line, and lines sent during a turn are
queued. `Ctrl+C` stops the turn and exits.

### Bottom toolbar

The toolbar shows the session title, the turn status and optional segments.
The status is one of `Thinking`, `Streaming`, `Running tool`, `Compacting` and
`Recovering`. Set these variables to `0`, `false`, `no` or `off` to hide a segment, or to any other
value to show it.

| Variable | Segment | Default |
| --- | --- | --- |
| `YOKE_BAR_TIMER` | Elapsed turn time | on |
| `YOKE_BAR_GAUGE` | Context usage gauge | on |
| `YOKE_BAR_TOOLS` | Tool calls this turn | on |
| `YOKE_BAR_TOKENS` | Token counts | off |
| `YOKE_BAR_TURN` | Turn number | off |
