from __future__ import annotations

# ruff: noqa

import base64
import io
import json
import pytest
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from threading import Event
import time
from typing import Any
from typing import Callable
from typing import cast
from collections.abc import Sequence
from collections.abc import Callable
from yoke.agent.compaction import COMPACTION_SUMMARY_PROMPT
from yoke.agent.context import CompactionPolicy
from yoke.agent.context import ContextManager
from yoke.agent.loop import AgentResult, INTERRUPTED_TURN_NOTICE
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import (
    Message,
    MessageLocalImageContentPart,
    MessageTextContentPart,
    TokenUsage,
    ToolCall,
    ToolFunction,
)
from yoke.agent.prompting import render_memory_message
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec
from yoke.agent.tools import COMMAND_TOOL_NAME
from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderError
from yoke.cli.bootstrap.types import ToolLoadReport
from yoke.cli.config import build_agent_from_args
from yoke.cli.config import build_tool_report
from yoke.cli.image_input import ImageAttachment
from yoke.cli.interactive import _format_bottom_toolbar
from yoke.cli.interactive import _format_context_usage_text
from yoke.cli.interactive import COMPACTION_IN_PROGRESS_NOTICE
from yoke.cli.interactive import PendingPrompt
from yoke.cli.interactive.prompt.paste import (
    _windows_paste_compat_keys,
    patch_prompt_toolkit_input_for_multiline_paste,
)
from yoke.cli.interactive.completion import SlashCommandCompleter
from yoke.cli.interactive.completion import current_slash_token
from yoke.cli.interactive.completion import current_skill_name_token
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.common import SHORTCUTS_NOTICE
from yoke.cli.interactive.prompt.keys import insert_attachment_reference
from yoke.cli.interactive.prompt.keys import (
    register_prompt_toolkit_key_bindings,
)
from yoke.cli.main import (
    CLIArgs,
    PromptToolkitLiveRenderer,
    app,
    main,
    run_continue_cli,
    run_cli,
    run_prompt_toolkit_cli,
    run_resume_cli,
)
from yoke.cli.runtime import create_active_session
from yoke.cli.runtime import estimate_context_usage
from yoke.cli.render import build_console
from yoke.cli.render import format_tool_preview
from yoke.cli.render import print_agent_output
from yoke.cli.render import print_scrollback_agent
from yoke.cli.render import print_scrollback_divider
from yoke.cli.render import print_scrollback_tool
from yoke.cli.render import print_session_scrollback
from yoke.cli.session import SessionStore


@dataclass
class FakeAgent:
    supports_message_history = True
    supports_user_message = False

    outputs: list[str] = field(default_factory=lambda: ["synthetic response"])
    seen_history_lengths: list[int] = field(default_factory=list)
    tool_report: ToolLoadReport | None = None
    provider: Any = None
    context_manager: ContextManager | None = None

    def run(
        self,
        prompt: str,
        messages: Sequence[Message] | None = None,
        *,
        on_event: Any = None,
        stop_requested: Any = None,
    ) -> AgentResult:
        del on_event, stop_requested
        self.seen_history_lengths.append(len(messages or []))
        output = self.outputs[
            min(len(self.seen_history_lengths) - 1, len(self.outputs) - 1)
        ]
        conversation = list(messages or [])
        conversation.append(Message.user(prompt))
        conversation.append(Message.assistant(output))
        return AgentResult(output=output, messages=conversation, iterations=1)


@dataclass
class ImageAwareAgent:
    supports_message_history = True
    supports_user_message = True

    seen_user_messages: list[Message] = field(default_factory=list)

    def run(
        self,
        prompt: str,
        messages: Sequence[Message] | None = None,
        *,
        user_message: Message | None = None,
        on_event: Any = None,
        stop_requested: Any = None,
    ) -> AgentResult:
        del on_event, stop_requested
        message = user_message or Message.user(prompt)
        self.seen_user_messages.append(message.model_copy(deep=True))
        conversation = list(messages or [])
        conversation.append(message.model_copy(deep=True))
        conversation.append(Message.assistant("image response"))
        return AgentResult(
            output="image response",
            messages=conversation,
            iterations=1,
        )


class TitleProvider:
    supports_image_inputs = False
    max_images_per_message: int | None = None

    def __init__(self, title: str) -> None:
        self.title = title
        self.prompts: list[str] = []

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del tools
        self.prompts.append(messages[-1].text_content() or "")
        return Message.assistant(self.title)


class ProviderConfig:
    model = "gpt-test"


class FakeProvider:
    config = ProviderConfig()
    supports_image_inputs = False
    max_images_per_message: int | None = None


class CaptureStream(io.StringIO):
    def isatty(self) -> bool:
        return False


class EncodedTTYCaptureStream(CaptureStream):
    encoding = "cp1252"

    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> int:
        visible_text = text
        while "\x1b[" in visible_text:
            prefix, _, remainder = visible_text.partition("\x1b[")
            _, sep, suffix = remainder.partition("m")
            visible_text = prefix + (suffix if sep else remainder)
        visible_text.encode(self.encoding)
        return super().write(text)


def active_session_for(root: Path):
    return create_active_session(CLIArgs(root=str(root)), root=root)


class FakePromptToolkitLoop:
    def call_soon_threadsafe(self, callback: Callable[[], object]) -> None:
        callback()


class FakePromptToolkitApp:
    def __init__(self) -> None:
        self.loop = FakePromptToolkitLoop()

    def invalidate(self) -> None:
        return None


PromptSource = Sequence[str] | Callable[[int], str]


class FakePromptToolkitSession:
    def __init__(self, prompts: PromptSource) -> None:
        self.app = FakePromptToolkitApp()
        self._prompt_callback = (
            cast(Callable[[int], str], prompts) if callable(prompts) else None
        )
        self._prompt_sequence = None if callable(prompts) else prompts
        self.calls = 0
        self.prompt_kwargs: dict[str, object] = {}

    def prompt(self, *_args: object, **kwargs: object) -> str:
        self.calls += 1
        self.prompt_kwargs = kwargs
        if self._prompt_callback is not None:
            return self._prompt_callback(self.calls)
        assert self._prompt_sequence is not None
        return self._prompt_sequence[self.calls - 1]


def install_fake_prompt_toolkit(
    monkeypatch: Any,
    prompts: PromptSource,
) -> dict[str, FakePromptToolkitSession]:
    import importlib
    import prompt_toolkit

    run_in_terminal_module = importlib.import_module(
        "prompt_toolkit.application.run_in_terminal"
    )
    holder: dict[str, FakePromptToolkitSession] = {}

    class PromptSessionFactory(FakePromptToolkitSession):
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            super().__init__(prompts)
            holder["session"] = self

    monkeypatch.setattr(prompt_toolkit, "PromptSession", PromptSessionFactory)
    monkeypatch.setattr(
        run_in_terminal_module,
        "run_in_terminal",
        lambda func, *args, **kwargs: func(),
    )
    return holder


class ConfigOnlyProvider:
    def __init__(self, config: Any) -> None:
        self.config = config


class CatalogProvider(ConfigOnlyProvider):
    provider_name = "codex"
    context_window_tokens = 200_000

    def complete(self, messages: object, tools: object) -> Message:
        del messages, tools
        return Message.assistant("ok")

    def current_model_info(self):
        from yoke.ai.providers.base import ProviderModelInfo

        return ProviderModelInfo(
            id=self.config.model,
            display_name=self.config.model,
            context_window_tokens=self.context_window_tokens,
            thinking_levels=("low", "medium", "high"),
            supports_image_inputs=True,
        )

    def current_model_id(self) -> str:
        return self.config.model

    def list_models(self):
        return [self.current_model_info()]

    def set_model(
        self,
        model_id: str,
        *,
        reasoning_effort: str | None = None,
    ) -> None:
        self.config.model = model_id
        self.config.reasoning_effort = reasoning_effort


def install_builtin_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider_cls: Callable[[Any], Any] = ConfigOnlyProvider,
    *,
    provider_name: str = "codex",
) -> None:
    import yoke.cli.config.providers as providers

    def factory(context: Any) -> ConfigOnlyProvider:
        model = context.model or "gpt-5.4"
        reasoning_effort = context.reasoning_effort
        if reasoning_effort is None and provider_name == "codex":
            reasoning_effort = "xhigh" if model == "gpt-5.4-mini" else "medium"
        return provider_cls(
            SimpleNamespace(
                model=model,
                reasoning_effort=reasoning_effort,
                home=context.home,
                name=context.name,
                env=context.env,
            )
        )

    monkeypatch.setitem(
        providers._BUILTIN_PROVIDER_FACTORIES,
        provider_name,
        factory,
    )
