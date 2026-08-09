"""Public immutable values for session trees."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import TYPE_CHECKING

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.models import MessagePhase
from yoke.agent.models import Role
from yoke.agent.models import TokenUsage
from yoke.agent.models import ToolCall

if TYPE_CHECKING:
    from collections.abc import Iterator


class EntryRef:
    """An opaque, session-scoped reference to one tree entry."""

    __slots__ = ("__entry_id", "__scope")

    def __init__(self, scope: str, entry_id: str) -> None:
        self.__scope = scope
        self.__entry_id = entry_id

    @property
    def display(self) -> str:
        """Return a short token suitable for user interfaces and logs."""
        return self.__entry_id[:8]

    def __repr__(self) -> str:
        """Return a safe representation without the persistence identifier."""
        return f"EntryRef({self.display!r})"

    def __hash__(self) -> int:
        """Hash the opaque session-scoped identity."""
        return hash((self.__scope, self.__entry_id))

    def __eq__(self, other: object) -> bool:
        """Compare opaque session-scoped identity."""
        if not isinstance(other, EntryRef):
            return NotImplemented
        return (self.__scope, self.__entry_id) == (
            other.__scope,
            other.__entry_id,
        )

    def _belongs_to(self, scope: str) -> bool:
        return self.__scope == scope

    def _entry_key(self) -> str:
        return self.__entry_id


@dataclass(frozen=True, slots=True)
class MessageView:
    """An immutable message that can produce defensive mutable copies."""

    _payload: str

    @classmethod
    def _from_message(cls, message: Message) -> MessageView:
        return cls(message.model_dump_json())

    def to_message(self) -> Message:
        """Return a new mutable message model."""
        return Message.model_validate_json(self._payload)

    @property
    def role(self) -> Role:
        """Return the message role."""
        return self.to_message().role

    @property
    def content(self) -> object:
        """Return a defensive copy of the message content."""
        return self.to_message().content

    @property
    def tool_call_id(self) -> str | None:
        """Return the associated tool call identifier, if present."""
        return self.to_message().tool_call_id

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        """Return defensive copies of tool calls."""
        return tuple(self.to_message().tool_calls)

    @property
    def phase(self) -> MessagePhase | None:
        """Return the assistant message phase."""
        return self.to_message().phase

    @property
    def reasoning_content(self) -> str | None:
        """Return provider reasoning content, if present."""
        return self.to_message().reasoning_content

    @property
    def usage(self) -> TokenUsage | None:
        """Return a defensive token-usage copy."""
        return self.to_message().usage

    def text_content(self) -> str | None:
        """Return readable text from the message."""
        return self.to_message().text_content()


@dataclass(frozen=True, slots=True)
class TreeExport:
    """A persistence-compatible defensive snapshot."""

    entries: tuple[ConversationEntry, ...]
    leaf_id: str | None

    def __iter__(self) -> Iterator[object]:
        """Support direct unpacking into entries and current leaf."""
        yield self.entries
        yield self.leaf_id

    def to_json(self) -> str:
        """Return a portable JSON representation of the snapshot."""
        return json.dumps(
            {
                "entries": [entry.model_dump(mode="json") for entry in self.entries],
                "leaf_id": self.leaf_id,
            },
            separators=(",", ":"),
        )
