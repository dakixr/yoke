"""Workflow context and Python step instrumentation for Yoke Observe."""

from __future__ import annotations

import functools
import inspect
import secrets
import threading
from collections.abc import Callable
from collections.abc import Iterator
from contextvars import ContextVar
from pathlib import Path
from typing import ParamSpec
from typing import Self
from typing import TypeVar
from typing import cast
from typing import overload

from pydantic import BaseModel

from yoke.observe.models import ObserveEvent
from yoke.observe.models import ObserveEventType
from yoke.observe.models import RunManifest
from yoke.observe.store import JsonlObserveStore
from yoke.observe.store import validate_run_id


P = ParamSpec("P")
R = TypeVar("R")
MAX_INLINE_PREVIEW_CHARS = 4000
MAX_PREVIEW_STRING_CHARS = 1200
MAX_PREVIEW_ITEMS = 20

_ACTIVE_WORKFLOW: ContextVar[WorkflowRun | None] = ContextVar(
    "yoke_observe_workflow",
    default=None,
)
_ACTIVE_NODE_STACK: ContextVar[tuple[str, ...]] = ContextVar(
    "yoke_observe_node_stack",
    default=(),
)


class WorkflowRun:
    """Active observed workflow run."""

    def __init__(
        self,
        name: str,
        *,
        root: str | Path | None = None,
        store: JsonlObserveStore | None = None,
        run_id: str | None = None,
    ) -> None:
        self.run_id = validate_run_id(run_id or f"run_{secrets.token_hex(8)}")
        self.name = name
        self.store = store or JsonlObserveStore(root)
        self._sequence = 0
        self._object_nodes: dict[int, str] = {}
        self._agent_nodes: dict[int, str] = {}
        self._open_agent_nodes: set[str] = set()
        self._token = None
        self._node_stack_token = None
        self._lock = threading.RLock()

    def __enter__(self) -> Self:
        """Start this workflow run."""
        self.store.create_run(RunManifest(run_id=self.run_id, name=self.name))
        self._token = _ACTIVE_WORKFLOW.set(self)
        self._node_stack_token = _ACTIVE_NODE_STACK.set(())
        try:
            self.emit("workflow_started", payload={"name": self.name})
        except BaseException:
            _ACTIVE_NODE_STACK.reset(self._node_stack_token)
            _ACTIVE_WORKFLOW.reset(self._token)
            self._node_stack_token = None
            self._token = None
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        """Finish this workflow run."""
        del traceback
        try:
            if exc_type is None:
                self._complete_open_agent_nodes()
                self.emit("workflow_completed")
            else:
                self._fail_open_agent_nodes(exc or RuntimeError("workflow failed"))
                self.emit(
                    "workflow_failed",
                    payload={"error": str(exc), "error_type": exc_type.__name__},
                )
        finally:
            if self._node_stack_token is not None:
                _ACTIVE_NODE_STACK.reset(self._node_stack_token)
                self._node_stack_token = None
            if self._token is not None:
                _ACTIVE_WORKFLOW.reset(self._token)
                self._token = None

    def emit(
        self,
        event_type: ObserveEventType,
        *,
        node_id: str | None = None,
        parent_node_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> ObserveEvent:
        """Append one event to this workflow."""
        with self._lock:
            self._sequence += 1
            event = ObserveEvent(
                run_id=self.run_id,
                sequence=self._sequence,
                event_id=f"evt_{secrets.token_hex(8)}",
                type=event_type,
                node_id=node_id,
                parent_node_id=parent_node_id,
                payload=payload or {},
            )
            self.store.append(event)
        return event

    def start_node(
        self,
        *,
        label: str,
        kind: str,
        parent_node_id: str | None = None,
        dependencies: list[str] | None = None,
        metadata: dict[str, object] | None = None,
        push: bool = True,
    ) -> str:
        """Create and push a running node."""
        node_id = f"node_{secrets.token_hex(8)}"
        parent = parent_node_id if parent_node_id is not None else self.current_node_id
        self.emit(
            "node_started",
            node_id=node_id,
            parent_node_id=parent,
            payload={"label": label, "kind": kind, **(metadata or {})},
        )
        for dependency in dependencies or []:
            self.emit(
                "edge_created",
                payload={
                    "from_node_id": dependency,
                    "to_node_id": node_id,
                    "label": "input",
                },
            )
        if push:
            _ACTIVE_NODE_STACK.set((*_ACTIVE_NODE_STACK.get(), node_id))
        return node_id

    def complete_node(self, node_id: str, output: object = None) -> None:
        """Mark a node completed and record produced structured values."""
        if output is not None:
            self.remember_output(node_id, output)
        self.emit("node_completed", node_id=node_id)
        self._pop_node(node_id)

    def fail_node(self, node_id: str, exc: BaseException) -> None:
        """Mark a node failed."""
        self.emit(
            "node_failed",
            node_id=node_id,
            payload={"error": str(exc), "error_type": type(exc).__name__},
        )
        self._pop_node(node_id)

    def remember_output(self, node_id: str, output: object) -> None:
        """Associate structured output values with a producing node."""
        with self._lock:
            for value in _walk_values(output):
                self._object_nodes[id(value)] = node_id
        if isinstance(output, BaseModel):
            self.emit_typed_output(node_id, output)

    def agent_node_for(
        self,
        *,
        agent: object,
        label: str,
        metadata: dict[str, object] | None = None,
    ) -> str:
        """Return the stable observed node for an Agent instance in this run."""
        key = id(agent)
        node_id = self._agent_nodes.get(key)
        if node_id is not None:
            if metadata:
                self.emit(
                    "agent_event",
                    node_id=node_id,
                    payload={"event": "agent_metadata", **metadata},
                )
            return node_id
        node_id = self.start_node(
            label=label,
            kind="agent",
            metadata=metadata,
            push=False,
        )
        self._agent_nodes[key] = node_id
        self._open_agent_nodes.add(node_id)
        return node_id

    def emit_typed_output(self, node_id: str, value: BaseModel) -> None:
        """Emit a typed output event for a Pydantic value."""
        self.emit(
            "typed_output_created",
            node_id=node_id,
            payload={
                "type": type(value).__name__,
                "schema": type(value).model_json_schema(),
                "preview": _safe_preview(value),
            },
        )

    def dependencies_for(self, values: tuple[object, ...]) -> list[str]:
        """Return producer node ids for observed input values."""
        dependencies: list[str] = []
        for value in values:
            for item in _walk_values(value):
                node_id = self._object_nodes.get(id(item))
                if node_id is not None and node_id not in dependencies:
                    dependencies.append(node_id)
        return dependencies

    @property
    def current_node_id(self) -> str | None:
        """Return the currently active node id."""
        stack = _ACTIVE_NODE_STACK.get()
        return stack[-1] if stack else None

    def _pop_node(self, node_id: str) -> None:
        stack = list(_ACTIVE_NODE_STACK.get())
        if stack and stack[-1] == node_id:
            _ACTIVE_NODE_STACK.set(tuple(stack[:-1]))
            return
        try:
            stack.remove(node_id)
        except ValueError:
            pass
        _ACTIVE_NODE_STACK.set(tuple(stack))

    def _complete_open_agent_nodes(self) -> None:
        for node_id in list(self._open_agent_nodes):
            self.emit("node_completed", node_id=node_id)
        self._open_agent_nodes.clear()

    def _fail_open_agent_nodes(self, exc: BaseException) -> None:
        for node_id in list(self._open_agent_nodes):
            self.emit(
                "node_failed",
                node_id=node_id,
                payload={"error": str(exc), "error_type": type(exc).__name__},
            )
        self._open_agent_nodes.clear()


def current_workflow() -> WorkflowRun | None:
    """Return the active workflow run, if any."""
    return _ACTIVE_WORKFLOW.get()


def workflow(
    name: str,
    *,
    root: str | Path | None = None,
    store: JsonlObserveStore | None = None,
    run_id: str | None = None,
) -> WorkflowRun:
    """Create an observed workflow context manager."""
    return WorkflowRun(name, root=root, store=store, run_id=run_id)


@overload
def step(func: Callable[P, R], *, label: str | None = None) -> Callable[P, R]: ...


@overload
def step(
    func: None = None,
    *,
    label: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def step(
    func: Callable[P, R] | None = None,
    *,
    label: str | None = None,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Observe a sync or async Python function as a workflow graph node."""

    def decorate(inner: Callable[P, R]) -> Callable[P, R]:
        node_label = label or getattr(inner, "__name__", "step")
        if inspect.iscoroutinefunction(inner):

            @functools.wraps(inner)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> object:
                run = current_workflow()
                if run is None:
                    return await inner(*args, **kwargs)
                node_id = run.start_node(
                    label=node_label,
                    kind="step",
                    dependencies=run.dependencies_for((*args, kwargs)),
                    metadata={
                        "inputs": _input_preview(args, kwargs),
                        "source": _callable_source(inner),
                    },
                )
                try:
                    result = await inner(*args, **kwargs)
                except BaseException as exc:
                    run.fail_node(node_id, exc)
                    raise
                run.complete_node(node_id, result)
                return result

            return cast(Callable[P, R], async_wrapper)

        @functools.wraps(inner)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> object:
            run = current_workflow()
            if run is None:
                return inner(*args, **kwargs)
            node_id = run.start_node(
                label=node_label,
                kind="step",
                dependencies=run.dependencies_for((*args, kwargs)),
                metadata={
                    "inputs": _input_preview(args, kwargs),
                    "source": _callable_source(inner),
                },
            )
            try:
                result = inner(*args, **kwargs)
            except BaseException as exc:
                run.fail_node(node_id, exc)
                raise
            run.complete_node(node_id, result)
            return result

        return cast(Callable[P, R], wrapper)

    if func is None:
        return decorate
    return decorate(func)


def _walk_values(value: object) -> Iterator[BaseModel]:
    yield from _walk_values_seen(value, set())


def _walk_values_seen(value: object, seen: set[int]) -> Iterator[BaseModel]:
    value_id = id(value)
    if value_id in seen:
        return
    seen.add(value_id)
    if isinstance(value, BaseModel):
        yield value
        for item in value.__dict__.values():
            yield from _walk_values_seen(item, seen)
        extra = getattr(value, "__pydantic_extra__", None)
        if isinstance(extra, dict):
            for item in extra.values():
                yield from _walk_values_seen(item, seen)
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_values_seen(item, seen)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _walk_values_seen(item, seen)


def _safe_preview(value: BaseModel) -> object:
    return _safe_json_preview(value.model_dump(mode="json"))


def _safe_json_preview(value: object) -> object:
    if isinstance(value, dict):
        items = list(value.items())
        preview = {
            str(key): _safe_json_preview(item)
            for key, item in items[:MAX_PREVIEW_ITEMS]
        }
        if len(items) > MAX_PREVIEW_ITEMS:
            preview["__truncated__"] = {
                "items": len(items),
                "shown": MAX_PREVIEW_ITEMS,
            }
        return preview
    if isinstance(value, list):
        preview = [_safe_json_preview(item) for item in value[:MAX_PREVIEW_ITEMS]]
        if len(value) > MAX_PREVIEW_ITEMS:
            preview.append(
                {
                    "__truncated__": {
                        "items": len(value),
                        "shown": MAX_PREVIEW_ITEMS,
                    }
                }
            )
        return preview
    if isinstance(value, str) and len(value) > MAX_PREVIEW_STRING_CHARS:
        return {
            "__truncated__": True,
            "characters": len(value),
            "preview": value[:MAX_PREVIEW_STRING_CHARS],
        }
    return value


def _input_preview(args: tuple[object, ...], kwargs: object) -> dict[str, object]:
    return {
        "args": [_safe_object_preview(value) for value in args],
        "kwargs": _safe_object_preview(kwargs),
    }


def _safe_object_preview(value: object) -> object:
    return _safe_object_preview_seen(value, set(), depth=0)


def _safe_object_preview_seen(
    value: object,
    seen: set[int],
    *,
    depth: int,
) -> object:
    if depth >= 12:
        return {"truncated": True, "reason": "maximum preview depth"}
    value_id = id(value)
    if isinstance(value, (BaseModel, dict, list, tuple)):
        if value_id in seen:
            return {"cycle": True}
        seen = {*seen, value_id}
    if isinstance(value, BaseModel):
        return {
            "type": type(value).__name__,
            "value": _safe_preview(value),
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): _safe_object_preview_seen(item, seen, depth=depth + 1)
            for key, item in list(value.items())[:MAX_PREVIEW_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        items = [
            _safe_object_preview_seen(item, seen, depth=depth + 1)
            for item in value[:MAX_PREVIEW_ITEMS]
        ]
        if len(value) > MAX_PREVIEW_ITEMS:
            items.append({"truncated": True, "items": len(value)})
        return items
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > MAX_PREVIEW_STRING_CHARS:
            return {
                "truncated": True,
                "characters": len(value),
                "preview": value[:MAX_PREVIEW_STRING_CHARS],
            }
        return value
    return repr(value)


def _callable_source(func: Callable[..., object]) -> dict[str, object]:
    source: dict[str, object] = {
        "name": getattr(func, "__name__", "step"),
        "module": getattr(func, "__module__", None),
    }
    try:
        path = inspect.getsourcefile(func)
        lines, line_number = inspect.getsourcelines(func)
    except (OSError, TypeError):
        return source
    if path is not None:
        source["path"] = path
    source["line"] = line_number
    text = "".join(lines)
    if len(text) > MAX_INLINE_PREVIEW_CHARS:
        source["code"] = text[:MAX_INLINE_PREVIEW_CHARS]
        source["truncated"] = True
    else:
        source["code"] = text
    return source
