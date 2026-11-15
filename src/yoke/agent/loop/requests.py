"""Provider requests and append-only compaction for one working context."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from yoke.agent.compaction.render import build_compaction_summary_prompt
from yoke.agent.compaction.types import CompactionPreparation, CompactionReason
from yoke.agent.context.helpers import append_conversation_entry
from yoke.agent.loop.events import (
    emit,
    emit_compaction_event,
    emit_compaction_failed,
    model_end_payload,
)
from yoke.agent.loop.types import (
    AgentEventHandler,
    AgentStoppedError,
    CompactionAttempt,
    StopRequested,
)
from yoke.agent.models import AgentContext, ConversationEntry, Message
from yoke.agent.multimodal import messages_for_provider_capabilities
from yoke.ai.providers.base import (
    ProviderCancelledError,
    ProviderError,
    complete_with_cancel,
    provider_event_handler,
)
from yoke.ai.providers.usage_context import (
    current_usage_metric_context,
    usage_metric_context,
)

if TYPE_CHECKING:
    from yoke.agent.loop.agent import RuntimeAgent


class ModelRequests:
    """Own the model and handoff requests for one run or manual compaction."""

    def __init__(
        self,
        runtime: RuntimeAgent,
        context: AgentContext,
        on_event: AgentEventHandler | None = None,
    ) -> None:
        self.runtime = runtime
        self.context = context
        self.on_event = on_event

    def complete(
        self, *, iteration: int, stop_requested: StopRequested | None = None
    ) -> Message:
        """Run one provider completion call."""
        context = self.context
        on_event = self.on_event
        # A command can finish while pre-model compaction uses the provider.
        self.runtime.command_notices.append(context)
        self._guard_images()
        provider_messages = messages_for_provider_capabilities(
            self.runtime.context_manager.messages_for_provider(context),
            self.runtime.provider,
        )
        emit(
            on_event,
            "model_start",
            {"iteration": iteration, "message_count": len(provider_messages)},
        )
        try:
            current_call_kind = current_usage_metric_context().call_kind
            with (
                provider_event_handler(on_event),
                usage_metric_context(call_kind=current_call_kind or "model_iteration"),
            ):
                assistant_message = complete_with_cancel(
                    self.runtime.provider,
                    provider_messages,
                    self.runtime.tool_definitions(),
                    cancel_requested=stop_requested,
                )
        except ProviderCancelledError as exc:
            raise AgentStoppedError() from exc
        except ProviderError as exc:
            if should_retry_after_overflow(exc):
                try:
                    recovered = self._retry(
                        iteration=iteration, stop_requested=stop_requested
                    )
                except ProviderCancelledError as cancelled:
                    raise AgentStoppedError() from cancelled
                if recovered is not None:
                    return recovered
            exc.partial_messages = context.messages
            raise
        context.provider_epoch_reset = False
        emit(
            on_event,
            "model_end",
            model_end_payload(iteration=iteration, message=assistant_message),
        )
        commentary = assistant_message.commentary_text_content()
        if commentary:
            emit(
                on_event,
                "assistant_message",
                {
                    "iteration": iteration,
                    "phase": "commentary",
                    "content": commentary,
                },
            )
        return assistant_message

    def compact(
        self,
        *,
        iteration: int = 0,
        after_tool_results: bool = False,
        reason: CompactionReason = "threshold",
    ) -> CompactionAttempt:
        """Generate a handoff and start a reduced provider-context epoch."""
        context = self.context
        on_event = self.on_event
        preparation = (
            self.runtime.context_manager.prepare_post_tool_compaction(context)
            if after_tool_results
            else self.runtime.context_manager.prepare_compaction(context, reason=reason)
        )
        if preparation is None:
            return CompactionAttempt()
        summary = self._summarize(preparation)
        if summary is None:
            emit_compaction_failed(
                on_event,
                preparation,
                iteration=iteration,
            )
            return CompactionAttempt(failed=True, preparation=preparation)
        result = self.runtime.context_manager.apply_compaction(
            context,
            preparation,
            instruction_message=summary[0],
            summary_message=summary[1],
        )
        compacted_estimate = self.runtime.context_manager.estimate_tokens(
            result.messages
        )
        emit_compaction_event(
            on_event,
            preparation,
            result,
            compacted_input_tokens=compacted_estimate.input_tokens,
            iteration=iteration,
        )
        return CompactionAttempt(result=result, preparation=preparation)

    def _summarize(
        self, preparation: CompactionPreparation
    ) -> tuple[Message, Message] | None:
        """Generate a handoff by extending the current provider epoch once."""
        context = self.context
        on_event = self.on_event
        estimated_input_tokens = preparation.estimate.input_tokens
        emit(
            on_event,
            "compaction_summary_start",
            {"estimated_input_tokens": estimated_input_tokens},
        )
        target_tokens = (
            self.runtime.context_manager.compaction_policy.handoff_target_tokens
        )
        instruction = Message.user(build_compaction_summary_prompt(target_tokens))
        messages = [*preparation.messages_to_summarize, instruction]
        provider_messages = messages_for_provider_capabilities(
            messages,
            self.runtime.provider,
        )
        start_time = time.perf_counter()
        try:
            with usage_metric_context(call_kind="compaction_summary"):
                response = complete_with_cancel(
                    self.runtime.provider,
                    provider_messages,
                    self.runtime.tool_definitions(),
                )
            summary = (response.plain_text_content or "").strip()
            if not summary:
                raise ValueError("Compaction provider returned an empty summary.")
        except Exception as exc:
            metadata: dict[str, object] = {
                "ok": False,
                "estimated_input_tokens": estimated_input_tokens,
                "duration_seconds": round(time.perf_counter() - start_time, 2),
                "error": f"{type(exc).__name__}: {exc}",
            }
            emit(on_event, "compaction_summary_end", metadata)
            append_conversation_entry(
                context,
                ConversationEntry(
                    kind="compaction_summary", message=instruction, metadata=metadata
                ),
            )
            return None
        metadata: dict[str, object] = {
            "ok": True,
            "estimated_input_tokens": estimated_input_tokens,
            "duration_seconds": round(time.perf_counter() - start_time, 2),
            "source_chunks": 1,
            "summary_calls": 1,
            "response_chars": len(summary),
            "handoff_target_tokens": target_tokens,
        }
        emit(on_event, "compaction_summary_end", metadata)
        return instruction, response

    def _retry(
        self, *, iteration: int, stop_requested: StopRequested | None
    ) -> Message | None:
        """Start one reduced epoch and retry the rejected request once."""
        context = self.context
        on_event = self.on_event
        compaction = self.compact(iteration=iteration, reason="overflow_retry")
        if compaction.failed or compaction.result is None:
            return None
        self._guard_images()
        self._guard_tokens()
        provider_messages = messages_for_provider_capabilities(
            self.runtime.context_manager.messages_for_provider(context),
            self.runtime.provider,
        )
        try:
            with usage_metric_context(call_kind="overflow_retry"):
                assistant_message = complete_with_cancel(
                    self.runtime.provider,
                    provider_messages,
                    self.runtime.tool_definitions(),
                    cancel_requested=stop_requested,
                )
        except ProviderCancelledError:
            # A stop during the retry is a stop, not a failed recovery.
            raise
        except ProviderError:
            return None
        context.provider_epoch_reset = False
        emit(
            on_event,
            "model_end",
            model_end_payload(iteration=iteration, message=assistant_message),
        )
        commentary = assistant_message.commentary_text_content()
        if commentary:
            emit(
                on_event,
                "assistant_message",
                {
                    "iteration": iteration,
                    "phase": "commentary",
                    "content": commentary,
                },
            )
        return assistant_message

    def _guard_images(self) -> None:
        """Reject a newest user message that exceeds provider image limits."""
        context = self.context
        newest_message = self.runtime.context_manager.newest_real_user_message(context)
        if newest_message is None:
            return
        max_images = getattr(self.runtime.provider, "max_images_per_message", None)
        image_count = self.runtime.context_manager.message_image_count(newest_message)
        if max_images is not None and image_count > max_images:
            raise ProviderError(
                "Newest user message exceeds provider image limit: "
                f"{image_count} images > {max_images}."
            )

    def _guard_tokens(self) -> None:
        """Reject a newest user message that still cannot fit after compaction."""
        context = self.context
        newest_message = self.runtime.context_manager.newest_real_user_message(context)
        if newest_message is None:
            return
        instruction_count = len(context.instructions)
        provider_messages = self.runtime.context_manager.messages_for_provider(context)
        newest_provider_messages = [
            *provider_messages[:instruction_count],
            newest_message.model_copy(deep=True),
        ]
        estimate = self.runtime.context_manager.estimate_tokens(
            newest_provider_messages
        )
        max_total_tokens = (
            self.runtime.context_manager.compaction_policy.max_total_tokens
        )
        reserved_output_tokens = (
            self.runtime.context_manager.compaction_policy.reserved_output_tokens
        )
        available_input_tokens = max(
            0,
            max_total_tokens - reserved_output_tokens,
        )
        if estimate.input_tokens > available_input_tokens:
            raise ProviderError(
                "Newest user message exceeds remaining provider context budget: "
                f"{estimate.input_tokens} input tokens > {available_input_tokens}."
            )

    def emit_usage(self, *, iteration: int, reason: str) -> None:
        """Emit estimated provider context usage."""
        context = self.context
        on_event = self.on_event
        if on_event is None:
            return
        provider_messages = self.runtime.context_manager.messages_for_provider(context)
        accounting = self.runtime.context_manager.account_tokens(provider_messages)
        payload: dict[str, object] = {
            "iteration": iteration,
            "reason": reason,
            "message_count": len(provider_messages),
            "input_tokens": accounting.input_tokens,
            "total_with_reserve": accounting.total_with_reserve,
            "estimated_input_tokens": accounting.estimated_input_tokens,
            "estimated_total_with_reserve": (accounting.estimated_total_with_reserve),
            "accounting_source": accounting.source,
        }
        if accounting.provider_reported_input_tokens is not None:
            payload["provider_reported_input_tokens"] = (
                accounting.provider_reported_input_tokens
            )
        if accounting.output_tokens is not None:
            payload["output_tokens"] = accounting.output_tokens
        if accounting.reasoning_tokens is not None:
            payload["reasoning_tokens"] = accounting.reasoning_tokens
        if accounting.total_tokens is not None:
            payload["provider_reported_total_tokens"] = accounting.total_tokens
        if accounting.cached_input_tokens is not None:
            payload["cached_input_tokens"] = accounting.cached_input_tokens
        if accounting.cache_creation_input_tokens is not None:
            payload["cache_creation_input_tokens"] = (
                accounting.cache_creation_input_tokens
            )
        max_total_tokens = self.runtime.context_manager.max_total_tokens
        if isinstance(max_total_tokens, int) and max_total_tokens > 0:
            payload["max_total_tokens"] = max_total_tokens
            payload["usage_percent"] = min(
                100,
                max(0, round((accounting.input_tokens / max_total_tokens) * 100)),
            )
        emit(on_event, "context_usage", payload)


def should_retry_after_overflow(error: ProviderError) -> bool:
    """Return whether a provider error looks like request overflow."""
    if error.status_code == 413:
        return True
    message = str(error).lower()
    overflow_phrase_found = any(
        phrase in message
        for phrase in (
            "too many image",
            "more than 50img",
            "more than 50 img",
            "context_length_exceeded",
            "exceeds the context window",
            "context too long",
            "maximum context length",
            "prompt token count",
            "exceeds the limit",
            "token limit",
            "request too large",
            "too large",
        )
    )
    if error.status_code == 400:
        return overflow_phrase_found
    if error.status_code is None:
        return overflow_phrase_found
    return False
