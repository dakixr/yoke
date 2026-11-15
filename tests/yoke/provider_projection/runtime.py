"""Identical public runtime configuration injected into both oracle paths."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from PIL import Image

from yoke.agent.context import CompactionPolicy, ContextManager
from yoke.agent.loop import RuntimeAgent
from yoke.agent.instructions import build_system_messages
from yoke.agent.skills.models import SkillSpec
from yoke.agent.skills.registry import SkillRegistry
from yoke.agent.tools import ExecCommandTool, ReadTool, RipgrepTool, WriteTool
from yoke.cli.config import DEFAULT_SYSTEM_PROMPT
from yoke.session import SessionRecord

from .recording import Gate, RecordingProvider, Tape


class World:
    """Own only test configuration; both paths construct identical runtime inputs."""

    def __init__(self, root: Path, directory: Path, scenario: str, tape: Tape):
        self.root = root
        self.directory = directory
        self.scenario = scenario
        self.tape = tape
        root.mkdir(parents=True, exist_ok=True)
        (root / "AGENTS.md").write_text(
            "Keep input.txt intact.\nVerify commands before finishing.\n",
            encoding="utf-8",
        )
        (root / "input.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        Image.new("RGB", (2, 2), color="red").save(root / "tiny.png")
        self.skills = []
        for name in ("oracle", "review"):
            skill_root = root / ".yoke" / "skills" / name
            skill_root.mkdir(parents=True, exist_ok=True)
            path = skill_root / "SKILL.md"
            path.write_text(
                f"---\nname: {name}\ndescription: {name} instructions\n---\n"
                f"Preserve the {name} constraint exactly.\n",
                encoding="utf-8",
            )
            self.skills.append(
                SkillSpec(
                    name=name,
                    description=f"{name} instructions",
                    root=skill_root,
                    skill_md_path=path,
                )
            )

    def agent(self, _record: SessionRecord) -> RuntimeAgent:
        tools = []
        if self.scenario in {"tools", "parallel_tools"}:
            tools = [
                RipgrepTool.bind(root=self.root),
                ReadTool.bind(root=self.root),
                WriteTool.bind(root=self.root),
                ExecCommandTool.bind(root=self.root),
            ]
        elif self.scenario == "checkpoint_stop":
            tools = [ReadTool.bind(root=self.root)]
        budget = 5000 if self.scenario == "automatic_compaction" else 100_000
        provider = RecordingProvider(
            self.tape,
            images=not self.scenario.endswith("images_unsupported"),
            budget=budget,
        )
        # Production's provider builder receives CLIArgs.session on both paths.
        provider.set_session_id(_record.id)
        return RuntimeAgent(
            provider=provider,
            tools=tools,
            tool_root=self.root,
            context_manager=ContextManager(
                instructions=build_system_messages(
                    root=self.root,
                    base_system_prompt=DEFAULT_SYSTEM_PROMPT,
                ),
                compaction_policy=CompactionPolicy(
                    max_total_tokens=budget,
                    reserved_output_tokens=100,
                    keep_recent_tokens=120,
                    recent_user_tokens=120,
                ),
            ),
            skill_registry=SkillRegistry(self.skills),
            available_skills=self.skills
            if self.scenario in {"inline_skill", "slash_skill"}
            else [],
        )


class Driver(Protocol):
    world: World
    session_id: str

    def prompt(
        self, text: str, *, image: bool = False, gate: Gate | None = None
    ) -> None: ...
    def compact(self) -> None: ...
    def activate(self) -> None: ...
    def navigate(self, target: str, *, summarize: bool = False) -> None: ...
    def fork(self) -> None: ...
    def title(self) -> None: ...
    def close(self) -> None: ...
