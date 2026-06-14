"""tool_policy module."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError
from pydantic import field_validator


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

    @field_validator("default_model")
    @classmethod
    def _validate_default_model(cls, value: str | None) -> str | None:
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

    @field_validator("default_reasoning_effort")
    @classmethod
    def _validate_default_reasoning_effort(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized not in {"none", "low", "medium", "high", "xhigh"}:
            raise ValueError("Expected one of none, low, medium, high, or xhigh.")
        return normalized


DEFAULT_ALLOWED_TOOL_NAMES = (
    "apply_patch",
    "attach_image",
    "bash",
    "edit",
    "extract_file_context",
    "find",
    "grep",
    "image_generation",
    "ls",
    "python_exec",
    "read",
    "rg",
    "subagent",
    "web_fetch",
    "web_research",
    "web_search",
)

TOOL_CAPABILITY_ALIASES = (
    frozenset({"edit", "apply_patch"}),
    frozenset({"rg", "grep", "find", "ls"}),
)

PROVIDER_GATED_BUILTIN_TOOL_NAMES = frozenset({"image_generation"})


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
            '{"tools": {"tool_name_or_glob": "allow|deny"}, '
            '"default_model": "provider-name:model-name", '
            '"default_reasoning_effort": "none|low|medium|high|xhigh"}. '
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
    tools = {"*": ToolPolicy.deny}
    tools.update(
        {tool_name: ToolPolicy.allow for tool_name in DEFAULT_ALLOWED_TOOL_NAMES}
    )
    return PiConfig(tools=tools)


def merge_configs(*configs: PiConfig) -> PiConfig:
    """merge_configs."""
    merged: dict[str, ToolPolicy] = {}
    default_model: str | None = None
    default_reasoning_effort: str | None = None
    for config in configs:
        merged.update(config.tools)
        if config.default_model is not None:
            default_model = config.default_model
        if config.default_reasoning_effort is not None:
            default_reasoning_effort = config.default_reasoning_effort
    return PiConfig(
        tools=merged,
        default_model=default_model,
        default_reasoning_effort=default_reasoning_effort,
    )


def is_tool_allowed(name: str, config: PiConfig) -> bool:
    """is_tool_allowed."""
    allowed = True
    for pattern, policy in config.tools.items():
        if fnmatch(name, pattern):
            allowed = policy == ToolPolicy.allow
    return allowed


def unmatched_tool_patterns(config: PiConfig, known_tool_names: set[str]) -> list[str]:
    """unmatched_tool_patterns."""
    unmatched: list[str] = []
    for pattern in config.tools:
        if not any(fnmatch(name, pattern) for name in known_tool_names):
            if pattern in PROVIDER_GATED_BUILTIN_TOOL_NAMES:
                continue
            if any(
                pattern in aliases and known_tool_names.intersection(aliases)
                for aliases in TOOL_CAPABILITY_ALIASES
            ):
                continue
            unmatched.append(pattern)
    return unmatched
