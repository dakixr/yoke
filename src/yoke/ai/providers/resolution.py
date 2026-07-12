"""Public provider readiness and construction helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.builtin_registry import BUILTIN_PROVIDER_NAMES
from yoke.ai.providers.builtin_registry import build_registered_provider
from yoke.ai.providers.builtin_registry import list_registered_models
from yoke.ai.providers.credentials import provider_environment
from yoke.ai.providers.plugins import available_custom_provider_names
from yoke.ai.providers.plugins import create_custom_provider
from yoke.ai.providers.plugins import list_custom_provider_models


@dataclass(slots=True, frozen=True)
class ProviderRef:
    """Parsed provider reference."""

    provider_name: str
    model: str | None = None
    reasoning_effort: str | None = None

    @property
    def qualified_name(self) -> str:
        """Return `provider:model:thinking` with missing parts omitted."""
        parts = [self.provider_name]
        if self.model is not None:
            parts.append(self.model)
        if self.reasoning_effort is not None:
            parts.append(self.reasoning_effort)
        return ":".join(parts)


@dataclass(slots=True, frozen=True)
class ProviderReadiness:
    """Constructability and catalog status for one provider in one environment."""

    provider_name: str
    ready: bool
    reason: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    models: tuple[ProviderModelInfo, ...] = ()


def parse_provider_ref(value: str) -> ProviderRef:
    """Parse `provider`, `provider:model`, or `provider:model:thinking`."""
    parts = [part.strip() for part in value.split(":")]
    if len(parts) > 3:
        raise ValueError(
            "Expected `provider`, `provider:model`, or "
            "`provider:model:thinking-effort`."
        )
    provider_name = parts[0].lower() if parts else ""
    if not provider_name:
        raise ValueError("Provider name must be non-empty.")
    model = _normalized_optional(parts[1]) if len(parts) >= 2 else None
    reasoning_effort = (
        _normalized_optional(parts[2].lower()) if len(parts) == 3 else None
    )
    if len(parts) == 3 and model is None:
        raise ValueError("Provider reference cannot include thinking without model.")
    return ProviderRef(
        provider_name=provider_name,
        model=model,
        reasoning_effort=reasoning_effort,
    )


def build_provider(
    qualified_name: str,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
    session_id: str | None = None,
) -> Provider:
    """Build a provider from `provider:model:thinking-effort`."""
    provider_ref = parse_provider_ref(qualified_name)
    resolved_home = _resolved_home(home)
    resolved_env = _resolved_env(env, home=resolved_home)
    credential_issue = _credential_issue(
        provider_ref.provider_name,
        env=resolved_env,
        home=resolved_home,
    )
    if credential_issue is not None:
        raise ValueError(credential_issue)
    if provider_ref.provider_name in BUILTIN_PROVIDER_NAMES:
        return build_registered_provider(
            provider_ref.provider_name,
            model=provider_ref.model,
            reasoning_effort=provider_ref.reasoning_effort,
            session_id=session_id,
            env=resolved_env,
            home=resolved_home,
        )
    provider = create_custom_provider(
        provider_ref.provider_name,
        model=provider_ref.model,
        reasoning_effort=provider_ref.reasoning_effort,
        session_id=session_id,
        home=resolved_home,
        env=resolved_env,
    )
    if provider is None:
        raise ValueError(
            f"Unsupported provider {provider_ref.provider_name!r}. "
            f"Supported providers: {', '.join(available_provider_names(home=resolved_home))}."
        )
    return provider


def build_builtin_provider(
    provider_name: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    session_id: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> Provider:
    """Build a known built-in provider without enforcing credential readiness."""
    normalized = provider_name.strip().lower()
    if normalized not in BUILTIN_PROVIDER_NAMES:
        raise ValueError(f"Unsupported built-in provider {normalized!r}.")
    resolved_home = _resolved_home(home)
    return build_registered_provider(
        normalized,
        model=_normalized_optional(model) if model is not None else None,
        reasoning_effort=(
            _normalized_optional(reasoning_effort.lower())
            if reasoning_effort is not None
            else None
        ),
        session_id=session_id,
        env=_resolved_env(env, home=resolved_home),
        home=resolved_home,
    )


def provider_readiness(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> list[ProviderReadiness]:
    """Report readiness for all known providers in this env."""
    resolved_home = _resolved_home(home)
    return [
        provider_status(provider_name, env=env, home=resolved_home)
        for provider_name in available_provider_names(home=resolved_home)
    ]


def provider_status(
    qualified_name: str,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> ProviderReadiness:
    """Report whether one provider reference can be constructed in this env."""
    try:
        provider_ref = parse_provider_ref(qualified_name)
    except ValueError as exc:
        return ProviderReadiness(
            provider_name=qualified_name, ready=False, reason=str(exc)
        )
    resolved_home = _resolved_home(home)
    try:
        resolved_env = _resolved_env(env, home=resolved_home)
    except ValueError as exc:
        return ProviderReadiness(
            provider_name=provider_ref.provider_name,
            ready=False,
            reason=str(exc),
            model=provider_ref.model,
            reasoning_effort=provider_ref.reasoning_effort,
        )
    try:
        models = tuple(
            list_provider_models(
                provider_ref.provider_name,
                model=provider_ref.model,
                reasoning_effort=provider_ref.reasoning_effort,
                env=resolved_env,
                home=resolved_home,
            )
            or []
        )
    except Exception as exc:
        return ProviderReadiness(
            provider_name=provider_ref.provider_name,
            ready=False,
            reason=(
                f"Could not list models for provider "
                f"`{provider_ref.provider_name}`: {exc}"
            ),
            model=provider_ref.model,
            reasoning_effort=provider_ref.reasoning_effort,
        )
    credential_issue = _credential_issue(
        provider_ref.provider_name,
        env=resolved_env,
        home=resolved_home,
    )
    if credential_issue is not None:
        return ProviderReadiness(
            provider_name=provider_ref.provider_name,
            ready=False,
            reason=credential_issue,
            model=provider_ref.model,
            reasoning_effort=provider_ref.reasoning_effort,
            models=models,
        )
    try:
        provider = build_provider(
            provider_ref.qualified_name, env=resolved_env, home=resolved_home
        )
    except Exception as exc:
        return ProviderReadiness(
            provider_name=provider_ref.provider_name,
            ready=False,
            reason=str(exc),
            model=provider_ref.model,
            reasoning_effort=provider_ref.reasoning_effort,
            models=models,
        )
    current_model = _current_model(provider) or provider_ref.model
    current_reasoning_effort = (
        _current_reasoning_effort(provider) or provider_ref.reasoning_effort
    )
    close = getattr(provider, "close", None)
    if callable(close):
        try:
            close()
        except Exception as exc:
            return ProviderReadiness(
                provider_name=provider_ref.provider_name,
                ready=False,
                reason=(
                    f"Could not close provider `{provider_ref.provider_name}`: {exc}"
                ),
                model=current_model,
                reasoning_effort=current_reasoning_effort,
                models=models,
            )
    return ProviderReadiness(
        provider_name=provider_ref.provider_name,
        ready=True,
        model=current_model,
        reasoning_effort=current_reasoning_effort,
        models=models,
    )


def list_provider_readiness(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> list[ProviderReadiness]:
    """Report readiness for all known built-in and custom providers."""
    return provider_readiness(env=env, home=home)


def is_provider_ready(
    qualified_name: str,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> bool:
    """Return whether one provider reference is ready in this env."""
    return provider_status(qualified_name, env=env, home=home).ready


def available_provider_names(*, home: Path | str | None = None) -> list[str]:
    """Return known built-in and custom provider names."""
    resolved_home = _resolved_home(home)
    return sorted(
        {
            *BUILTIN_PROVIDER_NAMES,
            *available_custom_provider_names(home=resolved_home),
        }
    )


def list_provider_models(
    provider_name: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> list[ProviderModelInfo] | None:
    """Return model metadata for a built-in or custom provider."""
    normalized = provider_name.strip().lower()
    resolved_home = _resolved_home(home)
    resolved_env = _resolved_env(env, home=resolved_home)
    models = list_registered_models(
        normalized,
        model=model,
        reasoning_effort=reasoning_effort,
        env=resolved_env,
        home=resolved_home,
    )
    if models is not None:
        return models
    return list_custom_provider_models(
        normalized,
        model=model,
        reasoning_effort=reasoning_effort,
        home=resolved_home,
        env=resolved_env,
    )


def _credential_issue(
    provider_name: str,
    *,
    env: Mapping[str, str],
    home: Path,
) -> str | None:
    if provider_name == "zai" and not env.get("ZAI_API_KEY"):
        return "zai provider requires ZAI_API_KEY."
    if provider_name == "opencode-go" and not env.get("OPENCODE_API_KEY"):
        return "opencode-go provider requires OPENCODE_API_KEY."
    if provider_name != "codex":
        return None
    if env.get("YOKE_CODEX_API_KEY"):
        return None
    if (home / ".codex" / "auth.json").is_file():
        return None
    if any((home / ".codex-auth" / "accounts").glob("*/auth.json")):
        return None
    auths_path = (
        Path(env["YOKE_CODEX_AUTHS_PATH"])
        if env.get("YOKE_CODEX_AUTHS_PATH")
        else home / ".yoke" / "providers" / "codex-auth" / "auths.json"
    )
    if auths_path.is_file():
        return None
    return "codex provider requires YOKE_CODEX_API_KEY or stored Codex auth."


def _current_model(provider: Provider) -> str | None:
    config = getattr(provider, "config", None)
    model = getattr(config, "model", None)
    return model.strip() if isinstance(model, str) and model.strip() else None


def _current_reasoning_effort(provider: Provider) -> str | None:
    config = getattr(provider, "config", None)
    effort = getattr(config, "reasoning_effort", None)
    return effort.strip() if isinstance(effort, str) and effort.strip() else None


def _resolved_home(home: Path | str | None) -> Path:
    return (Path.home() if home is None else Path(home)).resolve()


def _resolved_env(
    env: Mapping[str, str] | None,
    *,
    home: Path,
) -> Mapping[str, str]:
    if env is not None:
        return env
    return provider_environment(home=home, env=os.environ)


def _normalized_optional(value: str) -> str | None:
    normalized = value.strip()
    return normalized or None
