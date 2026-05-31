"""Data models for skill specifications and active skill state."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class SkillSpec(BaseModel):
    """Specification for a discoverable skill loaded from a SKILL.md file."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    root: Path
    skill_md_path: Path

    def load_content(self) -> str:
        """Read and return the full content of the SKILL.md file."""
        try:
            return self.skill_md_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(
                f"Could not read skill file `{self.skill_md_path}`: {exc}"
            ) from exc


class ActiveSkill(BaseModel):
    """A skill that has been loaded and is currently active in the agent."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    source_path: str
    content: str | None = None
    reload_on_next_use: bool = True

    @property
    def is_inline(self) -> bool:
        """Return whether this skill is backed by embedded inline content."""
        return self.source_path == "<inline>"

    def load_content(self) -> str:
        """Return canonical instructions for this active skill."""
        if self.is_inline:
            if isinstance(self.content, str) and self.content.strip():
                return self.content
            raise ValueError(f"Inline skill `{self.name}` is missing embedded content.")
        if not self.source_path.strip():
            raise ValueError(f"Active skill `{self.name}` is missing a source path.")
        try:
            return Path(self.source_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(
                f"Could not read active skill `{self.name}` from "
                f"`{self.source_path}`: {exc}"
            ) from exc
