"""Provider and model selection shared by CLI and HTTP runtimes."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from yoke.agent.budget import rebind_context_manager_budget
from yoke.agent.context import ContextManager
from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.provider_transition import prepare_context_for_provider
from yoke.ai.providers.base import ModelCatalogProvider
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.model_selection import compatible_reasoning_effort_for_model
from yoke.ai.providers.resolution import build_provider


@dataclass(slots=True, frozen=True)
class ProviderSessionState:
    """Persistable provider and model state for one session."""

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
        return [self.model_info]

    def current_model_id(self) -> str | None:
        return self.model_info.id

    def current_model_info(self) -> ProviderModelInfo | None:
        return self.model_info

    def set_model(self, model_id: str, *, reasoning_effort: str | None = None) -> None:
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
    """Build a normalized provider session state from persisted values."""
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
    """Capture the active provider and model snapshot from an agent."""
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
    return provider_session_state_from_values(
        provider_name=provider_name,
        model_id=_provider_model_id(provider, config),
        reasoning_effort=reasoning_effort,
        context_window_tokens=_provider_context_window_tokens(provider),
    )


def set_agent_model(
    agent: object,
    *,
    model_id: str,
    reasoning_effort: str | None = None,
) -> ProviderSessionState:
    """Switch the active model on the current provider."""
    provider = getattr(agent, "provider", None)
    if not isinstance(provider, ModelCatalogProvider):
        raise ValueError("The current provider does not support model switching.")
    target_provider = _target_provider_for_model(provider, model_id=model_id)
    transition = prepare_context_for_provider(agent, target_provider=target_provider)
    source_model_id = provider.current_model_id()
    source_effort = getattr(getattr(provider, "config", None), "reasoning_effort", None)
    resolved_effort = compatible_reasoning_effort_for_model(
        target_provider.model_info,
        reasoning_effort,
    )
    try:
        provider.set_model(model_id, reasoning_effort=resolved_effort)
        provider_config = getattr(provider, "config", None)
        if provider_config is not None and hasattr(provider_config, "reasoning_effort"):
            with suppress(AttributeError, TypeError):
                setattr(provider_config, "reasoning_effort", resolved_effort)
    except Exception:
        with suppress(Exception):
            if source_model_id is not None:
                provider.set_model(source_model_id, reasoning_effort=source_effort)
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
    provider_name: str,
    model_id: str,
    reasoning_effort: str | None = None,
    session_id: str | None = None,
    home: Path | None = None,
) -> ProviderSessionState:
    """Switch provider and model while preserving the current conversation."""
    normalized_provider = provider_name.strip().lower()
    normalized_model = model_id.strip()
    if not normalized_provider or not normalized_model:
        raise ValueError("Provider and model must be non-empty.")
    if _current_provider_name(agent) == normalized_provider:
        return set_agent_model(
            agent,
            model_id=normalized_model,
            reasoning_effort=reasoning_effort,
        )
    if not isinstance(agent, RuntimeAgent):
        raise ValueError(
            "Cross-provider model switching requires a RuntimeAgent-backed session."
        )
    qualified = f"{normalized_provider}:{normalized_model}"
    if reasoning_effort:
        qualified = f"{qualified}:{reasoning_effort.strip().lower()}"
    target_provider = build_provider(
        qualified,
        session_id=session_id,
        home=home or Path.home(),
    )
    previous_provider = agent.provider
    transition = None
    try:
        transition = prepare_context_for_provider(
            agent,
            target_provider=target_provider,
        )
        agent.provider = target_provider
        rebind_context_manager_budget(agent.context_manager, provider=target_provider)
    except Exception:
        agent.provider = previous_provider
        if transition is not None:
            with suppress(Exception):
                transition.rollback()
        else:
            with suppress(Exception):
                rebind_context_manager_budget(
                    agent.context_manager,
                    provider=previous_provider,
                )
        with suppress(Exception):
            close_target = getattr(target_provider, "close", None)
            if callable(close_target):
                close_target()
        raise
    close_previous = getattr(previous_provider, "close", None)
    if callable(close_previous):
        with suppress(Exception):
            close_previous()
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


def _agent_context_manager(agent: object) -> ContextManager | None:
    context_manager = getattr(agent, "context_manager", None)
    return context_manager if isinstance(context_manager, ContextManager) else None


def _target_provider_for_model(
    provider: ModelCatalogProvider,
    *,
    model_id: str,
) -> TargetModelProvider:
    models = provider.list_models()
    for model_info in models:
        if model_info.id == model_id:
            return TargetModelProvider(
                provider_name=getattr(provider, "provider_name", "provider"),
                model_info=model_info,
            )
    available = ", ".join(sorted(model.id for model in models))
    raise ValueError(f"Unknown model {model_id!r}. Available: {available}.")
