"""The lazy public Python export must not bypass daemon command dispatch."""

from unittest.mock import Mock
from inspect import signature

import pytest

from yoke.cli.config import CLIArgs

from .support import CaptureStream


@pytest.mark.parametrize("headless", [False, True])
def test_public_run_cli_uses_daemon_without_an_injected_agent(monkeypatch, headless):
    import yoke.cli as public

    run = Mock(return_value=7)
    name = "run_daemon_headless" if headless else "run_daemon_interactive"
    monkeypatch.setattr(f"yoke.cli.backend.startup.{name}", run)
    output, errors = CaptureStream(), CaptureStream()
    args = CLIArgs(headless=headless, prompt="export prompt")
    assert public.run_cli(args, stdout=output, stderr=errors) == 7
    assert run.call_args.args == (args,)
    assert run.call_args.kwargs["stdout"] is output
    assert run.call_args.kwargs["stderr"] is errors


def test_public_run_cli_rejects_local_agent_injection():
    from yoke.cli import run_cli

    with pytest.raises(TypeError, match="agent"):
        signature(run_cli).bind(CLIArgs(), agent=object())
