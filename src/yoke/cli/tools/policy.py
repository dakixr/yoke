"""Configuration policy for yoke tool capabilities and exact tool overrides."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError
from pydantic import field_validator

from yoke._file_io import atomic_write_text
from yoke._file_io import exclusive_file_lock
from yoke.agent.capabilities import known_builtin_capability_ids

GLOB_META_CHARS = ("*", "?", "[")


class ToolPolicy(str, Enum):
    """ToolPolicy."""

    allow = "allow"
    deny = "deny"


class PiConfig(BaseModel):
    """PiConfig."""

    model_config = ConfigDict(extra="forbid")

    capabilities: dict[str, ToolPolicy] = Field(default_factory=dict)
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
        if not normalized:
            raise ValueError("Expected a non-empty provider-supported effort.")
        return normalized


DEFAULT_ALLOWED_CAPABILITY_IDS = (
    "file.read",
    "file.write",
    "file.search",
    "image.attach",
    "image.generate",
    "web.fetch",
    "web.research",
    "web.search",
)


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


def _read_config_file(path: Path) -> LoadedWorkspaceConfig:
    if not path.is_file():
        return LoadedWorkspaceConfig(path=None, config=PiConfig())
    try:
        payload = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Could not read yoke config file `{path}`: {exc}") from exc
    try:
        config = PiConfig.model_validate_json(payload)
    except ValidationError as exc:
        summary = _summarize_config_validation_error(exc)
        raise ValueError(
            f"Invalid yoke config file `{path}`. {summary} "
            "Expected shape: "
            '{"capabilities": {"capability_id": "allow|deny"}, '
            '"tools": {"exact_tool_name": "allow|deny"}, '
            '"default_model": "provider-name:model-name", '
            '"default_reasoning_effort": "provider-supported-level"}. '
            "All fields are optional."
        ) from exc
    return LoadedWorkspaceConfig(path=path, config=config)


def load_config_file(path: Path) -> LoadedWorkspaceConfig:
    """Load one config file, applying the established legacy migration."""
    loaded = _read_config_file(path)
    if not _has_legacy_tool_globs(loaded.config):
        return loaded
    config = _update_config_file(path, lambda current: current, legacy_only=True)
    return LoadedWorkspaceConfig(
        path=path if path.is_file() else None,
        config=config,
    )


def load_workspace_config(root: Path) -> LoadedWorkspaceConfig:
    """load_workspace_config."""
    return load_config_file(root / ".yoke" / "config.json")


def load_global_config(home: Path) -> LoadedWorkspaceConfig:
    """load_global_config."""
    return load_config_file(home / ".yoke" / "config.json")


def _has_legacy_tool_globs(config: PiConfig) -> bool:
    return any(
        any(char in tool_name for char in GLOB_META_CHARS) for tool_name in config.tools
    )


def _migrate_legacy_glob_config(config: PiConfig) -> PiConfig:
    """Replace legacy tool glob policy while preserving model defaults."""
    migrated = default_yoke_config()
    migrated.default_model = config.default_model
    migrated.default_reasoning_effort = config.default_reasoning_effort
    return migrated


def _write_config_file(path: Path, config: PiConfig) -> None:
    atomic_write_text(
        path,
        config.model_dump_json(indent=2, exclude_none=True) + "\n",
    )


def update_config_file(
    path: Path,
    mutator: Callable[[PiConfig], PiConfig],
    *,
    migrate_legacy: bool = False,
) -> PiConfig:
    """Update a complete config under a sibling lock and replace it atomically."""
    return _update_config_file(
        path, mutator, legacy_only=False, migrate_legacy=migrate_legacy
    )


def _update_config_file(
    path: Path,
    mutator: Callable[[PiConfig], PiConfig],
    *,
    legacy_only: bool,
    migrate_legacy: bool = False,
) -> PiConfig:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with exclusive_file_lock(lock_path):
        current = _read_config_file(path).config
        has_legacy_globs = _has_legacy_tool_globs(current)
        if legacy_only and not has_legacy_globs:
            return current
        if has_legacy_globs and (legacy_only or migrate_legacy):
            current = _migrate_legacy_glob_config(current)
        candidate = mutator(current.model_copy(deep=True))
        validated = PiConfig.model_validate(candidate.model_dump(mode="python"))
        _write_config_file(path, validated)
        return validated


def default_yoke_config() -> PiConfig:
    """default_yoke_config."""
    return PiConfig(
        capabilities={
            capability_id: ToolPolicy.allow
            for capability_id in DEFAULT_ALLOWED_CAPABILITY_IDS
        }
    )


def merge_configs(*configs: PiConfig) -> PiConfig:
    """merge_configs."""
    capabilities: dict[str, ToolPolicy] = {}
    tools: dict[str, ToolPolicy] = {}
    default_model: str | None = None
    default_reasoning_effort: str | None = None
    for config in configs:
        capabilities.update(config.capabilities)
        tools.update(config.tools)
        if config.default_model is not None:
            default_model = config.default_model
        if config.default_reasoning_effort is not None:
            default_reasoning_effort = config.default_reasoning_effort
    return PiConfig(
        capabilities=capabilities,
        tools=tools,
        default_model=default_model,
        default_reasoning_effort=default_reasoning_effort,
    )


def is_capability_allowed(capability_id: str, config: PiConfig) -> bool:
    """Return whether an exact capability ID is allowed by policy."""
    return config.capabilities.get(capability_id) == ToolPolicy.allow


def is_tool_allowed(
    name: str,
    config: PiConfig,
    *,
    capability_id: str | None = None,
) -> bool:
    """Return whether an exact tool name is allowed by policy."""
    if capability_id is None:
        allowed = True
    else:
        allowed = is_capability_allowed(capability_id, config)
    override = config.tools.get(name)
    if override is not None:
        allowed = override == ToolPolicy.allow
    return allowed


def unmatched_tool_names(config: PiConfig, known_tool_names: set[str]) -> list[str]:
    """Return exact tool policy keys that do not match a loaded tool name."""
    return [name for name in config.tools if name not in known_tool_names]


def unmatched_capability_ids(
    config: PiConfig, known_capability_ids: set[str] | None = None
) -> list[str]:
    """Return capability policy keys that are not known exact IDs."""
    known = known_capability_ids or known_builtin_capability_ids()
    return [name for name in config.capabilities if name not in known]
