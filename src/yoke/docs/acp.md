# ACP and T3 Code

`yoke acp` is a stdio ACP agent backed by a running `yoke serve` daemon.
The daemon owns durable conversations and execution. Closing an ACP connection
releases its client tool configuration and prompt observers. Saved sessions
remain available, and T3 settlement synchronization continues in the daemon.

## T3 Code setup

Run the current Yoke version on both sides. Supply `YOKE_HTTP_TOKEN` to the
daemon and the same credential as `YOKE_ACP_TOKEN` to the ACP process. Set
`YOKE_ACP_URL` to the daemon's base URL. A service manager must also receive
the provider environment used by ordinary Yoke, including any configured
`YOKE_CODEX_DOMAIN`, `YOKE_CODEX_API_KEY`, `OPENCODE_API_KEY`, and `ZAI_API_KEY`.
A systemd service does not inherit the launching terminal's environment. Supply
these values in its protected environment file, or use Yoke's provider login
commands to save credentials in the private provider store. Use
`serve --hide-token` for service logs.

In T3's provider settings, add an ACP provider using a local command. Use
`yoke acp` if T3 already inherits the required environment. Otherwise point it
at an executable launcher that loads that environment and runs `yoke acp`.
The corresponding provider instance configuration is:

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

T3 discovers ready models and their reasoning controls from ACP configuration
options. Workspace skills arrive through `available_commands_update` as `$name`
commands, which T3 displays in its skills menu. Catalog discovery creates a
native session but performs no model inference. The generic T3 route requires
no Yoke-specific provider driver. The launcher already authenticates to the
daemon, so ACP advertises no additional sign-in methods.

The model picker includes every model from every ready Yoke provider. Compare
`GET /api/v1/provider` and `GET /api/v1/model` on the daemon with `yoke models
list` when entries are missing. Credentials must reach the daemon, even if the
ACP launcher already has them. Restart the daemon after changing its service
environment, then refresh Yoke in T3's provider settings. Matching model names
include their provider so the two GLM entries remain distinguishable. New ACP
sessions use the workspace's configured default model and reasoning effort;
switching models uses that model's supported reasoning choices and default.

## Session behavior

| Feature | Behavior |
| --- | --- |
| Sessions | Create, load with history replay, resume without replay, list with cursor and cwd filtering, fork, and close |
| Models | Native ready catalog, per-session model and reasoning selection, config change notifications |
| Content | Text, inline images, embedded resources, attachment-only prompts |
| Tools | Standard lifecycle, readable output, action labels, file locations, structured results |
| Metadata | Session title, context usage, task plans, and compaction lifecycle notifications |
| MCP | Client-injected stdio and HTTP servers, including T3's `t3-code` server |
| Interaction | ACP permission requests and form elicitation for user questions |
| Steering | Cancel, wait for physical drain, then send the corrected prompt in the same session |

ACP protocol version 1 is negotiated even when the client offers version 2.
The installed Python ACP SDK supports the standard and unstable methods used
here. SSE MCP and MCP-over-ACP transports are not advertised or accepted.

Injected MCP configuration takes precedence over a persisted server with the
same name. It lives only in the daemon's session runtime. Credentials are not
written into Yoke configuration, saved sessions, or client-status responses.
Load and resume replace the injected servers with the client's current values.
Closing the owning connection removes them. A different session cannot see
them. The model uses `mcp_inspect` and `mcp_call` to discover and call these
tools even when the ordinary CLI configuration does not enable MCP.

### T3 settlement

T3 sends ACP `session/close` both when settling a thread and when releasing
an ordinary idle session. The message carries no reason. Yoke therefore reads
the thread's durable `settled` state through T3's injected, scoped MCP service.
A confirmed settlement archives the corresponding native Yoke session after
its workers and queued prompts drain. Ordinary closes and failed state reads
leave the session active. Loading or resuming an archived session reactivates it.

The daemon checks on client detach and every 15 seconds. Its settlement
observer survives an ACP disconnect, so settling an already idle T3 thread
still reaches Yoke. The observer retains only its scoped MCP credential in
memory, separate from the model's tools, until settlement, credential
revocation, replacement by another client, or daemon shutdown. No credential
is persisted. After restarting the daemon, load or resume the T3 thread to
reattach this observer. Reopening a thread reactivates Yoke on its next ACP
load or resume. This synchronization runs from T3 to Yoke. Archiving directly
in Yoke does not settle T3. T3 archive actions that revoke MCP access before
Yoke can read state cannot be inferred safely from the resulting authorization
failure.

HTTP MCP connections send the server's negotiated `MCP-Protocol-Version`
header after initialization. T3 requires this header for subsequent requests.

## Modes and interactive tools

New ACP sessions default to `approval-required`. This routes mutations,
execution, and unclassified MCP/plugin calls through the client's permission
policy. A full-access T3 thread can approve these automatically; a thread
requiring approval can display them. File targets accompany permission requests
so the client can evaluate workspace write policy.

- `approval-required` permits known read tools and asks for other tools.
- `auto-accept-edits` also permits native file edit tools without asking.
- `full-access` executes tools without the ACP permission gate.
- `plan` permits known read tools and blocks changes, execution, and unclassified
  tools. Switching mode is required before those calls can run.

These are tool policies, not an operating-system sandbox. They apply to new
tool calls, and do not revoke background work already started. Native CLI and
SDK sessions without attached client configuration keep their existing behavior.

Attached sessions gain `update_plan` and `request_user_input` tools. Plans use
standard ACP plan notifications. Questions use form elicitation with free text,
a single choice, or multiple choices. A client unable to handle the form declines
the question. Interrupting a question rejects it and waits for its worker to
finish. Pending permissions/questions and the latest in-memory plan are
reconciled after event-stream reconnects.

The peer advertises `/compact`, `/info`, `/title`, `/model`, `/skill`, `/ps`,
`/agents`, `/mcp`, and `/shortcuts`. A prompt containing exactly one of these
commands runs the corresponding native operation. Other prompts pass to the
model normally. Compaction reports failure or continued work without claiming
that summarization succeeded. Compaction progress uses a standard `think` tool
lifecycle because this SDK has no dedicated ACP compaction notification.

## Generic T3 presentation limits

Yoke's native SDK-agent and managed-process observers retain their structured
records in tool updates. T3's generic ACP driver currently decodes dedicated
subagent/background extensions only for selected other agents. It therefore
shows Yoke-native observations as tool activity, rather than populating the
specialized Agents and Monitoring views. Those views require a T3-side decoder
for Yoke's extensions. T3-owned agents invoked through its injected MCP tools
use T3's own orchestration and presentation.

Older Yoke daemons continue to support the existing ACP operations. The peer
advertises the new session controls, interactive modes, and HTTP MCP support
only when the daemon exposes the session-client endpoint.
