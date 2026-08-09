"""Regression checks for user-facing Yoke branding."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEXT_ROOTS = (
    ROOT / "src",
    ROOT / "tests",
    ROOT / "examples",
    ROOT / "scripts",
    ROOT / ".githooks",
)
TEXT_FILES = (
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "pyproject.toml",
    ROOT / "uv.lock",
)
LEGACY_BRAND = "".join(("a", "x", "i"))
LEGACY_BRAND_PATTERN = re.compile(
    rf"(?i:(?:^|[^a-z]){LEGACY_BRAND})|(?<=[a-z]){LEGACY_BRAND.title()}"
)


def test_repository_has_no_legacy_branding() -> None:
    """Keep old product names out of shipped source, tests, and metadata."""
    paths = list(TEXT_FILES)
    for root in TEXT_ROOTS:
        paths.extend(path for path in root.rglob("*") if path.is_file())

    matches: list[str] = []
    for path in paths:
        relative_path = path.relative_to(ROOT).as_posix()
        if LEGACY_BRAND_PATTERN.search(relative_path):
            matches.append(relative_path)
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if LEGACY_BRAND_PATTERN.search(content):
            matches.append(relative_path)

    assert matches == []


def test_session_resume_notice_uses_yoke_command() -> None:
    """Render a usable Yoke resume command in both interactive frontends."""
    from yoke.cli.interactive.common import session_resume_notice

    assert session_resume_notice("session-123") == (
        "To resume this session run:\nyoke resume session-123"
    )
