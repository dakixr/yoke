"""Private SDK structured-output helpers."""

from __future__ import annotations

import json

from pydantic import TypeAdapter

from yoke.agent.models import Message
from yoke.ai.sdk.messages import append_text_to_user_message
from yoke.ai.sdk.types import StructuredOutputError


def parse_structured_output[StructuredT](
    output: str,
    *,
    output_type: type[StructuredT] | None,
) -> StructuredT | None:
    """Parse final text into a structured output value."""
    if output_type is None:
        return None
    try:
        return TypeAdapter(output_type).validate_json(output)
    except Exception as exc:
        raise StructuredOutputError(
            f"Failed to parse structured output as {output_type.__name__}.",
            output=output,
        ) from exc


def structured_output_retry_message(
    output_type: type[object],
    error: StructuredOutputError,
) -> Message:
    """Build a system correction message for invalid structured outputs."""
    return Message.system(
        "Your previous response did not match the required structured output "
        "schema. Retry now and adhere exactly to the schema. Return only one "
        "valid JSON object with no markdown fences, prose, comments, or extra "
        "keys.\n\n"
        f"Validation error: {error}\n\n"
        f"Previous response:\n{error.output}\n\n"
        f"{structured_output_instructions(output_type)}"
    )


def structured_output_instructions(output_type: type[object]) -> str:
    """Build model-facing instructions for structured SDK outputs."""
    schema = TypeAdapter(output_type).json_schema()
    schema_json = json.dumps(schema, ensure_ascii=False, indent=2)
    return (
        "Return exactly one valid JSON object matching this JSON Schema. "
        "Do not include markdown fences, prose, comments, or extra keys. "
        "Use the exact field names and required fields from the schema.\n\n"
        f"JSON Schema:\n{schema_json}"
    )


def append_structured_output_instructions(
    message: Message,
    *,
    output_type: type[object],
) -> Message:
    """Return a user message with structured-output instructions appended."""
    return append_text_to_user_message(
        message,
        structured_output_instructions(output_type),
    )
