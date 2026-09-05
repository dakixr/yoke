"""MCP-only skill discovery and loading tests."""

from __future__ import annotations

import asyncio
import os
import shutil
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


def test_skill_install_update_and_removal_are_visible_without_restart(
    tmp_path: Path,
) -> None:
    skill_dirs = tmp_path / "skills"
    skill_dirs.mkdir()

    async def scenario() -> None:
        service = create_service(
            MCPServerConfig(root=tmp_path, skill_dirs=(skill_dirs,))
        )
        async with memory_client(service) as client:
            initial = structured(await client.call_tool("skill", {}))
            assert "hot-skill" not in {item["name"] for item in initial["available"]}

            skill_root = write_skill(skill_dirs / "nested", "hot-skill", "First.")
            installed = structured(await client.call_tool("skill", {}))
            installed_by_name = {item["name"]: item for item in installed["available"]}
            assert installed_by_name["hot-skill"]["description"] == "First."

            reference = skill_root / "reference.md"
            reference.write_text("live reference", encoding="utf-8")
            skill_md = skill_root / "SKILL.md"
            skill_md.write_text(
                "---\nname: hot-skill\ndescription: Second.\n---\n\n# Updated\n",
                encoding="utf-8",
            )
            updated = structured(
                await client.call_tool("skill", {"load": ["hot-skill"]})
            )["skills"][0]
            assert updated["description"] == "Second."
            assert updated["content"].endswith("# Updated\n")
            assert str(reference.resolve()) in updated["files"]

            shutil.rmtree(skill_root)
            removed = structured(await client.call_tool("skill", {}))
            assert "hot-skill" not in {item["name"] for item in removed["available"]}

    asyncio.run(scenario())


def test_invalid_partial_skill_does_not_break_hot_discovery(tmp_path: Path) -> None:
    skill_dirs = tmp_path / "skills"
    valid_root = write_skill(skill_dirs, "valid-skill")
    partial_root = skill_dirs / "partial-skill"
    partial_root.mkdir()
    (partial_root / "SKILL.md").write_text("still installing", encoding="utf-8")

    registry = load_mcp_skill_registry((skill_dirs,))

    assert registry.require("valid-skill").root == valid_root
    assert registry.get("partial-skill") is None


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


def test_parse_config_streams_mcp_responses_by_default(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("YOKE_MCP_JSON_RESPONSE", raising=False)

    config = parse_config(["--root", str(tmp_path)])

    assert config.json_response is False


def test_parse_config_can_opt_into_json_responses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YOKE_MCP_JSON_RESPONSE", "true")

    env_config = parse_config(["--root", str(tmp_path)])
    cli_config = parse_config(["--root", str(tmp_path), "--no-json-response"])

    assert env_config.json_response is True
    assert cli_config.json_response is False
