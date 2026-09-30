"""Retained Responses state for the Codex WebSocket provider."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import cast
from typing import Literal

from yoke.agent.models import Message

type ContinuityMode = Literal[
    "visible_input",
    "previous_response_id",
    "encrypted_replay",
]


@dataclass(slots=True)
class CodexResponseChain:
    """Track one Codex response anchor and its stateless replay fallback."""

    response_id: str | None = None
    response_account_id: str | None = None
    response_auth_profile: str | None = None
    retained_request_payload: dict[str, object] | None = None
    retained_input_items: list[dict[str, Any]] = field(default_factory=list)
    retained_visible_items: list[dict[str, Any]] = field(default_factory=list)
    pending_response_id: str | None = None
    pending_output_items: list[dict[str, Any]] = field(default_factory=list)
    prepared_input_items: list[dict[str, Any]] = field(default_factory=list)
    prepared_mode: ContinuityMode = "visible_input"
    # Why the request could not continue the previous response, for logs only.
    # Holds key names, positions, and item types, never content.
    prepared_reason: str | None = None

    def prepare(
        self,
        payload: dict[str, object],
        *,
        account_id: str | None,
        auth_profile: str | None,
        selected_auth_profile: str | None,
    ) -> dict[str, object]:
        """Choose anchored continuation, encrypted replay, or visible input."""
        prepared = deepcopy(payload)
        current_input = _input_items(payload)
        self.prepared_input_items = deepcopy(current_input or [])
        self.prepared_mode = "visible_input"
        if current_input is None:
            self.prepared_reason = "input_not_item_list"
            return prepared
        self.prepared_reason = self._continuity_blocker(
            payload,
            account_id=account_id,
            auth_profile=auth_profile,
            selected_auth_profile=selected_auth_profile,
        )
        if self.prepared_reason is not None:
            return prepared

        incremental_items = strip_list_prefix(
            current_input,
            self.retained_visible_items,
        )
        if incremental_items is None:
            self.prepared_reason = history_divergence(
                current_input, self.retained_visible_items
            )
            return prepared

        if self.response_id:
            prepared["previous_response_id"] = self.response_id
            prepared["input"] = incremental_items
            self.prepared_input_items = deepcopy(incremental_items)
            self.prepared_mode = "previous_response_id"
            return prepared

        self.prepared_reason = "no_response_anchor"
        replay_items = [*self.retained_input_items, *incremental_items]
        prepared.pop("previous_response_id", None)
        prepared["input"] = replay_items
        self.prepared_input_items = deepcopy(replay_items)
        self.prepared_mode = "encrypted_replay"
        return prepared

    def stage_response(
        self,
        *,
        response_id: str | None,
        output_items: list[dict[str, Any]],
    ) -> None:
        """Stage parsed response state until the provider accepts the response."""
        self.pending_response_id = response_id
        self.pending_output_items = deepcopy(output_items)

    def remember(
        self,
        payload: dict[str, object],
        message: Message,
        *,
        account_id: str | None,
        auth_profile: str | None,
    ) -> None:
        """Commit a successful response to anchored and replayable state."""
        raw_output_items = self.pending_output_items or output_items_from_message(
            message
        )
        visible_items = visible_items_from_output(raw_output_items)
        current_input = _input_items(payload) or []
        if self.prepared_mode == "previous_response_id":
            retained_input = [
                *self.retained_input_items,
                *self.prepared_input_items,
                *raw_output_items,
            ]
        else:
            retained_input = [*self.prepared_input_items, *raw_output_items]

        self.response_id = self.pending_response_id
        self.response_account_id = account_id
        self.response_auth_profile = auth_profile
        self.retained_request_payload = deepcopy(payload)
        self.retained_input_items = deepcopy(retained_input)
        self.retained_visible_items = deepcopy([*current_input, *visible_items])
        self.clear_pending()

    def drop_anchor(self) -> None:
        """Forget connection-local response linkage but keep encrypted replay state."""
        self.response_id = None
        self.clear_pending()

    def reset(self) -> None:
        """Clear response linkage and all retained replay state."""
        self.response_id = None
        self.response_account_id = None
        self.response_auth_profile = None
        self.retained_request_payload = None
        self.retained_input_items = []
        self.retained_visible_items = []
        self.clear_pending()

    def clear_pending(self) -> None:
        """Clear response and request state for an unfinished attempt."""
        self.clear_staged_response()
        self.prepared_input_items = []
        self.prepared_mode = "visible_input"
        self.prepared_reason = None

    def clear_staged_response(self) -> None:
        """Clear only response output staged for the current request."""
        self.pending_response_id = None
        self.pending_output_items = []

    def fork_for_new_connection(self) -> CodexResponseChain:
        """Copy replay state without reusing a connection-local response anchor."""
        forked = deepcopy(self)
        forked.drop_anchor()
        return forked

    def _continuity_blocker(
        self,
        payload: dict[str, object],
        *,
        account_id: str | None,
        auth_profile: str | None,
        selected_auth_profile: str | None,
    ) -> str | None:
        if not self.retained_input_items or self.retained_request_payload is None:
            return "no_retained_state"
        if self.response_account_id != account_id:
            return "account_changed"
        if self.response_auth_profile != auth_profile:
            return "auth_profile_changed"
        if (
            selected_auth_profile is not None
            and self.response_auth_profile != selected_auth_profile
        ):
            return "selected_auth_profile_changed"
        changed = changed_request_properties(self.retained_request_payload, payload)
        if changed:
            return "request_changed:" + ",".join(changed)
        return None


def output_items_from_message(message: Message) -> list[dict[str, Any]]:
    """Build replayable Responses output items when a stream omits snapshots."""
    items: list[dict[str, Any]] = []
    text = message.text_content()
    if text:
        item: dict[str, Any] = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
        if message.phase is not None:
            item["phase"] = message.phase
        items.append(item)
    items.extend(
        {
            "type": "function_call",
            "call_id": tool_call.id,
            "name": tool_call.function.name,
            "arguments": tool_call.function.arguments,
        }
        for tool_call in message.tool_calls
    )
    return items


def visible_items_from_output(
    output_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project raw output into the visible items produced from Yoke messages."""
    visible: list[dict[str, Any]] = []
    for item in output_items:
        item_type = item.get("type")
        if item_type == "message":
            content = [
                {"type": "output_text", "text": part.get("text") or ""}
                for part in item.get("content") or []
                if isinstance(part, dict)
                and part.get("type") in {"output_text", "text"}
            ]
            visible.append(
                {
                    "role": item.get("role") or "assistant",
                    "content": content,
                }
            )
        elif item_type == "function_call":
            visible.append(
                {
                    "type": "function_call",
                    "call_id": item.get("call_id") or item.get("id") or "",
                    "name": item.get("name") or "",
                    "arguments": item.get("arguments") or "{}",
                }
            )
    return visible


def changed_request_properties(
    previous: dict[str, object], current: dict[str, object]
) -> list[str]:
    """Return sorted request keys that differ outside incremental state."""
    ignored_keys = {"input", "client_metadata", "previous_response_id"}
    keys = (previous.keys() | current.keys()) - ignored_keys
    missing = object()
    return sorted(
        key for key in keys if previous.get(key, missing) != current.get(key, missing)
    )


def history_divergence(
    items: list[dict[str, Any]], retained: list[dict[str, Any]]
) -> str:
    """Describe where input stopped extending retained history, without content."""
    if len(retained) > len(items):
        return f"history_shorter:{len(items)}<{len(retained)}"
    index = next(
        position
        for position, (current, previous) in enumerate(zip(items, retained))
        if current != previous
    )
    item = items[index]
    kind = item.get("type") or item.get("role") or "unknown"
    return f"history_diverged:{index}/{len(retained)}:{kind}"


def strip_list_prefix(items: list[Any], prefix: list[Any]) -> list[Any] | None:
    """Return items after an exact prefix, or None when it is not a prefix."""
    if len(prefix) > len(items):
        return None
    if items[: len(prefix)] != prefix:
        return None
    return items[len(prefix) :]


def _input_items(payload: dict[str, object]) -> list[dict[str, Any]] | None:
    value = payload.get("input")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return None
    return cast(list[dict[str, Any]], value)
