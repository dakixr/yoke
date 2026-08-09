"""Message-history normalization and recovery for Z.AI."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from yoke.agent.models import Message
from yoke.ai.providers.openai_compat.content import normalize_openai_request_messages
from yoke.ai.providers.zai.models import ZAIConfig


def message_text(message: Message) -> str:
    content = message.content
    if isinstance(content, str):
        return content.strip()
    return message.text_content() or ""


class ZAIMessageRecoveryMixin:
    config: ZAIConfig

    def _prepare_messages(self, messages: list[Message]) -> list[Message]:
        prepared = normalize_openai_request_messages(messages)
        prepared = self._merge_leading_system_messages(prepared)
        for message in prepared:
            if message.role == "assistant" and message.content is None:
                message.content = ""
        prepared = self._drop_empty_assistant_messages(prepared)
        return prepared

    def _message_to_api_dict(self, message: Message) -> dict[str, object]:
        payload = message.to_api_dict()
        # Z.AI preserved-thinking mode requires complete, unmodified prior
        # reasoning_content. Yoke cannot guarantee that across compaction and
        # transcript transforms, so do not replay it by default.
        payload.pop("reasoning_content", None)
        return payload

    def _merge_leading_system_messages(self, messages: list[Message]) -> list[Message]:
        leading_system_messages: list[Message] = []
        for message in messages:
            if message.role != "system":
                break
            leading_system_messages.append(message)
        if len(leading_system_messages) <= 1:
            return messages
        merged_content = "\n\n".join(
            content
            for message in leading_system_messages
            if (content := message_text(message))
        )
        return [
            Message.system(merged_content),
            *messages[len(leading_system_messages) :],
        ]

    def _drop_empty_assistant_messages(self, messages: list[Message]) -> list[Message]:
        return [
            message
            for message in messages
            if not (
                message.role == "assistant"
                and not message.tool_calls
                and not message_text(message)
            )
        ]

    def _looks_like_illegal_messages_error(self, detail: str) -> bool:
        normalized = detail.lower()
        return (
            "messages parameter is illegal" in normalized
            or "messages parameter" in normalized
        )

    def _recover_illegal_messages(self, messages: list[Message]) -> list[Message]:
        recovered: list[Message] = []
        system_messages: list[Message] = []
        index = 0
        while index < len(messages) and messages[index].role == "system":
            system_messages.append(messages[index].model_copy(deep=True))
            index += 1
        if system_messages:
            recovered.extend(self._merge_leading_system_messages(system_messages))
        textual_messages = self._render_tool_messages_as_text(messages[index:])
        recovered.extend(
            self._ensure_recoverable_dialogue_shape(
                self._coalesce_text_messages(textual_messages)
            )
        )
        return recovered

    def _render_tool_messages_as_text(self, messages: list[Message]) -> list[Message]:
        rendered: list[Message] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            if message.role == "assistant" and message.tool_calls:
                tool_ids = [tool_call.id for tool_call in message.tool_calls]
                tool_results: list[Message] = []
                lookahead = index + 1
                while lookahead < len(messages):
                    candidate = messages[lookahead]
                    if candidate.role == "tool" and candidate.tool_call_id in tool_ids:
                        tool_results.append(candidate)
                        lookahead += 1
                        continue
                    break
                content = self._render_tool_exchange(message, tool_results)
                if content:
                    rendered.append(Message.assistant(content))
                index = lookahead
                continue
            if message.role in {"user", "assistant"} and message_text(message):
                rendered.append(
                    Message(
                        role=message.role,
                        content=message_text(message),
                    )
                )
            index += 1
        return rendered

    def _render_tool_exchange(
        self, assistant_message: Message, tool_results: list[Message]
    ) -> str:
        parts: list[str] = []
        if assistant_content := message_text(assistant_message):
            parts.append(assistant_content)
        calls = [
            f"{tool_call.function.name}({tool_call.function.arguments})"
            for tool_call in assistant_message.tool_calls
        ]
        if calls:
            parts.append(f"[Assistant tool calls] {'; '.join(calls)}")
        for tool_message in tool_results:
            if tool_content := message_text(tool_message):
                parts.append(
                    f"[Tool result] {self._truncate_text(tool_content, limit=1_200)}"
                )
        return "\n".join(parts).strip()

    def _coalesce_text_messages(self, messages: list[Message]) -> list[Message]:
        coalesced: list[Message] = []
        for message in messages:
            if (
                coalesced
                and coalesced[-1].role == message.role
                and message.role != "system"
            ):
                merged_content = "\n\n".join(
                    part
                    for part in [
                        message_text(coalesced[-1]),
                        message_text(message),
                    ]
                    if part
                )
                coalesced[-1] = Message(role=message.role, content=merged_content)
                continue
            coalesced.append(message)
        return coalesced

    def _ensure_recoverable_dialogue_shape(
        self, messages: list[Message]
    ) -> list[Message]:
        if not messages:
            return [Message.user(self._recovery_prompt())]
        if messages[-1].role != "user":
            return [*messages, Message.user(self._recovery_prompt())]
        return messages

    def _recovery_prompt(self) -> str:
        return "Continue from the prior context and answer the latest request using the tool results already gathered."

    def _truncate_text(self, text: str, *, limit: int) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3].rstrip() + "..."

    def _log_debug_event(self, event: str, **payload: object) -> None:
        if not self.config.debug_log_path:
            return
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "model": self.config.model,
            **payload,
        }
        try:
            path = Path(self.config.debug_log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            return
