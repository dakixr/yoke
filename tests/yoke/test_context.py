# ruff: noqa: D100,D103,E501,S101

from __future__ import annotations

import base64
import io

from PIL import Image

from yoke.agent.compaction import CompactionPolicy
from yoke.agent.compaction import Compactor
from yoke.agent.context import ContextManager
from yoke.agent.message_sanitizer import normalize_tool_call_sequence
from yoke.ai.providers.openai_compat import (
    normalize_openai_request_messages,
)
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageImageURL
from yoke.agent.models import MessageLocalImageContentPart
from yoke.cli.runtime import estimate_messages_token_usage


def test_context_manager_prepare_compaction_rebuilds_checkpoint() -> None:
    manager = ContextManager(
        compaction_policy=CompactionPolicy(
            max_total_tokens=250,
            reserved_output_tokens=83,
            keep_recent_tokens=50,
        )
    )
    context = manager.initialize(
        "follow-up",
        [
            Message.user("older"),
            Message.assistant("done"),
            Message.user("big request"),
            Message.assistant("prefix " + ("alpha " * 200)),
            Message.tool("call-1", '{"ok":true,"stdout":"' + ("beta " * 200) + '"}'),
            Message.assistant("suffix"),
        ],
    )

    preparation = manager.prepare_compaction(context, reason="forced")

    assert preparation is not None
    assert preparation.boundary == "user"
    assert [message.role for message in preparation.messages_to_summarize] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert [message.role for message in preparation.kept_messages] == [
        "user",
        "user",
        "user",
    ]
    assert preparation.kept_messages[-1].content == "follow-up"


def test_image_only_message_counts_more_than_filename_text(tmp_path) -> None:
    image_path = tmp_path / "vision.png"
    image = Image.new("RGB", (1024, 1024), color=(255, 0, 0))
    image.save(image_path, format="PNG")

    message = Message.user([MessageLocalImageContentPart(path=str(image_path))])
    estimate = estimate_messages_token_usage([message])

    assert estimate.input_tokens == 766


def test_image_token_estimate_respects_detail_and_model_group(tmp_path) -> None:
    image_path = tmp_path / "detail.png"
    image = Image.new("RGB", (1024, 1024), color=(0, 255, 0))
    image.save(image_path, format="PNG")

    low_detail = Message.user(
        [MessageLocalImageContentPart(path=str(image_path), detail="low")]
    )
    high_detail = Message.user(
        [MessageLocalImageContentPart(path=str(image_path), detail="high")]
    )

    low_estimate = Compactor(model="gpt-5.4").estimate_tokens(
        [low_detail], reserve_tokens=0
    )
    high_estimate = Compactor(model="gpt-4o").estimate_tokens(
        [high_detail], reserve_tokens=0
    )
    mini_estimate = Compactor(model="gpt-4o-mini").estimate_tokens(
        [high_detail], reserve_tokens=0
    )

    assert low_estimate.input_tokens == 71
    assert high_estimate.input_tokens == 766
    assert mini_estimate.input_tokens == 25502


def test_remote_data_url_image_uses_embedded_dimensions() -> None:
    image = Image.new("RGB", (2048, 4096), color=(0, 0, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    data_url = base64.b64encode(buffer.getvalue()).decode("ascii")
    message = Message.user(
        [
            MessageImageURLContentPart(
                image_url=MessageImageURL(url=f"data:image/png;base64,{data_url}"),
                detail="high",
            )
        ]
    )

    estimate = Compactor(model="gpt-4o").estimate_tokens([message], reserve_tokens=0)

    assert estimate.input_tokens == 1106


def test_remote_image_without_dimensions_uses_fallback_estimate() -> None:
    message = Message.user(
        [
            MessageImageURLContentPart(
                image_url=MessageImageURL(url="https://example.com/image.png"),
                detail="high",
            )
        ]
    )

    estimate = Compactor(model="unknown-model").estimate_tokens(
        [message], reserve_tokens=0
    )

    assert estimate.input_tokens == 1025


def test_drop_incomplete_tool_turns_removes_dangling_assistant_tool_call() -> None:
    messages = [
        Message.user("Generate previews"),
        Message(
            role="assistant",
            content="Attaching previews now.",
            tool_calls=[
                ToolCall(
                    id="call-image-1",
                    function=ToolFunction(
                        name="attach_image",
                        arguments='{"path":"page1.png"}',
                    ),
                ),
                ToolCall(
                    id="call-image-2",
                    function=ToolFunction(
                        name="attach_image",
                        arguments='{"path":"page2.png"}',
                    ),
                ),
            ],
        ),
        Message.tool("call-image-1", '{"ok": true}'),
        Message.user("Continue"),
    ]

    repaired = normalize_tool_call_sequence(
        messages,
        drop_incomplete_assistant=True,
    )

    assert [message.role for message in repaired] == ["user", "tool", "user"]
    assert repaired[1].tool_call_id == "call-image-1"


def test_drop_incomplete_tool_turns_keeps_completed_tool_turn() -> None:
    messages = [
        Message.user("Generate previews"),
        Message(
            role="assistant",
            content="Attaching previews now.",
            tool_calls=[
                ToolCall(
                    id="call-image-1",
                    function=ToolFunction(
                        name="attach_image",
                        arguments='{"path":"page1.png"}',
                    ),
                ),
                ToolCall(
                    id="call-image-2",
                    function=ToolFunction(
                        name="attach_image",
                        arguments='{"path":"page2.png"}',
                    ),
                ),
            ],
        ),
        Message.tool("call-image-1", '{"ok": true}'),
        Message.tool("call-image-2", '{"ok": true}'),
        Message.user("Continue"),
    ]

    repaired = normalize_tool_call_sequence(
        messages,
        drop_incomplete_assistant=True,
    )

    assert [message.role for message in repaired] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "user",
    ]


def test_drop_incomplete_tool_turns_preserves_non_tool_assistant_text() -> None:
    messages = [
        Message.user("Start"),
        Message.assistant("Working on it"),
        Message(
            role="assistant",
            content="Attaching previews now.",
            tool_calls=[
                ToolCall(
                    id="call-image-1",
                    function=ToolFunction(
                        name="attach_image",
                        arguments='{"path":"page1.png"}',
                    ),
                )
            ],
        ),
        Message.user("Continue"),
    ]

    repaired = normalize_tool_call_sequence(
        messages,
        drop_incomplete_assistant=True,
    )

    assert [message.role for message in repaired] == [
        "user",
        "assistant",
        "user",
    ]
    assert repaired[1].content == "Working on it"


def test_drop_incomplete_tool_turns_keeps_follow_up_messages_after_bad_tool_turn() -> (
    None
):
    messages = [
        Message.user("Start"),
        Message(
            role="assistant",
            content="Attaching previews now.",
            tool_calls=[
                ToolCall(
                    id="call-image-1",
                    function=ToolFunction(
                        name="attach_image",
                        arguments='{"path":"page1.png"}',
                    ),
                ),
                ToolCall(
                    id="call-image-2",
                    function=ToolFunction(
                        name="attach_image",
                        arguments='{"path":"page2.png"}',
                    ),
                ),
                ToolCall(
                    id="call-run",
                    function=ToolFunction(
                        name="python_exec",
                        arguments='{"code":"print(1)"}',
                    ),
                ),
            ],
        ),
        Message.tool("call-image-1", '{"ok": true}'),
        Message.user("page 1 preview"),
        Message.user("page 2 preview"),
        Message.user("continue"),
    ]

    repaired = normalize_tool_call_sequence(
        messages,
        drop_incomplete_assistant=True,
    )

    assert [message.role for message in repaired] == [
        "user",
        "tool",
        "user",
        "user",
        "user",
    ]
    assert repaired[1].tool_call_id == "call-image-1"


def test_normalize_openai_request_messages_drops_invalid_tool_turn_and_tool_calls_on_tool() -> (
    None
):
    messages = [
        Message.user("Start"),
        Message(
            role="assistant",
            content="Attaching previews now.",
            tool_calls=[
                ToolCall(
                    id="call-image-1",
                    function=ToolFunction(
                        name="attach_image",
                        arguments='{"path":"page1.png"}',
                    ),
                ),
                ToolCall(
                    id="call-run",
                    function=ToolFunction(
                        name="python_exec",
                        arguments='{"code":"print(1)"}',
                    ),
                ),
            ],
        ),
        Message(
            role="tool",
            tool_call_id="call-image-1",
            content='{"ok": true}',
            tool_calls=[
                ToolCall(
                    id="illegal-nested-call",
                    function=ToolFunction(
                        name="attach_image",
                        arguments='{"path":"bad.png"}',
                    ),
                )
            ],
        ),
        Message.user("continue"),
    ]

    normalized = normalize_openai_request_messages(messages)

    assert [message.role for message in normalized] == ["user", "tool", "user"]
    assert normalized[1].tool_calls == []
