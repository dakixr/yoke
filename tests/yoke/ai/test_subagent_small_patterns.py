"""Run the actual Markdown examples, including tool spawning and process restarts."""

from __future__ import annotations

import asyncio
import ast
from pathlib import Path
import re
import runpy
import subprocess
import sys
from types import ModuleType

import pytest

from yoke.agent.models import Message

PATTERNS = (
    Path(__file__).resolve().parents[3]
    / "src/yoke/agent/skills/built_in/yoke-subagents/PATTERNS.md"
)


def example_source(name: str) -> str:
    for source in re.findall(r"```python\n(.*?)\n```", PATTERNS.read_text(), re.S):
        definitions = ast.parse(source).body
        if any(
            isinstance(node, ast.AsyncFunctionDef) and node.name == name
            for node in definitions
        ):
            return source
    raise AssertionError(f"Missing executable example: {name}")


def example(name: str) -> ModuleType:
    module = ModuleType(name)
    exec(compile(example_source(name), str(PATTERNS), "exec"), module.__dict__)
    return module


class RecordingProvider:
    supports_image_inputs = False
    max_images_per_message = None

    def __init__(self) -> None:
        self.transcripts: list[list[Message]] = []
        self.tools: list[dict[str, object]] = []
        self.closed = False

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.transcripts.append(list(messages))
        self.tools = tools
        prompt = next(
            message.text_content()
            for message in reversed(messages)
            if message.role == "user"
        )
        if prompt == "fail":
            raise RuntimeError("deliberate provider failure")
        return Message.assistant(f"Answer to {prompt}")

    def close(self) -> None:
        self.closed = True


def record_builds(module: ModuleType, monkeypatch: pytest.MonkeyPatch):
    created: list[RecordingProvider] = []
    sessions: list[str | None] = []

    def build(_selection: str, *, session_id: str | None = None) -> RecordingProvider:
        provider = RecordingProvider()
        created.append(provider)
        sessions.append(session_id)
        return provider

    monkeypatch.setattr(module, "build_builtin_provider", build)
    return created, sessions


def test_single_worker_keeps_followups_in_one_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = example("ask")
    providers, _ = record_builds(module, monkeypatch)
    asyncio.run(module.ask(tmp_path, "test:model", ["first question", "follow-up"]))
    assert len(providers) == 1 and providers[0].closed
    assert len(providers[0].transcripts) == 2
    assert any(
        message.content == "first question" for message in providers[0].transcripts[-1]
    )
    names = {
        function["name"]
        for tool in providers[0].tools
        if isinstance(function := tool.get("function"), dict)
    }
    assert "read" in names and not names & {
        "apply_patch",
        "write",
        "command_exec",
        "web_search",
    }
    assert not (tmp_path / ".agents_local").exists()


@pytest.mark.parametrize("name", ["ask", "audit"])
def test_empty_requests_build_no_providers(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = example(name)
    providers, _ = record_builds(module, monkeypatch)
    with pytest.raises(ValueError):
        asyncio.run(getattr(module, name)(tmp_path, "test:model", []))
    assert not providers


@pytest.mark.parametrize("name", ["ask", "audit"])
def test_small_failures_close_owners_and_keep_successful_answers(
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = example(name)
    providers, _ = record_builds(module, monkeypatch)
    with pytest.raises(RuntimeError):
        asyncio.run(getattr(module, name)(tmp_path, "test:model", ["good", "fail"]))
    assert all(provider.closed for provider in providers)
    assert "Answer to good" in capsys.readouterr().out
    assert len(providers) == (1 if name == "ask" else 2)
    assert not (tmp_path / ".agents_local").exists()


@pytest.mark.parametrize("name", ["ask", "audit", "continue_role"])
def test_small_examples_print_complete_answers(
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = example(name)
    marker = "END_MARKER"
    answer = "x" * 2500 + marker

    class LongProvider(RecordingProvider):
        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            self.transcripts.append(list(messages))
            self.tools = tools
            return Message.assistant(answer)

    providers: list[LongProvider] = []

    def build(_selection: str, *, session_id: str | None = None) -> LongProvider:
        del session_id
        provider = LongProvider()
        providers.append(provider)
        return provider

    monkeypatch.setattr(module, "build_builtin_provider", build)
    if name == "continue_role":
        state = tmp_path / ".agents_local" / "reviewer.json"
        asyncio.run(module.continue_role(tmp_path, "test:model", state, "review"))
    else:
        asyncio.run(getattr(module, name)(tmp_path, "test:model", ["review"]))
    output = capsys.readouterr().out
    assert marker in output
    assert answer in output
    assert all(provider.closed for provider in providers)


def test_durable_role_restores_context_without_a_pair_or_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = example("continue_role")
    providers, sessions = record_builds(module, monkeypatch)
    state = tmp_path / ".agents_local" / "one-role.json"
    asyncio.run(
        module.continue_role(tmp_path, "test:model", state, "Keep empty records")
    )
    saved = state.read_bytes()
    asyncio.run(
        module.continue_role(tmp_path, "test:model", state, "What was our decision?")
    )
    assert len(providers) == 2 and all(provider.closed for provider in providers)
    assert sessions[0] == sessions[1] and sessions[0]
    assert any(
        message.content == "Keep empty records"
        for message in providers[1].transcripts[0]
    )
    assert state.read_bytes() != saved
    assert list(state.parent.iterdir()) == [state]

    saved = state.read_bytes()
    with pytest.raises(RuntimeError, match="deliberate"):
        asyncio.run(module.continue_role(tmp_path, "test:model", state, "fail"))
    assert providers[-1].closed and state.read_bytes() == saved

    other = state.with_name("unrelated-role.json")
    asyncio.run(module.continue_role(tmp_path, "test:model", other, "A new job"))
    assert sessions[-1] != sessions[0]
    assert not any(
        message.content == "Keep empty records"
        for message in providers[-1].transcripts[0]
    )


@pytest.mark.parametrize("name", ["ask", "audit", "continue_role"])
def test_small_examples_are_import_safe(name: str, tmp_path: Path) -> None:
    path = tmp_path / "example.py"
    path.write_text(example_source(name))
    runpy.run_path(str(path), run_name="__mp_main__")
    assert not (tmp_path / ".agents_local").exists()


@pytest.mark.parametrize(
    "name,args,usage",
    [
        ("ask", [], "usage: ask.py ROOT SELECTION PROMPT [PROMPT ...]"),
        ("audit", [], "usage: audit.py ROOT SELECTION PROMPT [PROMPT ...]"),
        ("continue_role", [], "usage: durable.py ROOT SELECTION STATE PROMPT"),
    ],
)
def test_small_examples_have_concise_cli_usage(
    name: str,
    args: list[str],
    usage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "example.py"
    path.write_text(example_source(name))
    monkeypatch.setattr(sys, "argv", [str(path), *args])
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(path), run_name="__main__")
    code = exited.value.code
    if isinstance(code, str):
        print(code, file=sys.stderr)
        code = 1
    captured = capsys.readouterr()
    completed: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
        sys.argv, code or 0, captured.out, captured.err
    )
    assert completed.returncode != 0
    assert usage in completed.stderr
    assert "Traceback" not in completed.stderr
