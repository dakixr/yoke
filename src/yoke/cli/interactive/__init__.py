"""Interactive CLI exports."""

from __future__ import annotations

from yoke.agent.models import Message
from yoke.cli.config import CLIArgs
from yoke.cli.interactive.basic import run_basic_interactive_cli
from yoke.cli.interactive.common import (
    COMPACTION_IN_PROGRESS_NOTICE as COMPACTION_IN_PROGRESS_NOTICE,
)
from yoke.cli.interactive.common import InputFunc
from yoke.cli.interactive.common import PendingPrompt as PendingPrompt
from yoke.cli.interactive.common import (
    format_context_usage_text,
)
from yoke.cli.interactive.prompt import run_prompt_toolkit_cli
from yoke.cli.interactive.renderer import (
    PromptToolkitLiveRenderer as PromptToolkitLiveRenderer,
)
from yoke.cli.interactive.renderer import (
    format_bottom_toolbar,
)
from yoke.cli.render import OutputStream
from yoke.cli.render import build_console
from yoke.cli.runtime import ActiveSession
from yoke.cli.runtime import AgentRunner

_format_context_usage_text = format_context_usage_text
_format_bottom_toolbar = format_bottom_toolbar


def run_interactive_cli(
    args: CLIArgs,
    agent: AgentRunner,
    session_messages: list[Message],
    *,
    active_session: ActiveSession,
    input_func: InputFunc,
    stdout: OutputStream,
    stderr: OutputStream,
    replay_session: bool = False,
) -> int:
    """Run the appropriate interactive CLI implementation."""
    stdout_console = build_console(stdout)
    if input_func is input and stdout_console.is_terminal:
        return run_prompt_toolkit_cli(
            args,
            agent,
            session_messages,
            active_session=active_session,
            replay_session=replay_session,
        )
    return run_basic_interactive_cli(
        args,
        agent,
        session_messages,
        active_session=active_session,
        input_func=input_func,
        stdout=stdout,
        stderr=stderr,
        replay_session=replay_session,
    )
