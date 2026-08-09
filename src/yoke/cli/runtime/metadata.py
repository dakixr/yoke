"""Runtime metadata persistence without conversation processing."""

from __future__ import annotations

from yoke.cli.providers.state import capture_provider_session_state
from yoke.cli.providers.state import ProviderSessionState
from yoke.cli.runtime.base import ActiveSession
from yoke.cli.runtime.session import save_active_session_metadata


class _UnsetReasoningEffort:
    pass


_UNSET_REASONING_EFFORT = _UnsetReasoningEffort()


def persist_active_session_metadata(
    active_session: ActiveSession,
    agent: object,
    *,
    reasoning_effort: str | None | _UnsetReasoningEffort = (_UNSET_REASONING_EFFORT),
) -> None:
    """Persist changed runtime metadata without reading conversation state."""
    captured = capture_provider_session_state(agent)
    record = active_session.record
    save_active_session_metadata(
        active_session,
        ProviderSessionState(
            provider_name=captured.provider_name or record.provider_name,
            model_id=captured.model_id or record.model_id,
            reasoning_effort=(
                captured.reasoning_effort
                if isinstance(reasoning_effort, _UnsetReasoningEffort)
                else reasoning_effort
            ),
            context_window_tokens=(
                captured.context_window_tokens
                if captured.context_window_tokens is not None
                else record.context_window_tokens
            ),
        ),
    )
