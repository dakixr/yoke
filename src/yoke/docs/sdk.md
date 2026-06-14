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

`Agent` is stateful. Reuse the same object to keep conversation context across
prompts.

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

Custom runner objects used with CLI runtime helpers should implement
`run(prompt, *, on_event=None, stop_requested=None)`. Set
`supports_message_history = True` to receive `messages` as the second argument,
or `supports_user_message = True` to receive explicit multimodal
`user_message=...` payloads.

## Built-In Tools

Import built-in tools from `yoke.agent.tools` and pass the classes or
bound instances to `RunConfig.tools`.

```python
from yoke.agent.tools import ReadTool, EditTool, GrepTool
```

| Class | Runtime name | Purpose |
| --- | --- | --- |
| `ReadTool` | `read` | Read text files from the workspace, with pagination for large files. |
| `EditTool` | `edit` | Replace exact text in files, including targeted occurrences or replace-all edits. |
| `ApplyPatchTool` | `apply_patch` | Apply codex-style multi-file patches inside the workspace. |
| `CommandTool` | `bash` on macOS/Linux, `powershell` on Windows | Run shell commands from the workspace root. |
| `LsTool` | `ls` | List files and directories under a workspace path. |
| `FindTool` | `find` | Find files or directories by glob pattern. |
| `GrepTool` | `grep` | Search text files with a regular expression. |
| `RipgrepTool` | `rg` | Use native ripgrep for file listing and content search. |
| `ExtractFileContextTool` | `extract_file_context` | Extract readable text context from documents such as PDFs or Office files. |
| `AttachImageTool` | `attach_image` | Attach local images into the conversation for multimodal follow-up prompts. |
| `WebFetchTool` | `web_fetch` | Fetch a URL and return readable Markdown or text content. |
| `WebSearchTool` | `web_search` | Search the web using DuckDuckGo HTML results. |
| `WebResearchTool` | `web_research` | Answer a web research question with concise sources and notes. |
| `SkillTool` | `skill` | Let the agent load configured skills at runtime. |

When the active provider is `codex` or `codex-websockets`, `WebResearchTool`
uses Codex's hosted Responses `web_search` tool in-process through
`ToolRuntimeContext`. Other providers and standalone tool instances use YOKE's
local search-and-fetch pipeline with fast HTML parsing for fetched research
pages; the local synthesis agent can call both `web_fetch` and `web_search`.

`CommandTool` and `PythonExecTool` put shims for `python` and `python3` at the
front of `PATH`, so shell commands and Python subprocesses use the same
interpreter and virtual environment as the running yoke process.

Most workspace tools can be passed as classes and are bound to `RunConfig.root`
automatically. Pass already-bound instances when you need custom context.

`register_write_tool` exposes one model-appropriate writing interface: models
whose ID contains `gpt` receive `apply_patch`; every other model receives
`edit`.

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

`register_search_tools` exposes `rg` when the ripgrep executable is available.
Otherwise it exposes the Python fallback tools `grep`, `find`, and `ls`.

```python
from yoke.agent.tools import register_search_tools
```

The CLI and nested agents use this selector automatically. Explicit tool
classes remain available when an SDK application needs a fixed interface.

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
