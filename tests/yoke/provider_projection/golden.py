"""Narrow normalization preserving message strings and their serialization."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import yoke
from yoke.session import SessionRecord

# CLI DEFAULT_SYSTEM_PROMPT captures these paths at import time, before pytest
# installs each case's isolated HOME. Capture the same inputs, never prompt text.
_HARNESS_ROOT = str(Path(yoke.__file__).parent)
_GLOBAL_YOKE_DIR = str(Path.home() / ".yoke")


class Normalizer:
    """Name persisted identities by topology, retaining cache-scope relationships."""

    def __init__(self, root: Path):
        self.replacements = {
            str(root): "<WORKSPACE>",
            _HARNESS_ROOT: "<HARNESS_ROOT>",
            _GLOBAL_YOKE_DIR: "<GLOBAL_YOKE_DIR>",
        }
        self.processes: dict[str, str] = {}
        self.transient_titles: dict[str, str] = {}

    def register(self, record: SessionRecord, *, fork: bool = False) -> None:
        session = "<FORK_SESSION>" if fork else "oracle-session"
        if fork:
            self.replacements[record.id] = session
        for index, entry in enumerate(record.conversation_entries):
            prefix = "FORK_ENTRY" if fork else "ENTRY"
            self.replacements.setdefault(entry.id, f"<{prefix}:{index}:{entry.kind}>")

    def normalize(self, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        command_ids = {
            tool["id"]
            for call in calls
            for serialized in call["messages_json"]
            for tool in json.loads(serialized).get("tool_calls", [])
            if tool["function"]["name"] == "command_exec"
        }

        def process_id(identity: str) -> str:
            return self.processes.setdefault(identity, str(1 + len(self.processes)))

        def text(value: str) -> str:
            for original, replacement in sorted(
                (self.replacements | self.transient_titles).items(),
                key=lambda pair: len(pair[0]),
                reverse=True,
            ):
                value = value.replace(original, replacement)
            return value

        def message(value: str) -> str:
            parsed = json.loads(value)
            if parsed["role"] == "tool" and parsed["tool_call_id"] in command_ids:
                content = parsed["content"]
                assert isinstance(content, str)
                original_content = content

                def cursor(match: re.Match[str]) -> str:
                    # Only the first eight bytes are a random process handle.
                    # Preserve sequence/offset and leave cursor-like output text
                    # untouched. The key must be unescaped in the result JSON.
                    raw = base64.urlsafe_b64decode(match[2][4:])
                    identity = process_id(str(int.from_bytes(raw[:8], "big")))
                    normalized = int(identity).to_bytes(8, "big") + raw[8:]
                    encoded = "pc1_" + base64.urlsafe_b64encode(normalized).decode(
                        "ascii"
                    )
                    return match[1] + encoded + match[3]

                content = re.sub(
                    r'(?<!\\)("cursor":\s*")(pc1_[A-Za-z0-9_-]{32})(")', cursor, content
                )

                def process(match: re.Match[str]) -> str:
                    return match[1] + process_id(match[2])

                content = re.sub(r'(?<!\\)("session_id":\s*)(\d+)', process, content)
                # Replace just this field's serialized literal. Re-encoding the
                # whole Message would conceal serializer changes. All original
                # content whitespace and escaping outside the two IDs survive.
                value = value.replace(
                    json.dumps(original_content, ensure_ascii=False),
                    json.dumps(content, ensure_ascii=False),
                    1,
                )
            return text(value)

        def walk(value: Any) -> Any:
            if isinstance(value, str):
                return text(value)
            if isinstance(value, list):
                return [walk(item) for item in value]
            if isinstance(value, dict):
                return {
                    key: [message(serialized) for serialized in item]
                    if key == "messages_json"
                    else walk(item)
                    for key, item in value.items()
                }
            return value

        return walk(calls)


def encode(value: object) -> bytes:
    """Freeze outer formatting while leaving each Message JSON string intact."""
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def storage_projection(value: Any) -> Any:
    """Ignore only usage.raw in request Message wrappers, never response bytes.

    Provider serializers do not send usage. Old goldens retain the scripted
    diagnostic payload; v4 storage intentionally drops it. Preserve every byte
    outside that one field, including tool-result JSON embedded in content.
    """
    if isinstance(value, list):
        return [storage_projection(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: storage_projection(item) for key, item in value.items()}
    if "messages_json" in result:
        messages = []
        for serialized in result["messages_json"]:
            usage = json.loads(serialized).get("usage")
            if isinstance(usage, dict) and "raw" in usage:
                before = json.dumps(usage, ensure_ascii=False, separators=(",", ":"))
                usage["raw"] = {}
                after = json.dumps(usage, ensure_ascii=False, separators=(",", ":"))
                serialized = serialized.replace(
                    '"usage":' + before, '"usage":' + after, 1
                )
            messages.append(serialized)
        result["messages_json"] = messages
    return result
