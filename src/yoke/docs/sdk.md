# SDK

Embed yoke in Python with a small top-level surface and explicit submodules:

- `yoke.ai` for the common path: `Agent`, `RunConfig`, `Image`, `complete()`, and built-in provider construction
- `yoke.ai.types` for result and error value types
- `yoke.ai.providers` for concrete provider/config classes
- `yoke.ai.skills` for composable instruction sets
- `yoke.ai.utils` for optional diagnostics and low-level convenience helpers

## Agent

```python
from yoke.ai import Agent, build_builtin_provider

provider = build_builtin_provider("codex:gpt-5.5:medium")
agent = Agent(provider=provider)

result = agent.prompt("Add type annotations to src/utils.py")
print(result.output)
agent.close()
```

`Agent` is stateful. Reuse one instance to retain conversation context and
close it when finished. When `config` is omitted, the SDK builds a coding
configuration rooted at the current working directory. Its high-level
capabilities include file read/write/search, shell execution, image attachment
and generation, web fetch/search/research, and document extraction.
Provider-aware resolution removes unsupported tools and chooses the appropriate
concrete implementation.

Pass `RunConfig` when you need an explicit root, system prompt, tool list,
skills, hooks, compaction policy, or execution mode:

```python
from yoke.ai import Agent, RunConfig, build_builtin_provider

agent = Agent(
    provider=build_builtin_provider("opencode-go:gpt-5.6-luna:high"),
    config=RunConfig(
        root=".",
        tools=["file.read", "file.search", "file.write", "web.research"],
    ),
)
```

Provider selections use `provider:model:thinking_effort`. Yoke's public SDK
exposes `Agent`, `RunConfig`, `complete()`, `run_many()`, observers, durable
state, structured outputs, and provider helpers with consistent call shapes.

```python
from yoke.ai import build_builtin_provider
from yoke.ai.utils import print_builtin_provider_status

print_builtin_provider_status()
provider = build_builtin_provider("codex:gpt-5.5:medium")
```

The default SDK selection is `codex:gpt-5.5:medium`. Built-in providers are
`codex`, `opencode-go`, and `zai`. The same helper also resolves global
custom provider plugins under `~/.yoke/providers/`. Provider status includes
credential readiness, model catalogs, context windows, thinking controls, and
image-input metadata.

Codex can authenticate from its OAuth state, the Codex account vault, or
`YOKE_CODEX_API_KEY`. OpenCode Go uses `OPENCODE_API_KEY`; Z.ai uses
`ZAI_API_KEY`. The CLI credential store is also included in provider
resolution.

Codex uses Responses continuity and a stable cache scope. Healthy follow-up
requests can send only the new input against the prior response; stale anchors
recover from retained opaque output or visible history. Forked agents receive
isolated provider state. Provider/model system messages and tool capabilities
refresh when model identity changes.

## Async Agents

Use `prompt_async()` from an asyncio application. It has the same inputs and
result type as `prompt()`, plus an optional timeout.

```python
import asyncio

from yoke.ai import Agent, build_builtin_provider


async def main() -> None:
    agent = Agent(
        provider=build_builtin_provider("codex:gpt-5.4-mini:low")
    )
    async with agent:
        result = await agent.prompt_async(
            "Summarize the current project.", timeout=120
        )
        print(result.output)


asyncio.run(main())
```

The underlying agent runtime remains synchronous, so `prompt_async()` runs it
in a worker thread instead of blocking the event loop. Concurrent calls on the
same stateful `Agent` serialize before allocating worker threads, protecting its
conversation state without exhausting the executor. One `Agent` is bound to the
first event loop that calls `prompt_async()`; use a separate agent in another
event loop. Cross-loop use raises `RuntimeError` instead of risking a blocked
waiter. Closing is idempotent,
waits for active work, rejects queued prompts once closing begins, and prevents
subsequent state mutation. Cancelling `aclose()` still waits for its close worker
before it propagates cancellation. Transcript properties and prompt results
return snapshots, so changing their lists does not change agent state. Use
`run_many()` for independent work.

Synchronous event and tool hooks run in the worker thread. They must be
thread-safe and must not recursively prompt, close, save, restore, reset, or
fork the same agent.

## Usage Metrics

Yoke records every completed provider response from the CLI and SDK in daily
JSONL files under `~/.yoke/usage-metric-logs/<provider>/`. Records contain only
the provider, model, completion time, normalized token counts, and explicit
execution attribution. Attribution includes `surface`, `call_kind`, CLI session
identity, or the SDK operation and a random SDK run ID as applicable. CLI
`session_title` can contain up to 80 characters derived from the first user
prompt when the user has not assigned a title. Records do not contain complete
prompts, responses, tools, paths, caller-supplied batch task IDs, or raw provider
payloads. A provider that does not report token counts still gets a completion
record with an empty `usage` object. Usage records continue to use
`schema_version: 1`; the attribution fields are optional additions.

SDK operations are `complete`, `agent`, and `run_many`. A random `sdk_run_id`
groups the provider calls produced by one direct completion, agent prompt, or
batch task attempt. Call kinds distinguish direct completions, model iterations,
structured-output retries, compaction summaries, branch summaries, and overflow
retries. Yoke sets this metadata at its CLI and SDK entry points and propagates it
with execution context; it does not infer attribution from process arguments or
stack frames.

Set `YOKE_USAGE_METRIC_LOG_DIR` to store these local metrics in another
directory. Writes use a cross-process lock, retry transient failures, and flush
data to disk before returning. A persistent failure raises
`yoke.ai.providers.UsageLogWriteError` instead of silently losing a completed
usage record.

## Observing Agent Work

Use an SDK observer for live, structured visibility into an agent run. The
`actions` detail level shows assistant commentary, the final assistant message,
compact tool-call signatures, and tool failures. It redacts argument keys that
look like credentials and is the recommended level for subagents.

```python
from yoke.ai import Agent, ConsoleObserver

agent = Agent(
    provider=provider,
    observer=ConsoleObserver("actions", label="reviewer"),
)
result = agent.prompt("Review the current changes.")
```

The available detail levels are:

- `quiet`: do not emit live activity.
- `messages`: emit assistant commentary, the final message, and agent errors.
- `actions`: add compact tool-call signatures and tool failures.
- `full`: emit every runtime event, including tool results, usage, retries, and
  compaction activity.

`LoggingObserver` sends the same rendered output to a Python logger.
`JsonlObserver` writes redacted structured events for later inspection. Use
`CompositeObserver` to send one run to multiple destinations. You can set an
observer on `Agent`, pass one for a specific `prompt()` or `prompt_async()`
call, or pass one to `run_many()`. A batch observer automatically adds the task
ID and retry attempt to each event. The existing raw `on_event` callback remains
available for compatibility and custom runtime integrations. Observer failures
are logged and do not fail the agent run. Each observer receives an isolated
deep payload snapshot, so an observer cannot change runtime results or another
observer's input. Serialized tool arguments are parsed before credential
redaction; malformed serialized arguments are hidden rather than logged.

Cancellation and timeouts are cooperative. They signal the runtime through its
existing stop callback and propagate to the async caller immediately. The
synchronous worker may continue until it observes that signal, and its eventual
outcome is consumed in the background. The agent's prompt lock prevents another
prompt from mutating the same state while that worker retires, and closing the
agent waits for active work before releasing resources. Providers and tools
should still implement cooperative cancellation and lower-level timeouts. The
timeout budget includes time queued behind another prompt; a late completion
never replaces an already-raised timeout or cancellation.
In-process tools receive a latched cancellation signal during shutdown. Yoke
waits for a bounded cleanup window before releasing their resources; if a tool
ignores cancellation beyond that window, `close()` raises and leaves the agent
open so cleanup can be retried safely.

## Async Batches

`run_many()` executes independent tasks with bounded concurrency. Supply a
synchronous or asynchronous factory that creates a fresh `Agent` for each task
and retry attempt. Each agent must also own a different provider instance;
reused agents and providers are rejected. The helper closes every accepted
agent after its attempt without closing an active owner when it rejects a
duplicate. Results preserve input order even when tasks finish in a different
order. Synchronous factories run in worker threads and do not block the event
loop.

```python
import asyncio

from yoke.ai import Agent, BatchTask, ConsoleObserver, RunConfig
from yoke.ai import build_builtin_provider, run_many


def make_agent(task: BatchTask) -> Agent:
    return Agent(
        provider=build_builtin_provider("codex:gpt-5.4-mini:low"),
        config=RunConfig(root=".", tools=[]),
    )


async def main() -> None:
    batch = await run_many(
        [
            BatchTask(id="one", prompt="Return the number 1."),
            BatchTask(id="two", prompt="Return the number 2."),
        ],
        agent_factory=make_agent,
        max_concurrency=2,
        timeout=60,
        max_attempts=2,
        observer=ConsoleObserver("actions"),
    )
    for item in batch.items:
        output = item.result.output if item.result else item.error
        print(item.task.id, item.status, output)
    print(batch.usage.total_tokens)


asyncio.run(main())
```

Each item has a terminal status of `completed`, `error`, or `timed_out`, plus
its attempt count, duration, result, and exception. Failures remain isolated in
their input slots rather than cancelling unrelated tasks. `BatchResult.usage`
sums every provider-reported call retained in successful or partial task
transcripts, including tool-loop and structured-output retry calls. Calls that
fail before reporting usage cannot be counted. Usage already present in an
agent's initial or restored transcript is excluded from batch totals.

Pass `on_progress` a synchronous or asynchronous callback accepting one
`BatchProgress` event. Callback failures do not abort work; they are retained in
`BatchResult.progress_errors`. `should_retry` can reject non-transient errors,
and `retry_delay` adds a fixed non-negative delay before another fresh attempt.
A `should_retry` failure becomes the error for that item and does not stop other
items.

Batch observers also receive `batch_attempt_error` events for factory,
registration, prompt, retry-policy, and cleanup failures. The event carries the
task ID, attempt number, and failure stage. Built-in console and logging
observers bound the rendered error text.

`max_attempts` retries failed or timed-out tasks with a newly created agent.
Cancelling `run_many()` cancels queued tasks, cooperatively stops active agents,
waits for cleanup, and then re-raises `asyncio.CancelledError`.
Worker shutdown errors are suppressed in these paths so callers consistently
receive their original `TimeoutError` or `asyncio.CancelledError`.

## Durable Agent State

Use `Agent.save()` and `Agent.load()` to persist SDK agent state across
processes. The snapshot stores portable `AgentState` only: conversation entries,
active skills, and skill directories. It does not store provider clients, API
keys, tool instances, callbacks, shell sessions, or other live runtime objects.
Supply provider and `RunConfig` again when loading.
Codex response identifiers, routing state, and encrypted replay journals are
process-local provider state and are not serialized into `AgentState`. A
resumed process reconstructs provider context from the persisted visible
conversation. Direct compaction-summary requests retain the active provider
scope, then Yoke begins a reduced provider epoch containing stable
instructions, bounded recent user context, the newest handoff, and subsequent
messages. The canonical conversation log remains append-oriented and preserves
omitted history for audit and branching. Providers without native response
journals replay the reduced projection with the same logical cache scope.

```python
from yoke.ai import Agent, RunConfig, build_builtin_provider

provider = build_builtin_provider("codex:gpt-5.5:medium")
config = RunConfig(root=".")

agent = Agent(provider=provider, config=config)
agent.prompt("Summarize the latest input file.")
agent.save("state/summary-agent.json")

resumed = Agent.load(
    "state/summary-agent.json",
    provider=provider,
    config=config,
)
result = resumed.prompt("Draft the follow-up email.")
resumed.save()
print(result.output)
```

For recurring jobs, bind an agent to a state file and opt in to autosave.
If the file exists, the constructor restores it. If it does not exist, the
agent starts empty and uses that path for future saves.

```python
agent = Agent(
    provider=provider,
    config=RunConfig(root=".", tools=["file.read"]),
    state_path="state/nightly-agent.json",
    autosave=True,
)

agent.prompt("Process any new work since the previous run.")
```

Use `restore()` to replace an existing agent's portable state while keeping the
current provider and runtime configuration.

```python
agent.restore("state/checkpoint.json")
```

The default file format is a single human-readable JSON snapshot:

```json
{
  "format": "yoke.agent_state",
  "schema_version": 2,
  "sdk_version": "...",
  "created_at": "...",
  "updated_at": "...",
  "metadata": {},
  "state": { "conversation_entries": [] }
}
```

Treat state files as sensitive: they can contain prompts, model outputs, tool
results, local paths, and proprietary data. Do not load untrusted state into an
agent with privileged tools. Lower-level persistence helpers live in
`yoke.agent.persistence` for applications that need to store
`AgentState` in their own storage layer.

```python
from yoke.agent import capture_agent_state

state = capture_agent_state(agent)
# Store `state.model_dump(mode="json")` in your application's storage layer.
```

Custom runner objects used with CLI runtime helpers should implement
`run(prompt, *, on_event=None, stop_requested=None)`. Set
`supports_message_history = True` to receive `messages` as the second argument,
or `supports_user_message = True` to receive explicit multimodal
`user_message=...` payloads.

Providers may implement `complete_with_cancel(messages, tools, *,
cancel_requested)` for native request cancellation and `fork_for_turn()` for
independent mutable request state. Interactive turns reconstruct providers with
a clone of their `config` when possible; providers with neither hook remain
compatible but must tolerate concurrent calls if a retired request overlaps its
replacement. `Agent.close()` releases owned provider and tool resources.

## Built-In Tools

Import built-in tools from `yoke.agent.tools` and pass capability IDs,
classes, or bound instances to `RunConfig.tools`.

```python
from yoke.agent.tools import ReadTool, EditTool, WriteTool
```

| Class | Runtime name | Purpose |
| --- | --- | --- |
| `ReadTool` | `read` | Read text files from the workspace, with pagination for large files. |
| `EditTool` | `edit` | Replace exact text in files, including targeted occurrences or replace-all edits. |
| `WriteTool` | `write` | Create or overwrite a UTF-8 text file under the workspace. |
| `ApplyPatchTool` | `apply_patch` | Apply codex-style multi-file patches inside the workspace. |
| `ExecCommandTool` / `CommandTool` | `exec_command` | Run shell commands from the workspace root, returning output or a background session ID. |
| `WriteStdinTool` | `write_stdin` | Poll a running command session for up to 1 hour or send interactive input. |
| `FdTool` | `fd` | Run fd for file and directory discovery with regex, glob, ignore, type, extension, and depth behavior. |
| `RipgrepTool` | `rg` | Run ripgrep for fast recursive content search and file listing. |
| `ExtractFileContextTool` | `extract_file_context` | Extract readable text context from documents such as PDFs or Office files. |
| `AttachImageTool` | `attach_image` | Attach local images into the conversation for multimodal follow-up prompts. |
| `ImageGenerationTool` | `image_generation` | Generate or edit images when the active Codex provider supports it. |
| `WebFetchTool` | `web_fetch` | Fetch and page through readable URL content by character offset. |
| `WebSearchTool` | `web_search` | Return quick search links and snippets. |
| `WebResearchTool` | `web_research` | Answer a web research question with concise sources and notes. |
| `SkillTool` | `skill` | Let the agent load configured skills at runtime. |

Most workspace tools can be passed as classes and are bound to `RunConfig.root`
automatically. Pass already-bound instances when you need custom context. Pass
capability IDs such as `"file.write"` when you want the SDK to choose the
provider/model-specific concrete tools.

Capabilities are implemented in `yoke.agent.capabilities`. Each
`BaseCapability` resolves a high-level ability into one or more concrete tools
for the active provider/model, and both the CLI and SDK use that same registry.
Built-in `file.write` exposes `apply_patch` for GPT-family models and
`edit` plus `write` for other models. Image attachment is omitted when a
model is known not to support image input, and `image.generate` resolves only
for a compatible Codex provider. `web_research` uses Codex hosted Responses
web search when available; other providers use bounded local search, fetch, and
provider synthesis. The local workflow applies a shared fetched-source
character budget so many large pages cannot grow context without a bound.
SDK-bound tool classes receive provider context when they are resolved through
a capability or agent runtime.

`WebFetchTool` defaults to the first 20,000 characters of normalized page
content. Use its 1-based `offset` and `limit` fields to continue reading. Set
`mode="raw"` to page through the unprocessed response text. The tool caches the
selected representation for continuation calls and returns `next_offset` while
more content remains. It does not return duplicate summaries, chunks, or links.

## Direct Completion

Use `complete()` when you do not need the agent loop or local tools.

```python
from yoke.ai import complete

result = complete(
    provider=provider,
    sys_prompt="Answer briefly.",
    prompt="Summarize this text in three bullets.",
)

print(result.output)
```

`complete()` does not accept tools or function schemas. Local execution belongs
to `Agent`.

## Images

Use the same `Image` helper with `complete()` and `Agent.prompt()`.

```python
from yoke.ai import Image

result = complete(
    provider=provider,
    prompt="Describe [Image #1].",
    images=[Image.from_path("screenshot.png")],
)
```

```python
result = agent.prompt(
    "Compare [Image #1] and [Image #2].",
    images=[
        Image.from_path("current.png"),
        Image.from_url("https://example.com/reference.png"),
    ],
)
```

CLI and `AttachImageTool` local images are snapshotted as data URLs before they
are stored in session history, which keeps resumed sessions independent from
temporary or later-deleted local files.
The snapshot is stored only in the image message, not duplicated inside the
tool-result JSON sent back to the model.

## Structured Outputs

Pass a Pydantic model as `output_type`.

```python
from pydantic import BaseModel


class ReviewSummary(BaseModel):
    verdict: str
    risks: list[str]


result = agent.prompt(
    "Review the authentication module.",
    output_type=ReviewSummary,
)

summary = result.structured
```

When `output_type` is provided, the SDK asks the model for JSON matching that
schema and validates the final output. If validation fails, it sends a schema
correction system message and retries up to three total attempts before raising
`StructuredOutputError` from `yoke.ai.types` with the raw output
attached. Omit `output_type` for free-form text.

## Skills

Pass materialized `Skill` objects in `RunConfig.skills`.

```python
from yoke.ai.skills import Skill

agent = Agent(
    provider=provider,
    config=RunConfig(
        root=".",
        tools=[ReadTool],
        skills=[
            Skill.from_dir("./skills/code-review"),
            Skill.inline(
                name="repo-style",
                sys_prompt="Prefer minimal patches and explicit typing.",
            ),
        ],
    ),
)
```

## Local Tools

Subclass `LocalTool` for custom executable tools.

```python
from pydantic import Field

from yoke.agent.tools import LocalTool


class EchoTool(LocalTool):
    name = "echo"
    description = "Return the provided text."

    text: str = Field(min_length=1)

    def execute(self) -> dict[str, object]:
        return {"ok": True, "text": self.text}
```

Workspace-aware tools should subclass `WorkspaceTool`.

CLI tool plugins can use provider-aware registration with
`register_tools(context)`. The context exposes `root`, `home`, `provider`,
`provider_name`, `model_id`, `model_name`, `model_key`, `reasoning_effort`, and
`cancel_requested`; interactive CLI sessions refresh this registration when the
active provider or model changes.
