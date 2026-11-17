"""Approved image normalization is the only provider-visible storage change."""

from __future__ import annotations

import base64
import io
import json
import random

from PIL import Image, ImageDraw

from tests.yoke.loop.test_runtime_request_differential import (
    EchoTool,
    RecordingProvider,
    _calls,
)
from tests.yoke.storage.test_image_codec import legacy, quiet
from yoke.agent.context import ContextManager
from yoke.agent.image_data import image_bytes_to_data_url
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import (
    ConversationEntry,
    ConversationEntryKind,
    Message,
    MessageImageURLContentPart,
    MessageImageURL,
)
from yoke.agent.skills.models import ActiveSkill
from yoke.ai.providers.codex.subscription.messages import convert_messages
from yoke.ai.providers.openai_compat.content import serialize_message_for_openai
from yoke.session import SessionRecord, SessionStore, SessionTreeIndex
from yoke.storage.compactor import compact_database


def representative_images() -> tuple[list[str], list[str]]:
    rng = random.Random(174)
    screenshot = Image.frombytes("RGB", (512, 384), rng.randbytes(512 * 384 * 3))
    draw = ImageDraw.Draw(screenshot)
    draw.rectangle((10, 10, 360, 100), fill="white")
    draw.text((20, 30), "Session inspector: preserve text and tool order", fill="black")
    cases = [
        ("PNG", screenshot),
        ("PNG", Image.new("RGB", (1700, 1000), "red")),
        ("PNG", Image.new("RGBA", (120, 80), (10, 30, 50, 100))),
        ("JPEG", screenshot.resize((96, 64))),
        ("WEBP", screenshot.resize((96, 64))),
    ]
    originals, expected = [], []
    for format_name, image in cases:
        stream = io.BytesIO()
        image.save(stream, format=format_name)
        raw = stream.getvalue()
        mime = Image.MIME[format_name]
        url = f"data:{mime};base64," + base64.b64encode(raw).decode()
        candidate = image_bytes_to_data_url(raw)
        originals.append(url)
        expected.append(
            candidate if candidate.split(",")[1] != url.split(",")[1] else url
        )
    assert originals[0] != expected[0]  # In-limit opaque PNG becomes smaller JPEG.
    assert expected[2].startswith("data:image/png;")  # Alpha survives.
    return originals, expected


def capture(record):
    provider = RecordingProvider([_calls("continued"), Message.assistant("done")])
    agent = RuntimeAgent(
        provider=provider,
        tools=[EchoTool.bind()],
        context_manager=ContextManager(
            instructions=[Message.system("exact base instructions")]
        ),
        active_skills=record.active_skills,
    )
    try:
        active = SessionTreeIndex(
            record.conversation_entries, record.leaf_id
        ).active_entries()
        agent.load_conversation(
            conversation_entries=active, active_skills=record.active_skills
        )
        agent.run("continue without repeating tool work")
        requests = []
        for call in provider.records:
            message_json = call["messages_json"]
            assert isinstance(message_json, str)
            messages = [
                Message.model_validate(value) for value in json.loads(message_json)
            ]
            requests.append(
                {
                    **call,
                    "codex": convert_messages(messages),
                    "openai": [
                        serialize_message_for_openai(message) for message in messages
                    ],
                }
            )
        return requests
    finally:
        agent.close()


def test_compaction_resume_only_changes_approved_image_bytes_and_mime(tmp_path):
    originals, expected = representative_images()
    messages = [
        Message.user(
            [
                {"type": "text", "text": "Five image formats, exact labels and text"},
                *[
                    MessageImageURLContentPart(
                        image_url=MessageImageURL(url=url), label=f"image {index}"
                    )
                    for index, url in enumerate(originals)
                ],
            ]
        ),
        _calls("previous"),
        Message(
            role="tool",
            tool_call_id="oracle-previous",
            content='{"ok":true,"value":"previous"}',
        ),
        Message.user(
            [
                {"type": "text", "text": "Existing tool image context"},
                {
                    "type": "local_image",
                    "path": "/missing/screenshot.png",
                    "data_url": originals[0],
                },
            ]
        ),
        Message.assistant("Checkpoint text stays exact"),
    ]
    kinds: list[ConversationEntryKind] = [
        "user",
        "assistant_tool_calls",
        "tool_result",
        "tool_context",
        "assistant",
    ]
    entries = [
        ConversationEntry(
            id=str(index),
            parent_id=str(index - 1) if index else None,
            kind=kind,
            message=message,
        )
        for index, (kind, message) in enumerate(zip(kinds, messages))
    ]
    # Inactive branches are stored and compacted without entering the request.
    entries.append(
        ConversationEntry(
            id="branch",
            parent_id="0",
            kind="assistant",
            message=Message.assistant("hidden branch"),
        )
    )
    entries[-2].metadata["compaction_handoff"] = {
        "retained_messages": [message.model_dump(mode="json") for message in messages]
    }
    store = SessionStore(tmp_path, migrate=True)
    assert store.insert_record(
        SessionRecord(
            id="images",
            conversation_entries=entries,
            leaf_id="4",
            active_skills=[
                ActiveSkill(
                    name="exact",
                    description="fixed",
                    source_path="<inline>",
                    content="Preserve these exact skill instructions.",
                )
            ],
        )
    )
    before = capture(store.load("images"))
    legacy(store.database)
    quiet(store.database)
    result = compact_database(store.database)
    assert result.images_reencoded >= 3
    store.database.close()
    reopened = SessionStore(tmp_path)
    after = capture(reopened.load("images"))
    # Compare serialized request bytes, including text, ordering, tools, image
    # counts, labels and cache scope. Only the approved carrier URLs may differ.
    projected = json.dumps(before, ensure_ascii=False, sort_keys=True)
    for old, new in zip(originals, expected):
        projected = projected.replace(old, new)
    assert json.dumps(after, ensure_ascii=False, sort_keys=True) == projected
    assert len(after) == 2
