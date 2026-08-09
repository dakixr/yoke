"""Codex subscription provider implementation."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations

from typing import Any


from yoke.agent.models import Message
from yoke.ai.providers.openai_compat import serialize_message_for_openai


def convert_messages(
    messages: list[Message],
) -> tuple[str, list[dict[str, Any]]]:
    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in codex_request_messages(messages):
        if message.role == "system":
            text = message.text_content()
            if text:
                instructions.append(text)
            continue
        if message.role == "tool":
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id or "",
                    "output": message.text_content() or "",
                }
            )
            continue
        if message.role == "assistant" and message.tool_calls:
            text = message.text_content()
            if text:
                input_items.append(message_item(message.role, text))
            for tool_call in message.tool_calls:
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": tool_call.id,
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    }
                )
            continue
        input_items.append(convert_text_message(message))
    return "\n\n".join(instructions), input_items


def codex_request_messages(messages: list[Message]) -> list[Message]:
    repaired: list[Message] = []
    pending_index: int | None = None
    pending_ids: list[str] = []
    buffered_follow_ups: list[Message] = []
    for message in messages:
        copied = message.model_copy(deep=True)
        if copied.role == "tool" and copied.tool_calls:
            copied.tool_calls = []
        if copied.role == "assistant" and copied.tool_calls:
            if pending_index is not None:
                del repaired[pending_index:]
                repaired.extend(_codex_safe_follow_ups(buffered_follow_ups))
            pending_index = len(repaired)
            pending_ids = [tool_call.id for tool_call in copied.tool_calls]
            buffered_follow_ups = []
            repaired.append(copied)
            continue
        if pending_index is not None:
            if (
                copied.role == "tool"
                and pending_ids
                and copied.tool_call_id == pending_ids[0]
            ):
                repaired.append(copied)
                pending_ids.pop(0)
                if not pending_ids:
                    pending_index = None
                    buffered_follow_ups = []
                continue
            buffered_follow_ups.append(copied)
            continue
        if copied.role == "tool":
            continue
        repaired.append(copied)
    if pending_index is not None:
        del repaired[pending_index:]
        repaired.extend(_codex_safe_follow_ups(buffered_follow_ups))
    return repaired


def _codex_safe_follow_ups(messages: list[Message]) -> list[Message]:
    return [
        message.model_copy(deep=True) for message in messages if message.role != "tool"
    ]


def convert_text_message(message: Message) -> dict[str, Any]:
    serialized = serialize_message_for_openai(message)
    content = serialized.get("content", "")
    if isinstance(content, list):
        converted_parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                converted_parts.append(
                    {"type": "input_text", "text": part.get("text", "")}
                )
            elif part.get("type") == "image_url":
                image_url = part.get("image_url")
                url = image_url.get("url", "") if isinstance(image_url, dict) else ""
                converted_parts.append(
                    {
                        "type": "input_image",
                        "image_url": url,
                    }
                )
        return {"role": message.role, "content": converted_parts}
    return message_item(message.role, str(content or ""))


def message_item(role: str, text: str) -> dict[str, Any]:
    content_type = "output_text" if role == "assistant" else "input_text"
    return {"role": role, "content": [{"type": content_type, "text": text}]}


def convert_tools(tools: list[dict[str, object]]) -> list[dict[str, object]]:
    converted: list[dict[str, object]] = []
    for tool in tools:
        if tool.get("type") == "function":
            function = tool.get("function")
            if isinstance(function, dict):
                converted.append(
                    {
                        "type": "function",
                        "name": function.get("name"),
                        "description": function.get("description", ""),
                        "parameters": function.get("parameters", {}),
                        "strict": None,
                    }
                )
                continue
        converted.append(tool)
    return converted


def count_message_images(messages: list[Message]) -> int:
    count = 0
    for message in messages:
        content = message.content
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                count += 1
    return count
