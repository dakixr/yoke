"""Provider-derived context budgeting helpers for agents."""

from __future__ import annotations

from dataclasses import dataclass

from yoke.agent.compaction import CompactionPolicy
from yoke.agent.compaction import Compactor
from yoke.agent.compaction import DEFAULT_KEEP_RECENT_TOKENS
from yoke.agent.compaction import DEFAULT_RECENT_USER_TOKENS
from yoke.agent.compaction import DEFAULT_RESERVED_OUTPUT_TOKENS
from yoke.agent.compaction import DEFAULT_TOTAL_CONTEXT_TOKENS
from yoke.agent.context import ContextManager
from yoke.ai.providers.base import ModelCatalogProvider
from yoke.ai.providers.base import ProviderModelInfo

_DEFAULT_RESERVED_OUTPUT_RATIO = 0.16
_DEFAULT_KEEP_RECENT_RATIO = 0.11
_DEFAULT_RECENT_USER_RATIO = 0.11
_MIN_RESERVED_OUTPUT_TOKENS = 8_000
_MIN_KEEP_RECENT_TOKENS = 4_000
_MIN_RECENT_USER_TOKENS = 4_000


@dataclass(slots=True, frozen=True)
class ProviderCompactionBudget:
    """Resolved compaction budget derived from provider model metadata."""

    provider_name: str
    model_id: str
    context_window_tokens: int
    policy: CompactionPolicy
    compactor: Compactor


def build_provider_context_manager(
    *,
    provider: object,
    instructions,
) -> ContextManager:
    """Create a context manager budgeted from the provider's active model."""
    budget = resolve_provider_compaction_budget(provider)
    return ContextManager(
        instructions=instructions,
        compaction_policy=budget.policy,
        compactor=budget.compactor,
    )


def resolve_provider_compaction_budget(
    provider: object,
) -> ProviderCompactionBudget:
    """Resolve compaction policy and token estimator from provider metadata."""
    provider_name = _provider_name(provider)
    model_info = _provider_model_info_or_fallback(provider)
    return ProviderCompactionBudget(
        provider_name=provider_name,
        model_id=model_info.id,
        context_window_tokens=model_info.context_window_tokens,
        policy=_policy_from_context_window(model_info.context_window_tokens),
        compactor=Compactor(model=model_info.id, provider_name=provider_name),
    )


def rebind_context_manager_budget(
    context_manager: ContextManager,
    *,
    provider: object,
) -> ProviderCompactionBudget:
    """Reapply provider-derived compaction settings to one context manager."""
    budget = resolve_provider_compaction_budget(provider)
    context_manager.compaction_policy = budget.policy
    context_manager.compactor = budget.compactor
    context_manager.max_total_tokens = budget.policy.max_total_tokens
    context_manager.keep_recent_tokens = budget.policy.keep_recent_tokens
    return budget


def current_context_fits_provider_budget(
    context_manager: ContextManager,
    messages,
    *,
    provider: object,
) -> tuple[bool, ProviderCompactionBudget, int]:
    """Return whether the current context fits the target provider budget."""
    budget = resolve_provider_compaction_budget(provider)
    estimate = budget.compactor.estimate_tokens(
        list(messages),
        reserve_tokens=budget.policy.reserved_output_tokens,
    )
    available_input_tokens = max(
        0,
        budget.policy.max_total_tokens - budget.policy.reserved_output_tokens,
    )
    fits = estimate.input_tokens <= available_input_tokens
    return fits, budget, estimate.input_tokens


def _require_provider_model_info(provider: object) -> ProviderModelInfo:
    if not isinstance(provider, ModelCatalogProvider):
        raise ValueError(
            "The current provider does not expose model metadata required "
            "for compaction budgeting."
        )
    model_info = provider.current_model_info()
    if model_info is None:
        raise ValueError(
            "The current provider does not expose an active model metadata "
            "record required for compaction budgeting."
        )
    return model_info


def _provider_model_info_or_fallback(provider: object) -> ProviderModelInfo:
    try:
        return _require_provider_model_info(provider)
    except ValueError:
        config = getattr(provider, "config", None)
        model = getattr(config, "model", None)
        if not isinstance(model, str) or not model.strip():
            model = provider.__class__.__name__
        return ProviderModelInfo(
            id=model.strip(),
            display_name=model.strip(),
            context_window_tokens=DEFAULT_TOTAL_CONTEXT_TOKENS,
            thinking_levels=(
                "none",
                "low",
                "medium",
                "high",
                "xhigh",
            ),
            supports_image_inputs=getattr(provider, "supports_image_inputs", None),
        )


def _provider_name(provider: object) -> str:
    provider_name = getattr(provider, "provider_name", None)
    if isinstance(provider_name, str) and provider_name.strip():
        return provider_name.strip().lower()
    return provider.__class__.__name__.strip().lower()


def _policy_from_context_window(context_window_tokens: int) -> CompactionPolicy:
    if context_window_tokens == DEFAULT_TOTAL_CONTEXT_TOKENS:
        return CompactionPolicy(
            max_total_tokens=DEFAULT_TOTAL_CONTEXT_TOKENS,
            reserved_output_tokens=DEFAULT_RESERVED_OUTPUT_TOKENS,
            keep_recent_tokens=DEFAULT_KEEP_RECENT_TOKENS,
            recent_user_tokens=DEFAULT_RECENT_USER_TOKENS,
        )
    reserved_output_tokens = min(
        max(
            _MIN_RESERVED_OUTPUT_TOKENS,
            round(context_window_tokens * _DEFAULT_RESERVED_OUTPUT_RATIO),
        ),
        max(1, context_window_tokens // 2),
    )
    remaining_after_reserve = max(
        1,
        context_window_tokens - reserved_output_tokens,
    )
    keep_recent_tokens = min(
        max(
            _MIN_KEEP_RECENT_TOKENS,
            round(context_window_tokens * _DEFAULT_KEEP_RECENT_RATIO),
        ),
        remaining_after_reserve,
    )
    recent_user_tokens = min(
        max(
            _MIN_RECENT_USER_TOKENS,
            round(context_window_tokens * _DEFAULT_RECENT_USER_RATIO),
        ),
        keep_recent_tokens,
    )
    return CompactionPolicy(
        max_total_tokens=context_window_tokens,
        reserved_output_tokens=reserved_output_tokens,
        keep_recent_tokens=keep_recent_tokens,
        recent_user_tokens=recent_user_tokens,
    )
