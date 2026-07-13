"""Agent orchestration loop implementation."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING

from yoke.agent.capabilities import BaseCapability
from yoke.agent.capabilities import CapabilityContext
from yoke.agent.capabilities import CapabilityResolver
from yoke.agent.budget import build_provider_context_manager
from yoke.agent.context import ContextManager
from yoke.agent.loop.iteration import RuntimeAgentIterationMixin
from yoke.agent.loop.resources import acquire_tool_resources
from yoke.agent.loop.resources import release_tool_resources
from yoke.agent.loop.state import context_for_run
from yoke.agent.loop.state import persist_run_context
from yoke.agent.loop.tools.core import index_tools
from yoke.agent.loop.types import AfterToolCallHook
from yoke.agent.loop.types import AgentEventHandler
from yoke.agent.loop.types import AgentResult
from yoke.agent.loop.types import BeforeToolCallHook
from yoke.agent.loop.types import ConversationEntryHistory
from yoke.agent.loop.types import ConversationHistory
from yoke.agent.loop.types import MessageHistory
from yoke.agent.loop.types import MaxIterationsExceededError
from yoke.agent.loop.types import StopRequested
from yoke.agent.loop.types import ToolExecutionMode
from yoke.agent.loop.types import ToolResultCheckpoint
from yoke.agent.models import AgentContext
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec
from yoke.agent.skills.registry import SkillRegistry
from yoke.agent.tools import LocalTool
from yoke.agent.tools import ModelIdentity
from yoke.agent.tools import RegisterTools
from yoke.agent.tools import BackgroundProcessInfo
from yoke.agent.tools import CommandProcessManager
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools import ToolRuntimeContext
from yoke.agent.tools import never_cancel
from yoke.agent.tools.context import normalize_tool_registration
from yoke.agent.tools.context import resolve_model_identity
from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.base import provider_system_messages

if TYPE_CHECKING:
    from yoke.ai.sdk.types import AgentResult as SDKAgentResult
    from yoke.ai.sdk.types import Image
    from yoke.ai.sdk.types import RunConfig
    from yoke.cli.bootstrap.types import ToolLoadReport


_EMPTY_SKILL_REGISTRY = SkillRegistry([])


class RuntimeAgent(RuntimeAgentIterationMixin):
    """Orchestrates the LLM and tool-calling loop with compaction support."""

    supports_message_history = False
    supports_user_message = True

    @classmethod
    def from_run_config(
        cls,
        *,
        provider: Provider,
        config: RunConfig,
    ) -> RuntimeAgent:
        """Build a runtime agent from the public SDK run configuration."""
        from yoke.ai.sdk.runtime import build_agent_capabilities
        from yoke.ai.sdk.runtime import build_system_messages

        root = Path(config.root).resolve()
        active_skills = [skill.to_active_skill() for skill in config.skills]
        available_skills = [
            skill.to_skill_spec()
            for skill in config.skills
            if skill.source_path != "<inline>"
        ]
        return RuntimeAgent(
            provider=provider,
            tools=[],
            capabilities=build_agent_capabilities(
                capabilities=config.capabilities,
                tools=config.tools,
                register_tools=config.register_tools,
            ),
            tool_root=root,
            tool_home=Path.home().resolve(),
            max_iterations=config.max_iterations,
            context_manager=build_provider_context_manager(
                provider=provider,
                instructions=build_system_messages(
                    root=root,
                    sys_prompt=config.sys_prompt,
                    include_agents_file=config.include_agents_file,
                ),
                policy_override=config.compaction,
            ),
            tool_execution=config.tool_execution,
            before_tool_call=config.before_tool_call,
            after_tool_call=config.after_tool_call,
            available_skills=available_skills,
            active_skills=active_skills,
            history=config.history,
        )

    def __init__(
        self,
        provider: Provider,
        tools: Sequence[LocalTool],
        max_iterations: int = 30,
        context_manager: ContextManager | None = None,
        tool_execution: ToolExecutionMode = "parallel",
        before_tool_call: BeforeToolCallHook | None = None,
        after_tool_call: AfterToolCallHook | None = None,
        skill_registry: SkillRegistry = _EMPTY_SKILL_REGISTRY,
        available_skills: Sequence[SkillSpec] = (),
        active_skills: Sequence[ActiveSkill] = (),
        history: ConversationHistory | None = None,
        tool_factory: RegisterTools | None = None,
        capabilities: Sequence[BaseCapability] | None = None,
        tool_root: Path | None = None,
        tool_home: Path = Path.home(),
        base_instructions: Sequence[Message] | None = None,
    ) -> None:
        if tool_factory is not None and capabilities is not None:
            raise ValueError("Use either tool_factory or capabilities, not both.")
        if tool_factory is not None and tool_root is None:
            raise ValueError("Provider-aware tool registration requires tool_root.")
        self.provider = provider
        self._tool_factory = tool_factory
        self._capability_resolver = (
            CapabilityResolver(capabilities) if capabilities is not None else None
        )
        self._tool_root = (
            tool_root or _bound_tool_path(tools, "root") or Path.cwd()
        ).resolve()
        self._tool_home = tool_home.resolve()
        self.command_process_manager = CommandProcessManager()
        self._tool_provider: Provider | None = None
        self._tool_model: ModelIdentity | None = None
        self.max_iterations = max_iterations
        self.context_manager = context_manager or ContextManager()
        self._base_instructions = [
            message.model_copy(deep=True)
            for message in (
                base_instructions
                if base_instructions is not None
                else self.context_manager.instructions
            )
        ]
        self._provider_system_messages: list[Message] = []
        self._tool_system_messages: list[Message] = []
        self.context_manager.set_instructions(self._base_instructions)
        self._context: AgentContext | None = None
        self.tools: dict[str, LocalTool] = {}
        if tool_factory is not None or self._capability_resolver is not None:
            self.refresh_tools(force=True)
        else:
            model = resolve_model_identity(self.provider)
            self._install_tools(tools, model=model)
            self._set_dynamic_system_messages(tool_messages=[])
        self.tool_execution = tool_execution
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        self.skill_registry = skill_registry
        self.available_skills = list(available_skills)
        self.active_skills = list(active_skills)
        self.tool_report: ToolLoadReport | None = None
        if history is not None:
            self.load_conversation(history)

    def fork(self) -> RuntimeAgent:
        """Create an independent runtime copy of this agent."""
        forked = RuntimeAgent(
            provider=self.provider,
            tools=[_copy_tool_for_fork(tool) for tool in self.tools.values()],
            tool_factory=self._tool_factory,
            capabilities=(
                self._capability_resolver.capabilities
                if self._capability_resolver is not None
                else None
            ),
            tool_root=self._tool_root,
            tool_home=self._tool_home,
            base_instructions=self._base_instructions,
            max_iterations=self.max_iterations,
            context_manager=deepcopy(self.context_manager),
            tool_execution=self.tool_execution,
            before_tool_call=self.before_tool_call,
            after_tool_call=self.after_tool_call,
            skill_registry=deepcopy(self.skill_registry),
            available_skills=deepcopy(self.available_skills),
            active_skills=deepcopy(self.active_skills),
        )
        if self._context is not None:
            forked._context = self._context.model_copy(deep=True)
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
        """Clear conversation state and terminate its background commands."""
        self.command_process_manager.terminate_all()
        self._context = None

    def close(self) -> None:
        """Release tools and background commands owned by this runtime."""
        tools = self.tools
        self.tools = {}
        try:
            release_tool_resources(tools.values())
        finally:
            self.command_process_manager.terminate_all()

    def list_background_processes(self) -> list[BackgroundProcessInfo]:
        """Return running background commands for this runtime."""
        return self.command_process_manager.list_processes()

    def terminate_background_process(self, session_id: int) -> bool:
        """Terminate one background command by session identifier."""
        return self.command_process_manager.terminate_process(session_id)

    def terminate_all_background_processes(self) -> int:
        """Terminate all background commands and return the count."""
        return self.command_process_manager.terminate_all()

    def refresh_tools(self, *, force: bool = False) -> bool:
        """Refresh tool registration and runtime context for the active model."""
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
            tools = list(registration.tools)
            invalid = [tool for tool in tools if not isinstance(tool, LocalTool)]
            if invalid:
                raise TypeError(
                    "Tool registration callbacks must return LocalTool instances."
                )
            self._install_tools(tools, model=model)
            self._set_dynamic_system_messages(
                tool_messages=list(registration.system_messages),
            )
        elif self._capability_resolver is not None:
            resolution = self._capability_resolver.resolve(
                CapabilityContext(
                    root=self._tool_root,
                    home=self._tool_home,
                    provider=self.provider,
                    model=model,
                    cancel_requested=never_cancel,
                )
            )
            tools = list(resolution.tools)
            invalid = [tool for tool in tools if not isinstance(tool, LocalTool)]
            if invalid:
                raise TypeError("Capabilities must register LocalTool instances.")
            self._install_tools(tools, model=model)
            self._set_dynamic_system_messages(
                tool_messages=list(resolution.system_messages),
            )
        else:
            self._install_tools(list(self.tools.values()), model=model)
            self._set_dynamic_system_messages(tool_messages=[])
        return True

    def _set_dynamic_system_messages(
        self,
        *,
        tool_messages: Sequence[Message],
    ) -> None:
        self._provider_system_messages = provider_system_messages(self.provider)
        self._tool_system_messages = [
            message.model_copy(deep=True) for message in tool_messages
        ]
        self.context_manager.set_instructions(
            [
                *self._base_instructions,
                *self._provider_system_messages,
                *self._tool_system_messages,
            ],
            context=self._context,
        )

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
        )
        for tool in tools:
            tool.bind_runtime_context(runtime_context)
            tool._context["command_process_manager"] = self.command_process_manager
        indexed = index_tools(tools)
        previous_tools = self.tools
        acquire_tool_resources(indexed.values())
        self.tools = indexed
        release_tool_resources(previous_tools.values())
        self._tool_provider = self.provider
        self._tool_model = resolved_model

    def load_conversation(
        self,
        history: ConversationHistory,
        *,
        available_skills: Sequence[SkillSpec] | None = None,
        active_skills: Sequence[ActiveSkill] | None = None,
    ) -> None:
        """Replace the owned conversation state from persisted history."""
        messages = history.messages if isinstance(history, MessageHistory) else None
        conversation_entries = (
            history.entries if isinstance(history, ConversationEntryHistory) else None
        )
        skill_registry = self.skill_registry.with_skills(available_skills or ())
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
            active_skills=skill_registry.reconcile(active_skills, self.active_skills),
        )
        self.active_skills = [
            skill.model_copy(deep=True) for skill in self._context.active_skills
        ]

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
        self.refresh_tools(force=True)
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
            for iteration in range(1, self.max_iterations + 1):
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
            error = MaxIterationsExceededError(
                f"Agent exceeded max_iterations={self.max_iterations}"
            )
            error.partial_messages = context.messages
            error.partial_conversation_entries = [
                entry.model_copy(deep=True)
                for entry in context.conversation_log.entries
            ]
            persist_run_context(self, context)
            raise error
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

    def prompt[StructuredT](
        self,
        prompt: str,
        *,
        images: Sequence[Image | str | Path] = (),
        image_urls: Sequence[str] = (),
        output_type: type[StructuredT] | None = None,
        on_event: AgentEventHandler | None = None,
        stop_requested: StopRequested | None = None,
        before_tool_call: BeforeToolCallHook | None = None,
        after_tool_call: AfterToolCallHook | None = None,
    ) -> SDKAgentResult[StructuredT]:
        """Run the SDK-style agent prompt flow and return the public result."""
        from yoke.ai.sdk.types import AgentResult as SDKAgentResult
        from yoke.ai.sdk.types import (
            append_structured_output_instructions,
        )
        from yoke.ai.sdk.types import build_user_message_from_images
        from yoke.ai.sdk.types import normalize_image_inputs
        from yoke.ai.sdk.types import parse_structured_output

        user_message = None
        normalized_images, normalized_urls = normalize_image_inputs(
            images=images,
            image_urls=image_urls,
        )
        if normalized_images or normalized_urls:
            user_message = build_user_message_from_images(
                prompt,
                images=normalized_images,
                image_urls=normalized_urls,
            )
        if output_type is not None:
            base_message = user_message or Message.user(prompt)
            user_message = append_structured_output_instructions(
                base_message,
                output_type=output_type,
            )
        runtime_result = self.run(
            prompt,
            user_message=user_message,
            on_event=on_event,
            stop_requested=stop_requested,
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
        )
        structured = parse_structured_output(
            runtime_result.output,
            output_type=output_type,
        )
        message = (
            runtime_result.messages[-1]
            if runtime_result.messages
            else Message.assistant(runtime_result.output)
        )
        return SDKAgentResult(
            message=message,
            output=runtime_result.output,
            messages=runtime_result.messages,
            iterations=runtime_result.iterations,
            status=runtime_result.status,
            conversation_entries=runtime_result.conversation_entries,
            structured=structured,
        )


def _bound_tool_path(tools: Sequence[LocalTool], key: str) -> Path | None:
    for tool in tools:
        value = tool._context.get(key)
        if isinstance(value, Path):
            return value
        if isinstance(value, str):
            return Path(value)
    return None


def _copy_tool_for_fork(tool: LocalTool) -> LocalTool:
    copied = tool.model_copy(deep=False)
    copied._context = {
        key: value
        for key, value in tool._context.items()
        if key not in {"command_process_manager", "runtime_context"}
    }
    return copied
