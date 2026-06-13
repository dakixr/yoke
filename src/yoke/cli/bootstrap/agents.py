"""AGENTS.md loading helpers for yoke CLI bootstrap."""

from __future__ import annotations

from pathlib import Path

from yoke.agent.models import Message

MAX_AGENTS_FILE_CHARS = 20_000


def build_system_messages(
    *,
    root: Path,
    base_system_prompt: str | None,
    include_agents_file: bool = True,
    home: Path,
) -> list[Message]:
    """Build system messages from the base prompt and AGENTS files."""
    messages: list[Message] = []
    if base_system_prompt:
        messages.append(Message.system(base_system_prompt))
    if not include_agents_file:
        return messages
    messages.extend(load_agents_messages(root, home=home))
    return messages


def load_agents_messages(root: Path, *, home: Path) -> list[Message]:
    """Load global and repository AGENTS.md messages."""
    resolved_home = home.resolve()
    messages: list[Message] = []
    global_path = resolved_home / ".yoke" / "AGENTS.md"
    repo_path = root / "AGENTS.md"
    for label, path in [
        ("Global", global_path),
        ("Repository-specific", repo_path),
    ]:
        message = _load_agents_message(path, label=label)
        if message is not None:
            messages.append(message)
    return messages


def _load_agents_message(path: Path, *, label: str) -> Message | None:
    """Load a single AGENTS.md file into a system message."""
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        raise ValueError(
            f"Could not read {label.lower()} AGENTS file `{path}`: {exc}"
        ) from exc
    if not content:
        return None
    if len(content) > MAX_AGENTS_FILE_CHARS:
        content = (
            content[:MAX_AGENTS_FILE_CHARS].rstrip()
            + "\n\n[AGENTS.md truncated to fit the system prompt.]"
        )
    return Message.system(
        f"{label} instructions loaded from AGENTS.md ({path}):\n\n{content}"
    )
