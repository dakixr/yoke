"""Context manager implementation for yoke agents."""

from __future__ import annotations

import json
from collections.abc import Callable
from collections.abc import Sequence

from yoke.agent.compaction import CompactionPolicy
from yoke.agent.compaction import CompactionPreparation
from yoke.agent.compaction import CompactionReason
from yoke.agent.compaction import CompactionResult
from yoke.agent.compaction import Compactor
from yoke.agent.compaction import TokenEstimate
from yoke.agent.context.helpers import append_conversation_entry
from yoke.agent.context.helpers import entry_kind_for_message
from yoke.agent.context.helpers import initialize_context_state
from yoke.agent.context.helpers import initialize_owned_context_state
from yoke.agent.context.helpers import normalize_instructions
from yoke.agent.context.helpers import update_message_projection
from yoke.agent.context.compaction_projection import compacted_runtime_messages
from yoke.agent.context.compaction_projection import (
    next_compaction_generation_from_active_path,
)
from yoke.agent.context.accounting import latest_log_usage
from yoke.agent.context.accounting import latest_message_usage
from yoke.agent.context.accounting import message_entry_metadata
from yoke.agent.message_sanitizer import normalize_tool_call_sequence
from yoke.agent.models import AgentContext
from yoke.agent.models import CompactionHandoff
from yoke.agent.models import ConversationEntry
from yoke.agent.models import MemorySnapshot
from yoke.agent.models import Message
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.prompting import PromptBuilder
from yoke.agent.skills.context import skill_message_conversation_entry
from yoke.agent.skills.context import skill_name_from_message
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec
from yoke.agent.usage import UsageAccounting
from yoke.agent.usage import effective_usage_accounting

MessageTransform = Callable[[list[Message]], list[Message]]


class ContextManager:
    """Manages agent conversation context, instructions, and compaction."""

    def __init__(
        self,
        instructions: Sequence[Message] | None = None,
        transform_messages: MessageTransform | None = None,
        convert_messages: MessageTransform | None = None,
        compaction_policy: CompactionPolicy | None = None,
        prompt_builder: PromptBuilder | None = None,
        compactor: Compactor | None = None,
    ) -> None:
        self.instructions = normalize_instructions(instructions)
        self.system_prompt = (
            self.instructions[0].plain_text_content if self.instructions else None
        )
        self.transform_messages = transform_messages
        self.convert_messages = convert_messages
        self.prompt_builder = prompt_builder or PromptBuilder()
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
        """Initialize a new AgentContext from a prompt and optional history."""
        return initialize_context_state(
            prompt=prompt,
            messages=messages,
            instructions=self.instructions,
            system_prompt=self.system_prompt,
            user_message=user_message,
            append_prompt=append_prompt,
            conversation_entries=conversation_entries,
            available_skills=available_skills,
            active_skills=active_skills,
            append_message=self.append_message,
            transcript_messages=self.transcript_messages,
        )

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
        """Take an owned, validated active path into a new context."""
        return initialize_owned_context_state(
            prompt=prompt,
            entries=conversation_entries,
            instructions=self.instructions,
            system_prompt=self.system_prompt,
            user_message=user_message,
            append_prompt=append_prompt,
            available_skills=available_skills,
            active_skills=active_skills,
            append_message=self.append_message,
        )

    def append_message(self, context: AgentContext, message: Message) -> None:
        """Append a message to the context's conversation log."""
        copied = message.model_copy(deep=True)
        branching = append_conversation_entry(
            context,
            ConversationEntry(
                kind=entry_kind_for_message(copied),
                message=copied,
                metadata=message_entry_metadata(copied),
            ),
        )
        update_message_projection(context, copied, branching=branching)

    def append_skill_message(self, context: AgentContext, message: Message) -> None:
        """Append activated skill instructions to the context log."""
        copied = message.model_copy(deep=True)
        skill_name = skill_name_from_message(copied)
        existing_activation_ids = {
            activation_id
            for entry in context.conversation_log.entries
            if entry.kind == "skill_event"
            and isinstance(
                activation_id := entry.metadata.get("skill_activation_id"),
                str,
            )
        }
        activation_id = next(
            (
                skill.activation_id
                for skill in reversed(context.active_skills)
                if skill.name == skill_name
                and skill.activation_id
                and skill.activation_id not in existing_activation_ids
            ),
            None,
        )
        branching = append_conversation_entry(
            context,
            skill_message_conversation_entry(
                copied,
                parent_id=None,
                skill_name=skill_name,
                skill_activation_id=activation_id,
            ),
        )
        update_message_projection(context, copied, branching=branching)

    def append_tool_result(
        self,
        context: AgentContext,
        *,
        tool_call_id: str,
        result: dict[str, object],
    ) -> Message:
        """Append a tool result message to the context and return it."""
        message = Message.tool(
            tool_call_id=tool_call_id,
            content=json.dumps(result, ensure_ascii=False),
        )
        self.append_message(context, message)
        return message

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
            context.messages,
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
        prompt_context = self.prompt_builder.build(context)
        messages = [
            *prompt_context.instructions,
            *prompt_context.ordered_messages,
        ]
        if self.transform_messages is not None or self.convert_messages is not None:
            messages = [message.model_copy(deep=True) for message in messages]
        if self.transform_messages is not None:
            messages = self.transform_messages(messages)
        if self.convert_messages is not None:
            messages = self.convert_messages(messages)
        normalized = normalize_tool_call_sequence(
            messages,
            drop_incomplete_assistant=True,
        )
        return normalized

    def transcript_messages(self, context: AgentContext) -> list[Message]:
        """Return the compact runtime transcript from canonical history."""
        messages = [message.model_copy(deep=True) for message in context.instructions]
        from yoke.agent.conversation import project_conversation

        projection = project_conversation(
            context.conversation_log.entries,
            leaf_id=context.conversation_log.leaf_id,
        )
        messages.extend(
            message.model_copy(deep=True) for message in projection.runtime_messages
        )
        return messages

    def newest_real_user_message(self, context: AgentContext) -> Message | None:
        """Return the newest user-authored message from the transcript."""
        for entry in reversed(context.conversation_log.entries):
            if entry.kind == "user" and entry.message is not None:
                return entry.message.model_copy(deep=True)
        return None

    def message_image_count(self, message: Message) -> int:
        """Return the count of image inputs in one message."""
        if not isinstance(message.content, list):
            return 0
        return sum(
            1
            for part in message.content
            if isinstance(
                part,
                MessageImageURLContentPart | MessageLocalImageContentPart,
            )
        )

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


def _drop_incomplete_tool_turns(messages: Sequence[Message]) -> list[Message]:
    return normalize_tool_call_sequence(messages, drop_incomplete_assistant=True)
