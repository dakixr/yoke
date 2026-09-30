# ruff: noqa: D100,D103,S101

from __future__ import annotations

from typing import Any, cast

from yoke.agent.models import Message
from yoke.ai.providers.codex.response_state import CodexResponseChain

SECRET = "private prompt text"


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "input_text", "text": text}]}


def _payload(*items: dict[str, Any], **overrides: object) -> dict[str, object]:
    return {
        "model": "gpt-6-sol",
        "instructions": "base",
        "input": list(items),
        **overrides,
    }


def _prepare(chain: CodexResponseChain, payload: dict[str, object], **kwargs: Any):
    options = {
        "account_id": "acct",
        "auth_profile": None,
        "selected_auth_profile": None,
    }
    options.update(kwargs)
    return chain.prepare(payload, **options)


def _anchored_chain() -> tuple[CodexResponseChain, dict[str, object]]:
    chain = CodexResponseChain()
    first = _payload(_user(SECRET))
    _prepare(chain, first)
    chain.stage_response(response_id="resp-1", output_items=[])
    chain.remember(first, Message.assistant("ok"), account_id="acct", auth_profile=None)
    return chain, first


def _extended(first: dict[str, object], **overrides: object) -> dict[str, object]:
    retained = cast(list[dict[str, Any]], first["input"])
    reply = {"role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}
    return _payload(*retained, reply, _user("next"), **overrides)


def test_first_request_has_no_retained_state() -> None:
    chain = CodexResponseChain()

    _prepare(chain, _payload(_user(SECRET)))

    assert chain.prepared_mode == "visible_input"
    assert chain.prepared_reason == "no_retained_state"


def test_continuation_has_no_reason() -> None:
    chain, first = _anchored_chain()

    prepared = _prepare(chain, _extended(first))

    assert chain.prepared_mode == "previous_response_id"
    assert chain.prepared_reason is None
    assert prepared["previous_response_id"] == "resp-1"


def test_changed_request_properties_are_named_without_values() -> None:
    chain, first = _anchored_chain()

    _prepare(chain, _extended(first, instructions=SECRET, reasoning={"effort": "x"}))

    assert chain.prepared_mode == "visible_input"
    assert chain.prepared_reason == "request_changed:instructions,reasoning"


def test_missing_and_none_properties_still_block_continuation() -> None:
    chain, first = _anchored_chain()

    _prepare(chain, _extended(first, tools=None))

    assert chain.prepared_mode == "visible_input"
    assert chain.prepared_reason == "request_changed:tools"


def test_rewritten_history_reports_position_and_item_kind_only() -> None:
    chain, first = _anchored_chain()
    payload = _extended(first)
    items = cast(list[dict[str, Any]], payload["input"])
    items[0] = _user("rewritten " + SECRET)

    _prepare(chain, payload)

    assert chain.prepared_mode == "visible_input"
    assert chain.prepared_reason == "history_diverged:0/2:user"
    assert SECRET not in (chain.prepared_reason or "")


def test_shorter_history_reports_lengths() -> None:
    chain, _first = _anchored_chain()

    _prepare(chain, _payload())

    assert chain.prepared_reason == "history_shorter:0<2"


def test_account_switch_and_lost_anchor_are_reported() -> None:
    chain, first = _anchored_chain()

    _prepare(chain, _extended(first), account_id="other")
    assert chain.prepared_reason == "account_changed"

    chain.drop_anchor()
    _prepare(chain, _extended(first))
    assert chain.prepared_mode == "encrypted_replay"
    assert chain.prepared_reason == "no_response_anchor"
