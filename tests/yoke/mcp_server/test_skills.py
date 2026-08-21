"""MCP-only skill discovery and loading tests."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from yoke.mcp_server.cli import parse_config
from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.server import create_service
from yoke.mcp_server.skills import load_mcp_skill_registry

from .helpers import memory_client
from .helpers import structured


def write_skill(root: Path, name: str, description: str = "Example skill.") -> Path:
    skill_root = root / name
    skill_root.mkdir(parents=True)
    skill_md = skill_root / "SKILL.md"
    skill_md.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# Instructions\n",
        encoding="utf-8",
    )
    return skill_root


def test_skill_tool_lists_and_loads_full_skill_payload(tmp_path: Path) -> None:
    skill_dirs = tmp_path / "skills"
    skill_root = write_skill(skill_dirs / "group", "example-skill")
    reference = skill_root / "references" / "guide.md"
    reference.parent.mkdir()
    reference.write_text("supporting material", encoding="utf-8")

    async def scenario() -> None:
        service = create_service(
            MCPServerConfig(root=tmp_path, skill_dirs=(skill_dirs,))
        )
        async with memory_client(service) as client:
            catalog = structured(await client.call_tool("skill", {}))
            available = {item["name"]: item for item in catalog["available"]}
            assert "example-skill" in available
            assert Path(available["example-skill"]["skill_md_path"]).is_absolute()

            result = structured(
                await client.call_tool("skill", {"load": ["example-skill"]})
            )
            assert result["ok"] is True
            assert result["loaded"] == ["example-skill"]
            payload = result["skills"][0]
            assert payload["content"] == (skill_root / "SKILL.md").read_text(
                encoding="utf-8"
            )
            assert payload["files"] == [
                str((skill_root / "SKILL.md").resolve()),
                str(reference.resolve()),
            ]
            assert all(Path(path).is_absolute() for path in payload["files"])

    asyncio.run(scenario())


def test_skill_tool_reports_missing_names_without_losing_loaded_skills(
    tmp_path: Path,
) -> None:
    skill_dirs = tmp_path / "skills"
    write_skill(skill_dirs, "example-skill")

    async def scenario() -> None:
        service = create_service(
            MCPServerConfig(root=tmp_path, skill_dirs=(skill_dirs,))
        )
        async with memory_client(service) as client:
            response = await client.call_tool(
                "skill", {"load": ["example-skill", "missing-skill"]}
            )
            result = structured(response)
            assert response.is_error is True
            assert result["ok"] is False
            assert result["loaded"] == ["example-skill"]
            assert result["missing"] == ["missing-skill"]
            assert len(result["skills"]) == 1

    asyncio.run(scenario())


def test_mcp_skill_discovery_is_recursive_and_first_directory_wins(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_root = write_skill(first / "nested", "duplicate", "First.")
    write_skill(second, "duplicate", "Second.")

    registry = load_mcp_skill_registry((first, second))

    skill = registry.require("duplicate")
    assert skill.description == "First."
    assert skill.root == first_root


def test_parse_config_accepts_platform_separated_skill_dirs(
    tmp_path: Path, monkeypatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("YOKE_MCP_SKILL_DIRS", f"{first}{os.pathsep}{second}")

    config = parse_config(["--root", str(tmp_path)])

    assert config.skill_dirs == (first.resolve(), second.resolve())
