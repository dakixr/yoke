"""Memory checkpoint text rendering and parsing."""

from __future__ import annotations

_MEMORY_PREFIX = (
    "Another language model started to solve this problem and produced a "
    "summary of its work.\nUse this summary to continue the task without "
    "redoing already completed investigation.\nHere is the summary:\n"
)
_LEGACY_PREFIX = (
    "Conversation memory summary:\nAuto-compacted summary of earlier "
    "conversation. This is lossy; rely on later messages when conflicts "
    "exist.\n"
)
_LEGACY_SUFFIX = (
    "\nUse this as historical context. Prioritize newer messages when conflicts exist."
)
_CONTINUATION_NOTE = (
    "Continuation note: This task was compacted mid-turn. Continue working "
    "from the preserved recent tool calls and results. Do not treat "
    "compaction as task completion; keep making progress until the user's "
    "task is fully complete or you need clarification."
)


def render_memory_message(summary_text: str, *, continuation_note: bool = False) -> str:
    """Render checkpoint memory without depending on legacy helpers."""
    content = f"{_MEMORY_PREFIX}{summary_text}"
    if continuation_note:
        content = f"{content}\n{_CONTINUATION_NOTE}"
    return content


def parse_memory_message(content: str) -> str | None:
    """Parse current and legacy synthetic memory messages."""
    if content.startswith(_MEMORY_PREFIX):
        summary = content.removeprefix(_MEMORY_PREFIX).rstrip()
    elif content.startswith(_LEGACY_PREFIX) and content.endswith(_LEGACY_SUFFIX):
        summary = (
            content.removeprefix(_LEGACY_PREFIX).removesuffix(_LEGACY_SUFFIX).rstrip()
        )
    else:
        return None
    if summary.endswith(_CONTINUATION_NOTE):
        summary = summary[: -len(_CONTINUATION_NOTE)].rstrip()
    return summary


def memory_message_has_continuation_note(content: str) -> bool:
    """Return whether synthetic memory marks a mid-turn handoff."""
    return parse_memory_message(content) is not None and _CONTINUATION_NOTE in content
