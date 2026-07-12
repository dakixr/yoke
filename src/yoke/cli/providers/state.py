"""Helpers for provider/model runtime state in the CLI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yoke.agent.context import ContextManager
from yoke.agent.loop import RuntimeAgent
from yoke.ai.providers.base import ModelCatalogProvider
from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.resolution import available_provider_names
from yoke.agent.budget import current_context_fits_provider_budget
from yoke.agent.budget import rebind_context_manager_budget
from yoke.cli.config.args import CLIArgs
from yoke.cli.providers.catalog import parse_provider_model_identifier


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


def bind_provider_session(agent: object, session_id: str) -> None:
    """Bind a session-aware provider to the active CLI session."""
    provider = getattr(agent, "provider", None)
    set_session_id = getattr(provider, "set_session_id", None)
    if callable(set_session_id):
        set_session_id(session_id)


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
    if (
        args.model is None
        and session_state.provider_name is not None
        and session_state.provider_name
        not in available_provider_names(home=Path.home())
    ):
        return
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
    _ensure_context_fits_target_model(
        agent,
        provider=provider,
        model_id=model_id,
        reasoning_effort=reasoning_effort,
    )
    provider.set_model(model_id, reasoning_effort=reasoning_effort)
    if isinstance(agent, RuntimeAgent):
        agent.refresh_tools(force=True)
    context_manager = _agent_context_manager(agent)
    if context_manager is not None:
        rebind_context_manager_budget(context_manager, provider=provider)
    return capture_provider_session_state(agent)


def provider_model_choices(agent: object) -> list[str]:
    """Return known model ids for slash-command completion."""
    provider = getattr(agent, "provider", None)
    if not isinstance(provider, ModelCatalogProvider):
        return []
    return [model.id for model in provider.list_models()]


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
    _ensure_context_fits_target_model(agent, provider=target_provider)
    agent.provider = target_provider
    agent.refresh_tools(force=True)
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
    from yoke.cli.config.providers import build_provider_from_args
    from yoke.cli.config.providers import prepare_provider_args

    provider_args = CLIArgs(
        model=f"{provider_name}:{model_id}",
        reasoning_effort=reasoning_effort,
        root=args.root,
        skills=args.skills,
        images=args.images,
    )
    prepare_provider_args(provider_args)
    return build_provider_from_args(provider_args)


def _ensure_context_fits_target_model(
    agent: object,
    *,
    provider: object,
    model_id: str | None = None,
    reasoning_effort: str | None = None,
) -> None:
    if not isinstance(agent, RuntimeAgent):
        return
    target_provider = provider
    if model_id is not None:
        if not isinstance(provider, ModelCatalogProvider):
            return
        target_provider = _target_provider_for_model(
            provider,
            model_id=model_id,
        )
    context = agent._context
    if context is None:
        return
    provider_messages = agent.context_manager.messages_for_provider(context)
    fits, budget, input_tokens = current_context_fits_provider_budget(
        agent.context_manager,
        provider_messages,
        provider=target_provider,
    )
    if fits:
        return
    available_input_tokens = (
        budget.policy.max_total_tokens - budget.policy.reserved_output_tokens
    )
    raise ValueError(
        "compact before switching. context too large for "
        f"{budget.provider_name}:{budget.model_id} model "
        f"({input_tokens} input tokens > "
        f"{available_input_tokens} "
        "available)"
    )


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
