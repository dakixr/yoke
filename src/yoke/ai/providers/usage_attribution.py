"""Local accounting lineage across SDK and subprocess execution."""

from __future__ import annotations

from dataclasses import dataclass
import os

from yoke.ai.providers.usage_context import current_usage_metric_context

ROOT_SESSION_ENV = "YOKE_ROOT_SESSION_ID"
PARENT_RUN_ENV = "YOKE_PARENT_RUN_ID"


@dataclass(frozen=True, slots=True)
class UsageAttribution:
    """Resolved lineage, independent of provider prompts and conversation state."""

    root_session_id: str | None = None
    parent_run_id: str | None = None


def resolve_usage_attribution(
    *,
    root_session_id: str | None = None,
    parent_run_id: str | None = None,
    inherit: bool = True,
) -> UsageAttribution:
    """Resolve explicit values over active execution context over environment."""
    current = current_usage_metric_context()
    if inherit:
        if root_session_id is None:
            root_session_id = (
                (current.root_session_id or current.session_id)
                if current.surface is not None
                else os.environ.get(ROOT_SESSION_ENV)
            )
        if parent_run_id is None:
            parent_run_id = (
                (current.sdk_run_id or current.parent_run_id)
                if current.surface is not None
                else os.environ.get(PARENT_RUN_ENV)
            )
    return UsageAttribution(root_session_id or None, parent_run_id or None)


def attribute_subprocess_environment(env: dict[str, str]) -> None:
    """Project the active owner onto a copied child environment, never os.environ."""
    current = current_usage_metric_context()
    if current.surface is None:
        return
    values = {
        ROOT_SESSION_ENV: current.root_session_id or current.session_id,
        PARENT_RUN_ENV: current.sdk_run_id,
    }
    for key, value in values.items():
        if value:
            env[key] = value
        else:
            env.pop(key, None)
