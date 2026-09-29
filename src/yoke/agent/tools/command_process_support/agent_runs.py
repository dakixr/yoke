"""Host capability injection at the common command/Python admission point."""

from __future__ import annotations

from contextlib import suppress
import json
from typing import TYPE_CHECKING

from yoke.agent_runs.context import Binding, current_binding
from yoke.agent_runs.protocol import ENVIRONMENT_KEY, RegistrationError
from yoke.agent_runs.records import Owner
from yoke.ai.providers.usage_context import current_usage_metric_context

if TYPE_CHECKING:
    from yoke.agent.tools.command_process_manager import CommandProcessManager


def prepare_launch(
    manager: CommandProcessManager,
    environment: dict[str, str],
    session_id: int,
) -> Binding | None:
    # Read authority from the host context/environment, never the caller's env override.
    inherited = current_binding() or current_binding(manager.base_environment())
    if inherited is not None:
        binding = None
        try:
            binding = inherited.launch(session_id)
            environment[ENVIRONMENT_KEY] = binding.encode()
        except (RegistrationError, RuntimeError, KeyError):
            if binding is not None:
                with suppress(Exception):
                    binding.revoke()
            # Keep managed admission in the child, even when reporting is down.
            # An ordinary command needs no ACK; a child SDK prompt still does.
            environment[ENVIRONMENT_KEY] = json.dumps(
                {
                    "address": inherited.address or "unavailable",
                    "token": inherited.token,
                }
            )
            return None
        except BaseException:
            if binding is not None:
                with suppress(Exception):
                    binding.revoke()
            raise
        return binding
    usage = current_usage_metric_context()
    owner = Owner(
        session_id=usage.root_session_id or usage.session_id,
        runtime_session_id=session_id,
        parent_run_id=usage.sdk_run_id,
    )
    token = manager.agent_runs.capability(owner)
    binding = Binding(token=token, registry=manager.agent_runs)
    try:
        environment[ENVIRONMENT_KEY] = binding.encode()
    except BaseException:
        manager.agent_runs.revoke(token)
        raise
    return binding
