"""tool_policy module."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError
from pydantic import field_validator

from yoke.cli.bootstrap.types import LoadedTool


class ToolPolicy(str, Enum):
    """ToolPolicy."""

    allow = "allow"
    deny = "deny"


class PiConfig(BaseModel):
    """PiConfig."""

    model_config = ConfigDict(extra="forbid")

    tools: dict[str, ToolPolicy] = Field(default_factory=dict)
    default_model: str | None = None
    default_reasoning_effort: str | None = None
    title_model: str | None = None

    @field_validator("tools")
    @classmethod
    def _validate_tool_names(
        cls,
        value: dict[str, ToolPolicy],
    ) -> dict[str, ToolPolicy]:
        invalid = sorted(name for name in value if _looks_like_glob(name))
        if invalid:
            joined = ", ".join(repr(name) for name in invalid)
            raise ValueError(
                "Tool policy keys must be exact tool names, not glob patterns. "
                f"Invalid: {joined}."
            )
        return value

    @field_validator("default_model")
    @classmethod
    def _validate_provider_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if ":" not in normalized:
            raise ValueError("Expected `provider-name:model-name` separated by `:`.")
        provider_name, model_name = normalized.split(":", maxsplit=1)
        if not provider_name.strip() or not model_name.strip():
            raise ValueError(
                "Expected `provider-name:model-name` with both parts non-empty."
            )
        return f"{provider_name.strip().lower()}:{model_name.strip()}"

    @field_validator("title_model")
    @classmethod
    def _validate_title_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized.count(":") < 2:
            raise ValueError("Expected `provider-name:model-name:reasoning-effort`.")
        provider_name, rest = normalized.split(":", maxsplit=1)
        model_name, reasoning_effort = rest.rsplit(":", maxsplit=1)
        provider_name = provider_name.strip().lower()
        model_name = model_name.strip()
        reasoning_effort = _normalize_reasoning_effort(reasoning_effort)
        if not provider_name or not model_name:
            raise ValueError(
                "Expected `provider-name:model-name:reasoning-effort` "
                "with all parts non-empty."
            )
        return f"{provider_name}:{model_name}:{reasoning_effort}"

    @field_validator("default_reasoning_effort")
    @classmethod
    def _validate_reasoning_effort(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_reasoning_effort(value)


BUILTIN_CAPABILITY_NAMES = (
    "command_execution",
    "file.context",
    "file.edit",
    "file.read",
    "file.search",
    "image.generation",
    "image.input",
    "mcp",
    "web",
)

DEFAULT_ALLOWED_TOOL_NAMES = BUILTIN_CAPABILITY_NAMES

TOOL_CAPABILITY_ALIASES = (
    frozenset({"file.edit", "edit", "write", "apply_patch"}),
    frozenset({"file.search", "rg", "grep", "find", "ls"}),
    frozenset({"image.generation", "image_generation"}),
    frozenset({"image.input", "attach_image"}),
)

PROVIDER_GATED_BUILTIN_TOOL_NAMES = frozenset({"image.generation", "image_generation"})


@dataclass(slots=True, frozen=True)
class LoadedWorkspaceConfig:
    """LoadedWorkspaceConfig."""

    path: Path | None
    config: PiConfig


def _summarize_config_validation_error(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False)
    if not errors:
        return "The file is not a valid yoke config JSON document."
    first_error = errors[0]
    error_type = first_error.get("type")
    if error_type == "json_invalid":
        message = first_error.get("msg", "Invalid JSON.")
        return f"Invalid JSON syntax. {message}"
    location = first_error.get("loc") or ()
    location_text = ".".join(str(part) for part in location)
    message = first_error.get("msg", "Invalid value.")
    if location_text:
        return f"Invalid value at `{location_text}`. {message}"
    return str(message)


def load_config_file(path: Path) -> LoadedWorkspaceConfig:
    """load_config_file."""
    if not path.is_file():
        return LoadedWorkspaceConfig(path=None, config=PiConfig())
    try:
        payload = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Could not read yoke config file `{path}`: {exc}") from exc
    try:
        config = PiConfig.model_validate_json(payload)
    except ValidationError as exc:
        summary = _summarize_config_validation_error(exc)
        raise ValueError(
            f"Invalid yoke config file `{path}`. {summary} "
            "Expected shape: "
            '{"tools": {"capability_or_tool_name": "allow|deny"}, '
            '"default_model": "provider-name:model-name", '
            '"default_reasoning_effort": "none|low|medium|high|xhigh", '
            '"title_model": "provider-name:model-name:reasoning-effort"}. '
            "All fields are optional."
        ) from exc
    return LoadedWorkspaceConfig(path=path, config=config)


def load_workspace_config(root: Path) -> LoadedWorkspaceConfig:
    """load_workspace_config."""
    return load_config_file(root / ".yoke" / "config.json")


def load_global_config(home: Path) -> LoadedWorkspaceConfig:
    """load_global_config."""
    return load_config_file(home / ".yoke" / "config.json")


def default_yoke_config() -> PiConfig:
    """default_yoke_config."""
    return PiConfig(
        title_model="codex:gpt-5.4-mini:medium",
    )


def merge_configs(*configs: PiConfig) -> PiConfig:
    """merge_configs."""
    merged: dict[str, ToolPolicy] = {}
    default_model: str | None = None
    default_reasoning_effort: str | None = None
    title_model: str | None = None
    for config in configs:
        merged.update(config.tools)
        if config.default_model is not None:
            default_model = config.default_model
        if config.default_reasoning_effort is not None:
            default_reasoning_effort = config.default_reasoning_effort
        if config.title_model is not None:
            title_model = config.title_model
    return PiConfig(
        tools=merged,
        default_model=default_model,
        default_reasoning_effort=default_reasoning_effort,
        title_model=title_model,
    )


def is_tool_allowed(name: str, config: PiConfig) -> bool:
    """is_tool_allowed."""
    return config.tools.get(name) != ToolPolicy.deny


def policy_target_for_tool(tool: LoadedTool) -> str:
    """Return the policy target for a loaded tool."""
    return tool.capability_name or tool.tool.name


def policy_targets_for_tool(tool: LoadedTool) -> tuple[str, ...]:
    """Return accepted policy targets for a loaded tool."""
    primary = policy_target_for_tool(tool)
    if tool.capability_name is None:
        return (primary,)
    return (primary, tool.tool.name)


def is_loaded_tool_allowed(tool: LoadedTool, config: PiConfig) -> bool:
    """Return whether a loaded tool is allowed by exact policy target."""
    primary = policy_target_for_tool(tool)
    if primary in config.tools:
        return is_tool_allowed(primary, config)
    for aliases in TOOL_CAPABILITY_ALIASES:
        if primary not in aliases:
            continue
        policies = [
            config.tools[target] for target in aliases if target in config.tools
        ]
        if any(policy == ToolPolicy.deny for policy in policies):
            return False
        if any(policy == ToolPolicy.allow for policy in policies):
            return True
    return True


def unmatched_tool_patterns(config: PiConfig, known_tool_names: set[str]) -> list[str]:
    """unmatched_tool_patterns."""
    unmatched: list[str] = []
    for policy_target in config.tools:
        if policy_target not in known_tool_names:
            if policy_target in PROVIDER_GATED_BUILTIN_TOOL_NAMES:
                continue
            if any(
                policy_target in aliases and known_tool_names.intersection(aliases)
                for aliases in TOOL_CAPABILITY_ALIASES
            ):
                continue
            unmatched.append(policy_target)
    return unmatched


def _looks_like_glob(value: str) -> bool:
    return any(char in value for char in "*?[]")


def _normalize_reasoning_effort(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"none", "low", "medium", "high", "xhigh"}:
        raise ValueError("Expected one of none, low, medium, high, or xhigh.")
    return normalized
