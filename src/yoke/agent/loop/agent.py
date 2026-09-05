"""Agent orchestration loop implementation."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from copy import deepcopy
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING

from yoke.agent.context import ContextManager
from yoke.agent.loop.iteration import RuntimeAgentIterationMixin
from yoke.agent.loop.in_process_tool import shutdown_in_process_tools
from yoke.agent.loop.forking import copy_tool_for_fork
from yoke.agent.loop.forking import copy_tool_for_runtime
from yoke.agent.loop.state import context_for_run
from yoke.agent.loop.state import persist_run_context
from yoke.agent.loop.resources import release_tool_resources
from yoke.agent.loop.types import AfterToolCallHook
from yoke.agent.loop.types import AgentEventHandler
from yoke.agent.loop.types import AgentResult
from yoke.agent.loop.types import BeforeToolCallHook
from yoke.agent.loop.types import StopRequested
from yoke.agent.loop.types import ToolExecutionMode
from yoke.agent.loop.types import ToolResultCheckpoint
from yoke.agent.loop.tool_registration import ToolRegistrationMixin
from yoke.agent.loop.tool_registration import bound_tool_path
from yoke.agent.models import AgentContext
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec
from yoke.agent.skills.registry import SkillRegistry
from yoke.agent.tools import LocalTool
from yoke.agent.tools import ModelIdentity
from yoke.agent.tools import RegisterTools
from yoke.agent.tools.command_process_manager import (
    CommandProcessManager,
)
from yoke.agent.tools.context import resolve_model_identity
from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.base import fork_provider

if TYPE_CHECKING:
    from yoke.cli.bootstrap.types import ToolLoadReport


class RuntimeAgent(ToolRegistrationMixin, RuntimeAgentIterationMixin):
    """Orchestrates the LLM and tool-calling loop with compaction support."""

    supports_message_history = False
    supports_user_message = True

    def __init__(
        self,
        provider: Provider,
        tools: Sequence[LocalTool],
        context_manager: ContextManager | None = None,
        tool_execution: ToolExecutionMode = "parallel",
        before_tool_call: BeforeToolCallHook | None = None,
        after_tool_call: AfterToolCallHook | None = None,
        skill_registry: SkillRegistry | None = None,
        available_skills: Sequence[SkillSpec] = (),
        active_skills: Sequence[ActiveSkill] = (),
        messages: Sequence[Message] | None = None,
        conversation_entries: Sequence[ConversationEntry] | None = None,
        tool_factory: RegisterTools | None = None,
        tool_root: Path | None = None,
        tool_home: Path | None = None,
        command_process_manager: CommandProcessManager | None = None,
    ) -> None:
        if messages is not None and conversation_entries is not None:
            raise ValueError(
                "Provide either messages or conversation_entries, not both."
            )
        if tool_factory is not None and tool_root is None:
            raise ValueError("Provider-aware tool registration requires tool_root.")
        self.provider = provider
        self._closed = False
        self._tool_factory = tool_factory
        self._tool_root = (
            tool_root or bound_tool_path(tools, "root") or Path.cwd()
        ).resolve()
        self._tool_home = (tool_home or Path.home()).resolve()
        self._tool_provider: Provider | None = None
        self._tool_model: ModelIdentity | None = None
        self.tools: dict[str, LocalTool] = {}
        self.context_manager = context_manager or ContextManager()
        self._base_instructions = [
            message.model_copy(deep=True)
            for message in self.context_manager.instructions
        ]
        self._tool_system_messages: list[Message] = []
        self._session_enabled_tool_names: set[str] | None = None
        self.tool_execution = tool_execution
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        self.skill_registry = skill_registry
        self.available_skills = list(available_skills)
        self.active_skills = list(active_skills)
        self.tool_report: ToolLoadReport | None = None
        self._context: AgentContext | None = None
        self._context_owned_for_run = False
        self._seen_command_completion_events: set[str] = set()
        self._seen_dropped_completion_events = 0
        self.command_process_manager = (
            command_process_manager or CommandProcessManager()
        ).acquire()
        try:
            if tool_factory is not None:
                self.refresh_tools(force=True)
            else:
                self._install_tools(
                    [copy_tool_for_runtime(tool) for tool in tools],
                    model=resolve_model_identity(self.provider),
                )
            if messages is not None or conversation_entries is not None:
                self.load_conversation(
                    messages=messages,
                    conversation_entries=conversation_entries,
                )
        except BaseException:
            with suppress(Exception):
                self.close()
            raise

    def fork(
        self,
        *,
        isolate_provider: bool = False,
        include_state: bool = True,
    ) -> RuntimeAgent:
        """Create an independent runtime copy of this agent."""
        provider = fork_provider(self.provider) if isolate_provider else self.provider
        context_manager = deepcopy(self.context_manager)
        context_manager.instructions = [
            message.model_copy(deep=True) for message in self._base_instructions
        ]
        context_manager.system_prompt = (
            context_manager.instructions[0].plain_text_content
            if context_manager.instructions
            else None
        )
        forked = RuntimeAgent(
            provider=provider,
            tools=[copy_tool_for_fork(tool) for tool in self.tools.values()],
            tool_factory=self._tool_factory,
            tool_root=self._tool_root,
            tool_home=self._tool_home,
            command_process_manager=self.command_process_manager,
            context_manager=context_manager,
            tool_execution=self.tool_execution,
            before_tool_call=self.before_tool_call,
            after_tool_call=self.after_tool_call,
            skill_registry=deepcopy(self.skill_registry),
            available_skills=deepcopy(self.available_skills),
            active_skills=deepcopy(self.active_skills),
        )
        if include_state and self._context is not None:
            forked._context = self._context.model_copy(deep=True)
            forked._sync_context_instructions(forked._context)
        forked._seen_command_completion_events.update(
            self._seen_command_completion_events
        )
        forked._seen_dropped_completion_events = self._seen_dropped_completion_events
        return forked

    @property
    def has_state(self) -> bool:
        """Return whether the agent currently owns conversation state."""
        return self._context is not None

    @property
    def messages(self) -> list[Message]:
        """Return the current transcript messages."""
        if self._context is None:
            return []
        return self.context_manager.transcript_messages(self._context)

    @property
    def conversation_entries(self) -> list[ConversationEntry]:
        """Return the current structured conversation log."""
        if self._context is None:
            return []
        return [
            entry.model_copy(deep=True)
            for entry in self._context.conversation_log.entries
        ]

    def reset(self) -> None:
        """Clear the owned conversation state and keep runtime config."""
        self._context = None
        self._context_owned_for_run = False

    def close(self) -> None:
        """Release tool resources owned by this runtime."""
        if self._closed:
            return
        tools = self.tools
        shutdown_in_process_tools(tools)
        self._closed = True
        self.tools = {}
        try:
            release_tool_resources(tools.values())
        finally:
            self.command_process_manager.release()

    @property
    def closed(self) -> bool:
        """Return whether in-process work stopped and terminal cleanup began."""
        return self._closed

    def load_conversation(
        self,
        *,
        messages: Sequence[Message] | None = None,
        conversation_entries: Sequence[ConversationEntry] | None = None,
        available_skills: Sequence[SkillSpec] | None = None,
        active_skills: Sequence[ActiveSkill] | None = None,
    ) -> None:
        """Replace the owned conversation state from persisted history."""
        if messages is not None and conversation_entries is not None:
            raise ValueError(
                "Provide either messages or conversation_entries, not both."
            )
        self._context = self.context_manager.initialize(
            "",
            list(messages) if messages is not None else None,
            append_prompt=False,
            conversation_entries=conversation_entries,
            available_skills=list(
                available_skills
                if available_skills is not None
                else self.available_skills
            ),
            active_skills=list(
                active_skills if active_skills is not None else self.active_skills
            ),
        )
        self.active_skills = [
            skill.model_copy(deep=True) for skill in self._context.active_skills
        ]
        self._context_owned_for_run = False

    def load_owned_conversation(
        self,
        conversation_entries: list[ConversationEntry],
        *,
        available_skills: Sequence[SkillSpec] | None = None,
        active_skills: Sequence[ActiveSkill] | None = None,
    ) -> None:
        """Take a validated active path for one isolated runtime turn."""
        self._context = self.context_manager.initialize_owned(
            "",
            conversation_entries,
            append_prompt=False,
            available_skills=(
                available_skills
                if available_skills is not None
                else self.available_skills
            ),
            active_skills=(
                active_skills if active_skills is not None else self.active_skills
            ),
        )
        self.active_skills = [
            skill.model_copy(deep=True) for skill in self._context.active_skills
        ]
        self._context_owned_for_run = True

    def run(
        self,
        prompt: str,
        *,
        user_message: Message | None = None,
        on_event: AgentEventHandler | None = None,
        stop_requested: StopRequested | None = None,
        before_tool_call: BeforeToolCallHook | None = None,
        after_tool_call: AfterToolCallHook | None = None,
        after_tool_result_appended: ToolResultCheckpoint | None = None,
        available_skills: Sequence[SkillSpec] | None = None,
        active_skills: Sequence[ActiveSkill] | None = None,
    ) -> AgentResult:
        """Run the agent loop for the given prompt and return the result."""
        self.refresh_tools()
        context = context_for_run(
            self,
            prompt,
            user_message=user_message,
            available_skills=available_skills,
            active_skills=active_skills,
        )
        try:
            active_before_hook = before_tool_call or self.before_tool_call
            active_after_hook = after_tool_call or self.after_tool_call
            if self._is_stopped(stop_requested):
                stopped = self._stopped_result(context, iterations=0)
                persist_run_context(self, context)
                return stopped
            for iteration in count(1):
                iteration_result = self._run_iteration(
                    context,
                    iteration=iteration,
                    on_event=on_event,
                    stop_requested=stop_requested,
                    before_tool_call=active_before_hook,
                    after_tool_call=active_after_hook,
                    after_tool_result_appended=after_tool_result_appended,
                )
                if iteration_result is not None:
                    persist_run_context(self, context)
                    return iteration_result
            raise AssertionError("The unbounded iteration loop terminated.")
        except ProviderError as exc:
            exc.partial_messages = context.messages
            exc.partial_conversation_entries = [
                entry.model_copy(deep=True)
                for entry in context.conversation_log.entries
            ]
            persist_run_context(self, context)
            raise
        except Exception:
            persist_run_context(self, context)
            raise
