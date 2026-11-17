# SDK

The SDK runs Yoke agents inside your own Python process. It does not use the
daemon. Each `Agent` owns its provider, tools and conversation.

| Module | Contents |
| --- | --- |
| `yoke.ai` | `Agent`, `RunConfig`, `Image`, `complete()`, `run_many()`, observers, `build_builtin_provider()` |
| `yoke.ai.types` | Result and error types, including `AgentResult`, `BatchResult` and `StructuredOutputError` |
| `yoke.ai.providers` | Provider and config classes, provider resolution, `UsageLogWriteError` |
| `yoke.ai.skills` | `Skill` |
| `yoke.ai.utils` | `print_builtin_provider_status()`, default configs, message helpers |
| `yoke.agent.tools` | Built-in tool classes, `LocalTool`, `WorkspaceTool` |

## Create an agent

```python
from yoke.ai import Agent, build_builtin_provider

agent = Agent(provider=build_builtin_provider("codex:gpt-6.1-sol:medium"))
result = agent.prompt("Add type annotations to src/utils.py")
print(result.output)
agent.close()
```

An `Agent` keeps its conversation between prompts. Reuse one instance for a
conversation and call `close()` when you are done, which releases its provider
and tool resources.

Without a `config`, the agent works in the current directory with a short
coding system prompt and these capabilities: `file.read`, `file.write`,
`file.search`, `shell`, `image.attach`, `image.generate`, `web.fetch`,
`web.search` and `web.research`. Unlike the CLI, the SDK default includes
`shell`. Pass a `RunConfig` to choose for yourself:

```python
from yoke.ai import Agent, RunConfig, build_builtin_provider

agent = Agent(
    provider=build_builtin_provider("opencode-go:muse-spark-1.3-contributor:high"),
    config=RunConfig(
        root=".",
        tools=["file.read", "file.search", "file.write", "web.research"],
    ),
)
```

| `RunConfig` field | Default | Meaning |
| --- | --- | --- |
| `root` | required | Workspace root for file and process tools. |
| `sys_prompt` | none | System prompt. |
| `tools` | `()` | Capability IDs, tool classes or bound tool instances. |
| `skills` | `()` | `Skill` objects. See [Skills](#skills). |
| `include_agents_file` | `True` | Add `~/.yoke/AGENTS.md` and `<root>/AGENTS.md` to the system prompt. |
| `compaction` | model-based | A `CompactionPolicy`. |
| `tool_execution` | `"parallel"` | How tool calls from one model response run. |
| `before_tool_call`, `after_tool_call` | none | Hooks around each tool call. |
| `messages`, `conversation_entries` | none | Starting conversation. |
| `name` | none | Display name in agent tracking. Use a short role name such as `"reviewer"`. |
| `root_session_id`, `parent_run_id`, `inherit_usage_attribution` | inherited | Usage attribution. See [Usage records](#usage-records). |

Other `Agent` arguments are `state_path`, `autosave` and `observer`, covered
below.

An agent run has no iteration limit. To bound one, pass a `stop_requested`
callback to `prompt()`, or use `prompt_async(..., timeout=...)`.

## Choose a provider

```python
from yoke.ai import build_builtin_provider
from yoke.ai.utils import print_builtin_provider_status

print_builtin_provider_status()     # credentials, models, context windows
provider = build_builtin_provider("zai:glm-5.3-flash:max")
```

`build_builtin_provider()` takes a `provider:model:effort` selection and
defaults to `codex:gpt-6.1-sol:medium`. It resolves the built-in providers
`codex`, `opencode-go` and `zai`, and custom provider plugins in
`~/.yoke/providers/`. It reads credentials from the environment and from the
store that `yoke login` writes. The credentials and models for each provider are
listed in [Configuration](configuration.md#providers-and-credentials).

`build_builtin_provider()` also accepts `env=`, `home=` and `session_id=`.
OpenCode Go sends a stable `x-opencode-session` header per provider. Pass the
same `session_id` when you rebuild a provider and want to keep that identity.

To build a provider directly, use the classes in `yoke.ai.providers`, such as
`OpenCodeGoProvider(OpenCodeGoConfig(api_key=...))`.

Yoke checks TLS certificates on its own connections. `YOKE_DISABLE_TLS=1` turns
that off. An HTTP client you pass in keeps its own TLS settings.

### Retries and timeouts

Built-in providers retry connection failures, rate limits and server errors
with capped exponential backoff and jitter. They honor `Retry-After` in seconds
or as an HTTP date. They retry a read timeout only if no response bytes have
arrived. OpenAI-compatible chat providers, including OpenCode Go chat models,
never retry a read timeout, because the generation may still be running. Once a
stream has started, a failure is final and Yoke does not replay the generation
or run partial tool calls. Response and stream idle timeouts default to 15
minutes.

## Send a prompt

`prompt()` takes text and returns an `AgentResult` with `output`, the final
text. It also accepts these keyword arguments:

| Argument | Use |
| --- | --- |
| `images`, `image_urls` | Attach images. See [Images](#images). |
| `output_type` | A Pydantic model for structured output. |
| `observer`, `on_event` | Watch this prompt. See [Observe a run](#observe-a-run). |
| `stop_requested` | A callable that returns `True` to stop the run. |
| `before_tool_call`, `after_tool_call` | Hooks for this prompt only. |

Results and transcript properties such as `agent.messages` are copies. Changing
them does not change the agent.

### Images

```python
from yoke.ai import Image

result = agent.prompt(
    "Compare [Image #1] and [Image #2].",
    images=[
        Image.from_path("current.png"),
        Image.from_url("https://example.com/reference.png"),
    ],
)
```

`complete()` accepts the same `images` argument. The provider must support image
input. Yoke stores local images in the conversation as resized data URLs, so a
saved agent does not depend on the original file.

### Structured output

```python
from pydantic import BaseModel


class ReviewSummary(BaseModel):
    verdict: str
    risks: list[str]


result = agent.prompt("Review the authentication module.", output_type=ReviewSummary)
summary = result.structured
```

Yoke asks the model for JSON that matches the schema and validates the answer.
If validation fails, it tells the model what was wrong and tries again, up to
three attempts in total. After the third failure it raises
`StructuredOutputError` with the raw output attached.

### Skills

```python
from yoke.agent.tools import ReadTool
from yoke.ai.skills import Skill

agent = Agent(
    provider=provider,
    config=RunConfig(
        root=".",
        tools=[ReadTool],
        skills=[
            Skill.from_dir("./skills/code-review"),
            Skill.inline(name="repo-style", sys_prompt="Prefer small patches and explicit types."),
        ],
    ),
)
```

A `$skill-name` mention in a prompt activates a skill from `RunConfig.skills`
just before that prompt, the same way it works in the CLI. One prompt can
mention several skills, and Yoke activates each one once per prompt.

## Use asyncio

```python
import asyncio

from yoke.ai import Agent, build_builtin_provider


async def main() -> None:
    agent = Agent(provider=build_builtin_provider("codex:gpt-6.1-sol:medium"))
    async with agent:
        result = await agent.prompt_async("Summarize the project.", timeout=120)
        print(result.output)


asyncio.run(main())
```

`prompt_async()` takes the same arguments as `prompt()` plus `timeout`. The agent
loop is synchronous, so `prompt_async()` runs it in a worker thread. `async with`
closes the agent through `aclose()`. `Agent` does not support a plain `with`
block.

Rules for async use:

- Concurrent calls on one agent run one at a time, in order.
- An agent belongs to the first event loop that calls `prompt_async()`. Using it
  from another loop raises `RuntimeError`. Create one agent per loop.
- The timeout includes time spent waiting behind another prompt.
- Cancellation and timeouts stop the run through its stop callback. Your
  `await` returns at once, but the worker thread finishes in the background. A
  new prompt on the same agent waits until that worker stops.
- Closing waits for running work, rejects queued prompts and can be called more
  than once.

Synchronous event handlers and tool hooks run in the worker thread. They must be
thread safe, and they must not prompt, close, save, restore, reset or fork the
same agent.

## Run tasks in parallel

`run_many()` runs independent tasks with a concurrency limit. Give it a factory
that builds a new `Agent`, with its own provider instance, for each task and
each retry.

```python
import asyncio

from yoke.ai import Agent, BatchTask, ConsoleObserver, RunConfig
from yoke.ai import build_builtin_provider, run_many


def make_agent(task: BatchTask) -> Agent:
    return Agent(
        provider=build_builtin_provider("codex:gpt-6.1-sol:medium"),
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

| Argument | Default | Meaning |
| --- | --- | --- |
| `agent_factory` | required | Sync or async callable that takes a `BatchTask` and returns a new `Agent`. |
| `max_concurrency` | 8 | Tasks running at once. |
| `timeout` | none | Seconds per attempt. |
| `max_attempts` | 1 | Attempts per task, each with a new agent. |
| `retry_delay` | 0 | Seconds between attempts. |
| `should_retry` | retry all | Called with the exception. Return `False` to stop retrying. |
| `on_progress` | none | Sync or async callback that takes a `BatchProgress`. |
| `output_type` | none | Pydantic model for every task. |
| `observer` | none | Observer for every task. Events carry the task ID and attempt. |

`run_many()` rejects an agent or provider that it has already seen, and closes
each agent after its attempt. Results come back in input order. Each item has a
status of `completed`, `error` or `timed_out`, plus attempt count, duration,
result and exception. One failed task does not cancel the others.

`BatchResult.usage` adds up the token counts every attempt reported, including
retries. It excludes usage already in an agent's starting transcript. Calls that
failed before reporting usage are not counted. Errors raised by `on_progress`
are collected in `BatchResult.progress_errors` and do not stop the batch.

Cancelling `run_many()` cancels queued tasks, stops running agents, waits for
cleanup and then raises `asyncio.CancelledError`.

## Observe a run

An observer shows what an agent is doing while it works.

```python
from yoke.ai import Agent, ConsoleObserver

agent = Agent(provider=provider, observer=ConsoleObserver("actions", label="reviewer"))
```

| Detail level | Shows |
| --- | --- |
| `quiet` | Nothing. |
| `messages` | Commentary, the final message and errors. |
| `actions` | `messages`, plus one line per tool call and tool failures. The best level for subagents. |
| `full` | Every runtime event, including tool results, usage, retries and compaction. |

| Observer | Output |
| --- | --- |
| `ConsoleObserver(detail="actions", stream=None, label=None, max_length=1000)` | Text on stdout, or on `stream`. |
| `LoggingObserver(logger, detail="actions", level=logging.INFO, label=None, max_length=1000)` | Text through a Python logger. |
| `JsonlObserver(path, detail="full", label=None)` | One JSON event per line. |
| `CompositeObserver` | Sends events to several observers. |

Set an observer on the `Agent`, pass one to a single `prompt()` call, or pass one
to `run_many()`. The older `on_event` callback still receives raw runtime events.

Observers get a private copy of each event, so they cannot change results or
each other's input. If an observer raises, Yoke logs the error and the run continues.
Observers hide argument values whose keys look like credentials, such as
`accessToken` or `authToken`. Token counters such as `input_tokens` stay
visible.

## Save and restore state

`Agent.save()` and `Agent.load()` keep an agent's conversation across processes.

```python
from yoke.ai import Agent, RunConfig, build_builtin_provider

provider = build_builtin_provider("codex:gpt-6.1-sol:medium")
config = RunConfig(root=".")

agent = Agent(provider=provider, config=config)
agent.prompt("Summarize the latest input file.")
agent.save("state/summary-agent.json")

resumed = Agent.load("state/summary-agent.json", provider=provider, config=config)
result = resumed.prompt("Draft the follow-up email.")
resumed.save()
```

For a recurring job, give the agent a state file and turn on autosave. If the
file exists, the constructor loads it. If not, the agent starts empty. With
autosave on, the agent saves to that path after each completed tool result and
at the end of each prompt.

```python
agent = Agent(
    provider=provider,
    config=RunConfig(root=".", tools=["file.read"]),
    state_path="state/nightly-agent.json",
    autosave=True,
)
agent.prompt("Process any new work since the previous run.")
```

`agent.restore(path)` replaces the conversation of an existing agent and keeps
its provider and config.

The state file is JSON:

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

It holds conversation entries, active skills and skill directories. It does not
hold the provider, API keys, tools, callbacks or running processes, so pass the
provider and `RunConfig` again when loading. Provider response chains are not
saved either. A loaded agent rebuilds the provider context from the visible
conversation.

State files contain prompts, outputs, tool results and paths. Treat them as
sensitive, and do not load an untrusted state file into an agent with shell or
write tools.

To keep state in your own storage, use `yoke.agent.capture_agent_state()` and
the helpers in `yoke.agent.persistence`:

```python
from yoke.agent import capture_agent_state

state = capture_agent_state(agent)
payload = state.model_dump(mode="json")
```

## Call a model without tools

`complete()` sends one request with no agent loop and no tools.

```python
from yoke.ai import complete

result = complete(
    provider=provider,
    sys_prompt="Answer briefly.",
    prompt="Summarize this text in three bullets.",
)
print(result.output)
```

`complete()` also takes `messages`, `images`, `image_urls` and `output_type`.
It does not accept tools. Use `Agent` when the model needs to act.

## Built-in tools

Pass capability IDs, tool classes or bound tool instances in `RunConfig.tools`.
A capability ID such as `"file.write"` lets Yoke pick the right tools for the
provider and model. Classes are bound to `RunConfig.root`. Unknown capability
IDs fail before any tool is built.

```python
from yoke.agent.tools import EditTool, ReadTool, WriteTool
```

| Class | Tool name | Does |
| --- | --- | --- |
| `ReadTool` | `read` | Read a text file, a page at a time. |
| `EditTool` | `edit` | Replace exact text in a file. |
| `WriteTool` | `write` | Create or overwrite a UTF-8 file. |
| `ApplyPatchTool` | `apply_patch` | Apply a multi-file patch in Codex format. |
| `CommandExecTool` | `command_exec` | Run a shell command or an `argv` program. Also importable as `ExecCommandTool` and `CommandTool`. |
| `PythonExecTool` | `python_exec` | Run Python code with its own execution timeout. |
| `ProcessInputTool` | `process_input` | Send input to a running process. |
| `ProcessReadTool` | `process_read` | Wait for processes, read output or take a snapshot. |
| `ProcessCancelTool` | `process_cancel` | Stop a process tree and keep its final output. |
| `FdTool` | `fd` | Find files by name, type, extension, depth, time or size. |
| `RipgrepTool` | `rg` | Search file contents or list files. |
| `ExtractFileContextTool` | `extract_file_context` | Extract text from PDFs, Office files and images. |
| `AttachImageTool` | `attach_image` | Add a local image to the conversation. |
| `ImageGenerationTool` | `image_generation` | Generate or edit images with Codex. |
| `WebFetchTool` | `web_fetch` | Read a URL, 20,000 characters at a time by default. |
| `WebSearchTool` | `web_search` | Return search links and snippets. |
| `WebResearchTool` | `web_research` | Answer a question from several web sources. |
| `SkillTool` | `skill` | Let the agent activate skills. |

The process tool classes are also in `yoke.agent.tools.processes`, and their
request models are in `yoke.agent.tools.processes.models`. Treat process
cursors as opaque strings and pass them back unchanged.
[Shared process tools](process-tools.md) documents waits, paging and result
fields.

`fd` and `rg` take typed fields only, not raw command-line flags. Use
`command_exec` for pipelines. `web_fetch` takes a 1-based `offset` and `limit`,
returns `next_offset` while more text remains, and accepts `mode="raw"` for the
unprocessed response.

Tools return full JSON results, which the conversation keeps. Before sending a
request, Yoke shrinks the results of `rg`, `fd`, `apply_patch`, `web_search` and
the process tools into a smaller form for the model. Hooks and message
transforms see the full result.

### Custom tools

Subclass `LocalTool`, or `WorkspaceTool` for a tool that works with files in the
workspace. Fields are Pydantic fields and become the tool's arguments.

```python
from pydantic import Field

from yoke.agent.tools import LocalTool


class EchoTool(LocalTool):
    name = "echo"
    description = "Return the provided text."

    text: str = Field(min_length=1)

    def execute(self) -> dict[str, object]:
        return {"ok": True, "text": self.text}


agent = Agent(provider=provider, config=RunConfig(root=".", tools=[EchoTool]))
```

The CLI loads tools from plugin files. See
[Configuration](configuration.md#custom-tools).

## Delegate work to other agents

The built-in [`yoke-subagents` skill](../agent/skills/built_in/yoke-subagents/SKILL.md)
shows how an agent can start and coordinate SDK agents. It has short examples
for one worker, two parallel audits and a role that persists across processes.
Its larger examples add task contracts, traces and results under
`.agents_local/`.

## Automatic agent tracking

When an SDK agent runs inside a process started by Yoke's `command_exec` or
`python_exec`, it reports each prompt to the session that started it. Your
script needs no extra code. Set `RunConfig(name=...)` to give it a display name.

You can see these agents in the web UI's Agents panel, with `/agents` in the
CLI, and through the MCP `agent_runs` tool. HTTP and ACP clients get the same
data. Each entry shows a name, model, status and last tool. Prompts, tool
arguments, outputs and error messages are not reported.

`Agent.agent_id` identifies the agent instance. Each prompt gets its own run ID,
which matches `sdk_run_id` in usage records. A fork or a loaded agent gets a new
ID.

The rules:

- A prompt fails if it cannot register with its session before its first
  provider call. Later reporting failures do not affect the result.
- A missed heartbeat or lost connection marks a run's status as unknown, never
  as succeeded or failed.
- Tracking never controls the agent. Watching it does not stop it.
- A process may have at most 4096 tracked agents open, counting ones waiting to
  be released. Close agents you no longer need.
- An agent outside a Yoke-started process is not tracked.

Session IDs in the environment do not grant the right to report into a session.
Yoke gives each started process a private capability for that.

## Usage records

Yoke records every completed provider response, from the SDK, the CLI and the
HTTP API, in the `usage_events` table of `~/.yoke/yoke.db`. Set `YOKE_DB_PATH`
to use another file.

A record has the provider, model, completion time, token counts and attribution.
Attribution is the caller kind in `surface`, the `call_kind`, and either the
session ID or the SDK operation and `sdk_run_id`. It may include a `session_title` of up to 80
characters. It never contains prompts, responses, tool data, paths or batch task
IDs. A provider that reports no token counts still gets a record with an empty
`usage` object.

| SDK operation | `sdk_run_id` groups |
| --- | --- |
| `complete` | one `complete()` call |
| `agent` | one `prompt()` |
| `run_many` | one batch attempt |

`call_kind` separates direct completions, model iterations, structured output
retries, compaction summaries, branch summaries and overflow retries.

Read records with:

```python
from yoke.ai.providers.usage_writer import read_usage_events

for event in read_usage_events(provider="codex", since="2026-11-01T00:00:00Z"):
    print(event)
```

The SDK writes to the database only if it already exists with a schema version
this release supports. Otherwise it skips the record, logs at debug level and
lets the provider call succeed. Start the Yoke daemon to create or upgrade the
database. Any other write failure raises `UsageLogWriteError`, and no partial
row is left.

### Attribution across processes

`command_exec` and `python_exec` set `YOKE_ROOT_SESSION_ID` and
`YOKE_PARENT_RUN_ID` for the processes they start. An SDK agent in such a
process records the root session and the run that launched it, so you can add up
a session's total usage. These records keep `surface="sdk"` and their own
`sdk_run_id`, and set `session_id` to the root session. They do not change the
parent conversation's token counters.

An agent reads this context when you construct it. `complete()` reads it on
each call. Override it with `root_session_id` and `parent_run_id` on `RunConfig`
or `complete()`, or set `inherit_usage_attribution=False` to ignore the
inherited values. A loaded agent takes attribution from the process that loads
it. Remote processes need the variables forwarded explicitly.

## Custom providers

A provider must implement `complete(messages, tools)`. Custom providers are
plugins in `~/.yoke/providers/`. See
[Configuration](configuration.md#custom-providers).

These optional methods matter:

| Method | Why |
| --- | --- |
| `complete_with_cancel(messages, tools, *, cancel_requested)` | Lets Yoke cancel a request in flight. Without it Yoke calls `complete()`. |
| `fork_for_turn()` | Returns an independent copy for a forked agent or an isolated turn. Without it Yoke logs a warning and shares the instance, which then must tolerate overlapping calls. |

Built-in providers subclass `yoke.ai.providers.base.BaseProvider`. A forked
built-in provider shares an injected HTTP client and does not close it.
`provider_identity(provider)` in the same module returns the `name`, `model` and
`reasoning_effort` Yoke sees, with `unknown` for an unnamed provider.

If a provider plugin's factory returns an instance that fails validation, Yoke
closes that instance and reports the original error.
