"""Build provider/tool runtimes for HTTP-owned sessions."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import Message
from yoke.ai.providers.usage_context import UsageMetricContext
from yoke.ai.providers.usage_context import usage_metric_context
from yoke.cli.config import CLIArgs
from yoke.cli.config.runtime import build_cli_agent_from_args
from yoke.cli.runtime.session import apply_session_defaults_to_args
from yoke.http.services.session_runtime import close_owned_agent
from yoke.session import fallback_session_title
from yoke.session import SessionRecord


# Every object returned by this factory is owned by its HTTP caller.
type SessionAgentFactory = Callable[[SessionRecord], object]


@contextmanager
def http_session_usage_metric_context(
    record: SessionRecord,
    prompt: str = "",
) -> Iterator[UsageMetricContext]:
    """Attribute an HTTP-owned provider call to its Yoke session."""
    with usage_metric_context(
        surface="http",
        session_id=record.id,
        session_title=record.title or fallback_session_title(prompt),
    ) as context:
        yield context


def generate_http_session_title(
    agent_factory: SessionAgentFactory,
    record: SessionRecord,
    messages: Sequence[Message],
) -> str | None:
    """Generate a title with the session's HTTP agent configuration."""
    from yoke.session.title import generate_session_title

    prompt = next(
        (
            message.plain_text_content or ""
            for message in reversed(messages)
            if message.role == "user"
        ),
        "",
    )
    with http_session_usage_metric_context(record, prompt):
        agent = agent_factory(record)
        try:
            return generate_session_title(agent, messages)
        finally:
            close_owned_agent(
                agent,
                description=f"HTTP title agent for session {record.id}",
            )


def build_http_session_agent(record: SessionRecord) -> RuntimeAgent:
    """Build the same configured agent used by the CLI for one saved session."""
    if not record.root:
        raise ValueError("Session does not have a workspace root.")
    root = Path(record.root).resolve()
    args = CLIArgs(session=record.id, root=str(root))
    apply_session_defaults_to_args(args, record)
    built = build_cli_agent_from_args(args)
    agent = built.agent
    if record.active_skills:
        agent.active_skills = [
            skill.model_copy(deep=True) for skill in record.active_skills
        ]
    # SessionRuntime owns loading the active conversation into either the
    # primary agent or an isolated turn fork. Loading it here as well makes a
    # cold large-session turn materialize the same history twice before the
    # provider can start.
    return agent
