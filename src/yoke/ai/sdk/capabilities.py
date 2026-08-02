"""Provider-aware capability discovery for SDK agent construction."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from yoke.agent.capabilities import CapabilityContext
from yoke.agent.capabilities.builtin import resolve_builtin_capability_id
from yoke.ai.providers.base import Provider


@dataclass(slots=True, frozen=True)
class CapabilityInfo:
    """One selectable SDK capability resolved for a provider and workspace."""

    id: str
    description: str
    available: bool
    tool_names: tuple[str, ...]
    aliases: tuple[str, ...] = ()


_CAPABILITY_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("file.read", "Read workspace files.", ()),
    ("file.search", "Search workspace content and paths.", ()),
    (
        "file.extract_context",
        "Extract readable context from documents and common binary files.",
        ("file.context",),
    ),
    (
        "file.write",
        "Edit workspace files through the active model's preferred interface.",
        ("file.edit",),
    ),
    (
        "shell",
        "Run shell commands and Python in the workspace.",
        ("command_execution",),
    ),
    ("web.fetch", "Fetch URL content.", ()),
    ("web.search", "Search the web.", ()),
    ("web.research", "Research questions using web sources.", ()),
    ("web", "Fetch, search, and research web content.", ()),
    (
        "image.attach",
        "Attach local images when the active model accepts image inputs.",
        ("image.input",),
    ),
    (
        "image.generation",
        "Generate images when the active provider supports image generation.",
        (),
    ),
    ("mcp", "Inspect and call configured MCP servers.", ()),
)


def discover_capabilities(
    provider: Provider | None = None,
    *,
    selection: str | None = None,
    root: str | Path | None = None,
    home: str | Path | None = None,
    capability_ids: Sequence[str] | None = None,
) -> tuple[CapabilityInfo, ...]:
    """Resolve selectable capability IDs for a provider and workspace.

    Pass exactly one provider or selection. Selection-based discovery owns and
    closes its temporary provider. Pass explicit IDs for a focused preflight,
    or omit them to inspect the complete catalog.
    """
    if (provider is None) == (selection is None):
        raise ValueError("Provide exactly one of provider or selection.")
    if isinstance(capability_ids, str):
        raise TypeError("capability_ids must be a sequence of capability ID strings.")
    if selection is not None:
        from yoke.ai.sdk.agent import Agent
        from yoke.ai.sdk.providers import build_builtin_provider
        from yoke.ai.sdk.types import RunConfig

        resolved_root = Path.cwd() if root is None else Path(root)
        owner = Agent(
            provider=build_builtin_provider(selection),
            config=RunConfig(
                root=resolved_root,
                tools=(),
                include_agents_file=False,
            ),
        )
        try:
            return discover_capabilities(
                owner.provider,
                root=resolved_root,
                home=home,
                capability_ids=capability_ids,
            )
        finally:
            owner.close()
    assert provider is not None
    ids = (
        tuple(capability_ids)
        if capability_ids is not None
        else tuple(
            capability_id for capability_id, _description, _aliases in _CAPABILITY_SPECS
        )
    )
    specs = {item[0]: item[1:] for item in _CAPABILITY_SPECS}
    unknown = [capability_id for capability_id in ids if capability_id not in specs]
    if unknown:
        raise ValueError(f"Unknown discoverable capability IDs: {', '.join(unknown)}")
    context = CapabilityContext.from_provider(
        root=Path.cwd() if root is None else Path(root),
        home=Path.home() if home is None else Path(home),
        provider=provider,
    )
    discovered: list[CapabilityInfo] = []
    resources: dict[int, object] = {}
    try:
        for capability_id in ids:
            description, aliases = specs[capability_id]
            registration = resolve_builtin_capability_id(capability_id, context)
            for tool in registration.tools:
                for resource in tool.owned_resources():
                    resources[id(resource)] = resource
            tool_names = tuple(tool.name for tool in registration.tools)
            discovered.append(
                CapabilityInfo(
                    id=capability_id,
                    description=description,
                    available=bool(tool_names or registration.system_messages),
                    tool_names=tool_names,
                    aliases=aliases,
                )
            )
    finally:
        for resource in resources.values():
            close = getattr(resource, "close", None)
            if callable(close):
                close()
    return tuple(discovered)
