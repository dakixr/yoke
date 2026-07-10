# SDK

Embed yoke in Python with a small public surface:

- `Agent` for stateful, tool-using workflows
- `RunConfig` for agent runtime configuration
- `complete()` for direct completions without local tools
- `Image` for multimodal prompt inputs
- `Skill` for composable instruction sets
- `LocalTool` for user-authored executable tools

## Agent

```python
from pathlib import Path

from yoke.agent.tools import EditTool
from yoke.agent.tools import ReadTool
from yoke.ai import Agent
from yoke.ai import OpenCodeGoConfig
from yoke.ai import OpenCodeGoProvider
from yoke.ai import RunConfig

provider = OpenCodeGoProvider(OpenCodeGoConfig(api_key="..."))

agent = Agent(
    provider=provider,
    config=RunConfig(
        root=Path.cwd(),
        sys_prompt="Be concise and precise.",
        tools=[ReadTool, EditTool],
    ),
)

result = agent.prompt("Add type annotations to src/utils.py")
print(result.output)
```

Built-in provider classes include `CodexSubscriptionProvider`,
`CodexWebSockets`, `OpenCodeGoProvider`, and
`ZAIProvider`. For standard OpenAI-compatible endpoints, use
`OpenAICompatibleProvider` with `OpenAICompatibleConfig.from_env()`.

For workflow scripts and automations, construct providers from a qualified
reference string:

```python
from yoke.ai import build_provider, provider_readiness

readiness = provider_readiness()
ready_names = [item.provider_name for item in readiness if item.ready]
if "zai" not in ready_names:
    raise RuntimeError("zai is not ready in this environment")

provider = build_provider("zai:glm-5.2:none")
```

The accepted forms are `provider`, `provider:model`, and
`provider:model:thinking-effort`. Pass `env=` and `home=` to check or build
against a specific environment rather than the current process. Agents should
call `provider_readiness(env=..., home=...)` first to decide which providers
are available before writing or running a workflow. Use `provider_status(...)`
when you need to inspect one specific qualified provider reference.

Providers can contribute model-specific system messages through their model
catalog. Those messages are inserted only for the active provider/model and are
refreshed when the selected model changes:

```python
from yoke.agent.models import Message
from yoke.ai import ProviderModelInfo

ProviderModelInfo(
    id="kimi-k2.7-code",
    display_name="Kimi K2.7 Code",
    context_window_tokens=262_144,
    thinking_levels=("low", "medium", "high"),
    system_messages=(
        Message.system("Use Kimi-specific coding guidance."),
    ),
)
```

Provider classes that do not expose a model catalog can instead implement
`current_model_system_messages(self) -> Iterable[Message]`. CLI sessions, SDK
`Agent`, and direct `complete()` calls all apply the same provider/model prompt
layer.

Providers can optionally implement
`complete_with_cancel(messages, tools, *, cancel_requested)` to abort in-flight
model requests when a turn is stopped or steered. Providers without this hook
remain compatible; yoke checks cancellation before and after their synchronous
`complete()` call.

`Agent` is stateful. Reuse the same object to keep conversation context across
prompts. Call `agent.close()` when finished to release MCP clients and other
closeable resources owned by registered tools. Forked agents share a lease on
any explicitly shared resource, so it is closed only after the last runtime
using it closes.

Initialize an agent with exactly one explicit history representation. Use
`MessageHistory` for a provider transcript or `ConversationEntryHistory` for
structured state:

```python
from yoke.ai import MessageHistory

config = RunConfig(
    root=Path.cwd(),
    history=MessageHistory(previous_messages),
)
```

The tagged history API prevents transcript messages and structured entries
from being supplied together.

For applications that persist agent state, capture structured session state
instead of only transcript messages. Structured entries preserve memory
snapshots and compaction handoffs.

```python
from yoke.agent import capture_agent_state

state = capture_agent_state(agent)
# Store `state.model_dump(mode="json")` in your application's storage layer.
```

`AgentState.leaf_id` identifies the selected leaf when `conversation_entries`
contains multiple branches. Its `messages` projection and state hydration use
only that root-to-leaf path, while persistence can retain the complete tree.
Pass an explicit `leaf_id` when capturing a full tree whose selected branch is
not the last stored entry.

Custom runner objects used with CLI runtime helpers should implement
`run(prompt, *, on_event=None, stop_requested=None)`. Set
`supports_message_history = True` to receive `messages` as the second argument,
or `supports_user_message = True` to receive explicit multimodal
`user_message=...` payloads.

## Capabilities

Capabilities are context-aware bundles that register one or more tools based on
the active provider, model, operating system, and workspace environment.

```python
from yoke.agent.capabilities import FileEditCapability, FileSearchCapability

agent = Agent(
    provider=provider,
    config=RunConfig(
        root=Path.cwd(),
        capabilities=[FileSearchCapability, FileEditCapability],
    ),
)
```

Built-in capability names include `file.read`, `file.context`, `file.search`,
`file.edit`, `command_execution`, `web`, `image.input`, and
`image.generation`. The CLI uses these same agent-owned capabilities for its
built-in tool set, then applies CLI-specific plugin discovery and tool policy.

## Built-In Tools

Import built-in tools from `yoke.agent.tools` and pass the classes or
bound instances to `RunConfig.tools`.

```python
from yoke.agent.tools import ReadTool, EditTool, WriteTool, GrepTool
```

| Class | Runtime name | Purpose |
| --- | --- | --- |
| `ReadTool` | `read` | Read text files from the workspace, with pagination for large files. |
| `EditTool` | `edit` | Replace exact text in files, with optional replace-all behavior. |
| `WriteTool` | `write` | Create or overwrite complete text files. |
| `ApplyPatchTool` | `apply_patch` | Apply codex-style multi-file patches inside the workspace. |
| `ExecCommandTool` (`CommandTool` alias) | `exec_command` | Run a shell command until it exits or yields a background session. |
| `WriteStdinTool` | `write_stdin` | Poll a background command or send it terminal input. |
| `LsTool` | `ls` | List files and directories under a workspace path. |
| `FindTool` | `find` | Find files or directories by glob pattern. |
| `GrepTool` | `grep` | Search text files with a regular expression. |
| `RipgrepTool` | `rg` | Use native ripgrep for file listing and content search. |
| `ExtractFileContextTool` | `extract_file_context` | Extract readable text context from documents such as PDFs or Office files. |
| `AttachImageTool` | `attach_image` | Attach local images into the conversation for multimodal follow-up prompts. |
| `ImageGenerationTool` | `image_generation` | Generate a PNG through Codex subscription auth and attach it to context. |
| `WebFetchTool` | `web_fetch` | Fetch one known URL and return readable Markdown/text, chunks, links, or metadata. |
| `WebSearchTool` | `web_search` | Run a quick DuckDuckGo HTML search and return raw result links/snippets. |
| `WebResearchTool` | `web_research` | Autonomously search, fetch, and synthesize a multi-source research answer with evidence. |
| `SkillTool` | `skill` | Let the agent load configured skills at runtime. |

When the active provider is `codex`, `WebResearchTool`
uses Codex's hosted Responses `web_search` tool in-process through
`ToolRuntimeContext`. Other providers and standalone tool instances use YOKE's
local search-and-fetch pipeline with fast HTML parsing for fetched research
pages; the local synthesis agent can call both `web_fetch` and `web_search`.
Use `web_search` when you only need result URLs/snippets, `web_fetch` when you
already know the URL to inspect, and `web_research` when the task is an
open-ended question, needs current facts, or benefits from multiple sources and
source-grounded synthesis. `web_fetch` uses best-effort document conversion for
HTML, PDFs, Office files, and other known document responses, and falls back to
readable text or binary metadata when extraction fails.

`ImageGenerationTool` is registered only for Codex-backed providers. It sends
the `prompt` to Codex's subscription image endpoint, writes the PNG
to the requested output path, and appends the generated image as follow-up
multimodal context. Optional `referenced_image_paths` and
`num_last_images_to_include` inputs switch it to Codex's image-edit endpoint
with up to five reference images.

`exec_command` and `write_stdin` are available to every model and replace the
former platform-specific `bash`/`powershell` interface. `exec_command` waits up
to `yield_time_ms` and returns a numeric `session_id` when the process remains
active. Command waits default to 30 seconds and accept up to 2 hours. Pass that
ID to `write_stdin` with empty `chars` to poll or non-empty `chars` to interact.
Background commands survive turn interruption and later
turns in the same live runtime, but they are not restored after the yoke process
exits. `ExecCommandTool` and `PythonExecTool` put shims for `python` and
`python3` at the front of `PATH`, so commands and Python subprocesses use the
same interpreter and virtual environment as the running yoke process.

Most workspace tools can be passed as classes and are bound to `RunConfig.root`
automatically. Pass already-bound instances when you need custom context.
Explicit `RunConfig.tools` and `RunConfig.register_tools` are preserved as
compatibility paths and are internally wrapped as capabilities.

`FileEditCapability` exposes model-appropriate writing tools: models whose ID
contains `gpt` receive `apply_patch`; every other model receives `edit` and
`write`. The legacy `register_write_tool` callback delegates to this
capability.

```python
from yoke.agent.tools import register_write_tool

agent = Agent(
    provider=provider,
    config=RunConfig(
        root=Path.cwd(),
        register_tools=register_write_tool,
    ),
)
```

`FileSearchCapability` exposes `rg` when the ripgrep executable is available.
Otherwise it exposes the Python fallback tools `grep`, `find`, and `ls`. The
legacy `register_search_tools` callback delegates to this capability.

```python
from yoke.agent.tools import register_search_tools
```

The CLI uses these selectors automatically. Explicit tool classes remain
available when an SDK application needs a fixed interface.

## Provider-Aware Tools

CLI and SDK tools receive the same public runtime context. Use `self.context`
inside a `LocalTool`:

```python
class ProviderBackedTool(LocalTool):
    name = "provider_backed"
    description = "Run a provider-backed operation."
    execute_in_process = True

    def execute(self) -> dict[str, object]:
        provider = self.context.provider
        return {
            "ok": True,
            "provider_name": self.context.provider_name,
            "model_id": self.context.model_id,
            "model_key": self.context.model_key,
            "reasoning_effort": self.context.reasoning_effort,
        }
```

The raw provider supports direct completions and nested agents. Stable string
metadata should be preferred for provider/model routing. Provider-backed tools
should set `execute_in_process = True`; provider objects are not guaranteed to
be serializable into isolated tool worker processes.

Use a custom `RunConfig.register_tools` callback for other model-dependent
implementations or schemas:

```python
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools import ToolRegistrationResult
from yoke.agent.models import Message


def register_tools(context: ToolRegistrationContext):
    if context.model_key == "opencode-go:kimi-k2.7-code":
        return ToolRegistrationResult(
            tools=[KimiWriteTool.bind(root=context.root)],
            system_messages=[
                Message.system("Use the Kimi write schema conservatively.")
            ],
        )
    return ToolRegistrationResult(
        tools=[SimpleEditTool.bind(root=context.root)],
    )


agent = Agent(
    provider=provider,
    config=RunConfig(
        root=Path.cwd(),
        register_tools=register_tools,
    ),
)
```

`ToolRegistrationContext` exposes `root`, `home`, `provider`,
`provider_name`, `model_id`/`model_name`, `model_key`, and
`reasoning_effort`. Workspace, home, provider, and cancellation callback are
always present. Yoke invokes the callback again when the provider, model, or
reasoning effort changes. `ToolRegistrationResult.system_messages`
contributes tool-use instructions while those registered tools are active.
Re-registration replaces the previous tool instruction layer rather than
appending to it. Plain iterable returns remain supported for callbacks that do
not need prompt contributions. The resulting tools receive matching
`ToolRuntimeContext` metadata.

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

Local images are read and encoded as base64 data URLs at attachment time, so
the full image content is embedded in the session data. This means conversations
remain intact even if the original file on disk is renamed, moved, or deleted.

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
schema and validates the final output. If validation fails, it raises
`StructuredOutputError` with the raw output attached. Omit `output_type` for
free-form text.

## Observe

Yoke Observe records live workflow execution data for SDK workflows. Use it
when a Python workflow creates typed agent outputs, fans work out across
multiple steps, merges results, or needs to be inspected while it is running.

```python
from pydantic import BaseModel

from yoke.ai import Agent, RunConfig
from yoke.observe import step, workflow


class ReviewPlan(BaseModel):
    files: list[str]


class FileReview(BaseModel):
    path: str
    risks: list[str]


class MergeDecision(BaseModel):
    should_merge: bool


@step
def review_file(path: str) -> FileReview:
    result = reviewer.prompt(
        f"Review {path}.",
        output_type=FileReview,
    )
    assert result.structured is not None
    return result.structured


with workflow("review-pr", root=".") as run:
    plan = planner.prompt("Plan the review.", output_type=ReviewPlan)
    assert plan.structured is not None

    reviews = [review_file(path) for path in plan.structured.files]
    decision = merger.prompt(
        f"Merge these reviews: {reviews}",
        output_type=MergeDecision,
    )

print(run.run_id)
```

Inside an active `workflow(...)`, each SDK `Agent` instance becomes one stable
observed agent node for the duration of the run. Reuse the same `Agent` object
when repeated prompts should share conversation context and appear as one
conversation node, such as a coder/reviewer loop. Runtime events from the
existing agent loop are attached under that node, including prompt starts,
model starts and ends, tool execution events, context usage events, and
compaction events. When `output_type` is a Pydantic model, Observe records the
model name, JSON Schema, and a small JSON preview of the latest structured
output produced by that node.

Decorate normal Python functions with `@step` to expose application-level
workflow structure. Step functions can be sync or async. When an observed step
receives a Pydantic value that was produced by an earlier observed node, Yoke
records a dependency edge. This lets dynamic fan-out grow the graph at runtime;
the graph does not need to be known before the workflow runs.

Structured outputs are also a good way to control loops. For example, a
reviewer model can return `ok: bool` and `next_request: str`; the workflow can
continue prompting the same `Agent` until `ok` is true, with an explicit
max-iteration guard. Observe records those repeated prompts on the same agent
node when the same `Agent` instance is reused.

Observe stores events locally under the workflow root:

```text
.yoke/observe/runs/<run_id>/manifest.json
.yoke/observe/runs/<run_id>/events.jsonl
.yoke/observe/runs/<run_id>/artifacts/
```

The JSONL event log is the durable source of truth. The current workflow state
is a projection rebuilt from those events, so viewers can load a state snapshot
and then consume events after the last sequence number.

CLI inspection:

```bash
yoke observe list --root .
yoke observe state latest --root .
yoke observe state latest --root . --json
yoke observe events latest --root .
yoke observe watch latest --root .
```

For a live browser viewer and JSON API, run the local server:

```bash
yoke observe serve --root . --host 127.0.0.1 --port 8787
```

Open the printed URL to see the built-in workflow viewer. The root page is a
run selector; `/runs/{run_id}` shows the workflow graph and node inspector. The
viewer renders the current graph projection, shows node status and typed output
previews, and keeps the sidebar focused on compact node summary data. Use the
node detail button to open a drill-down view with structured input, structured
output, final agent messages, and per-turn prompt/commentary history. It
refreshes from the event stream while the workflow runs.

The built-in graph keeps root-level agents visible, but collapses agent nodes
that run inside an observed `@step` into the parent step. This keeps
implementation details out of the main workflow diagram while preserving the
agent prompt and output events in the run state.

Endpoints:

```text
GET /
GET /runs/{run_id}
GET /runs
GET /runs/{run_id}/state
GET /runs/{run_id}/events?after=123
```

Custom viewers can fetch `/state`, render the projected graph, then poll
`/events?after=<last_sequence>` and apply new events incrementally. The local
store keeps a per-run byte cursor, so sequential polling reads only the newly
appended event-log tail instead of rescanning prior events.

## Skills

Pass materialized `Skill` objects in `RunConfig.skills`.

```python
from yoke.ai import Skill

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

When a directory-backed skill is active, yoke includes the absolute path of
every file in that skill directory in the skill system message. Use this for
skills that have supporting reference files, templates, or examples next to
`SKILL.md`.

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
Its root anchors relative paths but is not a security boundary; `_resolve_path`
also accepts absolute paths and `..` traversal. Applications that accept
untrusted tool arguments should enforce their own path allow-list or sandbox.
