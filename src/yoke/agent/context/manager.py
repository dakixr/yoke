"""Context manager implementation for yoke agents."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.compaction import (
    CompactionPolicy,
    CompactionPreparation,
    CompactionReason,
    CompactionResult,
    Compactor,
    TokenEstimate,
)
from yoke.agent.context.accounting import latest_log_usage, latest_message_usage
from yoke.agent.context.compaction_projection import (
    compacted_runtime_messages,
    compaction_user_entries,
    next_compaction_generation_from_active_path,
)
from yoke.agent.context.helpers import (
    append_conversation_entry,
    build_conversation_log,
    build_conversation_log_from_entries,
    extract_memory_snapshot_from_entries,
    extract_persisted_memory_snapshot,
    normalize_instructions,
    resolve_instructions,
    strip_persisted_memory_messages,
)
from yoke.agent.context.history import (
    append_message,
    append_skill_message,
    append_tool_context_message,
    append_tool_result,
    message_image_count,
    newest_real_user_message,
    transcript_messages,
)
from yoke.agent.message_sanitizer import normalize_tool_call_sequence
from yoke.agent.models import (
    AgentContext,
    CompactionHandoff,
    ConversationEntry,
    ConversationLog,
    MemorySnapshot,
    Message,
)
from yoke.agent.prompting import build_prompt_context
from yoke.agent.session_tree import SessionTree
from yoke.agent.skills.models import ActiveSkill, SkillSpec
from yoke.agent.tool_result_projection import project_tool_results_for_provider
from yoke.agent.usage import UsageAccounting, effective_usage_accounting


class ContextManager:
    """Manages agent conversation context, instructions, and compaction."""

    def __init__(
        self,
        instructions: Sequence[Message] | None = None,
        compaction_policy: CompactionPolicy | None = None,
        compactor: Compactor | None = None,
    ) -> None:
        self.instructions = normalize_instructions(instructions)
        self.system_prompt = (
            self.instructions[0].plain_text_content if self.instructions else None
        )
        self.compactor = compactor or Compactor()
        self.compaction_policy = compaction_policy or CompactionPolicy()
        self.max_total_tokens = (
            None
            if not self.compaction_policy.enabled
            else self.compaction_policy.max_total_tokens
        )
        self.keep_recent_tokens = self.compaction_policy.keep_recent_tokens

    def initialize(
        self,
        prompt: str,
        messages: list[Message] | None = None,
        *,
        user_message: Message | None = None,
        append_prompt: bool = True,
        conversation_entries: Sequence[ConversationEntry] | None = None,
        available_skills: Sequence[SkillSpec] | None = None,
        active_skills: Sequence[ActiveSkill] | None = None,
    ) -> AgentContext:
        """Build the initial AgentContext state from persisted messages/entries."""
        if conversation_entries is not None:
            persisted_messages = [
                entry.message
                for entry in conversation_entries
                if entry.message is not None and entry.kind != "instruction"
            ]
            resolved_instructions = resolve_instructions(
                persisted_messages, self.instructions
            )
            prior_memory_snapshot = extract_memory_snapshot_from_entries(
                conversation_entries
            )
            conversation_log = build_conversation_log_from_entries(
                conversation_entries,
                prior_memory_snapshot,
            )
        else:
            persisted_messages = [
                message.model_copy(deep=True) for message in messages or []
            ]
            prior_memory_snapshot = extract_persisted_memory_snapshot(
                persisted_messages
            )
            recent_messages = strip_persisted_memory_messages(persisted_messages)
            resolved_instructions = resolve_instructions(
                recent_messages, self.instructions
            )
            conversation_log = build_conversation_log(
                persisted_messages,
                prior_memory_snapshot,
            )
        context = AgentContext(
            system_prompt=self.system_prompt,
            messages=[],
            instructions=resolved_instructions,
            conversation_log=conversation_log,
            available_skills=[
                skill.model_copy(deep=True) for skill in available_skills or []
            ],
            active_skills=[
                skill.model_copy(deep=True) for skill in active_skills or []
            ],
        )
        context.messages = self.transcript_messages(context)
        if append_prompt:
            self.append_message(context, user_message or Message.user(prompt))
        return context

    def initialize_owned(
        self,
        prompt: str,
        conversation_entries: list[ConversationEntry],
        *,
        user_message: Message | None = None,
        append_prompt: bool = True,
        available_skills: Sequence[SkillSpec] | None = None,
        active_skills: Sequence[ActiveSkill] | None = None,
    ) -> AgentContext:
        """Take a validated active path without recopying its entry values."""
        persisted_messages = [
            entry.message
            for entry in conversation_entries
            if entry.message is not None and entry.kind != "instruction"
        ]
        resolved_instructions = resolve_instructions(
            persisted_messages, self.instructions
        )
        seed = SessionTree.take_validated_runtime(conversation_entries)
        context = AgentContext.model_construct(
            system_prompt=self.system_prompt,
            messages=[*resolved_instructions, *seed.messages],
            instructions=resolved_instructions,
            conversation_log=ConversationLog.model_construct(
                entries=seed.entries,
                leaf_id=seed.leaf_id,
            ),
            available_skills=[
                skill.model_copy(deep=True) for skill in available_skills or []
            ],
            active_skills=[
                skill.model_copy(deep=True) for skill in active_skills or []
            ],
            provider_epoch_reset=False,
        )
        if append_prompt:
            self.append_message(context, user_message or Message.user(prompt))
        return context

    append_message = staticmethod(append_message)

    append_tool_context_message = staticmethod(append_tool_context_message)

    append_skill_message = staticmethod(append_skill_message)

    append_tool_result = staticmethod(append_tool_result)

    def prepare_compaction(
        self,
        context: AgentContext,
        *,
        reason: CompactionReason,
    ) -> CompactionPreparation | None:
        """Prepare a compaction if needed; return None if skipped."""
        if not self.compaction_policy.enabled:
            return None
        visible_messages = self.messages_for_provider(context)
        estimate = self.estimate_tokens(visible_messages)
        accounting = effective_usage_accounting(
            estimate,
            latest_usage=latest_log_usage(context.conversation_log.entries),
            provider_name=self.compactor.provider_name,
            model_id=self.compactor.model,
        )
        if reason == "threshold" and not self.compactor.should_compact(
            TokenEstimate(
                input_tokens=accounting.input_tokens,
                total_with_reserve=accounting.total_with_reserve,
            ),
            policy=self.compaction_policy,
        ):
            return None
        recent_user_messages = self.compactor.collect_recent_user_messages(
            compaction_user_entries(context),
            token_budget=self.compaction_policy.recent_user_tokens,
        )
        return CompactionPreparation(
            reason=reason,
            estimate=estimate,
            boundary="user",
            messages_to_summarize=visible_messages,
            kept_messages=[
                message.model_copy(deep=True) for message in recent_user_messages
            ],
            recent_user_messages=recent_user_messages,
        )

    def prepare_post_tool_compaction(
        self,
        context: AgentContext,
    ) -> CompactionPreparation | None:
        """Prepare a compaction to run after tool results are appended."""
        if not self.compaction_policy.enabled:
            return None
        rendered_messages = self.messages_for_provider(context)
        estimate = self.estimate_tokens(rendered_messages)
        accounting = effective_usage_accounting(
            estimate,
            latest_usage=latest_log_usage(context.conversation_log.entries),
            provider_name=self.compactor.provider_name,
            model_id=self.compactor.model,
        )
        if not self.compactor.should_compact(
            TokenEstimate(
                input_tokens=accounting.input_tokens,
                total_with_reserve=accounting.total_with_reserve,
            ),
            policy=self.compaction_policy,
        ):
            return None
        return self.prepare_compaction(context, reason="threshold")

    def apply_compaction(
        self,
        context: AgentContext,
        preparation: CompactionPreparation,
        *,
        instruction_message: Message,
        summary_message: Message,
    ) -> CompactionResult:
        """Apply a prepared compaction to the context using the summary text."""
        summary_text = (summary_message.plain_text_content or "").strip()
        generation = next_compaction_generation_from_active_path(context)
        retained_user_messages = len(preparation.kept_messages)
        handoff = CompactionHandoff(
            summary_text=summary_text,
            reason=preparation.reason,
            boundary=preparation.boundary,
            summarized_messages=len(preparation.messages_to_summarize),
            retained_user_messages=retained_user_messages,
            retained_messages=[
                message.model_copy(deep=True) for message in preparation.kept_messages
            ],
            generation=generation,
            input_tokens=preparation.estimate.input_tokens,
            total_tokens=preparation.estimate.total_with_reserve,
        )
        snapshot = MemorySnapshot(
            id="memory-current",
            summary_text=summary_text,
            compaction_handoff=handoff,
            metadata={
                "boundary": handoff.boundary,
                "summarized_messages": handoff.summarized_messages,
                "retained_user_messages": retained_user_messages,
                "generation": handoff.generation,
            },
        )
        append_conversation_entry(
            context,
            ConversationEntry(
                kind="compaction_summary",
                message=instruction_message.model_copy(deep=True),
                metadata={"ok": True, "generation": generation},
            ),
        )
        append_conversation_entry(
            context,
            ConversationEntry(
                kind="memory_snapshot",
                message=summary_message.model_copy(deep=True),
                metadata=snapshot.model_dump(),
            ),
        )
        context.messages = compacted_runtime_messages(
            context,
            kept_messages=preparation.kept_messages,
            summary_message=summary_message,
        )
        context.provider_epoch_reset = True
        return CompactionResult(
            messages=[message.model_copy(deep=True) for message in context.messages],
            summary_text=summary_text,
        )

    def messages_for_provider(self, context: AgentContext) -> list[Message]:
        """Build the message list to send to the provider for this context."""
        prompt_context = build_prompt_context(context)
        messages = [
            *prompt_context.instructions,
            *prompt_context.ordered_messages,
        ]
        normalized = normalize_tool_call_sequence(
            messages,
            drop_incomplete_assistant=True,
        )
        return project_tool_results_for_provider(normalized)

    transcript_messages = staticmethod(transcript_messages)

    newest_real_user_message = staticmethod(newest_real_user_message)

    message_image_count = staticmethod(message_image_count)

    def estimate_tokens(self, messages: Sequence[Message]) -> TokenEstimate:
        """Estimate token usage for the given messages."""
        return self.compactor.estimate_tokens(
            messages,
            reserve_tokens=self.compaction_policy.reserved_output_tokens,
        )

    def account_tokens(self, messages: Sequence[Message]) -> UsageAccounting:
        """Return effective token accounting for provider messages."""
        estimate = self.estimate_tokens(messages)
        return effective_usage_accounting(
            estimate,
            latest_usage=latest_message_usage(messages),
            provider_name=self.compactor.provider_name,
            model_id=self.compactor.model,
        )
