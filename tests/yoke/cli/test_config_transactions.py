"""Regression tests for transactional CLI config updates."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
from threading import Barrier
from typing import Iterator

import pytest
import typer

from yoke.cli import models_app
from yoke.cli.interactive import tools_menu
from yoke.cli.models_app import set_default_model
from yoke.cli.tools import app as tools_app
from yoke.cli.tools import policy as config_policy
from yoke.cli.tools.policy import default_yoke_config
from yoke.cli.tools.policy import load_config_file


def _avoid_model_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        models_app, "list_provider_models", lambda *_args, **_kwargs: None
    )


def test_concurrent_model_and_tool_commands_preserve_both_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / ".yoke" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '{"capabilities":{"file.read":"deny"},"tools":{"unrelated":"allow"}}\n',
        encoding="utf-8",
    )
    _avoid_model_discovery(monkeypatch)
    real_lock = config_policy.exclusive_file_lock
    writers_ready = Barrier(2)

    @contextmanager
    def synchronized_lock(path: Path) -> Iterator[None]:
        writers_ready.wait(timeout=5)
        with real_lock(path):
            yield

    monkeypatch.setattr(config_policy, "exclusive_file_lock", synchronized_lock)

    with ThreadPoolExecutor(max_workers=2) as executor:
        model_update = executor.submit(
            set_default_model,
            "demo:new-model",
            root=tmp_path,
            reasoning_effort="high",
            repo_scope=True,
        )
        tool_update = executor.submit(
            tools_app.tools_activate,
            "shell",
            root=tmp_path,
            global_scope=False,
            repo_scope=True,
            tool_override=False,
        )
        model_update.result(timeout=10)
        tool_update.result(timeout=10)

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted == {
        "capabilities": {"file.read": "deny", "shell": "allow"},
        "tools": {"unrelated": "allow"},
        "default_model": "demo:new-model",
        "default_reasoning_effort": "high",
    }


@pytest.mark.parametrize("entrypoint", ["model", "tool"])
def test_atomic_replacement_failure_keeps_legacy_config_bytes(
    entrypoint: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / ".yoke" / "config.json"
    config_path.parent.mkdir(parents=True)
    original = (
        b'{"capabilities":{"shell":"deny"},'
        b'"tools":{"*":"deny","special":"allow"},'
        b'"default_model":"demo:old"}\n'
    )
    config_path.write_bytes(original)
    _avoid_model_discovery(monkeypatch)
    real_replace = Path.replace

    def fail_config_replace(source: Path, target: Path) -> Path:
        if target == config_path and source.name.endswith(".tmp"):
            raise OSError("injected config replacement failure")
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_config_replace)

    with pytest.raises(OSError, match="injected config replacement failure"):
        if entrypoint == "model":
            set_default_model(
                "demo:new",
                root=tmp_path,
                repo_scope=True,
            )
        else:
            tools_app.tools_activate(
                "shell",
                root=tmp_path,
                global_scope=False,
                repo_scope=True,
                tool_override=False,
            )

    assert config_path.read_bytes() == original
    assert not list(config_path.parent.glob(f".{config_path.name}.*.tmp"))


def test_malformed_json_is_rejected_consistently_by_config_entrypoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / ".yoke" / "config.json"
    config_path.parent.mkdir(parents=True)
    original = b'{"capabilities":{"shell":"allow"}\n'
    config_path.write_bytes(original)
    _avoid_model_discovery(monkeypatch)

    with pytest.raises(ValueError) as model_error:
        set_default_model("demo:new", root=tmp_path, repo_scope=True)
    with pytest.raises(typer.Exit) as tool_error:
        tools_app.tools_deactivate(
            "shell",
            root=tmp_path,
            global_scope=False,
            repo_scope=True,
            tool_override=False,
        )
    tool_message = capsys.readouterr().err.strip()
    with pytest.raises(ValueError) as menu_error:
        tools_menu._write_tool_policy_config(
            config_path,
            rows=[],
            active_names=set(),
        )

    assert tool_error.value.exit_code == 1
    assert tool_message == str(model_error.value) == str(menu_error.value)
    assert "Invalid JSON syntax" in tool_message
    assert config_path.read_bytes() == original
    assert not list(config_path.parent.glob(f".{config_path.name}.*.tmp"))


def test_model_mutation_is_validated_before_config_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / ".yoke" / "config.json"
    config_path.parent.mkdir(parents=True)
    original = b'{"tools":{"unrelated":"allow"}}\n'
    config_path.write_bytes(original)
    _avoid_model_discovery(monkeypatch)
    monkeypatch.setattr(
        models_app,
        "parse_provider_model_identifier",
        lambda _model: ("demo", ""),
    )

    with pytest.raises(ValueError, match="both parts non-empty"):
        set_default_model("demo:new", root=tmp_path, repo_scope=True)

    assert config_path.read_bytes() == original
    assert not list(config_path.parent.glob(f".{config_path.name}.*.tmp"))


def test_legacy_glob_load_keeps_established_destructive_migration(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"capabilities":{"shell":"deny"},'
        '"tools":{"*":"deny","special":"allow"},'
        '"default_model":"demo:old",'
        '"default_reasoning_effort":"high"}\n',
        encoding="utf-8",
    )

    loaded = load_config_file(config_path)
    expected = default_yoke_config()

    assert loaded.config.capabilities == expected.capabilities
    assert loaded.config.tools == {}
    assert loaded.config.default_model == "demo:old"
    assert loaded.config.default_reasoning_effort == "high"
    assert load_config_file(config_path).config == loaded.config


@pytest.mark.parametrize("entrypoint", ["model", "tool", "menu"])
def test_config_edits_preserve_their_existing_legacy_migration_policy(
    entrypoint: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".yoke" / "config.json"
    path.parent.mkdir()
    path.write_text(
        '{"capabilities":{"shell":"deny"},"tools":{"*":"deny","special":"allow"}}'
    )
    _avoid_model_discovery(monkeypatch)

    if entrypoint == "model":
        set_default_model("demo:new", root=tmp_path, repo_scope=True)
    elif entrypoint == "tool":
        tools_app.tools_activate(
            "shell",
            root=tmp_path,
            global_scope=False,
            repo_scope=True,
            tool_override=False,
        )
    else:
        tools_menu._write_tool_policy_config(path, rows=[], active_names=set())

    persisted = json.loads(path.read_text())
    if entrypoint == "menu":
        assert persisted["tools"] == {}
        assert (
            persisted["capabilities"]
            == default_yoke_config().model_dump(mode="json")["capabilities"]
        )
    else:
        assert persisted["tools"] == {"*": "deny", "special": "allow"}
        assert persisted["capabilities"] == {
            "shell": "allow" if entrypoint == "tool" else "deny"
        }


def test_model_update_preserves_a_symlinked_config_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "managed" / "yoke.json"
    target.parent.mkdir()
    target.write_text('{"tools":{"special":"allow"}}')
    link = tmp_path / ".yoke" / "config.json"
    link.parent.mkdir()
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"Symlinks unavailable: {exc}")
    _avoid_model_discovery(monkeypatch)

    set_default_model("demo:new", root=tmp_path, repo_scope=True)

    assert link.is_symlink()
    assert json.loads(target.read_text())["default_model"] == "demo:new"
    assert json.loads(target.read_text())["tools"] == {"special": "allow"}


def test_invalid_utf8_config_error_names_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".yoke" / "config.json"
    path.parent.mkdir()
    path.write_bytes(b"\xff")
    _avoid_model_discovery(monkeypatch)

    with pytest.raises(ValueError) as caught:
        set_default_model("demo:new", root=tmp_path, repo_scope=True)

    assert str(path) in str(caught.value)
    assert path.read_bytes() == b"\xff"
