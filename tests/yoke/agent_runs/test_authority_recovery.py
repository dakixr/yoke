"""Recovery must preserve captured lineage and reclaim closed SDK authority."""

from __future__ import annotations

from contextvars import Context

import pytest

from yoke.agent_runs import AgentRunRegistry
from yoke.agent_runs.context import Binding, bind_binding
from yoke.agent_runs.protocol import ENVIRONMENT_KEY, RegistrationError
from yoke.agent_runs.records import Owner
from yoke.ai import Agent

from tests.yoke.ai.test_agent_run_tracking import Provider, config


@pytest.mark.parametrize("lost_ack", [False, True])
def test_retry_captured_authority_after_initial_retention_failure(
    tmp_path, monkeypatch, lost_ack
):
    monkeypatch.delenv(ENVIRONMENT_KEY, raising=False)
    registry = AgentRunRegistry(history_limit=1)
    children = []
    original = Binding.retain

    class ParentProvider(Provider):
        def complete(self, messages, tools):
            def fail(host, *args, **kwargs):
                if lost_ack:
                    original(host, *args, **kwargs)
                raise RegistrationError("temporary retention outage")

            with monkeypatch.context() as scoped:
                scoped.setattr(Binding, "retain", fail)
                children.append(Agent(provider=Provider(), config=config(tmp_path)))
            return super().complete(messages, tools)

    parent = None
    try:
        with registry.bind(session_id="owner"):
            parent = Agent(provider=ParentProvider(), config=config(tmp_path))
            parent.prompt("construct during outage")
        parent_id = registry.snapshots()[0]["runId"]
        parent.close()
        child = children[0]
        run_ids = set()
        for _ in range(2):
            assert Context().run(child.prompt, "resume").output == "SECRET_OUTPUT"
            row = registry.snapshots()[0]
            assert row["parentRunId"] == parent_id
            assert row["parentAgentId"] == parent.agent_id
            run_ids.add(row["runId"])
        assert len(run_ids) == 2
        child.close()
        # Retention ACK retries must not create another unseen capability.
        assert len(registry._capabilities) == 1
    finally:
        for child in children:
            child.close()
        if parent is not None:
            parent.close()
        registry.close()


def test_failed_revoke_is_retried_after_agent_is_closed(tmp_path, monkeypatch):
    from yoke.agent_runs.reclamation import Revocations
    from yoke.ai.sdk import tracking

    pending = Revocations(retry_seconds=60)
    monkeypatch.setattr(tracking, "REVOCATIONS", pending)
    registry = AgentRunRegistry()
    token = registry.capability(Owner("owner"))
    host = Binding(token=token, address=registry.address())
    original = Binding.release_retained
    failed = set()

    def fail_once(binding, request_id):
        if request_id not in failed:
            failed.add(request_id)
            raise RegistrationError("temporary release outage")
        return original(binding, request_id)

    monkeypatch.setattr(Binding, "release_retained", fail_once)
    try:
        for _ in range(3):
            with bind_binding(host):
                agent = Agent(provider=Provider(), config=config(tmp_path))
            authority = agent._run_host.binding
            assert authority is not None
            lifetime = registry._capabilities[authority.token].lifetime
            agent.close()
            assert agent.closed
            assert authority.token in registry._capabilities
            pending.flush()
            assert authority.token not in registry._capabilities
            assert lifetime is not None and lifetime.worker is not None
            assert lifetime.worker.fd is None
        assert pending.wait_idle(2)
        assert len(registry._capabilities) == 1
    finally:
        monkeypatch.setattr(Binding, "release_retained", original)
        pending.flush()
        pending.wait_idle(2)
        registry.close()


def test_pending_reclamation_reserves_bounded_capacity():
    from yoke.agent_runs.reclamation import Revocations

    pending = Revocations(limit=1, retry_seconds=60)
    ticket = pending.reserve()
    with pytest.raises(RegistrationError, match="capacity"):
        pending.reserve()
    pending.defer(ticket, lambda: None)
    pending.flush()
    assert pending.wait_idle(2)
    next_ticket = pending.reserve()
    pending.release(next_ticket)


@pytest.mark.parametrize("prompt_after_expiry", [False, True])
def test_lost_retention_ack_recovers_or_releases_after_parent_expiry(
    tmp_path, monkeypatch, prompt_after_expiry
):
    monkeypatch.delenv(ENVIRONMENT_KEY, raising=False)
    registry = AgentRunRegistry(history_limit=0)
    parent = registry.capability(Owner("owner"))
    host = Binding(token=parent, address=registry.address())
    original = Binding.retain

    def lose_ack(binding, *args, **kwargs):
        original(binding, *args, **kwargs)
        raise RegistrationError("ACK lost after retention committed")

    agent = None
    try:
        with monkeypatch.context() as scoped, bind_binding(host):
            scoped.setattr(Binding, "retain", lose_ack)
            agent = Agent(provider=Provider(), config=config(tmp_path))
        registry.revoke(parent)
        assert len(registry._capabilities) == 1
        if prompt_after_expiry:
            assert Context().run(agent.prompt, "recover").output == "SECRET_OUTPUT"
        agent.close()
        assert registry._capabilities == {}
        # Replaying release after the ACK was lost is a harmless success.
        host.release_retained(agent._run_host._request_id)
    finally:
        if agent is not None:
            agent.close()
        registry.close()


def test_unconfirmed_launches_have_bounded_capacity(monkeypatch):
    import os
    from yoke.agent_runs import capabilities

    monkeypatch.setattr(capabilities, "MAX_UNATTACHED_LAUNCHES", 2)
    registry = AgentRunRegistry()
    parent = registry.capability(Owner("owner"))
    try:
        for _ in range(2):
            registry.dispatch({"op": "launch", "token": parent}, pid=os.getpid())
        with pytest.raises(ValueError, match="capacity"):
            registry.dispatch({"op": "launch", "token": parent}, pid=os.getpid())
        assert len(registry._capabilities) == 3
    finally:
        registry.close()
