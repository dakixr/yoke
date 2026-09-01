"""Transactional context preparation for provider and model transitions."""

from __future__ import annotations

from dataclasses import dataclass

from yoke.agent.budget import current_context_fits_provider_budget
from yoke.agent.budget import rebind_context_manager_budget
from yoke.agent.compaction import force_compact_agent
from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import AgentContext
from yoke.ai.providers.base import ModelCatalogProvider


class ContextWindowTooSmallError(ValueError):
    """The target model cannot fit the active context even after compaction."""

    def __init__(
        self,
        *,
        provider_name: str,
        model_id: str,
        context_window_tokens: int,
        max_input_tokens: int,
        input_tokens: int,
    ) -> None:
        self.provider_name = provider_name
        self.model_id = model_id
        self.context_window_tokens = context_window_tokens
        self.max_input_tokens = max_input_tokens
        self.input_tokens = input_tokens
        super().__init__(
            "model switch cancelled: automatic context compaction failed "
            f"(the session still needs about {input_tokens} input tokens, but "
            f"{model_id} allows about {max_input_tokens} input tokens within its "
            f"{context_window_tokens}-token context window)"
        )


@dataclass(slots=True)
class PreparedContextTransition:
    """Rollback handle for a target-budget context preparation."""

    agent: RuntimeAgent
    source_provider: object
    source_context: AgentContext

    def rollback(self) -> None:
        """Restore context and budgeting from before the preparation."""
        self.agent._context = self.source_context
        rebind_context_manager_budget(
            self.agent.context_manager,
            provider=self.source_provider,
        )


def prepare_context_for_provider(
    agent: object,
    *,
    target_provider: object,
) -> PreparedContextTransition | None:
    """Prepare a reduced provider epoch for a smaller target transactionally."""
    if not isinstance(agent, RuntimeAgent) or agent._context is None:
        return None
    if _target_context_is_not_smaller(agent.provider, target_provider):
        return None
    if _context_fit(agent, target_provider)[0]:
        return None

    transition = PreparedContextTransition(
        agent=agent,
        source_provider=agent.provider,
        source_context=agent._context.model_copy(deep=True),
    )
    try:
        rebind_context_manager_budget(
            agent.context_manager,
            provider=target_provider,
        )
        compacted = force_compact_agent(
            agent,
            agent.messages,
            conversation_entries=agent.conversation_entries,
        )
        fits, budget, input_tokens = _context_fit(agent, target_provider)
    except Exception as exc:
        transition.rollback()
        raise ValueError(
            f"model switch cancelled: automatic context compaction failed ({exc})"
        ) from exc
    if compacted is None or not fits:
        transition.rollback()
        max_input_tokens = max(
            0,
            budget.policy.max_total_tokens - budget.policy.reserved_output_tokens,
        )
        raise ContextWindowTooSmallError(
            provider_name=budget.provider_name,
            model_id=budget.model_id,
            context_window_tokens=budget.context_window_tokens,
            max_input_tokens=max_input_tokens,
            input_tokens=input_tokens,
        )
    return transition


def _context_fit(agent: RuntimeAgent, provider: object):  # noqa: ANN202
    context = agent._context
    if context is None:
        raise ValueError("Runtime context is unavailable for model transition.")
    provider_messages = agent.context_manager.messages_for_provider(context)
    return current_context_fits_provider_budget(
        agent.context_manager,
        provider_messages,
        provider=provider,
    )


def _target_context_is_not_smaller(
    source_provider: object, target_provider: object
) -> bool:
    source = _context_window(source_provider)
    target = _context_window(target_provider)
    return source is not None and target is not None and target >= source


def _context_window(provider: object) -> int | None:
    if not isinstance(provider, ModelCatalogProvider):
        return None
    model_info = provider.current_model_info()
    return model_info.context_window_tokens if model_info is not None else None
