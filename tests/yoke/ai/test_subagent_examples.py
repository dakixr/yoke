"""Execute the shipped skill examples with real SDK agents and deterministic providers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic_core import PydanticSerializationError

from yoke.agent.models import Message
from yoke.ai import BatchUsage, ConsoleObserver

PREFIX = "yoke.agent.skills.built_in.yoke-subagents.scripts"
support = importlib.import_module(f"{PREFIX}.orchestration_support")
fan = importlib.import_module(f"{PREFIX}.fan_out")
pair = importlib.import_module(f"{PREFIX}.review_pair")


class ScriptedProvider:
    supports_image_inputs = False
    max_images_per_message = None

    def __init__(self, answer: Callable[[str, int], str]) -> None:
        self.answer = answer
        self.prompts: list[str] = []
        self.transcripts: list[list[Message]] = []
        self.tools: list[dict[str, object]] = []
        self.closed = False

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.transcripts.append(list(messages))
        self.tools = tools
        prompt = str(
            next(
                message.content
                for message in reversed(messages)
                if message.role == "user"
            )
        )
        self.prompts.append(prompt)
        return Message.assistant(self.answer(prompt, len(self.prompts)))

    def close(self) -> None:
        self.closed = True


def task(task_id: str = "parser-fix"):
    return support.TaskSpec(
        id=task_id,
        request="Implement exact parser behavior from SPEC.md.",
        scope=["parser.py", "tests/test_parser.py"],
        acceptance=["Empty input is handled without dropping a record."],
    )


def findings(*, blocker: str | None = None) -> str:
    return json.dumps(
        {
            "summary": "Inspected the parser.",
            "evidence": ["parser.py:3"],
            "validation": ["Not executed; parent should run pytest."],
            "blocker": blocker,
        }
    )


@pytest.mark.parametrize("failure", ["transport", "blocker", "progress"])
def test_fan_out_preserves_mixed_results_and_closes_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    providers: list[ScriptedProvider] = []

    def answer(prompt: str, _call: int) -> str:
        if '"id": "second"' in prompt:
            if failure == "transport":
                raise RuntimeError("planned provider failure")
            if failure == "blocker":
                return findings(blocker="Required source is missing")
        return findings()

    def build(_selection: str) -> ScriptedProvider:
        provider = ScriptedProvider(answer)
        providers.append(provider)
        return provider

    monkeypatch.setattr(fan, "build_builtin_provider", build)
    if failure == "progress":
        real_run_many = fan.run_many

        def broken_progress(_progress: object) -> None:
            raise RuntimeError("planned progress failure")

        async def with_progress(tasks, **kwargs):
            return await real_run_many(tasks, on_progress=broken_progress, **kwargs)

        monkeypatch.setattr(fan, "run_many", with_progress)

    payload = asyncio.run(
        fan.audit(
            [task("first"), task("second")],
            root=tmp_path,
            selection="test:model:medium",
            observer=ConsoleObserver("quiet"),
            max_concurrency=2,
        )
    )
    assert payload["status"] == "needs_main_agent"
    assert [item["id"] for item in payload["items"]] == ["first", "second"]
    assert payload["items"][0]["findings"].evidence == ["parser.py:3"]
    assert len(providers) == 2 and all(provider.closed for provider in providers)
    assert all(item["attempts"] == 1 for item in payload["items"])
    if failure == "transport":
        assert payload["items"][1]["status"] == "error"
        assert "planned provider failure" in payload["items"][1]["error"]
    elif failure == "blocker":
        assert payload["items"][1]["status"] == "completed"
        assert payload["items"][1]["findings"].blocker
    else:
        assert len(payload["progress_errors"]) == 2
    assert support.finish(tmp_path, payload) == 1
    retained = json.loads((tmp_path / "results.json").read_text())
    assert len(retained["items"]) == 2


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("codebase", ["file.read", "file.search"]),
        ("web", ["web.fetch", "web.search", "web.research"]),
        (
            "mixed",
            ["file.read", "file.search", "web.fetch", "web.search", "web.research"],
        ),
    ],
)
def test_research_capabilities_are_selected_not_fanned_out(
    mode: str, expected: list[str]
) -> None:
    assert support.read_tools(mode) == expected


@pytest.mark.parametrize(
    "ids,concurrency", [([], 1), (["same", "same"], 1), (["one"], 17)]
)
def test_bad_batch_inputs_fail_before_building_workers(
    tmp_path: Path,
    ids: list[str],
    concurrency: int,
) -> None:
    with pytest.raises(ValueError):
        asyncio.run(
            fan.audit(
                [task(identifier) for identifier in ids],
                root=tmp_path,
                selection="unavailable:model",
                observer=ConsoleObserver("quiet"),
                max_concurrency=concurrency,
            )
        )


@pytest.mark.parametrize("accepted_on", [1, 2, 3, None])
def test_pair_has_no_unreviewed_final_revision_and_keeps_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    accepted_on: int | None,
) -> None:
    providers: dict[str, ScriptedProvider] = {}
    session_ids: list[str] = []

    def build(_selection: str, *, session_id: str) -> ScriptedProvider:
        role = session_id.rsplit("-", 1)[1]
        session_ids.append(session_id)

        def answer(_prompt: str, call: int) -> str:
            if role == "coder":
                return f"Revision {call}. Changed parser.py. Parent must run pytest."
            return json.dumps(
                {
                    "verdict": "ok" if call == accepted_on else "nok",
                    "evidence": ["parser.py:3"],
                    "feedback": [f"Fix issue {call}"],
                    "risks": ["Tests not executed"],
                }
            )

        provider = ScriptedProvider(answer)
        providers[role] = provider
        return provider

    monkeypatch.setattr(pair, "build_builtin_provider", build)
    payload = asyncio.run(
        pair.review_pair(
            task(),
            root=tmp_path,
            selection="test:model:medium",
            run_dir=tmp_path,
        )
    )
    expected_turns = accepted_on or 3
    assert (
        len(providers["coder"].prompts)
        == len(providers["reviewer"].prompts)
        == expected_turns
    )
    assert payload["latest_output_reviewed"] is True
    assert payload["status"] == ("accepted" if accepted_on else "needs_main_agent")
    assert payload["history"][-1]["review"].risks == ["Tests not executed"]
    for provider in providers.values():
        assert provider.closed
        assert "Implement exact parser behavior" in provider.prompts[0]
        assert "tests/test_parser.py" in provider.prompts[0]
        assert "Empty input is handled" in provider.prompts[0]
    assert len(set(session_ids)) == 2
    assert (tmp_path / "parser-fix.coder.json").exists()
    assert (tmp_path / "parser-fix.reviewer.json").exists()


def test_reviewer_construction_failure_closes_coder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coder = ScriptedProvider(lambda _prompt, _call: "unused")
    monkeypatch.setattr(pair, "build_builtin_provider", lambda *_args, **_kwargs: coder)
    make_role = pair.role_agent

    def fail_reviewer(role: str, *args):
        if role == "reviewer":
            raise ValueError("Corrupt reviewer snapshot")
        return make_role(role, *args)

    monkeypatch.setattr(pair, "role_agent", fail_reviewer)
    payload = asyncio.run(
        pair.review_pair(
            task(),
            root=tmp_path,
            selection="test:model",
            run_dir=tmp_path,
        )
    )
    assert payload["status"] == "error"
    assert "Corrupt reviewer snapshot" in payload["error"]
    assert coder.closed and not coder.prompts


def test_resume_requires_same_contract_and_restores_role_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[str, ScriptedProvider]] = []

    def build(_selection: str, *, session_id: str) -> ScriptedProvider:
        answer = "Changed parser.py"
        if session_id.endswith("reviewer"):
            answer = (
                '{"verdict":"ok","evidence":["parser.py:3"],"feedback":[],"risks":[]}'
            )
        provider = ScriptedProvider(lambda _prompt, _call: answer)
        created.append((session_id, provider))
        return provider

    monkeypatch.setattr(pair, "build_builtin_provider", build)
    kwargs = {"root": tmp_path, "selection": "test:model", "run_dir": tmp_path}
    assert asyncio.run(pair.review_pair(task(), **kwargs))["status"] == "accepted"
    with pytest.raises(ValueError, match="explicit resume"):
        asyncio.run(pair.review_pair(task(), **kwargs))
    with pytest.raises(ValueError, match="same task"):
        asyncio.run(pair.review_pair(task("unrelated"), resume=True, **kwargs))
    assert (
        asyncio.run(pair.review_pair(task(), resume=True, **kwargs))["status"]
        == "accepted"
    )
    assert created[0][0] == created[2][0] and created[1][0] == created[3][0]
    assert len(created[2][1].transcripts[0]) > len(created[0][1].transcripts[0])
    assert all(provider.closed for _, provider in created)


def test_json_handles_nested_models_paths_usage_and_rejects_unknown_objects(
    tmp_path: Path,
) -> None:
    path = tmp_path / "result.json"
    support.write_json(
        path, {"tasks": [task()], "path": tmp_path, "usage": BatchUsage(calls=2)}
    )
    original = path.read_text()
    retained = json.loads(original)
    assert retained["tasks"][0]["id"] == "parser-fix"
    assert retained["path"] == str(tmp_path) and retained["usage"]["calls"] == 2
    with pytest.raises(PydanticSerializationError):
        support.write_json(path, object())
    assert path.read_text() == original


def test_preflight_rejects_effort_fallback_and_closes_validation_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider(lambda _prompt, _call: "unused")
    built: list[str] = []
    model = SimpleNamespace(id="model", thinking_levels=("medium",))
    monkeypatch.setattr(
        support,
        "builtin_provider_status",
        lambda: [
            SimpleNamespace(
                name="test", ready=True, models=[SimpleNamespace(model=model)]
            ),
        ],
    )

    def build(selection: str) -> ScriptedProvider:
        built.append(selection)
        return provider

    monkeypatch.setattr(support, "build_builtin_provider", build)
    with pytest.raises(ValueError, match="Unsupported thinking"):
        asyncio.run(support.preflight("test:model:invented", tmp_path))
    assert not built
    asyncio.run(support.preflight("test:model:medium", tmp_path))
    assert built == ["test:model:medium"] and provider.closed and not provider.prompts


def test_run_namespaces_require_explicit_resume(tmp_path: Path) -> None:
    first = support.prepare_run(tmp_path, "first-job")
    with pytest.raises(FileExistsError):
        support.prepare_run(tmp_path, "first-job")
    assert support.prepare_run(tmp_path, "first-job", resume=True) == first
    assert support.prepare_run(tmp_path, "other-job") != first
    with pytest.raises(ValueError, match="missing"):
        support.prepare_run(tmp_path, "missing-job", resume=True)
    with pytest.raises(ValueError, match="filename-safe"):
        support.prepare_run(tmp_path, "../outside")


@pytest.mark.parametrize("filename", ["fan_out.py", "review_pair.py"])
@pytest.mark.parametrize("mode", ["help", "spawn-import"])
def test_copied_examples_support_direct_and_multiprocessing_entrypoints(
    tmp_path: Path,
    filename: str,
    mode: str,
) -> None:
    assert support.__file__ is not None
    scripts = Path(support.__file__).parent
    copied = tmp_path / "scripts"
    shutil.copytree(scripts, copied, ignore=shutil.ignore_patterns("__pycache__"))
    script = copied / filename
    command = [sys.executable, str(script), "--help"]
    if mode == "spawn-import":
        # This is how multiprocessing prepares a script's child interpreter.
        command = [
            sys.executable,
            "-c",
            (f"import runpy; runpy.run_path({str(script)!r}, run_name='__mp_main__')"),
        ]
    completed = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert not (tmp_path / ".agents_local").exists()


def test_malformed_review_is_an_error_not_an_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers: dict[str, ScriptedProvider] = {}

    def build(_selection: str, *, session_id: str) -> ScriptedProvider:
        role = session_id.rsplit("-", 1)[1]
        output = "not valid JSON" if role == "reviewer" else "Changed parser.py"
        provider = ScriptedProvider(lambda _prompt, _call: output)
        providers[role] = provider
        return provider

    monkeypatch.setattr(pair, "build_builtin_provider", build)
    payload = asyncio.run(
        pair.review_pair(
            task(),
            root=tmp_path,
            selection="test:model",
            run_dir=tmp_path,
        )
    )
    assert payload["status"] == "error" and not payload["latest_output_reviewed"]
    assert not payload["history"]
    assert "StructuredOutputError" in payload["error"]
    assert len(providers["coder"].prompts) == 1
    assert len(providers["reviewer"].prompts) == 3
    assert all(provider.closed for provider in providers.values())


def test_rejected_cli_resume_preserves_last_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = support.prepare_run(tmp_path, "existing-run")
    support.write_json(run_dir / "results.json", {"status": "accepted", "prior": True})
    support.write_json(run_dir / "contract.json", {"unrelated": "contract"})
    task_file = tmp_path / "task.json"
    task_file.write_text(task().model_dump_json())

    async def checked_selection(_selection: str, _root: Path) -> None:
        pass

    monkeypatch.setattr(pair, "preflight", checked_selection)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "review_pair.py",
            "--root",
            str(tmp_path),
            "--selection",
            "test:model",
            "--task",
            str(task_file),
            "--run-id",
            "existing-run",
            "--resume",
        ],
    )
    assert asyncio.run(pair.main()) == 1
    assert json.loads((run_dir / "results.json").read_text())["prior"] is True
    assert (
        "same task" in json.loads((run_dir / "resume-error.json").read_text())["error"]
    )
