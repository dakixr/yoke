"""Carry host-issued launch authority without changing usage attribution."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
import json
import os
from typing import TYPE_CHECKING

from yoke.agent_runs.protocol import ENVIRONMENT_KEY, RegistrationError, request
from yoke.agent_runs.records import Owner

if TYPE_CHECKING:
    from yoke.agent_runs.registry import AgentRunRegistry


@dataclass(frozen=True)
class Binding:
    token: str = field(repr=False)
    address: str | None = field(default=None, repr=False)
    registry: AgentRunRegistry | None = field(default=None, repr=False)
    temporary: bool = False

    def endpoint(self) -> str:
        if self.registry is not None:
            try:
                return self.registry.address()
            except RuntimeError as exc:
                raise RegistrationError("Agent run host is closed") from exc
        if self.address is None:
            raise RegistrationError("Agent run host address is missing")
        return self.address

    def call(self, frame: dict[str, object]) -> dict[str, object]:
        return request(self.endpoint(), {**frame, "token": self.token})

    def encode(self) -> str:
        return json.dumps({"address": self.endpoint(), "token": self.token})

    def launch(self, runtime_session_id: int) -> Binding:
        if self.registry is not None:
            return Binding(
                token=self.registry.launch(self.token, runtime_session_id),
                registry=self.registry,
            )
        response = self.call({"op": "launch"})
        token = response.get("token")
        if not isinstance(token, str) or not token:
            raise RegistrationError("Agent run host did not acknowledge launch")
        return Binding(token=token, address=self.endpoint())

    def retain(self, *, request_id: str | None = None) -> Binding:
        if self.registry is not None:
            return Binding(
                token=self.registry.launch(
                    self.token,
                    pid=os.getpid(),
                    process_launch=False,
                    request_id=request_id,
                ),
                registry=self.registry,
            )
        response = self.call({"op": "retain", "requestId": request_id})
        token = response.get("token")
        if not isinstance(token, str) or not token:
            raise RegistrationError("Agent run host did not acknowledge retention")
        return Binding(token=token, address=self.endpoint())

    def release_retained(self, request_id: str) -> None:
        if self.registry is not None:
            self.registry.release_retained(self.token, request_id, pid=os.getpid())
        else:
            self.call({"op": "release_retained", "requestId": request_id})

    def attach(self, pid: int) -> None:
        self.call({"op": "attach", "childPid": pid})

    def revoke(self) -> None:
        if self.registry is not None:
            self.registry.revoke(self.token)
        else:
            self.call({"op": "revoke"})


_BINDING: ContextVar[Binding | None] = ContextVar(
    "yoke_agent_run_binding", default=None
)
_BATCH: ContextVar[tuple[str, int] | None] = ContextVar(
    "yoke_agent_run_batch", default=None
)
_BATCH_RUN_ID: ContextVar[str | None] = ContextVar("yoke_batch_run_id", default=None)


def current_binding(environment: Mapping[str, str] | None = None) -> Binding | None:
    active = _BINDING.get()
    if active is not None:
        return active
    raw = (os.environ if environment is None else environment).get(ENVIRONMENT_KEY)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        address, token = value["address"], value["token"]
        if (
            not isinstance(address, str)
            or not isinstance(token, str)
            or not address
            or not token
        ):
            raise ValueError
        return Binding(token=token, address=address)
    except (ValueError, KeyError, TypeError) as exc:
        raise RegistrationError("Invalid managed agent run environment") from exc


@contextmanager
def bind_binding(binding: Binding | None) -> Iterator[None]:
    token = _BINDING.set(binding)
    try:
        yield
    finally:
        _BINDING.reset(token)


@contextmanager
def bind_host(registry: AgentRunRegistry, *, session_id: str | None) -> Iterator[None]:
    capability = registry.capability(Owner(session_id=session_id))
    try:
        with bind_binding(Binding(token=capability, registry=registry)):
            yield
    finally:
        registry.revoke(capability)


@contextmanager
def batch_attempt(task_id: str, attempt: int, run_id: str) -> Iterator[None]:
    token = _BATCH.set((task_id, attempt))
    run_token = _BATCH_RUN_ID.set(run_id)
    try:
        yield
    finally:
        _BATCH.reset(token)
        _BATCH_RUN_ID.reset(run_token)


def consume_batch_run_id() -> str | None:
    """Only the immediate prompt may claim a batch attempt's usage run ID."""
    run_id = _BATCH_RUN_ID.get()
    _BATCH_RUN_ID.set(None)
    return run_id


def batch_metadata() -> dict[str, object]:
    from yoke.agent_runs.records import task_label

    value = _BATCH.get()
    return (
        {} if value is None else {"taskId": task_label(value[0]), "attempt": value[1]}
    )


def tool_binding(tools: Mapping[str, object]) -> Binding | None:
    """Find the bound host in a tool set, not through a child-supplied session ID."""
    from yoke.ai.providers.usage_context import current_usage_metric_context

    binding = current_binding()
    if binding is not None:
        return replace(binding, temporary=False)
    for tool in tools.values():
        context = getattr(tool, "_context", {})
        manager = context.get("command_process_manager")
        registry = getattr(manager, "agent_runs", None)
        if registry is not None:
            usage = current_usage_metric_context()
            # This executes in the host before a tool worker is started.
            owner = Owner(
                usage.root_session_id or usage.session_id,
                parent_run_id=usage.sdk_run_id,
            )
            return Binding(
                token=registry.capability(owner), registry=registry, temporary=True
            )
    return None


def release_tool_binding(binding: Binding | None) -> None:
    if binding is not None and binding.temporary and binding.registry is not None:
        binding.registry.revoke(binding.token)
