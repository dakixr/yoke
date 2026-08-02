# Progressive capability discovery

Pass stable capability IDs directly to `RunConfig.tools`. Start with only the
operations the task needs, then inspect what those IDs resolve to for the selected
provider, model, OS, and workspace.

## Provider-aware preflight

```python
from pathlib import Path

from yoke.ai import discover_capabilities

resolved = discover_capabilities(
    selection="codex:gpt-5.6-luna:high",
    root=Path.cwd(),
    capability_ids=[
        "file.read",
        "file.search",
        "web.fetch",
    ],
)
for capability in resolved:
    print(
        capability.id,
        "available" if capability.available else "unavailable",
        capability.tool_names,
    )
```

Selection-based discovery creates and closes a temporary provider owner. When
passing an existing `provider=` instead, ownership remains with the caller; close
the `Agent` or application component that owns it. Synchronous code can use
`with Agent(...)`; async code can use `async with Agent(...)`. Never call
`provider.close()` directly because `Provider` has no public close contract.

With no `capability_ids`, `discover_capabilities()` returns the complete stable
catalog, including aliases and unavailable provider-gated entries. Filter that
catalog or pass an explicit list when preflighting one agent.

## Selection ladder

1. Identify the operations the prompt requires.
2. Express them as stable capability IDs.
3. Inspect resolved `available` and `tool_names` values.
4. Remove unavailable or unnecessary IDs from `RunConfig.tools`.
5. Re-run discovery after changing the provider or model.

Important stable IDs include `file.read`, `file.search`,
`file.extract_context`, `file.write`, `shell`, `web.fetch`, `web.search`,
`web.research`, `image.attach`, `image.generation`, and `mcp`. Native aliases
such as `file.edit`, `file.context`, `command_execution`, `image.input`, and
`web` remain accepted.

Provider-aware behavior includes:

- `file.write` resolves to `apply_patch` for GPT-family models and `edit` plus
  `write` for other models.
- `file.search` prefers installed `rg`/`fd`, with Python fallbacks.
- `image.attach` and `image.generation` may resolve unavailable.
- `mcp` resolves unavailable when no server is configured.

Capability discovery is local construction, not remote service health. A provider
or remote tool change still needs a representative real function-tool round trip.
