# ACP and T3 Code

This page is for people who use Yoke from an Agent Client Protocol (ACP)
client such as T3 Code. It covers setup, what the ACP bridge supports, how
approval modes work, and how T3 thread settlement reaches Yoke.

`yoke acp` is an ACP agent on stdio. It does not run turns itself. It forwards
each ACP session to the per-user Yoke daemon described in [http.md](http.md),
which owns saved conversations and execution. Closing the ACP connection drops
its client configuration and observers. Saved sessions stay in the daemon.

## Set up T3 Code

In T3's provider settings, add an ACP provider that runs a local command. The
provider instance configuration looks like this:

```json
{
  "driver": "acpRegistry",
  "displayName": "Yoke",
  "enabled": true,
  "config": {
    "source": "local",
    "commandPath": "/absolute/path/to/yoke-acp-launcher",
    "commandArgs": []
  }
}
```

`commandPath` can be `yoke` with `commandArgs: ["acp"]`, or a launcher script
that sets up `PATH` and then runs `yoke acp`. No URL or token is needed.
`yoke acp` finds the local daemon, or starts `yoke serve --daemon` and waits up
to five seconds for it. It then holds a daemon lease named `acp` until the ACP
connection closes. On Linux and macOS it talks to the daemon over the Unix
socket. On Windows it uses the loopback port and token from
`~/.yoke/daemon.json`. The daemon logs to `~/.yoke/run/daemon.log`, and
`yoke daemon status` shows whether it runs.

To use a daemon somewhere else, pass `--url` or set `YOKE_ACP_URL`. That mode
requires `--token` or `YOKE_ACP_TOKEN` and does not discover or start a local
daemon. Prefer the environment variable for the token.

### Provider credentials

ACP sessions run with the daemon's environment, not the launcher's. The bridge
sends no environment when it attaches. A daemon started on demand inherits only
a short list of system and Yoke variables, so provider API keys exported in the
launcher do not reach it. Give the daemon credentials in one of these ways:

- Save them with Yoke's provider login commands.
- Put them in the project's `.env`, which the daemon loads for that directory.
- Run the daemon under a service manager with an `EnvironmentFile`. The sample
  unit is [`docs/yoke-daemon.service`](../../../docs/yoke-daemon.service).

After changing the daemon's service environment, restart it with
`yoke daemon restart` and refresh Yoke in T3's provider settings.

### Models and skills

At startup the bridge reads the daemon's model catalog and offers every model
from every ready provider as an ACP configuration option, with that model's
reasoning efforts. Names include the provider, so models with the same name
from different providers stay distinct. New sessions use the directory's
configured default model and reasoning effort. Switching models resets the
effort to the new model's default.

If a model is missing, compare `GET /api/v1/provider` and `GET /api/v1/model`
on the daemon with `yoke models list`. The daemon, not the launcher, needs the
credentials.

Workspace skills arrive through `available_commands_update` as `$name`
commands, which T3 shows in its skills menu. Reading the catalog creates a
session but runs no model inference. ACP advertises no sign-in methods.

## What the bridge supports

| Area | Support |
| --- | --- |
| Sessions | Create, load with history replay, resume without replay, list with cursor and directory filter, fork, close |
| Models | Ready catalog, per-session model and reasoning effort, configuration change notifications |
| Content | Text, inline images, embedded resources, prompts with only attachments |
| Tools | Standard tool-call lifecycle, readable output, action labels, file locations, structured results |
| Metadata | Session title, context usage, task plans, compaction progress |
| MCP | Client-supplied stdio and HTTP servers, including T3's `t3-code` server |
| Interaction | Permission requests and form elicitation for questions |
| Steering | Cancel, wait for the turn's workers to exit, then send the corrected prompt in the same session |

The bridge negotiates ACP protocol version 1, even when the client offers
version 2. It does not accept SSE MCP servers or MCP over ACP.

On load, history replay sets each saved entry's `messageId` on all of its
content chunks. Consecutive messages stay separate even with the same role or
text. A live final reply uses the same ID it will have on replay. Live
commentary uses an ID derived from the input and model iteration, because its
entry is not saved yet.

The bridge does not retry a failed request to the daemon, because a prompt may
already have been admitted. A failed turn still interrupts admitted work and
waits for its workers to exit. Errors name the failure, the request and the
elapsed time, for example
`Native HTTP read timeout during POST session/wait after 35.0s`. That message
describes the bridge's request to the daemon, not the model provider. It
omits session IDs, query values, credentials and response bodies. The bridge
closes idle pooled connections after two seconds, before the daemon's
five-second keep-alive timeout.

When the daemon restarts, the bridge reopens its lease against the new daemon.
`yoke daemon stop` ends that reconnect loop.

### Commands

The bridge advertises `/compact`, `/info`, `/title`, `/model`, `/skill`, `/ps`,
`/agents`, `/mcp` and `/shortcuts`. A prompt that consists of exactly one of
these commands runs the matching daemon operation. Any other prompt goes to the
model. Compaction progress appears as a `think` tool call, because ACP has no
compaction notification. If compaction fails or leaves work undone, the result
says so instead of reporting success.

### Client MCP servers

MCP servers that the client sends with a session take precedence over a saved
server with the same name. They exist only in the daemon's memory for that
session. The daemon never writes their credentials to Yoke configuration,
saved sessions or status responses. Load and resume replace them with the
client's current list, and closing the connection removes them. Other sessions
cannot see them. The model reaches them through `mcp_inspect` and `mcp_call`,
even when the user's MCP configuration does not enable MCP.

After initialization, HTTP MCP requests send the negotiated
`MCP-Protocol-Version` header, which T3 requires.

## Approval modes

New ACP sessions start in `approval-required`. T3 can switch the mode from the
session's configuration options.

| Mode | Behaviour |
| --- | --- |
| `approval-required` | Run read-only tools. Ask the client before anything else. |
| `auto-accept-edits` | Also run Yoke's file edit tools without asking. |
| `full-access` | Run every tool without asking. |
| `plan` | Run read-only tools. Block edits, commands and unclassified tools until the mode changes. |

The daemon sends permission requests through ACP with the tool's kind and file
targets, so a full-access T3 thread can approve them automatically and an
approval thread can show them. Modes are a tool policy, not an operating-system
sandbox. A mode change applies to new tool calls and does not stop background
work that already started. [daemon-client.md](daemon-client.md#attach-a-client)
lists the tools each mode allows.

ACP sessions also get two tools:

- `update_plan` sends standard ACP plan notifications.
- `request_user_input` asks a question through form elicitation, with free
  text, a single choice or several choices. A client that cannot show the form
  declines the question.

Interrupting a turn rejects its pending question and waits for the worker to
exit. After the bridge reconnects to the daemon's event stream, it reconciles
pending permission requests, questions and the latest plan.

## T3 thread settlement

T3 sends ACP `session/close` both when the user settles a thread and when it
releases an idle session, and the message carries no reason. To tell them
apart, the daemon reads the thread's `settled` state through T3's injected
`t3-code` MCP server:

- If T3 reports the thread settled, the daemon archives the matching Yoke
  session once its workers and queued inputs finish.
- If the thread is not settled, or the state cannot be read, the session stays
  active.
- If the user unsettles the thread in T3, the daemon unarchives the session on
  its next check, without a new message or connection. It only reverses
  archives that this observer made, and leaves sessions archived directly in
  Yoke alone.
- Loading or resuming an archived session unarchives it at once.

The daemon checks when the client detaches and every 15 seconds after that. The
observer keeps running after the ACP connection closes, so settling an idle
thread still reaches Yoke. It holds T3's scoped MCP credential in memory,
separate from the model's tools, until T3 revokes it, another client replaces
it, or the daemon exits. The daemon never saves that credential. After a daemon
restart, load or resume the T3 thread to start observing again.

Settlement flows from T3 to Yoke only. Archiving a session in Yoke does not
settle the T3 thread. If T3 revokes MCP access before the daemon reads the
state, the daemon cannot tell a settled thread from a revoked one, and leaves
the session active.

## Generic T3 presentation limits

The bridge sends Yoke's SDK agent runs and managed processes as structured
records inside tool updates. T3's generic ACP driver decodes its dedicated
subagent and background-process views only for some other agents. It shows
Yoke's records as tool activity instead of filling the Agents and Monitoring
views. Those views need a T3-side decoder for Yoke's records. Agents that T3
starts through its own MCP tools use T3's orchestration and views.

The bridge works with older daemons that lack the session client endpoint. It
then does not offer the session controls, approval modes or HTTP MCP support,
and it rejects client MCP servers and additional directories with an error
asking you to upgrade the daemon.

## Prompt ownership and cancellation

When the daemon advertises `features.inputLifecycle`, attaching an ACP client
activates input tracking. The bridge tracks each prompt's input ID and phase
through queued, paused, promoted, running, settled and removed. It reads the
input snapshot after admission, consumes `session.input.changed`, and uses
`/wait` as a timeout guard that triggers another input read. Session idleness
does not settle a queued ACP prompt.

ACP cancel calls `POST /session/{id}/input/{inputID}/cancel`. It removes a
queued input or stops that input's active turn. Another client's turn keeps
running. A cancel that races settlement reads the input's terminal result and
returns its real outcome. A completed answer remains visible even if cancel
arrives during finalization. Stopped turns still wait for physical worker
cleanup. A paused input produces an error naming its ID, and the bridge cancels
that input before releasing the prompt handler.

Session close cancels its prompt first, then serializes detach with other
session operations. It removes local session and catalog state even when
server detach fails. Older daemons retain the existing interrupt and drain
behavior when they lack the input lifecycle feature.
