"""CLI adapters for shared provider and model runtime state."""

from __future__ import annotations

from yoke.agent.provider_selection import ProviderSessionState
from yoke.agent.provider_selection import (
    capture_provider_session_state as capture_provider_session_state,
)
from yoke.agent.provider_selection import (
    provider_session_state_from_values as provider_session_state_from_values,
)
from yoke.agent.provider_selection import set_agent_model as set_agent_model
from yoke.agent.provider_selection import (
    switch_agent_provider_model as _switch_agent_provider_model,
)
from yoke.cli.config import CLIArgs
from yoke.cli.providers.catalog import parse_provider_model_identifier


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


def switch_agent_provider_model(
    agent: object,
    *,
    args: CLIArgs,
    qualified_model_id: str,
    reasoning_effort: str | None = None,
) -> ProviderSessionState:
    """Switch provider/model through the shared agent-level implementation."""
    provider_name, model_id = parse_provider_model_identifier(qualified_model_id)
    return _switch_agent_provider_model(
        agent,
        provider_name=provider_name,
        model_id=model_id,
        reasoning_effort=reasoning_effort,
        session_id=args.session,
    )
