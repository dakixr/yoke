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
`CodexWebSockets`, `GitHubCopilotProvider`, `OpenCodeGoProvider`, and
`ZAIProvider`. For standard OpenAI-compatible endpoints, use
`OpenAICompatibleProvider` with `OpenAICompatibleConfig.from_env()`.

`Agent` is stateful. Reuse the same object to keep conversation context across
prompts.

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
| `ExtractFileContextTool` | `extract_file_context` | Extract readable text context from documents such as PDFs or Office files. |
| `AttachImageTool` | `attach_image` | Attach local images into the conversation for multimodal follow-up prompts. |
| `WebFetchTool` | `web_fetch` | Fetch a URL and return readable Markdown or text content. |
| `WebResearchTool` | `web_research` | Answer a web research question with concise sources and notes. |
| `SkillTool` | `skill` | Let the agent load configured skills at runtime. |

`CommandTool` and `PythonExecTool` put shims for `python` and `python3` at the
front of `PATH`, so shell commands and Python subprocesses use the same
interpreter and virtual environment as the running yoke process.

Most workspace tools can be passed as classes and are bound to `RunConfig.root`
automatically. Pass already-bound instances when you need custom context.

When the CLI builds tools, it also injects the current provider into tool
context. Provider-aware tools such as `web_research` can use that provider to
spawn a scoped sub-agent with a restricted tool set and return synthesized
structured output. `web_research` chooses its own research depth and encourages
the scoped sub-agent to review many sources, commonly 20+ for non-trivial
questions. SDK-bound tool classes do not receive a provider unless you bind one
explicitly.

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
