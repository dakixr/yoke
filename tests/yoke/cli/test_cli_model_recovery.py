"""CLI startup recovery without provider requests or changes to saved defaults."""

# ruff: noqa: ANN001, ANN002, ANN003, D103, S101

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from yoke.agent.models import Message
from yoke.ai.providers.plugins import ProviderPluginContext
from yoke.ai.providers.resolution import UnknownModelError
from yoke.cli.bootstrap.types import ResolvedAgentConfig, ToolLoadReport
from yoke.cli.config import CLIArgs
from yoke.cli.config import runtime as config_runtime
from yoke.cli.config import providers as provider_config
from yoke.cli.providers.state import apply_session_provider_defaults
from yoke.cli.providers.state import ProviderSessionState
from yoke.cli.runtime.cli import run_cli, run_resume_cli
from yoke.cli.session import SessionStore

from .support import CaptureStream


@pytest.fixture(autouse=True)
def isolate_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("ZAI_API_KEY", "OPENCODE_API_KEY", "YOKE_CODEX_API_KEY"):
        monkeypatch.setenv(key, "test-key")
    monkeypatch.setattr(config_runtime, "_load_cli_skill_registry", lambda _root: None)

    def resolve(**_kwargs) -> ResolvedAgentConfig:
        return ResolvedAgentConfig(
            system_messages=[Message.system("Test instructions.")],
            tools=[],
            tool_report=ToolLoadReport(
                discovered_tools=[], active_tools=[], denied_tools=[]
            ),
        )

    def reject_network(*_args, **_kwargs) -> None:
        pytest.fail("Startup must not issue a provider request")

    monkeypatch.setattr(config_runtime, "_resolve_cli_agent_config", resolve)
    monkeypatch.setattr(httpx.Client, "send", reject_network)


def write_default(root: Path, model: str) -> Path:
    path = root / ".yoke" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"default_model": model, "default_reasoning_effort": "low"}),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("scope", ["global", "repo"])
@pytest.mark.parametrize(
    ("provider_name", "default_model", "default_effort"),
    [
        ("zai", "glm-5.3-flash", "max"),
        ("opencode-go", "glm-5.3-flash", "max"),
        ("codex", "gpt-5.6-sol", "medium"),
    ],
)
def test_interactive_stale_default_opens_and_keeps_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    provider_name: str,
    default_model: str,
    default_effort: str,
) -> None:
    path = write_default(
        Path.home() if scope == "global" else tmp_path, f"{provider_name}:retired"
    )
    original = path.read_bytes()
    opened = []

    def interactive(args, agent, messages, **kwargs) -> int:
        opened.append(agent)
        assert args.provider_name == provider_name
        assert args.model == default_model
        assert args.reasoning_effort == default_effort
        assert agent.provider.config.model == default_model
        assert agent.provider.config.reasoning_effort == default_effort
        assert kwargs["active_session"].record.model_id == default_model
        assert [message.text_content() for message in messages] == ["Keep my prompt"]
        return 0

    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", interactive)
    stderr = CaptureStream()
    result = run_cli(
        CLIArgs(root=str(tmp_path), prompt="Keep my prompt"),
        stdout=CaptureStream(),
        stderr=stderr,
    )

    assert result == 0
    assert len(opened) == 1
    assert opened[0]._closed
    assert path.read_bytes() == original
    warning = " ".join(stderr.getvalue().split())
    assert warning.count("Warning:") == 1
    assert f"Configured model {provider_name}:retired" in warning
    assert f"Started with {provider_name}:{default_model}" in warning
    assert "/model" in warning


@pytest.mark.parametrize("headless", [False, True])
def test_explicit_unknown_model_does_not_recover(
    tmp_path: Path, headless: bool
) -> None:
    write_default(tmp_path, "zai:glm-5.3-flash")
    stderr = CaptureStream()
    args = CLIArgs(
        root=str(tmp_path), model="zai:retired", headless=headless, prompt="Do work"
    )

    assert run_cli(args, stdout=CaptureStream(), stderr=stderr) == 1
    assert args.model_source == "cli"
    assert "Unknown model 'retired'" in stderr.getvalue()
    assert "Warning:" not in stderr.getvalue()


def test_headless_config_default_does_not_recover(tmp_path: Path) -> None:
    path = write_default(tmp_path, "zai:retired")
    original = path.read_bytes()
    stderr = CaptureStream()

    assert (
        run_cli(
            CLIArgs(root=str(tmp_path), headless=True, prompt="Do work"),
            stdout=CaptureStream(),
            stderr=stderr,
        )
        == 1
    )
    assert "Unknown model 'retired'" in stderr.getvalue()
    assert "Warning:" not in stderr.getvalue()
    assert path.read_bytes() == original


def test_noninteractive_builder_remains_strict(tmp_path: Path) -> None:
    write_default(tmp_path, "zai:retired")

    with pytest.raises(UnknownModelError, match="Unknown model 'retired'"):
        config_runtime.build_cli_agent_from_args(CLIArgs(root=str(tmp_path)))


def test_resume_recovers_saved_model_without_losing_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_default(tmp_path, "opencode-go:muse-spark-1.3-contributor")
    original = path.read_bytes()
    store = SessionStore()
    store.save(
        "saved",
        [Message.user("old"), Message.assistant("answer")],
        root=tmp_path,
        provider_name="zai",
        model_id="retired",
        reasoning_effort="low",
    )
    opened = []

    def interactive(args, agent, messages, **kwargs) -> int:
        opened.append(agent)
        assert args.model_source == "session"
        assert agent.provider.provider_name == "zai"
        assert agent.provider.config.model == "glm-5.3-flash"
        assert agent.provider.config.reasoning_effort == "max"
        assert [message.text_content() for message in messages] == ["old", "answer"]
        assert kwargs["active_session"].id == "saved"
        assert kwargs["replay_session"]
        return 0

    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", interactive)
    stderr = CaptureStream()

    assert (
        run_resume_cli(
            CLIArgs(root=str(tmp_path)), "saved", stdout=CaptureStream(), stderr=stderr
        )
        == 0
    )
    assert len(opened) == 1
    assert opened[0]._closed
    assert "Saved model zai:retired" in " ".join(stderr.getvalue().split())
    assert path.read_bytes() == original
    assert [message.text_content() for message in store.load("saved").messages] == [
        "old",
        "answer",
    ]


@pytest.mark.parametrize("selection", ["zai:retired", "missing-provider:retired"])
def test_resume_explicit_invalid_selection_does_not_recover(
    tmp_path: Path, selection: str
) -> None:
    SessionStore().save(
        "saved",
        [Message.user("old")],
        root=tmp_path,
        provider_name="zai",
        model_id="retired",
    )
    stderr = CaptureStream()

    assert (
        run_resume_cli(
            CLIArgs(root=str(tmp_path), model=selection),
            "saved",
            stdout=CaptureStream(),
            stderr=stderr,
        )
        == 1
    )
    assert "Warning:" not in stderr.getvalue()
    assert "Falling back" not in stderr.getvalue()


@pytest.mark.parametrize("source", ["cli", "config", "session"])
def test_model_source_survives_repeated_preparation(
    tmp_path: Path, source: str
) -> None:
    args = CLIArgs(root=str(tmp_path))
    if source == "cli":
        args.model = "zai:glm-5.3-flash"
    elif source == "config":
        write_default(tmp_path, "zai:glm-5.3-flash")
    else:
        apply_session_provider_defaults(
            args, ProviderSessionState(provider_name="zai", model_id="glm-5.3-flash")
        )

    for _ in range(2):
        provider_config.prepare_provider_args(args)
        assert args.model_source == source
        assert args.model == "glm-5.3-flash"


@pytest.mark.parametrize(
    "error",
    [
        ValueError("Unknown model from an unrelated failure"),
        ValueError("Credentials unavailable"),
        RuntimeError("Network unavailable"),
    ],
)
def test_unrelated_provider_errors_do_not_trigger_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    write_default(tmp_path, "zai:glm-5.3-flash")
    attempts = []

    def fail(context: ProviderPluginContext):
        attempts.append(context.model)
        raise error

    monkeypatch.setitem(provider_config._BUILTIN_PROVIDER_FACTORIES, "zai", fail)
    stderr = CaptureStream()

    assert (
        run_cli(CLIArgs(root=str(tmp_path)), stdout=CaptureStream(), stderr=stderr) == 1
    )
    assert attempts == ["glm-5.3-flash"]
    assert str(error) in " ".join(stderr.getvalue().split())
    assert "Warning:" not in stderr.getvalue()


def test_failed_default_does_not_retry_or_switch_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_default(tmp_path, "zai:retired")
    attempts = []

    def fail(context: ProviderPluginContext):
        attempts.append((context.name, context.model, context.reasoning_effort))
        raise UnknownModelError("zai", context.model or "broken-default", [])

    monkeypatch.setitem(provider_config._BUILTIN_PROVIDER_FACTORIES, "zai", fail)
    stderr = CaptureStream()
    args = CLIArgs(root=str(tmp_path))

    assert run_cli(args, stdout=CaptureStream(), stderr=stderr) == 1
    assert attempts == [("zai", None, None)]
    assert args.model == "retired"
    assert "broken-default" in stderr.getvalue()
    assert "Warning:" not in stderr.getvalue()


def test_fresh_unavailable_provider_does_not_switch(tmp_path: Path) -> None:
    write_default(tmp_path, "removed-provider:retired")
    stderr = CaptureStream()

    assert (
        run_cli(CLIArgs(root=str(tmp_path)), stdout=CaptureStream(), stderr=stderr) == 1
    )
    assert "Unsupported provider" in stderr.getvalue()
    assert "Warning:" not in stderr.getvalue()


def test_valid_default_does_not_warn(tmp_path: Path) -> None:
    write_default(tmp_path, "zai:glm-5.3-flash")
    stderr = CaptureStream()

    assert (
        run_cli(
            CLIArgs(root=str(tmp_path)),
            input_func=lambda _prompt: "quit",
            stdout=CaptureStream(),
            stderr=stderr,
        )
        == 0
    )
    assert stderr.getvalue() == ""


def test_recovered_provider_is_closed_when_skills_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_default(tmp_path, "zai:retired")
    built_providers = []
    real_build = config_runtime.build_provider_from_args

    def build(args):
        provider = real_build(args)
        built_providers.append(provider)
        return provider

    def fail_skills(_root):
        raise ValueError("Broken skill registry")

    monkeypatch.setattr(config_runtime, "build_provider_from_args", build)
    monkeypatch.setattr(config_runtime, "_load_cli_skill_registry", fail_skills)
    stderr = CaptureStream()

    assert (
        run_cli(CLIArgs(root=str(tmp_path)), stdout=CaptureStream(), stderr=stderr) == 1
    )
    assert len(built_providers) == 1
    assert built_providers[0]._client.is_closed
    assert "Broken skill registry" in stderr.getvalue()
    assert "Warning:" not in stderr.getvalue()


@pytest.mark.parametrize("has_lister", [False, True])
def test_custom_provider_recovers_its_own_default_not_first_catalog_entry(
    tmp_path: Path, has_lister: bool
) -> None:
    plugin = Path.home() / ".yoke" / "providers" / "demo.py"
    plugin.parent.mkdir(parents=True)
    source = """
from yoke.ai.providers import OpenAICompatibleConfig, OpenAICompatibleProvider
from yoke.ai.providers.base import ProviderModelInfo

MODELS = (
    ProviderModelInfo(id="first", display_name="First", context_window_tokens=16000),
    ProviderModelInfo(
        id="current", display_name="Current", context_window_tokens=16000,
        thinking_levels=("low", "high"), default_thinking_level="high",
    ),
)

def register_provider(context):
    return OpenAICompatibleProvider(OpenAICompatibleConfig(
        api_key="test-key", provider_name=context.name,
        model=context.model or "current", model_catalog=MODELS,
        reasoning_effort=context.reasoning_effort,
    ))
"""
    if has_lister:
        source += "\ndef list_provider_models(context):\n    return list(MODELS)\n"
    plugin.write_text(source, encoding="utf-8")
    path = write_default(tmp_path, "demo:retired")
    original = path.read_bytes()
    stderr = CaptureStream()
    args = CLIArgs(root=str(tmp_path))

    assert (
        run_cli(
            args,
            input_func=lambda _prompt: "quit",
            stdout=CaptureStream(),
            stderr=stderr,
        )
        == 0
    )

    assert args.provider_name == "demo"
    assert args.model == "current"
    assert args.reasoning_effort == "high"
    assert "Started with demo:current" in " ".join(stderr.getvalue().split())
    assert path.read_bytes() == original


@pytest.mark.parametrize("mode", ["interactive", "explicit", "headless"])
def test_catalog_is_checked_even_when_custom_factory_ignores_model(
    tmp_path: Path, mode: str
) -> None:
    plugin = Path.home() / ".yoke" / "providers" / "demo.py"
    plugin.parent.mkdir(parents=True)
    factory_calls = tmp_path / "factory-calls.txt"
    plugin.write_text(
        f"""
from pathlib import Path
from yoke.ai.providers import OpenAICompatibleConfig, OpenAICompatibleProvider
from yoke.ai.providers.base import ProviderModelInfo
MODELS = (ProviderModelInfo(
    id="current", display_name="Current", context_window_tokens=16000,
),)
def list_provider_models(context):
    return list(MODELS)
def register_provider(context):
    with Path({str(factory_calls)!r}).open("a") as stream:
        stream.write(str(context.model) + "\\n")
    return OpenAICompatibleProvider(OpenAICompatibleConfig(
        api_key="test-key", provider_name=context.name, model="current",
        model_catalog=MODELS,
    ))
""",
        encoding="utf-8",
    )
    path = write_default(tmp_path, "demo:retired")
    original = path.read_bytes()
    args = CLIArgs(
        root=str(tmp_path),
        model="demo:retired" if mode == "explicit" else None,
        headless=mode == "headless",
        prompt="Do work" if mode == "headless" else None,
    )
    stderr = CaptureStream()

    result = run_cli(
        args,
        input_func=lambda _prompt: "quit",
        stdout=CaptureStream(),
        stderr=stderr,
    )

    if mode == "interactive":
        assert result == 0
        assert factory_calls.read_text() == "None\n"
        assert "Started with demo:current" in " ".join(stderr.getvalue().split())
    else:
        assert result == 1
        assert not factory_calls.exists()
        assert "Unknown model 'retired'" in stderr.getvalue()
        assert "Warning:" not in stderr.getvalue()
    assert path.read_bytes() == original


@pytest.mark.parametrize("fork", [False, True])
@pytest.mark.parametrize("saved_model", ["retired", "glm-5.3-flash"])
def test_named_and_forked_sessions_use_saved_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fork: bool, saved_model: str
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    destination = tmp_path / "destination"
    destination.mkdir()
    config_path = write_default(destination, "opencode-go:muse-spark-1.3-contributor")
    config_before = config_path.read_bytes()
    store = SessionStore()
    store.save(
        "saved",
        [Message.user("old"), Message.assistant("answer")],
        root=source_root,
        provider_name="zai",
        model_id=saved_model,
        reasoning_effort="low",
    )
    original = (store.directory / "saved.jsonl").read_bytes()
    opened = []

    def interactive(args, agent, messages, **kwargs) -> int:
        active = kwargs["active_session"]
        opened.append(active.id)
        assert args.model_source == "session"
        assert agent.provider.provider_name == "zai"
        assert agent.provider.config.model == "glm-5.3-flash"
        assert agent.provider.config.reasoning_effort == (
            "max" if saved_model == "retired" else "low"
        )
        assert active.root == (destination if fork else source_root)
        assert Path(args.root) == active.root
        assert active.record.model_id == "glm-5.3-flash"
        assert [message.text_content() for message in messages] == ["old", "answer"]
        return 0

    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", interactive)
    stderr = CaptureStream()
    args = CLIArgs(
        root=str(destination),
        session=None if fork else "saved",
        fork_session_id="saved" if fork else None,
    )

    assert run_cli(args, stdout=CaptureStream(), stderr=stderr) == 0
    assert len(opened) == 1
    assert (opened[0] != "saved") is fork
    warning = " ".join(stderr.getvalue().split())
    if saved_model == "retired":
        assert "Saved model zai:retired" in warning
        assert "Resumed with zai:glm-5.3-flash" in warning
    else:
        assert warning == ""
    if fork:
        assert (store.directory / "saved.jsonl").read_bytes() == original
    assert config_path.read_bytes() == config_before


@pytest.mark.parametrize("fork", [False, True])
@pytest.mark.parametrize("headless", [False, True])
def test_strict_continuation_failure_does_not_modify_or_fork_source(
    tmp_path: Path, fork: bool, headless: bool
) -> None:
    store = SessionStore()
    store.save(
        "saved",
        [Message.user("old")],
        root=tmp_path,
        provider_name="zai",
        model_id="retired",
    )
    path = store.directory / "saved.jsonl"
    original = path.read_bytes()
    args = CLIArgs(
        root=str(tmp_path),
        session=None if fork else "saved",
        fork_session_id="saved" if fork else None,
        headless=headless,
        prompt="Do work" if headless else None,
        model=None if headless else "zai:explicit-missing",
    )
    stderr = CaptureStream()

    assert run_cli(args, stdout=CaptureStream(), stderr=stderr) == 1

    assert "Unknown model" in stderr.getvalue()
    assert "Warning:" not in stderr.getvalue()
    assert path.read_bytes() == original
    assert list(store.directory.glob("*.jsonl")) == [path]


@pytest.mark.parametrize("fork", [False, True])
def test_explicit_continuation_selection_and_effort_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fork: bool
) -> None:
    SessionStore().save(
        "saved",
        [Message.user("old")],
        root=tmp_path,
        provider_name="zai",
        model_id="retired",
        reasoning_effort="low",
    )

    def interactive(args, agent, _messages, **_kwargs) -> int:
        assert args.model_source == "cli"
        assert agent.provider.provider_name == "opencode-go"
        assert agent.provider.config.model == "glm-5.3-flash"
        assert agent.provider.config.reasoning_effort == "high"
        return 0

    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", interactive)
    args = CLIArgs(
        root=str(tmp_path),
        session=None if fork else "saved",
        fork_session_id="saved" if fork else None,
        model="opencode-go:glm-5.3-flash:high",
    )
    stderr = CaptureStream()

    assert run_cli(args, stdout=CaptureStream(), stderr=stderr) == 0
    assert stderr.getvalue() == ""
