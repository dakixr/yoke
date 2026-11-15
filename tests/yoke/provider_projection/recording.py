"""Scripted provider recording the public provider boundary, before any mutation."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass, field
from threading import Event, Lock
from typing import Any

from yoke.agent.models import Message, TokenUsage, ToolCall, ToolFunction
from yoke.ai.providers.base import (
    ProviderError,
    ProviderModelInfo,
    ProviderRequestContext,
)
from yoke.ai.providers.usage_context import current_usage_metric_context


def answer(text: str = "Recorded answer.", *, output_tokens: int = 19) -> Message:
    """Keep usage and reasoning fields in the persisted assistant transcript."""
    return Message(
        role="assistant",
        content=text,
        phase="final_answer",
        reasoning_content="Recorded reasoning.",
        reasoning_signature="oracle-signature",
        usage=TokenUsage(
            provider_name="oracle",
            model_id="oracle-model",
            input_tokens=37,
            output_tokens=output_tokens,
            cached_input_tokens=11,
            total_tokens=37 + output_tokens,
            raw={"scripted": True},
        ),
    )


def tool_calls(*calls: tuple[str, dict[str, object]]) -> Message:
    """Return stable call IDs and exact argument JSON."""
    response = answer("Inspecting the workspace.")
    response.phase = "commentary"
    response.tool_calls = [
        ToolCall(
            id=f"call-{index}-{name}",
            function=ToolFunction(name=name, arguments=json.dumps(arguments)),
        )
        for index, (name, arguments) in enumerate(calls, 1)
    ]
    return response


@dataclass
class Gate:
    """Hold an already-recorded call until the driver requests interruption."""

    entered: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)


@dataclass
class Step:
    kind: str
    response: Message | ProviderError
    gate: Gate | None = None


class Tape:
    """Own the shared script and recording across isolated provider instances."""

    def __init__(self) -> None:
        self.steps: deque[Step] = deque()
        self.calls: list[dict[str, Any]] = []
        self.lock = Lock()
        self.stage = "scenario"
        self.observations: list[dict[str, object]] = []

    def expect(
        self,
        response: Message | ProviderError | None = None,
        *,
        kind: str = "model_iteration",
        gate: Gate | None = None,
    ) -> None:
        self.steps.append(Step(kind, response or answer(), gate))


class RecordingProvider:
    """Implement contextual calls, catalog metadata, and explicit turn isolation."""

    provider_name = "oracle"
    max_images_per_message = None

    def __init__(self, tape: Tape, *, images: bool = True, budget: int = 100_000):
        self.tape = tape
        self.supports_image_inputs = images
        self.budget = budget
        self.model_id = "oracle-model"
        self.session_id: str | None = None

    def set_session_id(self, session_id: str) -> None:
        self.session_id = session_id

    def fork_for_turn(self) -> RecordingProvider:
        fork = RecordingProvider(
            self.tape, images=self.supports_image_inputs, budget=self.budget
        )
        fork.session_id = self.session_id
        fork.model_id = self.model_id
        return fork

    def current_model_id(self) -> str:
        return self.model_id

    def current_model_info(self) -> ProviderModelInfo:
        return ProviderModelInfo(
            id=self.model_id,
            display_name="Projection oracle",
            context_window_tokens=self.budget,
            supports_image_inputs=self.supports_image_inputs,
        )

    def list_models(self) -> list[ProviderModelInfo]:
        return [
            self.current_model_info().model_copy(
                update={"id": model, "supports_image_inputs": model == "oracle-vision"}
            )
            for model in ("oracle-model", "oracle-vision")
        ]

    def set_model(self, model_id: str, *, reasoning_effort: str | None = None) -> None:
        assert model_id in {"oracle-model", "oracle-vision"}
        self.model_id = model_id
        self.supports_image_inputs = model_id == "oracle-vision"

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        return self._record(messages, tools, None)

    def complete_with_context(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        request_context: ProviderRequestContext,
    ) -> Message:
        return self._record(messages, tools, request_context)

    def _record(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        request_context: ProviderRequestContext | None,
    ) -> Message:
        usage = current_usage_metric_context()
        with self.tape.lock:
            assert self.tape.steps, f"Unscripted provider call: {usage.call_kind}"
            step = self.tape.steps.popleft()
            assert usage.call_kind == step.kind
            # Retain the serializer's bytes as strings as well as decoded JSON.
            # Re-encoding model_dump() alone would hide serialization changes.
            serialized = [message.model_dump_json() for message in messages]
            self.tape.calls.append(
                {
                    "stage": self.tape.stage,
                    "kind": usage.call_kind,
                    "request_context": asdict(request_context)
                    if request_context
                    else None,
                    "provider_session_id": self.session_id,
                    "provider_model": self.model_id,
                    "supports_image_inputs": self.supports_image_inputs,
                    "messages_json": serialized,
                    "tools_json": json.dumps(
                        tools, ensure_ascii=False, separators=(",", ":")
                    ),
                    "image_projection": [
                        {"message": index, "part": part.model_dump(mode="json")}
                        for index, message in enumerate(messages)
                        if isinstance(message.content, list)
                        for part in message.content
                        if part.type != "text"
                    ],
                    "response_json": step.response.model_dump_json()
                    if isinstance(step.response, Message)
                    else None,
                    "error": str(step.response)
                    if isinstance(step.response, ProviderError)
                    else None,
                }
            )
        if step.gate is not None:
            step.gate.entered.set()
            assert step.gate.release.wait(5), "Interruption gate was not released"
        if isinstance(step.response, ProviderError):
            raise step.response
        return step.response.model_copy(deep=True)
