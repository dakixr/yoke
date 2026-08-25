# AI and agent dependency boundary

## Status

Proposed refactor. No implementation is scheduled by this document.

This plan describes a package-boundary problem. It does not propose changes to
agent behavior, provider behavior, persisted session data, or the documented
public SDK API.

## Summary

`yoke.ai` depends on `yoke.agent` for good reasons. The public SDK wraps the
agent runtime, and provider implementations use the runtime's canonical
message types. That direction should remain.

The problem is the reverse direction. The agent runtime imports provider
contracts and call helpers from `yoke.ai.providers`, and the web-research tool
imports the public `yoke.ai.Agent` facade. The result is a package cycle:

```text
yoke.ai
  -> yoke.agent
  -> yoke.ai
```

Small modules make the code navigable, but they do not remove this cycle. A
static import analysis currently places much of the agent and SDK code in one
large strongly connected component.

The proposed rule is simple:

```text
yoke.agent must not import yoke.ai
yoke.ai may import yoke.agent
```

Implement that rule by moving provider contracts and provider-call
coordination into a deep package owned by `yoke.agent`, then make the existing
`yoke.ai.providers` modules compatibility facades where needed. Remove the
web-research tool's dynamic SDK import through dependency injection.

This is smaller and safer than introducing a generic `yoke.core` package or
renaming the whole runtime.

## Current package roles

The names make the present relationship less obvious than it is.

### `yoke.agent`

`yoke.agent` contains the runtime and most shared domain types:

- canonical messages, content parts, tool calls, conversation entries, and
  token usage;
- the agent loop and per-turn lifecycle;
- tool definitions and tool registration;
- context accounting and compaction;
- skills and instruction loading;
- persisted agent state and session-tree operations.

It is already the runtime package, even though it is named `agent`.

### `yoke.ai`

`yoke.ai` contains two different layers:

- the public SDK facade under `yoke.ai.sdk`;
- provider implementations and provider selection under `yoke.ai.providers`.

The SDK creates and controls `yoke.agent.loop.RuntimeAgent`. Provider
implementations translate external request and response formats to the
canonical values in `yoke.agent.models`.

## Current `ai -> agent` dependencies

There are direct `yoke.agent` imports in 35 Python files under `src/yoke/ai`.
Most belong to one of the groups below.

### Canonical conversation values

Provider implementations import these values from `yoke.agent.models`:

- `Message`;
- `MessagePhase` and `Role`;
- `ToolCall` and `ToolFunction`;
- image and text content parts;
- `TokenUsage` and `TokenUsageDetails`.

Representative consumers include:

- `src/yoke/ai/providers/base.py`;
- `src/yoke/ai/providers/openai_compat/content.py`;
- `src/yoke/ai/providers/codex/subscription/sse.py`;
- `src/yoke/ai/providers/codex/websocket/events.py`;
- `src/yoke/ai/providers/zai/streaming.py`.

This dependency is correct. Providers need one runtime-independent message
format, and Yoke already has one. Duplicating those models inside each
provider would add conversions and create opportunities for the formats to
drift.

### Runtime construction

`src/yoke/ai/sdk/runtime.py` imports:

- `RuntimeAgent`;
- `ContextManager`;
- built-in capabilities;
- tool registration values and helpers;
- skill values and registries;
- `AGENTS.md` instruction loading.

This dependency is also correct. The SDK `Agent` is a facade over the runtime,
not a second agent implementation.

### SDK types and prompt execution

The SDK imports runtime hooks, cancellation callbacks, messages, compaction
policy, skills, tool types, and runtime results. The main consumers are:

- `src/yoke/ai/sdk/agent.py`;
- `src/yoke/ai/sdk/types.py`;
- `src/yoke/ai/sdk/prompt_runner.py`;
- `src/yoke/ai/sdk/prompting.py`;
- `src/yoke/ai/sdk/observability.py`.

The SDK converts these runtime values into its public results and adds locking,
async bridging, durability, batches, and observers.

### Persistence

`src/yoke/ai/sdk/agent.py` and `src/yoke/ai/sdk/durable.py` delegate state save
and restore operations to `yoke.agent.persistence`.

This keeps one state format. It should remain that way.

### Public compatibility exports

`src/yoke/ai/__init__.py` lazily re-exports agent state values such as
`AgentStateSnapshot` and its persistence errors. This is an API choice rather
than a runtime dependency.

## Current `agent -> ai` dependencies

These imports create the unwanted half of the cycle.

### Provider contracts and errors

The runtime imports provider values from `yoke.ai.providers.base`:

- `Provider`;
- `ProviderError` and its subclasses;
- `ProviderModelInfo`;
- `ProviderRequestContext`;
- optional provider protocols;
- `fork_provider`;
- `complete_with_cancel`;
- provider event routing and turn lifecycle helpers.

These imports appear in:

- `src/yoke/agent/loop/agent.py`;
- `src/yoke/agent/loop/lifecycle.py`;
- `src/yoke/agent/loop/state.py`;
- `src/yoke/agent/loop/overflow.py`;
- `src/yoke/agent/loop/compaction_summary.py`;
- `src/yoke/agent/loop/tool_registration.py`;
- `src/yoke/agent/budget.py`;
- provider-aware tools under `src/yoke/agent/tools/`.

The runtime owns the requirement expressed by the `Provider` protocol. A
provider implementation satisfies that requirement. Placing the protocol next
to its implementations reverses the dependency: the runtime must import the
package that is supposed to plug into it.

### Usage recording

`complete_with_cancel` records provider usage through
`yoke.ai.providers.usage_log`, while agent execution establishes attribution
through `yoke.ai.providers.usage_context`.

Usage recording is part of provider-call coordination. The CLI and SDK set
attribution, but the runtime is the common caller that guarantees each
completed provider response is recorded. Keeping this coordination under
`yoke.ai` forces the runtime to reach upward.

### Nested web-research agent

`src/yoke/agent/tools/web/research.py` dynamically imports `Agent` and
`RunConfig` from `yoke.ai` when it needs a child agent for synthesis.

The import is local, so it avoids an import-time crash. It still violates the
package rule and makes a low-level built-in tool know about the public SDK
facade.

## Why the cycle matters

The current code works, and Python's local imports and lazy package exports
hide many import-order failures. The cost appears in maintenance rather than
startup errors.

### Ownership is unclear

It is difficult to answer where a new provider-related type belongs. Provider
configuration tends to land under `ai.providers`, but runtime request context,
cancellation, usage, and errors are shared with `agent`. New work can deepen
the cycle without looking wrong in the file being edited.

### Import safety relies on care

`TYPE_CHECKING`, local imports, and lazy `__getattr__` exports are useful tools.
They should solve startup-cost or optional-dependency problems. Using them to
keep mutually dependent packages importable makes refactors fragile.

### Tests do not enforce a direction

Ruff, ty, Pyright, and the test suite can all pass after a new reverse import.
No automated check states that `agent` is below `ai`. The cycle can grow one
reasonable import at a time.

### Reuse becomes harder

A small runtime-only consumer should not need to reason about the public SDK
package. A provider implementation should depend on a stable runtime contract,
not on SDK packaging decisions. The present cycle blurs both cases.

### File splitting gives a misleading signal

Yoke keeps production modules below 400 lines. That is good for reading and
review. It does not constrain coupling. Dozens of small files in one dependency
cycle still behave like one large component when a contract changes.

## Desired dependency direction

The target is:

```text
yoke.agent.models
        ^
        |
yoke.agent.providers
  contracts, calls, events, usage
        ^
        |
yoke.agent runtime and tools
        ^
        |
yoke.ai providers and SDK
        ^
        |
yoke.cli and yoke.mcp_server
```

This diagram describes ownership, not a requirement that every upper package
import every lower package.

The enforceable rule is narrower:

- code under `src/yoke/agent` may import `yoke.agent` and lower-level standard
  or third-party dependencies;
- code under `src/yoke/agent` must not import `yoke.ai` or `yoke.cli`;
- provider implementations under `yoke.ai.providers` may import agent-owned
  contracts and message models;
- the SDK may import the runtime, tools, skills, persistence, and provider
  implementations;
- the CLI and MCP packages remain composition layers.

## Proposed package structure

Add a focused provider-integration package under `yoke.agent`:

```text
src/yoke/agent/providers/
  __init__.py
  contracts.py
  calls.py
  events.py
  usage/
    __init__.py
    context.py
    log.py
    models.py
```

The final split should follow actual responsibilities discovered during the
move. Do not copy the list mechanically if a file would become a thin wrapper
with no independent reason to change.

### `contracts.py`

Own provider-facing types required by the runtime:

- provider protocols;
- provider errors;
- `ProviderModelInfo`;
- `ProviderRequestContext` and response-continuity values;
- cancellable and contextual provider protocols.

This module may import canonical messages from `yoke.agent.models`. It must not
import concrete providers or SDK code.

### `calls.py`

Own runtime coordination around a provider call:

- cancellation checks;
- contextual versus legacy provider dispatch;
- provider forking;
- start-turn and reset notifications;
- usage recording after a completed response.

This is behavior, not a pure contract, so keeping it separate prevents
`contracts.py` from becoming another large base module.

### `events.py`

Own the context-local provider event handler and emission helpers. Concrete
providers can import this module without importing SDK observers.

### `usage/`

Own provider-neutral usage values, attribution context, and durable local
logging. The CLI and SDK set attribution fields; the call coordinator writes
the final record.

If inspection shows that the existing usage files already have clear seams,
move them with minimal edits. This refactor should change ownership before it
changes behavior.

### Compatibility modules

Keep the documented and currently imported paths working during the migration:

```python
# yoke.ai.providers.base
from yoke.agent.providers.contracts import Provider as Provider
from yoke.agent.providers.contracts import ProviderError as ProviderError
```

Use explicit re-exports. Do not create subclasses or duplicate Pydantic models,
because callers and tests may rely on class identity.

The same approach applies to `yoke.ai.providers.usage_context` and
`yoke.ai.providers.usage_log` if those paths have external consumers.

## Web-research dependency injection

Moving provider contracts does not remove the dynamic `yoke.ai` import in the
web-research tool. Handle it separately.

The preferred solution is to inject the child-agent operation through the
existing tool registration context. The tool should request a narrow callable,
not the SDK `Agent` class.

An illustrative contract is:

```python
type ResearchSynthesis = Callable[
    [str, Provider, StopRequested | None],
    str,
]
```

The exact parameters should match the information the tool already has. Avoid
passing the whole SDK or CLI state object.

The composition layer that registers built-in tools supplies the callable. It
may construct an SDK agent, construct a runtime agent directly, or use a
provider-native research path. The web tool only asks for synthesis and handles
the result.

If adding the callable to the general tool context would burden every tool,
define a web-research-specific dependency object and bind it only when that
capability is selected.

Do not solve this by moving `WebResearchTool` into `yoke.ai`. File, web, and
provider-aware tools belong together in the runtime. The issue is the tool's
knowledge of the public SDK, not its current directory.

## Migration plan

Each phase should be independently reviewable and keep all supported imports
working.

### Phase 0: record and enforce the present boundary

1. Add an import-boundary test that scans Python imports under
   `src/yoke/agent`.
2. Allow the current `yoke.ai` imports temporarily through an explicit list.
3. Fail the test when a new reverse import appears.
4. Record the existing exceptions with file and imported module names.

This prevents the problem from growing while later phases are in progress.
Use AST parsing rather than text matching so multiline and aliased imports are
handled correctly.

### Phase 1: move provider contracts and events

1. Create `yoke.agent.providers.contracts` and
   `yoke.agent.providers.events`.
2. Move protocol, error, request-context, model-catalog, and event definitions
   without changing their behavior.
3. Turn `yoke.ai.providers.base` into an explicit compatibility facade.
4. Update agent runtime imports to use the canonical agent-owned paths.
5. Update concrete provider implementations to use the canonical paths where
   doing so does not create a large mixed commit.
6. Add identity assertions for every compatibility re-export.

At the end of this phase, runtime modules should no longer import
`yoke.ai.providers.base`.

### Phase 2: move provider-call coordination

1. Move `complete_with_cancel`, provider lifecycle notification, and provider
   forking into `yoke.agent.providers.calls`.
2. Keep dispatch order, exception translation, and cancellation checks exactly
   as they are.
3. Update the runtime, compaction, overflow recovery, hosted search, and image
   generation to import the new call helpers.
4. Leave compatibility re-exports at the old paths.

This phase is behavior-sensitive. Avoid combining it with cancellation or
retry improvements, even if the existing implementation suggests them.

### Phase 3: move usage attribution and logging

1. Move usage context and record models under `yoke.agent.providers.usage`.
2. Move durable JSONL logging without changing its directory, schema, locking,
   flush, or error behavior.
3. Update the call coordinator to use the canonical logger.
4. Update CLI and SDK attribution contexts.
5. Preserve the existing environment variable and log format.
6. Retain old import paths as explicit re-exports if they have external users.

At the end of this phase, agent execution should have no reason to import
`yoke.ai.providers`.

### Phase 4: remove the web-research SDK import

1. Define the narrow synthesis dependency.
2. Supply it through tool or capability registration.
3. Update `WebResearchTool` to call the injected operation.
4. Preserve provider isolation, cancellation, root selection, tool selection,
   and usage attribution.
5. Add a test that imports and executes the research tool while blocking all
   `yoke.ai` imports from `yoke.agent`.

At the end of this phase, the import-boundary exception list should be empty.

### Phase 5: tighten exports and documentation

1. Replace temporary wildcard or indirect compatibility exports with explicit
   names and `__all__` entries.
2. Document canonical public imports under `yoke.ai`; do not encourage users
   to import the new agent-internal provider modules.
3. Update architecture notes in `src/yoke/docs`.
4. Update the built-in `yoke-subagents` skill, `PATTERNS.md`, and
   `SDK_SURFACE.md` if SDK examples or orchestration imports change.
5. Remove compatibility facades only in a future breaking release and only if
   they were part of a supported API.

## Compatibility requirements

### Public SDK

These imports and behaviors must remain unchanged:

```python
from yoke.ai import Agent, RunConfig, build_builtin_provider
from yoke.ai.providers import Provider, ProviderError
```

The full documented API under `yoke.ai`, `yoke.ai.types`,
`yoke.ai.providers`, `yoke.ai.skills`, and `yoke.ai.utils` must continue to
work.

### Type identity

Compatibility exports must refer to the canonical class object:

```python
from yoke.ai.providers.base import ProviderError as OldProviderError
from yoke.agent.providers.contracts import ProviderError

assert OldProviderError is ProviderError
```

Do not maintain old and new copies of Pydantic models or exception classes.

### Persisted data

The refactor must not change:

- agent snapshot JSON;
- CLI session JSONL;
- usage metric JSONL;
- provider profile storage;
- MCP configuration;
- tool schemas.

Check whether any persisted format stores fully qualified Python names before
moving a class definition. If it does, keep that class at its existing module
or add a tested migration before the move.

### Provider plugins

Global provider plugins may import `yoke.ai.providers` directly. Preserve
those paths. Test at least one representative plugin through discovery,
construction, model selection, completion, cancellation, and usage recording.

### Pickling and process tools

Yoke uses spawned child processes for isolated tool execution. Moving classes
can change their import path during pickling even when ordinary imports remain
compatible. Exercise process-spawn tests on Linux and Windows before accepting
the refactor.

## Test plan

### Boundary tests

Add a test with these assertions:

- no runtime import under `src/yoke/agent` targets `yoke.ai`;
- `TYPE_CHECKING` imports follow the same rule unless a documented exception
  is unavoidable;
- imports inside functions count as dependencies;
- compatibility modules under `yoke.ai` may import canonical agent modules;
- the test prints the source file, line, and imported module on failure.

An AST-based repository test is sufficient. A separate dependency-analysis
package is unnecessary for one rule.

### Contract tests

Test provider protocol dispatch for:

- plain `Provider.complete`;
- contextual completion;
- cancellable completion;
- cancellation before and after the call;
- provider errors with partial messages and conversation entries;
- provider fork success and fallback;
- provider start-turn and reset hooks.

Reuse existing tests where possible. The goal is to prove the move did not
change dispatch behavior.

### Compatibility tests

Import every documented provider type from its existing public path and assert
identity with the canonical definition. Include errors, protocols, model info,
request context, and usage values.

### Usage tests

Verify:

- one record per completed provider response;
- no record for a response that never completed;
- unchanged CLI and SDK attribution;
- unchanged environment-variable redirection;
- concurrent writers retain valid JSONL;
- persistent write failures still reach the caller.

### Web-research tests

Verify that injected synthesis retains:

- provider isolation;
- cooperative cancellation;
- aggregate source budgets;
- hosted-search preference for compatible Codex providers;
- local synthesis for other providers;
- correct usage attribution;
- no import from `yoke.agent` to `yoke.ai`.

### Full validation

Run:

```sh
uv run ruff check .
uv run ruff format .
uv run ty check
uv run pyright
uv run pytest
```

Because the changes affect process pickling, provider cancellation, and import
order, also run the suite from a clean interpreter on Linux and Windows. Do not
accept a result obtained only from tests in a long-lived development process.

## Documentation work

This refactor changes code under `src/yoke`, so update the relevant documents
under `src/yoke/docs` in the same change.

At minimum:

- describe `yoke.agent` as the runtime implementation;
- describe `yoke.ai` as the supported public SDK and provider package;
- keep all examples on public `yoke.ai` imports;
- document provider plugin imports that remain supported;
- avoid documenting `yoke.agent.providers` as public API unless that is an
  intentional new promise.

After changing the SDK, read
`src/yoke/agent/skills/built_in/yoke-subagents/SKILL.md`. Update it,
`PATTERNS.md`, and `SDK_SURFACE.md` if orchestration API names or examples are
affected.

## Release and versioning

The internal move can be backward compatible if all supported imports and data
formats remain intact. In that case, use a patch version bump because this is
an internal architectural fix with no new public feature.

Use a major version bump if implementation requires removing a supported
provider import path or changing a public protocol. Do not call a public break
an internal cleanup.

Before every commit that changes Yoke, synchronize:

- `src/yoke/_version.py`;
- `pyproject.toml`;
- `uv.lock`.

## Risks and controls

### Risk: a move changes runtime behavior

Control it by separating moves from fixes. Copy behavior exactly, land the new
ownership, then improve cancellation, retries, or logging in later commits.

### Risk: compatibility facades recreate the cycle

The old `yoke.ai` modules may import new `yoke.agent` modules. New agent modules
must never import the old facades. The boundary test must inspect the canonical
implementation, not excuse imports because a facade exists.

### Risk: `yoke.agent.providers` becomes a miscellaneous directory

Keep protocols, calls, events, and usage separate. Do not move provider
catalogs, authentication, HTTP clients, or concrete provider configuration out
of `yoke.ai.providers`. Those belong to implementations, not the runtime's
requirements.

### Risk: dependency injection makes tool registration harder to understand

Inject one narrow web-research callable. Do not add a generic service locator
or pass the entire SDK agent into every tool.

### Risk: public users import undocumented modules

Preserve cheap compatibility paths during the refactor. Record which paths are
documented and which are merely observed in repository tests. Defer removals
until there is evidence that carrying a facade has a real cost.

## Suggested commit sequence

Keep commits narrow enough to review by responsibility:

1. Add the import-boundary test with the current exception list.
2. Move provider contracts and events; add compatibility identity tests.
3. Move provider-call coordination without behavior changes.
4. Move usage context and logging without format changes.
5. Inject web-research synthesis and remove its SDK import.
6. Make the boundary exception list empty and update documentation.

Each Yoke-changing commit needs the semantic version files synchronized as
required by `AGENTS.md`. If versioning every intermediate commit makes the
sequence awkward, keep the refactor as one commit only after reviewing the
individual stages locally.

## Acceptance criteria

The work is complete when all of these statements are true:

- no Python module under `src/yoke/agent` imports `yoke.ai`;
- an automated test enforces that rule;
- `yoke.ai` continues to depend on and wrap `yoke.agent`;
- concrete providers use one canonical set of message and provider contracts;
- old supported provider import paths resolve to the same class objects;
- the web-research tool does not know about the public SDK `Agent` class;
- provider dispatch, cancellation, usage recording, persistence, and tool
  schemas behave as before;
- provider plugins continue to load;
- all static checks and tests pass on clean Linux and Windows interpreters;
- SDK, CLI, and built-in orchestration documentation matches the final paths;
- version metadata is synchronized.

## Non-goals

This plan does not propose:

- rewriting the agent loop;
- renaming `yoke.agent` to `yoke.runtime`;
- moving concrete provider implementations under `yoke.agent`;
- duplicating message models between the runtime and providers;
- redesigning cancellation, retries, or usage accounting;
- removing compatibility imports during the initial refactor;
- changing the public `yoke.ai.Agent` API.

An eventual `agent` to `runtime` rename might make package names clearer, but
it would create far more churn than the dependency fix requires. First make
the existing direction honest and enforceable.
