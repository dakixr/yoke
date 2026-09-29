"""Import-safe subprocess fixture for agent-tracking integration tests."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import threading
from typing import ClassVar

from yoke.agent.models import Message, TokenUsage
from yoke.ai import Agent, RunConfig


class FixtureProvider:
    """A provider whose execution can be held without a network request."""

    provider_name = "tracking-fixture"
    supports_image_inputs: ClassVar[bool] = False
    max_images_per_message: ClassVar[int | None] = None

    def __init__(self, name: str, gate: threading.Event | None = None) -> None:
        self.name = name
        self.gate = gate

    def current_model_id(self) -> str:
        return "fixture-model"

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del messages, tools
        print(f"PROVIDER_ENTERED:{self.name}", flush=True)
        if self.gate is not None and not self.gate.wait(45):
            raise TimeoutError("Fixture controller did not release the provider")
        if self.name == "failed":
            raise RuntimeError("SECRET_ERROR_MUST_NOT_REACH_THE_ROSTER")
        response = Message.assistant("SECRET_OUTPUT_MUST_NOT_REACH_THE_ROSTER")
        response.usage = TokenUsage(
            provider_name=self.provider_name,
            model_id="fixture-model",
            input_tokens=7,
            output_tokens=2,
            total_tokens=9,
        )
        return response


def make_agent(name: str, gate: threading.Event | None = None) -> Agent:
    return Agent(
        provider=FixtureProvider(name, gate),
        config=RunConfig(
            root=Path.cwd(), tools=[], include_agents_file=False, name=name
        ),
    )


async def mixed() -> None:
    gate = threading.Event()

    def release() -> None:
        command = sys.stdin.readline().strip()
        if command == "crash":
            os._exit(7)
        gate.set()

    threading.Thread(target=release, daemon=True).start()
    async with (
        make_agent("success") as success,
        make_agent("failed") as failed,
        make_agent("waiting", gate) as waiting,
    ):
        outcomes = await asyncio.gather(
            success.prompt_async("SECRET_FIRST_PROMPT"),
            failed.prompt_async("SECRET_FAILURE_PROMPT"),
            waiting.prompt_async("SECRET_WAITING_PROMPT"),
            return_exceptions=True,
        )
        assert isinstance(outcomes[1], RuntimeError)
        await success.prompt_async("SECRET_SECOND_PROMPT")
        print("ALL_AGENTS_SETTLED", flush=True)


async def cancellation() -> None:
    gate = threading.Event()
    async with make_agent("cancelled", gate) as agent:
        task = asyncio.create_task(agent.prompt_async("SECRET_CANCEL_PROMPT"))
        command = await asyncio.to_thread(sys.stdin.readline)
        assert command.strip() == "cancel"
        task.cancel()
        # Let the cancellation handler run before announcing its receipt.
        await asyncio.sleep(0)
        print("CANCEL_REQUESTED", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        gate.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
    print("CANCEL_DRAINED", flush=True)


def nested() -> None:
    from yoke.agent.models import ToolCall, ToolFunction

    child_code = """
from pathlib import Path
from yoke.ai import Agent, RunConfig
from yoke.agent.models import Message
class Provider:
    provider_name = 'nested-fixture'
    supports_image_inputs = False
    max_images_per_message = None
    def complete(self, messages, tools):
        return Message.assistant('SECRET_NESTED_OUTPUT')
agent = Agent(provider=Provider(), config=RunConfig(root=Path.cwd(), tools=[], include_agents_file=False, name='nested-child'))
try:
    agent.prompt('SECRET_NESTED_PROMPT')
finally:
    agent.close()
print('NESTED_FINISHED', flush=True)
"""

    class ParentProvider(FixtureProvider):
        calls = 0

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            self.calls += 1
            if self.calls == 1:
                result = Message.assistant("")
                result.tool_calls = [
                    ToolCall(
                        id="nested-launch",
                        function=ToolFunction(
                            name="python_exec",
                            arguments=json.dumps({"code": child_code, "timeout": 30}),
                        ),
                    )
                ]
                return result
            # The nested tool must really have run, not merely been announced.
            assert any(
                message.role == "tool"
                and "NESTED_FINISHED" in (message.plain_text_content or "")
                for message in messages
            ), repr(messages)
            return super().complete(messages, tools)

    agent = Agent(
        provider=ParentProvider("nested-parent"),
        config=RunConfig(
            root=Path.cwd(),
            tools=["shell"],
            include_agents_file=False,
            name="nested-parent",
        ),
    )
    try:
        agent.prompt("SECRET_PARENT_PROMPT")
    finally:
        agent.close()
    print("NESTED_PARENT_SETTLED", flush=True)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "mixed"
    if mode == "nested":
        nested()
    else:
        asyncio.run(cancellation() if mode == "cancellation" else mixed())
