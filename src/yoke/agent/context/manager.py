"""Context manager implementation for yoke agents."""

from __future__ import annotations

import json
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Sequence

from yoke.agent.compaction import CompactionPolicy
from yoke.agent.compaction import CompactionPreparation
from yoke.agent.compaction import CompactionReason
from yoke.agent.compaction import CompactionResult
from yoke.agent.compaction import Compactor
from yoke.agent.compaction import TokenEstimate
from yoke.agent.context.helpers import entry_kind_for_message
from yoke.agent.context.helpers import initialize_context_state
from yoke.agent.context.helpers import next_compaction_generation
from yoke.agent.context.helpers import normalize_instructions
from yoke.agent.context.helpers import recent_log_messages
from yoke.agent.message_sanitizer import normalize_tool_call_sequence
from yoke.agent.models import AgentContext
from yoke.agent.models import CompactionHandoff
from yoke.agent.models import ConversationEntry
from yoke.agent.models import MemorySnapshot
from yoke.agent.models import Message
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import TokenUsage
from yoke.agent.prompting import PromptBuilder
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec
from yoke.agent.usage import compact_usage_payload
from yoke.agent.usage import effective_usage_accounting
from yoke.agent.usage import UsageAccounting

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

    def append_message(self, context: AgentContext, message: Message) -> None:
        """Append a message to the context's conversation log."""
        copied = message.model_copy(deep=True)
        parent_id = (
            context.conversation_log.entries[-1].id
            if context.conversation_log.entries
            else None
        )
        context.conversation_log.entries.append(
            ConversationEntry(
                kind=entry_kind_for_message(copied),
                message=copied,
                parent_id=parent_id,
                metadata=_message_entry_metadata(copied),
            )
        )
        context.messages = self.transcript_messages(context)

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
            latest_usage=_latest_log_usage(context.conversation_log.entries),
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
            recent_log_messages(context),
            token_budget=min(
                self.compaction_policy.recent_user_tokens,
                self.compaction_policy.keep_recent_tokens,
            ),
        )
        if not recent_user_messages:
            return None
        return CompactionPreparation(
            reason=reason,
            estimate=estimate,
            boundary="user",
            messages_to_summarize=[
                message.model_copy(deep=True) for message in visible_messages
            ],
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
            latest_usage=_latest_log_usage(context.conversation_log.entries),
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
        summary_text: str,
    ) -> CompactionResult:
        """Apply a prepared compaction to the context using the summary text."""
        result = self.compactor.compact_messages(
            preparation,
            instruction_messages=[
                message.model_copy(deep=True) for message in context.instructions
            ],
            summary_text=summary_text,
        )
        generation = next_compaction_generation(context)
        retained_messages = [
            message.model_copy(deep=True)
            for message in (
                preparation.recent_user_messages or preparation.kept_messages
            )
        ]
        handoff = CompactionHandoff(
            summary_text=result.summary_text,
            reason=preparation.reason,
            boundary=preparation.boundary,
            summarized_messages=len(preparation.messages_to_summarize),
            retained_user_messages=len(retained_messages),
            retained_messages=retained_messages,
            generation=generation,
            input_tokens=preparation.estimate.input_tokens,
            total_tokens=preparation.estimate.total_with_reserve,
        )
        snapshot = MemorySnapshot(
            id="memory-current",
            summary_text=result.summary_text,
            compaction_handoff=handoff,
            metadata={
                "boundary": handoff.boundary,
                "summarized_messages": handoff.summarized_messages,
                "retained_user_messages": handoff.retained_user_messages,
                "retained_messages": [
                    message.model_dump() for message in retained_messages
                ],
                "generation": handoff.generation,
            },
        )
        context.memory.current_snapshot = snapshot
        parent_id = (
            context.conversation_log.entries[-1].id
            if context.conversation_log.entries
            else None
        )
        context.conversation_log.entries.append(
            ConversationEntry(
                kind="memory_snapshot",
                parent_id=parent_id,
                metadata=snapshot.model_dump(),
            )
        )
        context.messages = self.transcript_messages(context)
        return result

    def messages_for_provider(self, context: AgentContext) -> list[Message]:
        """Build the message list to send to the provider for this context."""
        prompt_context = self.prompt_builder.build(context)
        messages = [
            *[message.model_copy(deep=True) for message in prompt_context.instructions],
            *[
                message.model_copy(deep=True)
                for message in prompt_context.ordered_messages
            ],
        ]
        if self.transform_messages is not None:
            messages = self.transform_messages(messages)
        if self.convert_messages is not None:
            messages = self.convert_messages(messages)
        normalized = normalize_tool_call_sequence(
            messages,
            drop_incomplete_assistant=True,
        )
        return [message.model_copy(deep=True) for message in normalized]

    def transcript_messages(self, context: AgentContext) -> list[Message]:
        """Return all non-snapshot messages from the conversation log."""
        messages: list[Message] = []
        for entry in context.conversation_log.entries:
            if entry.kind != "memory_snapshot" and entry.message is not None:
                messages.append(entry.message.model_copy(deep=True))
        return messages

    def newest_real_user_message(self, context: AgentContext) -> Message | None:
        """Return the newest user-authored message from the transcript."""
        for message in reversed(recent_log_messages(context)):
            if message.role == "user":
                return message.model_copy(deep=True)
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
            latest_usage=_latest_message_usage(messages),
        )


def _latest_message_usage(messages: Sequence[Message]) -> TokenUsage | None:
    for message in reversed(messages):
        if message.usage is not None and message.usage.input_tokens is not None:
            return message.usage
    return None


def _latest_log_usage(
    entries: Sequence[ConversationEntry],
) -> TokenUsage | None:
    for entry in reversed(entries):
        if entry.kind == "memory_snapshot":
            return None
        if entry.message is None:
            continue
        if (
            entry.message.usage is not None
            and entry.message.usage.input_tokens is not None
        ):
            return entry.message.usage
    return None


def _drop_incomplete_tool_turns(messages: Iterable[Message]) -> list[Message]:
    """Remove assistant tool-call wrappers that are not fully resolved."""
    return normalize_tool_call_sequence(
        messages,
        drop_incomplete_assistant=True,
    )


def _message_entry_metadata(message: Message) -> dict[str, object]:
    usage = compact_usage_payload(message.usage)
    if usage is None:
        return {}
    return {"usage": usage}
