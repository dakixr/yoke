"""Provider-aware runtime tool registration helpers."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from yoke.agent.loop.tool_core import index_tools
from yoke.agent.loop.resources import acquire_tool_resources
from yoke.agent.loop.resources import release_tool_resources
from yoke.agent.models import AgentContext
from yoke.agent.models import Message
from yoke.agent.tools import LocalTool
from yoke.agent.tools import ModelIdentity
from yoke.agent.tools import RegisterTools
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools import ToolRuntimeContext
from yoke.agent.tools import never_cancel
from yoke.agent.tools.context import normalize_tool_registration
from yoke.agent.tools.context import resolve_model_identity
from yoke.ai.providers.base import Provider

if TYPE_CHECKING:
    from yoke.agent.context import ContextManager
    from yoke.agent.tools.command_process_manager import (
        CommandProcessManager,
    )


class ToolRegistrationMixin:
    """Mixin for refreshing tools when provider/model identity changes."""

    provider: Provider
    tools: dict[str, LocalTool]
    _tool_factory: RegisterTools | None
    _tool_root: Path
    _tool_home: Path
    _tool_provider: Provider | None
    _tool_model: ModelIdentity | None
    _base_instructions: list[Message]
    _tool_system_messages: list[Message]
    _session_enabled_tool_names: set[str] | None
    _seen_command_completion_events: set[str]
    command_process_manager: CommandProcessManager
    context_manager: ContextManager

    def refresh_tools(self, *, force: bool = False) -> bool:
        """Refresh tool registration for the active model."""
        model = resolve_model_identity(self.provider)
        changed = self._tool_provider is not self.provider or self._tool_model != model
        if not force and not changed:
            return False
        if self._tool_factory is not None:
            registration = normalize_tool_registration(
                self._tool_factory(
                    ToolRegistrationContext(
                        root=self._tool_root,
                        home=self._tool_home,
                        provider=self.provider,
                        model=model,
                        cancel_requested=never_cancel,
                    )
                )
            )
            tools = self._filter_session_enabled_tools(list(registration.tools))
            invalid = [tool for tool in tools if not isinstance(tool, LocalTool)]
            if invalid:
                raise TypeError(
                    "Tool registration callbacks must return LocalTool instances."
                )
            self._install_tools(tools, model=model)
            self._install_tool_system_messages(list(registration.system_messages))
            if self._session_enabled_tool_names is not None:
                self._install_session_filtered_tool_system_messages()
        else:
            self._install_tools(list(self.tools.values()), model=model)
            self._install_tool_system_messages([])
        return True

    def _filter_session_enabled_tools(
        self,
        tools: list[LocalTool],
    ) -> list[LocalTool]:
        if self._session_enabled_tool_names is None:
            return tools
        return [tool for tool in tools if tool.name in self._session_enabled_tool_names]

    def set_session_enabled_tools(self, tool_names: set[str] | None) -> None:
        """Set a session-only runtime tool allowlist."""
        self._session_enabled_tool_names = (
            set(tool_names) if tool_names is not None else None
        )
        self.refresh_tools(force=True)

    def _install_tools(
        self,
        tools: Sequence[LocalTool],
        *,
        model: ModelIdentity | None = None,
    ) -> None:
        resolved_model = model or resolve_model_identity(self.provider)
        runtime_context = ToolRuntimeContext(
            root=self._tool_root,
            home=self._tool_home,
            provider=self.provider,
            model=resolved_model,
            cancel_requested=never_cancel,
            command_process_manager=self.command_process_manager,
            seen_command_completion_events=self._seen_command_completion_events,
        )
        for tool in tools:
            tool.bind_runtime_context(runtime_context)
        indexed = index_tools(tools)
        previous_tools = self.tools
        acquire_tool_resources(indexed.values())
        self.tools = indexed
        release_tool_resources(previous_tools.values())
        self._tool_provider = self.provider
        self._tool_model = resolved_model

    def _install_tool_system_messages(self, messages: Sequence[Message]) -> None:
        """Install tool registration system messages into live context."""
        if not hasattr(self, "context_manager"):
            return
        self._tool_system_messages = [
            message.model_copy(deep=True) for message in messages
        ]
        instructions = [
            *(message.model_copy(deep=True) for message in self._base_instructions),
            *(message.model_copy(deep=True) for message in self._tool_system_messages),
        ]
        self.context_manager.instructions = instructions
        self.context_manager.system_prompt = (
            instructions[0].plain_text_content if instructions else None
        )

    def _install_session_filtered_tool_system_messages(self) -> None:
        report = getattr(self, "tool_report", None)
        if report is None or report.system_messages is None:
            return
        active_ids = {
            entry.registration_id
            for entry in report.discovered_tools
            if entry.registration_id is not None and entry.tool.name in self.tools
        }
        messages = [
            entry.message
            for entry in report.system_messages
            if entry.registration_id is not None and entry.registration_id in active_ids
        ]
        self._install_tool_system_messages(messages)

    def _sync_context_instructions(self, context: AgentContext) -> None:
        """Sync current context instructions after runtime tool refresh."""
        if not hasattr(self, "context_manager"):
            return
        runtime_messages = context.messages[len(context.instructions) :]
        context.instructions = [
            message.model_copy(deep=True)
            for message in self.context_manager.instructions
        ]
        context.messages = [*context.instructions, *runtime_messages]


def bound_tool_path(tools: Sequence[LocalTool], key: str) -> Path | None:
    """Return a bound path-like tool context value from a tool sequence."""
    for tool in tools:
        value = tool._context.get(key)
        if isinstance(value, Path):
            return value
        if isinstance(value, str):
            return Path(value)
    return None
