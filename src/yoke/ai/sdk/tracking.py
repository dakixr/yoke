"""SDK tracking isolated from provider messages, tools and cache identity."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
import secrets
from typing import TYPE_CHECKING

from yoke.agent.tools.context import resolve_model_identity
from yoke.agent.loop.types import AgentStoppedError
from yoke.agent_runs.context import (
    Binding,
    batch_metadata,
    bind_binding,
    current_binding,
)
from yoke.agent_runs.protocol import RegistrationError
from yoke.agent_runs.reclamation import REVOCATIONS
from yoke.agent_runs.reporting import Reporter, bind_reporter
from yoke.ai.providers.usage_context import current_usage_metric_context
from yoke.ai.sdk.resources import ProviderLease

if TYPE_CHECKING:
    from yoke.agent.loop.agent import RuntimeAgent
    from yoke.ai.sdk.agent import Agent


class RunAuthority:
    """One SDK instance's authority, independent of retained prompt history."""

    def __init__(self, host: Binding | None = None) -> None:
        self.binding = host
        self._source = host
        self._owned = False
        self._request_id = secrets.token_urlsafe(24)
        self._ticket = REVOCATIONS.reserve() if host is not None else None
        if host is not None:
            try:
                self.acquire()
            except (RegistrationError, RuntimeError, KeyError):
                # Construction stays possible during outages. A later prompt
                # must still register against the original managed authority.
                pass
            except BaseException:
                self.close()
                raise

    def acquire(self) -> Binding | None:
        """Retry unacknowledged retention before its parent's history expires."""
        if self.binding is not None and not self._owned:
            self.binding = self.binding.retain(request_id=self._request_id)
            self._owned = True
        return self.binding

    def close(self) -> None:
        if self._ticket is None:
            return
        ticket, self._ticket = self._ticket, None

        def revoke() -> None:
            # The host can release an unacknowledged retention without creating
            # new authority or requiring the parent to remain in public history.
            if self._source is not None:
                self._source.release_retained(self._request_id)

        try:
            revoke()
        except Exception:
            REVOCATIONS.defer(ticket, revoke)
        else:
            REVOCATIONS.release(ticket)


def fork_ownership(
    agent: Agent, runtime: RuntimeAgent
) -> tuple[ProviderLease, RunAuthority]:
    """Give a fork its own authority and unwind both resource claims on failure."""
    lease = None
    try:
        lease = (
            agent._provider_lease.acquire()
            if runtime.provider is agent.provider
            else ProviderLease.claim(runtime.provider)
        )
        return lease, RunAuthority(agent._run_host.binding)
    except BaseException:
        try:
            runtime.close()
        finally:
            if lease is not None:
                lease.release()
        raise


@contextmanager
def track_prompt(agent: Agent) -> Iterator[Reporter | None]:
    host = current_binding() or agent._run_host.acquire()
    if host is None:
        yield None
        return
    identity = resolve_model_identity(agent.provider)
    reporter = Reporter(
        host,
        {
            "agentId": agent.agent_id,
            "runId": current_usage_metric_context().sdk_run_id,
            "provider": identity.provider_name,
            "model": identity.model_id,
            "name": agent.config.name or identity.model_id or identity.provider_name,
            **batch_metadata(),
        },
    )
    try:
        with bind_reporter(reporter), bind_binding(reporter.binding):
            yield reporter
    except BaseException as exc:
        status = (
            "cancelled"
            if isinstance(
                exc, (AgentStoppedError, asyncio.CancelledError, KeyboardInterrupt)
            )
            else "failed"
        )
        reporter.finish(status, type(exc).__name__)
        raise
