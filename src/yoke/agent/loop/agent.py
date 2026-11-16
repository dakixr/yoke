"""Agent orchestration loop implementation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import suppress
from copy import deepcopy
from pathlib import Path

from yoke.agent.context import ContextManager
from yoke.agent.loop.command_notifications import CommandCompletionNotices
from yoke.agent.loop.forking import (
    bound_tool_path,
    copy_tool_for_fork,
    copy_tool_for_runtime,
)
from yoke.agent.loop.in_process_tool import (
    InProcessTools,
    RuntimeTools,
    shutdown_in_process_tools,
)
from yoke.agent.loop.iteration import AgentRun
from yoke.agent.loop.resources import ToolResources
from yoke.agent.loop.tool_core import index_tools, tool_definitions
from yoke.agent.loop.types import (
    AfterToolCallHook,
    AgentEventHandler,
    AgentResult,
    BeforeToolCallHook,
    StopRequested,
    ToolExecutionMode,
    ToolResultCheckpoint,
)
from yoke.agent.models import AgentContext, ConversationEntry, Message
from yoke.agent.skills.context import append_missing_active_skill_messages
from yoke.agent.skills.mentions import activate_mentioned_skills
from yoke.agent.skills.models import ActiveSkill, SkillSpec
from yoke.agent.skills.registry import SkillRegistry
from yoke.agent.tools import (
    LocalTool,
    ModelIdentity,
    RegisterTools,
    ToolRegistrationContext,
    ToolRuntimeContext,
    never_cancel,
)
from yoke.agent.tools.command_process import CommandProcessManager
from yoke.agent.tools.context import (
    ToolSessionContext,
    normalize_tool_registration,
    resolve_model_identity,
)
from yoke.agent.tools.registration import ToolLoadReport
from yoke.ai.providers.base import Provider, fork_provider, start_provider_turn


class RuntimeAgent:
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
        tool_session_context: ToolSessionContext | None = None,
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
        from yoke.environment import current_environment

        environment = current_environment()
        self.tool_session_context = tool_session_context or ToolSessionContext(
            environment=dict(environment.values) if environment is not None else None,
            cwd=environment.cwd if environment is not None else None,
        )
        self._tool_root = (
            tool_root or bound_tool_path(tools, "root") or Path.cwd()
        ).resolve()
        self._tool_home = (tool_home or Path.home()).resolve()
        self._tool_provider: Provider | None = None
        self._tool_model: ModelIdentity | None = None
        self._tool_resources = ToolResources()
        self._workers = InProcessTools()
        self._tools = RuntimeTools(self._workers)
        self.context_manager = context_manager or ContextManager()
        self._base_instructions = [
            message.model_copy(deep=True)
            for message in self.context_manager.instructions
        ]
        self._tool_system_messages: list[Message] = []
        self._session_enabled_tool_names: set[str] | None = None
        self.tool_execution: ToolExecutionMode = tool_execution
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        self.skill_registry = skill_registry
        self.available_skills = list(available_skills)
        self.active_skills = list(active_skills)
        self.tool_report: ToolLoadReport | None = None
        self._context: AgentContext | None = None
        self._context_owned_for_run = False
        self.command_process_manager = (
            command_process_manager
            or CommandProcessManager(
                base_environment=self.tool_session_context.environment
            )
        ).acquire()
        self.command_notices = CommandCompletionNotices(self.command_process_manager)
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
        forked: RuntimeAgent | None = None
        try:
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
                tool_session_context=self.tool_session_context,
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
            forked.command_notices.seen.update(self.command_notices.seen)
            forked.command_notices.dropped = self.command_notices.dropped
            return forked
        except BaseException:
            # A directory can disappear during fork construction. Until return,
            # the caller cannot own or retire this fork and its isolated provider.
            if forked is not None:
                with suppress(Exception):
                    forked.close()
            if provider is not self.provider:
                close_provider = getattr(provider, "close", None)
                if callable(close_provider):
                    with suppress(Exception):
                        close_provider()
            raise

    @property
    def has_state(self) -> bool:
        """Return whether the agent currently owns conversation state."""
        return self._context is not None

    @property
    def conversation_marker(self) -> tuple[int, str | None, str | None] | None:
        """Identify owned context, leaf and checkpoint without copying messages.

        Presentation adapters can detect compaction or context replacement
        during model selection without inspecting this runtime's private state.
        """
        from yoke.agent.conversation import project_conversation

        context = self._context
        if context is None:
            return None
        log = context.conversation_log
        checkpoint = project_conversation(log.entries, leaf_id=log.leaf_id).checkpoint
        return (
            id(context),
            log.leaf_id,
            checkpoint.id if checkpoint is not None else None,
        )

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

    def apply_tool_selection(
        self, tools: Sequence[LocalTool], *, report: ToolLoadReport | None = None
    ) -> None:
        """Apply session tool selection and rebuild its registered instructions.

        Callers pass the complete selection, including hidden runtime tools.
        Factory-backed runtimes rebuild through their normal registration path.
        Replace the report after registration and before installing its filtered
        instructions, preserving the menu's existing instruction ordering.
        """
        if self._tool_factory is None:
            self.tools = index_tools(tools)
        else:
            self.set_session_enabled_tools({tool.name for tool in tools})
        if report is not None:
            self.tool_report = report
        self._install_session_filtered_tool_system_messages()

    def close(self) -> None:
        """Release tool resources owned by this runtime."""
        if self._closed:
            return
        tools = self.tools
        shutdown_in_process_tools(tools)
        self._closed = True
        self.tools = {}
        try:
            self._tool_resources.release(tools.values())
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
        append_user_message: bool = True,
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
        context = self.context_for_run(
            prompt,
            user_message=user_message,
            append_user_message=append_user_message,
            available_skills=available_skills,
            active_skills=active_skills,
        )
        return AgentRun(
            self,
            context,
            on_event=on_event,
            stop_requested=stop_requested,
            before_tool_call=before_tool_call or self.before_tool_call,
            after_tool_call=after_tool_call or self.after_tool_call,
            after_tool_result_appended=after_tool_result_appended,
        ).run()

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
                        session=self.tool_session_context,
                        cancel_requested=never_cancel,
                    )
                )
            )
            tools = [
                tool
                for tool in registration.tools
                if self._session_enabled_tool_names is None
                or tool.name in self._session_enabled_tool_names
            ]
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

    def configure_session_tools(
        self,
        *,
        session: ToolSessionContext | None = None,
        refresh: bool = True,
        filter_instructions: bool = False,
        sync_context: bool = True,
        wrap_factory: Callable[
            [RegisterTools | None, tuple[LocalTool, ...]], RegisterTools
        ]
        | None = None,
    ) -> None:
        """Bind session configuration and refresh the owned registration/context."""
        if session is not None:
            self.tool_session_context = session
        if wrap_factory is not None:
            self._tool_factory = wrap_factory(
                self._tool_factory, tuple(self.tools.values())
            )
        if refresh:
            self.refresh_tools(force=True)
        if filter_instructions:
            self._install_session_filtered_tool_system_messages()
        if sync_context and self._context is not None:
            self._sync_context_instructions(self._context)

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
            session=self.tool_session_context,
            cancel_requested=never_cancel,
            command_process_manager=self.command_process_manager,
            seen_command_completion_events=self.command_notices.seen,
        )
        for tool in tools:
            tool.bind_runtime_context(runtime_context)
        indexed = index_tools(tools)
        previous_tools = self.tools
        self._tool_resources.acquire(indexed.values())
        self.tools = indexed
        self._tool_resources.release(previous_tools.values())
        self._tool_provider = self.provider
        self._tool_model = resolved_model

    def _install_tool_system_messages(self, messages: Sequence[Message]) -> None:
        """Install tool registration system messages into live context."""
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
        runtime_messages = context.messages[len(context.instructions) :]
        context.instructions = [
            message.model_copy(deep=True)
            for message in self.context_manager.instructions
        ]
        context.messages = [*context.instructions, *runtime_messages]

    def context_for_run(
        self,
        prompt: str,
        *,
        user_message: Message | None,
        available_skills: Sequence[SkillSpec] | None,
        active_skills: Sequence[ActiveSkill] | None,
        append_user_message: bool = True,
    ) -> AgentContext:
        """Build the working context for one agent run."""
        if not append_user_message and (prompt or user_message is not None):
            raise ValueError("Continuation must not contain user input")
        if not append_user_message and (
            self._context is None
            or not any(message.role == "user" for message in self._context.messages)
        ):
            raise ValueError("Continuation requires conversation history")
        start_provider_turn(self.provider)
        resolved_available_skills = list(
            available_skills if available_skills is not None else self.available_skills
        )
        resolved_active_skills = list(
            active_skills if active_skills is not None else self.active_skills
        )
        if append_user_message:
            resolved_active_skills = activate_mentioned_skills(
                prompt=prompt,
                registry=self.skill_registry,
                active_skills=resolved_active_skills,
            )
            self.active_skills = [
                skill.model_copy(deep=True) for skill in resolved_active_skills
            ]
        if self._context is None:
            context = self.context_manager.initialize(
                prompt,
                None,
                user_message=user_message,
                append_prompt=False,
                available_skills=resolved_available_skills,
                active_skills=resolved_active_skills,
            )
            append_missing_active_skill_messages(context)
            self.context_manager.append_message(
                context, user_message or Message.user(prompt)
            )
            return context
        if self._context_owned_for_run:
            context = self._context
            self._context_owned_for_run = False
        else:
            context = self._context.model_copy(deep=True)
        context.available_skills = [
            skill.model_copy(deep=True) for skill in resolved_available_skills
        ]
        context.active_skills = [
            skill.model_copy(deep=True) for skill in resolved_active_skills
        ]
        append_missing_active_skill_messages(context)
        if append_user_message:
            self.context_manager.append_message(
                context, user_message or Message.user(prompt)
            )
        return context

    def replace_context(self, context: AgentContext) -> None:
        """Adopt a completed run or a manual compaction's working context."""
        self._context = context

    def tool_definitions(self) -> list[dict[str, object]]:
        """Return the exact tools advertised on model and handoff requests."""
        return tool_definitions(self.tools)

    @property
    def tool_root(self) -> Path:
        """Return the runtime's configured tool workspace."""
        return self._tool_root

    def context_for_messages(self, messages: Sequence[Message]) -> AgentContext:
        """Use owned state when it matches a transcript, otherwise rebuild it."""
        if self._context is not None and self.messages == list(messages):
            return self._context
        return self.context_manager.initialize(
            "",
            list(messages),
            append_prompt=False,
            available_skills=self.available_skills,
            active_skills=self.active_skills,
        )

    sync_context_instructions = _sync_context_instructions

    def promote(self, forked: RuntimeAgent) -> None:
        """Accept an isolated turn while leaving its old resources on the fork."""
        self.provider, forked.provider = forked.provider, self.provider
        self._context = forked._context
        self._context_owned_for_run = False
        forked._context = None
        self.command_notices.seen.clear()
        self.command_notices.seen.update(forked.command_notices.seen)
        self.command_notices.dropped = forked.command_notices.dropped
        self.active_skills = [
            skill.model_copy(deep=True) for skill in forked.active_skills
        ]
        if self.refresh_tools(force=True) and self._context is not None:
            self._sync_context_instructions(self._context)

    @property
    def tools(self) -> dict[str, LocalTool]:
        """Return the tool index carrying this runtime's worker ownership."""
        return self._tools

    @tools.setter
    def tools(self, tools: dict[str, LocalTool]) -> None:
        self._workers = InProcessTools()
        self._tools = RuntimeTools(self._workers, tools)
