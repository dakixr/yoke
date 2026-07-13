"""Skill discovery utilities for loading SkillSpec objects from directories."""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from yoke.agent.skills.models import SkillSpec

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillDiscoveryError(ValueError):
    """Raised when a skill directory cannot be loaded."""

    pass


@dataclass(slots=True, frozen=True)
class SkillLoadFailure:
    """One skill directory that could not be loaded."""

    source_path: Path
    error: str


@dataclass(slots=True)
class SkillDiscoveryResult:
    """Valid discovered skills plus isolated load failures."""

    skills: list[SkillSpec]
    failures: list[SkillLoadFailure] = field(default_factory=list)


def builtin_skill_dir() -> Path:
    """Return the directory containing built-in skills shipped with yoke."""
    return Path(__file__).resolve().parent / "built_in"


def discover_skills(skill_dirs: list[Path]) -> list[SkillSpec]:
    """Discover all skills in the given directories and return their specs."""
    result = discover_skills_with_failures(skill_dirs)
    if result.failures:
        raise SkillDiscoveryError(result.failures[0].error)
    return result.skills


def discover_skills_with_failures(skill_dirs: list[Path]) -> SkillDiscoveryResult:
    """Discover valid skills without letting one stale directory abort loading."""
    discovered: list[SkillSpec] = []
    failures: list[SkillLoadFailure] = []
    seen: set[str] = set()
    all_skill_dirs = [builtin_skill_dir(), *skill_dirs]
    for skill_dir in all_skill_dirs:
        resolved_dir = skill_dir.resolve()
        if not resolved_dir.is_dir():
            continue
        try:
            children = sorted(resolved_dir.iterdir())
        except OSError as exc:
            failures.append(
                SkillLoadFailure(
                    source_path=resolved_dir,
                    error=f"Could not read skill directory `{resolved_dir}`: {exc}",
                )
            )
            continue
        for child in children:
            if not child.is_dir():
                continue
            try:
                spec = load_skill(child)
            except SkillDiscoveryError as exc:
                failures.append(SkillLoadFailure(source_path=child, error=str(exc)))
                continue
            if spec.name in seen:
                failures.append(
                    SkillLoadFailure(
                        source_path=child,
                        error=(
                            f"Duplicate skill name `{spec.name}` found while "
                            "loading skills. Rename one of the skill directories."
                        ),
                    )
                )
                continue
            seen.add(spec.name)
            discovered.append(spec)
    return SkillDiscoveryResult(skills=discovered, failures=failures)


def load_skill(root: Path) -> SkillSpec:
    """Load a SkillSpec from a skill root directory containing SKILL.md."""
    skill_md_path = root / "SKILL.md"
    if not skill_md_path.is_file():
        raise SkillDiscoveryError(
            f"Invalid skill directory `{root}`. Expected a `SKILL.md` file."
        )
    try:
        content = skill_md_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillDiscoveryError(
            f"Could not read skill file `{skill_md_path}`: {exc}"
        ) from exc
    frontmatter = _parse_frontmatter(content, skill_md_path)
    name = frontmatter.get("name", "").strip()
    description = frontmatter.get("description", "").strip()
    if not name:
        raise SkillDiscoveryError(
            f"Skill file `{skill_md_path}` is missing a `name:` "
            "field in the frontmatter."
        )
    if not _NAME_RE.fullmatch(name):
        raise SkillDiscoveryError(
            f"Skill file `{skill_md_path}` has invalid name `{name}`. "
            "Use lowercase letters, numbers, and dashes only."
        )
    if root.name != name:
        raise SkillDiscoveryError(
            f"Skill directory `{root.name}` does not match the "
            f"declared skill name `{name}` in `{skill_md_path}`."
        )
    if not description:
        raise SkillDiscoveryError(
            f"Skill file `{skill_md_path}` is missing a "
            "`description:` field in the frontmatter."
        )
    return SkillSpec(
        name=name,
        description=description,
        root=root.resolve(),
        skill_md_path=skill_md_path.resolve(),
        file_paths=_skill_file_paths(root),
    )


def _skill_file_paths(root: Path) -> list[str]:
    return sorted(str(path.resolve()) for path in root.rglob("*") if path.is_file())


def _parse_frontmatter(content: str, path: Path) -> dict[str, str]:
    match = _FRONTMATTER_RE.match(content)
    if match is None:
        raise SkillDiscoveryError(
            f"Skill file `{path}` is missing YAML frontmatter. "
            "Start the file with `---`, then add `name:` and "
            "`description:` fields."
        )
    fields: dict[str, str] = {}
    for raw_line in match.group(1).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields
