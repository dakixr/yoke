"""Stored interruption markers and their provider instruction projection."""

from collections.abc import Sequence

from yoke.agent.models import Message

INTERRUPTED_TURN_NOTICE = (
    "The previous turn was interrupted by the user before completion. Continue "
    "from the current state and follow the user's next instruction."
)
INTERRUPTED_RESPONSE_NOTE = "The previous response was interrupted by the user."


def is_interruption_notice(message: Message | None) -> bool:
    """Recognize current markers and exact legacy assistant notices."""
    return (
        message is not None
        and not message.tool_calls
        and (
            message.role == "assistant"
            and message.plain_text_content == INTERRUPTED_TURN_NOTICE
            or message.role == "system"
            and message.plain_text_content == INTERRUPTED_RESPONSE_NOTE
        )
    )


def project_interruptions(messages: Sequence[Message]) -> list[Message]:
    """Place one instruction before the next user in each interrupted run.

    User messages and internal events do not end a run. Only assistant output
    makes a later interruption a separate run. A trailing marker also supports
    resuming a stopped turn without appending another user message.
    """
    projected: list[Message] = []
    pending = False
    noted = False
    for message in messages:
        if is_interruption_notice(message):
            pending = True
            continue
        if pending and message.role in {"user", "assistant"}:
            if not noted:
                projected.append(Message.system(INTERRUPTED_RESPONSE_NOTE))
                noted = True
            pending = False
        projected.append(message)
        if message.role == "assistant" and (message.content or message.tool_calls):
            noted = False
    if pending and not noted:
        projected.append(Message.system(INTERRUPTED_RESPONSE_NOTE))
    return projected
