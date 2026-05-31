# Yoke Codebase Architecture Analysis

Date: 2026-05-31

## Executive Summary

The codebase has a good high-level shape. The main packages map to real concerns:

- `yoke.cli`: command-line UX, interactive prompt loops, rendering, local config, session persistence, provider selection, and workspace bootstrap.
- `yoke.agent`: stateful agent execution, context/memory, compaction, skills, tool abstractions, local tools, and structured conversation state.
- `yoke.ai`: public SDK facade plus provider implementations.

The responsibility split between CLI and agent is mostly sound. The CLI does not directly execute tool calls or manage the provider loop. The agent does not generally know about terminal rendering or interactive input. The best seams are the `Provider` protocol, `LocalTool` abstraction, `ContextManager`, `RuntimeAgent`, `AgentRunner`, and event callback surface.

The main architectural pressure is that the CLI is doing both application orchestration and persistence-format ownership. It knows a lot about `RuntimeAgent` internals, conversation branches, active skills, provider session state, and compaction display payloads. This is manageable at current size, but it is the biggest source of future coupling.

Overall assessment:

- Responsibility distribution: good, with moderate leakage from agent runtime into CLI runtime/session code.
- Module depth: mixed. The agent loop/context modules are reasonably deep. The CLI interactive layer is shallow-to-medium and spread across many small coordination modules. Provider modules vary widely, with `codex_subscription.py` standing out as too large.
- Seams: good at provider/tool/event boundaries; weaker at state hydration, session persistence, SDK/runtime construction, and CLI compaction.
- Test posture: strong breadth. There are 57 test files covering CLI, runtime, tools, providers, SDK, bootstrap, and compaction.

## Repository Shape

The source tree is organized around three product surfaces:

```text
src/yoke/
  cli/       CLI app, interactive UI, rendering, sessions, provider config
  agent/     Runtime loop, context, compaction, tools, skills, state models
  ai/        SDK facade and provider implementations
  docs/      User-facing docs
tests/       57 test files across CLI, agent, SDK, providers, tools
```

Approximate size from `wc -l`:

- `src/yoke`: 25,577 Python lines.
- Largest files:
  - `src/yoke/ai/providers/codex_subscription.py`: 1,807 lines.
  - `src/yoke/ai/providers/github_copilot_subscription.py`: 733 lines.
  - `src/yoke/ai/providers/zai.py`: 663 lines.
  - `src/yoke/ai/providers/opencode_go.py`: 542 lines.
  - Many CLI and agent modules sit in the 250 to 400 line range.

This is not an unhealthy size for a native CLI agent, but the provider layer has a few files that are doing too much.

## Responsibility Distribution

### What the CLI Owns

The CLI appropriately owns:

- Typer/click command definitions and argv normalization in `src/yoke/cli/main.py`.
- Interactive prompt-toolkit and basic input loops under `src/yoke/cli/interactive/`.
- Rendering/status/scrollback under `src/yoke/cli/render/`.
- Session storage in `src/yoke/cli/session.py`.
- CLI runtime orchestration in `src/yoke/cli/runtime/`.
- Workspace bootstrap/config/tool discovery in `src/yoke/cli/bootstrap/` and `src/yoke/cli/tools/`.
- Provider selection and stored defaults in `src/yoke/cli/config/` and `src/yoke/cli/providers/`.

This is the correct product boundary. The CLI is an application shell around the runtime.

### What the Agent Owns

The agent appropriately owns:

- Runtime loop orchestration in `src/yoke/agent/loop/agent.py`.
- Per-iteration control in `src/yoke/agent/loop/iteration.py`.
- Tool preparation/execution/finalization in `src/yoke/agent/loop/tool_core.py` and `src/yoke/agent/loop/tool_runner.py`.
- Conversation context, memory snapshots, compaction, and provider message rendering in `src/yoke/agent/context/` and `src/yoke/agent/compaction/`.
- Canonical message and conversation-entry models in `src/yoke/agent/models.py`.
- Storage-agnostic state capture/hydration primitives in `src/yoke/agent/state.py`.
- Tool abstractions and built-in tools in `src/yoke/agent/tools/`.

This is also the right product boundary. The agent package is usable below the CLI.

### Where Responsibilities Leak

The leaks are not catastrophic, but they are visible:

1. CLI runtime has `RuntimeAgent` special cases.

   In `src/yoke/cli/runtime/cli.py`, fresh and resumed sessions branch on `isinstance(active_agent, RuntimeAgent)` to call `load_conversation`, inject active skills, and expose available skills. The same pattern exists in `src/yoke/cli/runtime/base.py`, where `execute_turn()` has a `RuntimeAgent` branch.

   This means the CLI's supposedly generic `AgentRunner` seam is only partially generic. Custom runners work for simple prompts, but not for the richer persisted state, skill, branch, and image flows unless they mimic `RuntimeAgent` behavior or the CLI gains another branch.

2. Session persistence is CLI-owned but stores agent-native structure.

   `src/yoke/cli/session.py` persists `ConversationEntry`, `Message`, and `ActiveSkill` directly. It also imports migration/transcript helpers from `yoke.agent.state` and private helpers from `yoke.cli.session_tree`.

   This is practical, but it means the on-disk CLI session format is tightly coupled to agent model evolution. That may be acceptable if only this CLI owns sessions, but it is not a clean app/runtime seam.

3. SDK construction reaches through runtime and CLI bootstrap.

   `RuntimeAgent.from_run_config()` imports `yoke.ai.sdk_runtime`, and `yoke.ai.sdk_runtime.load_agents_messages()` delegates to `yoke.cli.bootstrap.agents`. This creates a dependency path from SDK/agent construction back into CLI bootstrap code for `AGENTS.md` loading.

   The implementation is small, but the direction is awkward. Loading workspace instructions is not inherently CLI-specific.

4. Agent tools import CLI runtime config.

   `src/yoke/agent/tools/subagent.py` imports `DEFAULT_SYSTEM_PROMPT` from `yoke.cli.config.runtime`. That makes a built-in agent tool depend on the CLI's prompt/config package. This is a clearer architectural violation than the session coupling.

5. CLI compaction helpers wrap agent compaction with display payload construction.

   `src/yoke/cli/runtime/compaction.py` calls agent compaction APIs, then builds CLI event-like payload dictionaries. That is acceptable as presentation adaptation, but it relies on agent internals being shaped exactly like the CLI event model.

## CLI vs Agent Boundary Verdict

The current split is good enough and mostly well distributed:

- CLI: user interaction, persistence, workspace policy, rendering, process-level config.
- Agent: model loop, context, tools, compaction, canonical conversation model.

The main improvement is to turn the implicit `RuntimeAgent` knowledge in the CLI into explicit protocols. The CLI should not need to know that an agent is specifically `RuntimeAgent`; it should ask whether the agent supports structured state, available skills, active skills, context usage, and compaction.

Recommended seam:

```python
class StatefulAgentRunner(AgentRunner, Protocol):
    has_state: bool
    messages: list[Message]
    conversation_entries: list[ConversationEntry]
    active_skills: list[ActiveSkill]
    available_skills: list[SkillSpec]

    def load_conversation(...): ...
```

Then replace direct `RuntimeAgent` checks with `isinstance(agent, StatefulAgentRunner)` or capability helpers.

## Module Depth

### Deep, Healthy Modules

These modules expose simple APIs while hiding meaningful complexity:

- `RuntimeAgent` in `src/yoke/agent/loop/agent.py`
  - Public surface is `run()`, `prompt()`, `load_conversation()`, `messages`, `conversation_entries`, `fork()`, `reset()`.
  - Internals delegate to iteration, lifecycle, tool runner, context manager.
  - This is a good deep module because callers do not need to understand per-iteration provider/tool/compaction sequencing.

- `ContextManager` in `src/yoke/agent/context/manager.py`
  - Owns initialization, transcript projection, provider-message rendering, compaction preparation/application, and token accounting.
  - It is broad, but cohesive: all operations mutate or project `AgentContext`.
  - The API is deep because callers ask for high-level context operations, not individual log mutations.

- `LocalTool` and `WorkspaceTool` in `src/yoke/agent/tools/base.py`
  - Tool binding, JSON schema exposure, argument parsing, workspace path resolution, and result hooks are hidden behind a stable abstraction.
  - This is one of the strongest seams in the codebase.

- `Provider` in `src/yoke/ai/providers/base.py`
  - A deliberately small protocol: `complete(messages, tools) -> Message`.
  - This is a good anti-corruption boundary around provider complexity.

- `SessionStore` in `src/yoke/cli/session.py`
  - Hides file layout, index maintenance, migration, retention pruning, and JSON validation.
  - It is deep from CLI callers' point of view, although tightly coupled to agent models.

### Modules That Are Too Shallow or Coordination-Heavy

- `src/yoke/cli/runtime/cli.py`
  - It coordinates mode resolution, agent construction, session creation, resume behavior, headless execution, image resolution, error persistence, and interactive dispatch.
  - It is not huge at 372 lines, but it has too many policy decisions in one path.
  - The fresh/resume/headless flows repeat the same state-hydration and persistence patterns.

- `src/yoke/cli/interactive/`
  - The split is understandable, but many files are event/control glue around shared mutable state.
  - This is expected for terminal UI, but it makes reasoning about turn lifecycle harder.
  - `PromptCliState` is a central mutable object with many responsibilities: worker tracking, queued prompts, images, stop requests, toolbar context, spinner, thinking effort, editor preload.

- `src/yoke/cli/config/runtime.py`
  - It builds the full runtime agent from CLI args, loads skills, resolves bootstrap config, creates providers, injects providers into tools, creates context managers, and formats status.
  - This is a natural composition root, but it mixes construction with global defaults and presentation helpers.

### Modules That Are Too Large

- `src/yoke/ai/providers/codex_subscription.py`
  - At 1,807 lines, this should be split. Provider files often grow because auth, streaming/event parsing, request serialization, token usage, model catalog, error mapping, and persistence all accumulate.
  - It is the strongest candidate for decomposition.

- `src/yoke/ai/providers/github_copilot_subscription.py`, `zai.py`, and `opencode_go.py`
  - These are smaller but still large enough to watch.
  - If they repeat request/response/error/model-selection patterns, shared provider support modules would pay off.

## Seams and Interfaces

### Strong Seams

1. Provider seam

   `Provider.complete(messages, tools) -> Message` is simple, stable, and testable. Agent loop code does not depend on provider-specific transports.

2. Tool seam

   `LocalTool` owns schema, parsing, execution, context side effects, and pending context messages. The agent can index and execute tools without caring whether a tool is built-in, repo-defined, or global.

3. Event seam

   Agent loop emits events through `on_event(event, payload)`. CLI renderers consume the stream without the agent importing UI code.

4. Context/compaction seam

   The agent asks `ContextManager` for provider messages and compaction operations. This keeps prompt assembly and memory snapshots out of the core loop.

5. State capture seam

   `yoke.agent.state.capture_agent_state()` is a useful storage-agnostic adapter. It lets CLI persistence avoid reaching only into `RuntimeAgent`, even though other code still does direct checks.

### Weak Seams

1. Stateful agent capability

   `AgentRunner` is too small for the actual CLI feature set. CLI runtime then compensates with `RuntimeAgent` checks.

2. Session format

   Session persistence mixes CLI record metadata with canonical agent runtime entries. The format is useful, but the ownership boundary is blurry.

3. SDK/runtime construction

   `RuntimeAgent.from_run_config()` couples runtime construction to SDK-specific helpers. `sdk_runtime` then delegates `AGENTS.md` loading to CLI bootstrap.

4. Tool context injection

   `build_cli_agent_from_args()` mutates each tool's private `_context` to inject `provider`. That breaks the abstraction of `LocalTool.bind()` and makes provider availability an implicit side channel.

5. Private helper imports

   `src/yoke/cli/session.py` imports `_raw_record_missing_tree_fields`, `_resolve_saved_conversation_tree`, and `_sanitize_conversation_entries` from `cli.session_tree`. Underscore imports are a smell that the session-tree migration/persistence helpers want a public module or a different home.

## Detailed Findings

### 1. CLI Knows Too Much About RuntimeAgent

Examples:

- Fresh sessions call `active_agent.load_conversation()` only if the agent is a `RuntimeAgent`.
- Resume flows directly assign `active_agent.active_skills`.
- Headless execution passes `available_skills` only when the agent is a `RuntimeAgent`.
- `execute_turn()` uses a `RuntimeAgent` branch before falling back to generic `AgentRunner`.

Impact:

- Custom agent runners are second-class in persisted/session flows.
- CLI tests must encode runtime-specific behavior.
- Adding another runtime implementation would require modifying CLI runtime branches.

Recommendation:

- Promote structured state operations to protocols in `yoke.agent.protocols`.
- Move capability detection into small adapter helpers, for example `supports_structured_state(agent)`.
- Make `execute_turn()` depend on protocols instead of concrete classes.

### 2. SessionStore Is Deep but Owns Mixed Concerns

`SessionStore` is a good deep module because it hides JSON read/write, validation, index pruning, migration, and title/root metadata. The weakness is its domain boundary:

- It persists CLI metadata and agent conversation data together.
- It performs migration using agent state functions.
- It imports private helpers from `cli.session_tree`.

Recommendation:

- Keep `SessionStore` as the CLI persistence facade.
- Move conversation-entry migration and sanitization into a public agent module, for example `yoke.agent.conversation_tree`.
- Keep CLI-only session index/title/root logic in `yoke.cli.session`.

### 3. RuntimeAgent Is a Good Center, but Construction Is Split Awkwardly

`RuntimeAgent.__init__()` is clean: provider, tools, context manager, hooks, skills, state. That is a good dependency-injection shape.

The awkward part is `RuntimeAgent.from_run_config()`:

- It imports SDK runtime helpers.
- It transforms SDK `Skill` values into runtime active/spec values.
- It builds system messages.
- It binds tools.

Recommendation:

- Move `from_run_config()` to the SDK facade or a runtime factory module.
- Keep `RuntimeAgent` free of `yoke.ai` imports if possible.
- Move `AGENTS.md` loading to an agent/workspace module used by both CLI and SDK.

### 4. Agent Loop Has Good Internal Decomposition

The loop is split into:

- `agent.py`: public runtime object and run boundary.
- `iteration.py`: one iteration's flow.
- `lifecycle.py`: model call, compaction, event payloads, final result.
- `tool_core.py`: preparation/finalization.
- `tool_runner.py`: sequential/parallel execution.
- `state.py`: run context hydration/persistence.

This is a healthy decomposition. The call chain is readable and each layer has a different level of abstraction.

Potential improvement:

- `lifecycle.py` uses untyped `agent` parameters. A private protocol for the methods/fields it needs would make the internal seam clearer without changing behavior.

### 5. ContextManager Is Cohesive but Getting Broad

`ContextManager` owns enough related behavior to justify its size. It is still cohesive around `AgentContext`, provider-message rendering, and compaction.

Watch areas:

- It knows about prompt building, message transforms, compaction policy, memory snapshots, image counting, and token accounting.
- If more behavior lands here, split by role:
  - `ConversationLogManager`
  - `ProviderContextRenderer`
  - `CompactionCoordinator`

Do not split prematurely. Right now, the module is broad but useful.

### 6. Provider Modules Need Deeper Internal Structure

The provider seam is good externally, but provider implementations are the least balanced part of the repo by file size.

Recommendation for `codex_subscription.py`:

- Split auth/session management from transport.
- Split request/response serialization from streaming/event parsing.
- Split model catalog from completion execution.
- Keep the public provider class as a thin orchestrator.

Likely structure:

```text
yoke/ai/providers/codex_subscription/
  __init__.py
  provider.py
  auth.py
  transport.py
  streaming.py
  models.py
  errors.py
  serialization.py
```

This would make the module deeper: a small public provider class hiding many implementation details.

### 7. Tool Context Injection Should Be Formalized

`build_cli_agent_from_args()` sets `tool._context["provider"] = provider`. That is practical but bypasses the binding abstraction.

Recommendation:

- Add an explicit `bind_runtime_context()` or extend `LocalTool.bind()` usage so provider/root/cancel callbacks are installed in one place.
- Avoid direct private `_context` mutation outside `LocalTool`.

### 8. Interactive CLI State Is Functional but Hard to Reason About

The prompt-toolkit control path uses threads, events, queued prompts, steering prompts, abandoned turn IDs, scrollback callbacks, and mutable state.

This is not inherently wrong; terminal UIs are stateful. The issue is that `PromptCliState` is a large mutable coordination object and lifecycle transitions are spread across `prompt_control.py`, `prompt_turns.py`, `prompt_loop.py`, and `common.py`.

Recommendation:

- Introduce a small `TurnController` domain object that owns state transitions:
  - idle -> running
  - running -> stopping
  - running -> steering
  - running -> completed
  - completed -> queued next
- Let prompt-toolkit callbacks call this controller rather than editing `PromptCliState` directly in many places.

## Design Scorecard

| Area | Assessment | Notes |
| --- | --- | --- |
| CLI/agent responsibility split | Good | Clear top-level packages; leaks around stateful runtime handling |
| Provider seam | Strong | Small protocol isolates transports |
| Tool seam | Strong | `LocalTool`/`WorkspaceTool` are deep and reusable |
| Runtime loop | Strong | Good layering across agent, iteration, lifecycle, runner |
| Context/compaction | Good | Cohesive but broad |
| Session persistence | Medium-good | Deep facade, but coupled to agent internals |
| SDK/runtime split | Medium | Public facade is clean, construction dependency direction is awkward |
| Interactive CLI | Medium | Works as layered UI code, but state transitions are diffuse |
| Provider implementation depth | Medium-low | External seam is good; internal provider files are too large |
| Test coverage shape | Good | Broad tests across most subsystems |

## Prioritized Refactor Plan

### P0: Do Not Refactor These Yet

- Do not break up `RuntimeAgent` just because it is central. It is serving as a useful facade.
- Do not split `ContextManager` until a concrete new responsibility appears. It is broad but still coherent.
- Do not replace the `Provider` protocol. Its simplicity is a strength.

### P1: Firm Up Agent Capability Protocols

Add protocols in `yoke.agent.protocols` for richer CLI features:

- `StatefulAgentRunner`
- `SkillAwareAgentRunner`
- `ContextUsageAgentRunner` if needed
- `CompactionCapableAgentRunner` if forced compaction should be generic

Then replace `RuntimeAgent` checks in CLI runtime with protocol checks.

Expected benefit:

- CLI becomes genuinely runtime-agnostic.
- Tests can use protocol-shaped fakes instead of relying on concrete runtime behavior.
- Future SDK or plugin runners can participate in sessions, images, skills, and branches.

### P2: Move Conversation Tree Helpers Out of CLI Private Modules

Create a public module such as:

```text
src/yoke/agent/conversation_tree.py
```

Move or re-export:

- migration
- branch extraction
- branch merge
- sanitization
- legacy message-to-entry conversion
- transcript projection

Expected benefit:

- `SessionStore` stops importing private helpers.
- The canonical conversation model has one obvious home.
- CLI tree UI can still live in `yoke.cli.runtime.tree` and `yoke.cli.interactive.tree_selector`.

### P3: Untangle SDK Construction From Runtime and CLI Bootstrap

Move `RuntimeAgent.from_run_config()` logic into `yoke.ai.sdk_agent` or `yoke.ai.sdk_runtime`.

Move `AGENTS.md` loading from `yoke.cli.bootstrap.agents` to a neutral module, for example:

```text
src/yoke/agent/workspace_instructions.py
```

Expected benefit:

- `yoke.agent` no longer imports SDK-only construction helpers.
- `yoke.ai` no longer delegates workspace instruction loading to `yoke.cli`.
- CLI and SDK both consume a shared neutral workspace-instruction function.

### P4: Split the Largest Provider

Start with `codex_subscription.py`. Extract by volatility:

1. Auth/token/session state.
2. Request serialization.
3. Streaming/event parsing.
4. Error mapping.
5. Model catalog.

Expected benefit:

- Easier provider-specific tests.
- Less risk when changing auth or streaming behavior.
- A cleaner pattern for other providers.

### P5: Formalize Tool Runtime Binding

Replace direct private `_context` mutation with explicit binding:

```python
tool = tool.with_runtime_context(provider=provider, root=root, cancel_requested=...)
```

or:

```python
tool.bind_runtime(provider=provider)
```

Expected benefit:

- Tool context dependencies become searchable and explicit.
- Built-in tools and custom tools get the same lifecycle.
- Fewer private attribute mutations outside the tool abstraction.

## Suggested Target Architecture

Longer term, aim for this dependency direction:

```text
yoke.cli
  depends on yoke.agent, yoke.ai, yoke.cli.*

yoke.ai
  depends on yoke.agent models/protocols/tools as needed
  does not depend on yoke.cli

yoke.agent
  depends on yoke.ai.providers.base only for Provider protocol, or moves Provider
  protocol to a neutral module
  does not depend on yoke.cli or yoke.ai SDK facade

yoke.agent.tools
  depends on agent primitives
  avoids importing cli config/defaults
```

The current code is close, but there are specific dependency direction violations:

- `yoke.ai.sdk_runtime` imports `yoke.cli.bootstrap.agents`.
- `yoke.agent.tools.subagent` imports `yoke.cli.config.runtime`.
- `RuntimeAgent.from_run_config()` imports SDK construction helpers.

## Final Verdict

This is a solid codebase with a real architecture, not just folders named after concepts. The CLI and agent responsibilities are mostly well distributed, and the core loop/tool/provider seams are good. The best modules are deep in the right way: `RuntimeAgent`, `ContextManager`, `LocalTool`, `SessionStore`, and the provider protocol.

The main work is boundary hardening:

- Make stateful agent capabilities explicit instead of checking `RuntimeAgent`.
- Move conversation tree/state helpers to a neutral agent module.
- Remove CLI dependencies from SDK/runtime/tool code.
- Split the largest provider implementation.
- Formalize tool runtime context binding.

If those changes are made, the architecture will be easier to extend without turning the CLI into a privileged special case around one concrete runtime.
