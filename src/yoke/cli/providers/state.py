"""Helpers for provider/model runtime state in the CLI."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

from yoke.agent.budget import rebind_context_manager_budget
from yoke.agent.context import ContextManager
from yoke.agent.loop.agent import RuntimeAgent
from yoke.ai.providers.base import ModelCatalogProvider
from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.model_selection import compatible_reasoning_effort_for_model
from yoke.cli.config import CLIArgs
from yoke.cli.config.providers import build_provider_from_args
from yoke.cli.config.providers import prepare_provider_args
from yoke.cli.providers.catalog import parse_provider_model_identifier
from yoke.cli.providers.context_transition import (
    prepare_context_for_provider,
)


@dataclass(slots=True, frozen=True)
class ProviderSessionState:
    """Persistable provider/model state for a CLI session."""

    provider_name: str | None = None
    model_id: str | None = None
    reasoning_effort: str | None = None
    context_window_tokens: int | None = None


@dataclass(slots=True)
class TargetModelProvider:
    """Lightweight provider metadata for pre-switch budget checks."""

    provider_name: str
    model_info: ProviderModelInfo

    def list_models(self) -> list[ProviderModelInfo]:
        """Return the target model as a one-item catalog."""
        return [self.model_info]

    def current_model_id(self) -> str | None:
        """Return the target model id."""
        return self.model_info.id

    def current_model_info(self) -> ProviderModelInfo | None:
        """Return the target model metadata."""
        return self.model_info

    def set_model(self, model_id: str, *, reasoning_effort: str | None = None) -> None:
        """Satisfy the model catalog protocol for budget-only use."""
        del reasoning_effort
        if model_id != self.model_info.id:
            raise ValueError(f"Unknown model {model_id!r}.")


def provider_session_state_from_values(
    *,
    provider_name: str | None = None,
    model_id: str | None = None,
    reasoning_effort: str | None = None,
    context_window_tokens: int | None = None,
) -> ProviderSessionState:
    """Build a normalized provider session state from raw persisted values."""
    return ProviderSessionState(
        provider_name=(
            provider_name.strip().lower()
            if isinstance(provider_name, str) and provider_name.strip()
            else None
        ),
        model_id=(
            model_id.strip() if isinstance(model_id, str) and model_id.strip() else None
        ),
        reasoning_effort=(
            reasoning_effort.strip().lower()
            if isinstance(reasoning_effort, str) and reasoning_effort.strip()
            else None
        ),
        context_window_tokens=context_window_tokens,
    )


def capture_provider_session_state(agent: object) -> ProviderSessionState:
    """Capture the active provider/model snapshot from an agent."""
    provider = getattr(agent, "provider", None)
    if provider is None:
        return ProviderSessionState()
    provider_name = getattr(provider, "provider_name", None)
    if not isinstance(provider_name, str) or not provider_name.strip():
        provider_name = getattr(provider.__class__, "__name__", None)
    config = getattr(provider, "config", None)
    reasoning_effort = getattr(config, "reasoning_effort", None)
    if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
        reasoning_effort = None
    model_id = _provider_model_id(provider, config)
    context_window_tokens = _provider_context_window_tokens(provider)
    return provider_session_state_from_values(
        provider_name=provider_name,
        model_id=model_id,
        reasoning_effort=reasoning_effort,
        context_window_tokens=context_window_tokens,
    )


def apply_session_provider_defaults(
    args: CLIArgs,
    session_state: ProviderSessionState,
) -> None:
    """Fill unset CLI args from persisted session provider state."""
    if getattr(args, "model", None) is None and session_state.model_id:
        if session_state.provider_name:
            args.model = f"{session_state.provider_name}:{session_state.model_id}"
        else:
            args.model = session_state.model_id
    if (
        getattr(args, "reasoning_effort", None) is None
        and session_state.reasoning_effort
    ):
        args.reasoning_effort = session_state.reasoning_effort


def set_agent_model(
    agent: object,
    *,
    model_id: str,
    reasoning_effort: str | None = None,
) -> ProviderSessionState:
    """Switch the active model on a provider-backed agent."""
    provider = getattr(agent, "provider", None)
    if not isinstance(provider, ModelCatalogProvider):
        raise ValueError("The current provider does not support model switching.")
    target_provider = _target_provider_for_model(
        provider,
        model_id=model_id,
    )
    transition = prepare_context_for_provider(
        agent,
        target_provider=target_provider,
    )
    source_model_id = provider.current_model_id()
    source_effort = getattr(getattr(provider, "config", None), "reasoning_effort", None)
    resolved_effort = compatible_reasoning_effort_for_model(
        target_provider.model_info,
        reasoning_effort,
    )
    try:
        provider.set_model(model_id, reasoning_effort=resolved_effort)
        provider_config = getattr(provider, "config", None)
        if provider_config is not None and hasattr(
            provider_config,
            "reasoning_effort",
        ):
            with suppress(AttributeError, TypeError):
                setattr(provider_config, "reasoning_effort", resolved_effort)
    except Exception:
        with suppress(Exception):
            if source_model_id is not None:
                provider.set_model(
                    source_model_id,
                    reasoning_effort=source_effort,
                )
        if transition is not None:
            transition.rollback()
        raise
    context_manager = _agent_context_manager(agent)
    if context_manager is not None:
        rebind_context_manager_budget(context_manager, provider=provider)
    return capture_provider_session_state(agent)


def switch_agent_provider_model(
    agent: object,
    *,
    args: CLIArgs,
    qualified_model_id: str,
    reasoning_effort: str | None = None,
) -> ProviderSessionState:
    """Switch the active provider/model, rebuilding the provider when needed."""
    provider_name, model_id = parse_provider_model_identifier(qualified_model_id)
    if _current_provider_name(agent) == provider_name:
        return set_agent_model(
            agent,
            model_id=model_id,
            reasoning_effort=reasoning_effort,
        )
    if not isinstance(agent, RuntimeAgent):
        raise ValueError(
            "Cross-provider model switching requires a RuntimeAgent-backed session."
        )
    target_provider = _build_provider_for_model(
        args,
        provider_name=provider_name,
        model_id=model_id,
        reasoning_effort=reasoning_effort,
    )
    prepare_context_for_provider(agent, target_provider=target_provider)
    agent.provider = target_provider
    rebind_context_manager_budget(
        agent.context_manager,
        provider=target_provider,
    )
    return capture_provider_session_state(agent)


def _provider_model_id(provider: object, config: object) -> str | None:
    if isinstance(provider, ModelCatalogProvider):
        return provider.current_model_id()
    model = getattr(config, "model", None)
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def _provider_context_window_tokens(provider: object) -> int | None:
    if not isinstance(provider, ModelCatalogProvider):
        return None
    model_info = provider.current_model_info()
    return model_info.context_window_tokens if model_info is not None else None


def _current_provider_name(agent: object) -> str | None:
    provider = getattr(agent, "provider", None)
    provider_name = getattr(provider, "provider_name", None)
    if not isinstance(provider_name, str) or not provider_name.strip():
        return None
    return provider_name.strip().lower()


def _build_provider_for_model(
    args: CLIArgs,
    *,
    provider_name: str,
    model_id: str,
    reasoning_effort: str | None,
) -> Provider:
    target_args = CLIArgs(
        model=f"{provider_name}:{model_id}",
        reasoning_effort=reasoning_effort,
        root=args.root,
        skills=args.skills,
        images=args.images,
    )
    prepare_provider_args(target_args)
    return build_provider_from_args(target_args)


def _agent_context_manager(agent: object) -> ContextManager | None:
    context_manager = getattr(agent, "context_manager", None)
    if isinstance(context_manager, ContextManager):
        return context_manager
    return None


def _target_provider_for_model(
    provider: ModelCatalogProvider,
    *,
    model_id: str,
) -> TargetModelProvider:
    for model_info in provider.list_models():
        if model_info.id == model_id:
            return TargetModelProvider(
                provider_name=getattr(provider, "provider_name", "provider"),
                model_info=model_info,
            )
    available = ", ".join(sorted(model.id for model in provider.list_models()))
    raise ValueError(f"Unknown model {model_id!r}. Available: {available}.")
