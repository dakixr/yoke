"""Contract coverage for shared provider retry, reduction and identity seams."""

# ruff: noqa: S101
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from yoke.agent.budget import resolve_provider_compaction_budget
from yoke.agent.models import Message
from yoke.agent.provider_selection import capture_provider_session_state
from yoke.agent.tools.context import resolve_model_identity
from yoke.agent.tools.web.hosted import supports_hosted_web_search
from yoke.ai.providers.base import (
    BaseProvider,
    ModelCatalogProvider,
    ProviderCancelledError,
    ProviderError,
)
from yoke.ai.providers.codex.events import (
    CodexResponseState,
    build_message,
    classify_error_event,
    reduce_event,
)
from yoke.ai.providers.http_retry import RetryDecision, error_detail, run_with_retry

from tests.yoke.ai.providers.transport_support import HTTP_KINDS, _provider


@pytest.mark.parametrize("kind", (*HTTP_KINDS, "codex-ws"))
def test_agent_identity_outputs_match_builtin_metadata(
    kind: str, tmp_path: Path
) -> None:
    with httpx.Client() as client:
        provider = _provider(kind, tmp_path, client)
        try:
            assert isinstance(provider, BaseProvider)
            assert isinstance(provider, ModelCatalogProvider)
            model = provider.current_model_info()
            assert model is not None
            identity = resolve_model_identity(provider)
            assert identity.provider_name == provider.provider_name.strip().lower()
            assert identity.model_id == provider.config.model.strip()
            assert identity.reasoning_effort == provider.config.reasoning_effort
            assert identity.supports_image_inputs == model.supports_image_inputs
            session = capture_provider_session_state(SimpleNamespace(provider=provider))
            assert session.provider_name == identity.provider_name
            assert session.model_id == identity.model_id
            assert session.reasoning_effort == identity.reasoning_effort
            assert session.context_window_tokens == model.context_window_tokens
            budget = resolve_provider_compaction_budget(provider)
            assert budget.provider_name == identity.provider_name
            assert budget.model_id == identity.model_id
            assert budget.context_window_tokens == model.context_window_tokens
            assert supports_hosted_web_search(provider) == kind.startswith("codex-")
        finally:
            provider.close()
        assert not client.is_closed


def test_retry_driver_recovery_has_one_extra_resend_and_keeps_attempt_numbers() -> None:
    attempts: list[int] = []
    transitions: list[int] = []
    sleeps: list[float] = []
    failure = ProviderError("failed")

    def attempt(number: int) -> Message:
        attempts.append(number)
        raise failure

    def classify(error: Exception, number: int) -> RetryDecision:
        return RetryDecision(
            error, 0.0, recover=True, before_retry=lambda: transitions.append(number)
        )

    with pytest.raises(ProviderError, match="^failed$") as caught:
        run_with_retry(
            attempt,
            classify,
            max_retries=0,
            max_recoveries=1,
            cancel_requested=lambda: False,
            sleep=sleeps.append,
        )
    assert caught.value is failure
    assert attempts == [0, 1]
    assert transitions == [0]
    assert sleeps == []


def test_retry_driver_cancellation_during_backoff_runs_hook_once() -> None:
    cancelled = False
    hooks: list[str] = []

    def sleep(_seconds: float) -> None:
        nonlocal cancelled
        cancelled = True

    def attempt(_number: int) -> Message:
        raise ProviderError("retry")

    with pytest.raises(ProviderCancelledError):
        run_with_retry(
            attempt,
            lambda error, _number: RetryDecision(
                error, 1.0, on_backoff_cancel=lambda: hooks.append("cancelled")
            ),
            max_retries=1,
            cancel_requested=lambda: cancelled,
            sleep=sleep,
        )
    assert hooks == ["cancelled"]


@pytest.mark.parametrize(
    "body,expected",
    [
        ("<html><body>proxy failure</body></html>", "Bad Gateway"),
        ("x" * 600, "x" * 500),
        (" short plain text ", "short plain text"),
        ("", "HTTP 502"),
    ],
)
def test_error_detail_bounds_only_unstructured_bodies(body: str, expected: str) -> None:
    assert error_detail(httpx.Response(502, text=body)) == expected
    assert (
        error_detail(httpx.Response(502, json={"error": {"message": "x" * 600}}))
        == "x" * 600
    )


@pytest.mark.parametrize(
    "marker",
    [
        "server_is_overloaded",
        "temporarily unavailable",
        "currently overloaded",
        "try again later",
    ],
)
def test_both_codex_transports_share_transient_markers(marker: str) -> None:
    for websocket in (False, True):
        error = classify_error_event(
            {"type": "error", "error": {"message": marker}}, websocket=websocket
        )
        assert type(error).__name__ == "ProviderServerError"
        assert error.status_code == 503


def test_codex_reducer_preserves_tools_phase_text_and_usage_for_both_transports() -> (
    None
):
    events = [
        {"type": "response.output_text.delta", "delta": "answer"},
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item",
            "delta": '{"q":',
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item",
            "delta": "1}",
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "item",
                "call_id": "call",
                "name": "read",
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "response",
                "output": [
                    {
                        "type": "message",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "answer"}],
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        },
    ]
    messages = []
    for websocket in (False, True):
        state = CodexResponseState(
            text_parts=[], function_calls={}, websocket=websocket
        )
        for event in events:
            reduce_event(event, state)
        messages.append(build_message(state, provider_name="codex", model_id="test"))
    assert messages[0] == messages[1]
    message = messages[0]
    assert message.text_content() == "answer"
    assert message.phase == "final_answer"
    assert message.tool_calls[0].function.arguments == '{"q":1}'
    assert message.tool_calls[0].function.name == "read"
    assert message.tool_calls[0].id == "call"
    assert message.usage is not None
    assert message.usage.input_tokens == 5
    assert message.usage.output_tokens == 2


def test_websocket_unexpected_failure_after_frame_does_not_replay(
    tmp_path: Path,
) -> None:
    attempts: list[object] = []
    sleeps: list[float] = []

    class Socket:
        frames = 0

        def send(self, _payload: str) -> None:
            pass

        def recv(self, timeout: float | None = None) -> str:
            self.frames += 1
            if self.frames == 1:
                return '{"type":"response.output_text.delta","delta":"partial"}'
            raise RuntimeError("connection reader failed")

        def close(self) -> None:
            pass

    def factory(*args: object, **kwargs: object) -> Socket:
        attempts.append(args)
        return Socket()

    provider = _provider("codex-ws", tmp_path, None, sleeps.append)
    provider.config.api_key = "test"
    provider._websocket_factory = factory
    try:
        with pytest.raises(
            ProviderError,
            match="Codex WebSocket request failed: connection reader failed",
        ):
            provider.complete([Message.user("hello")], [])
        assert len(attempts) == 1
        assert sleeps == []
    finally:
        provider.close()
