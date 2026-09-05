"""Snapshot failures retain the documented errors and previous saved state."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from yoke.agent.persistence.io import read_agent_state_snapshot
from yoke.agent.persistence.io import write_agent_state_snapshot
from yoke.agent.persistence.models import AgentStateLoadError, AgentStateSaveError
from yoke.agent.state import AgentState


def test_invalid_utf8_snapshot_raises_load_error(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b'{"metadata": "\xff"}')

    with pytest.raises(AgentStateLoadError) as caught:
        read_agent_state_snapshot(path)

    assert str(path) in str(caught.value)
    assert isinstance(caught.value.__cause__, UnicodeDecodeError)


def test_missing_snapshot_retains_file_not_found_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_agent_state_snapshot(tmp_path / "missing.json")


@pytest.mark.parametrize("atomic", [False, True])
def test_unserializable_metadata_preserves_saved_snapshot(
    tmp_path: Path, atomic: bool
) -> None:
    path = tmp_path / "state.json"
    write_agent_state_snapshot(path, AgentState(), metadata={"saved": True})
    previous = path.read_bytes()

    with pytest.raises(AgentStateSaveError) as caught:
        write_agent_state_snapshot(
            path, AgentState(), metadata={"bad": object()}, atomic=atomic
        )

    assert str(path) in str(caught.value)
    assert path.read_bytes() == previous


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_replacement_failure_preserves_snapshot_and_primary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool
) -> None:
    path = tmp_path / "state.json"
    write_agent_state_snapshot(path, AgentState(), metadata={"saved": True})
    previous = path.read_bytes()
    primary = OSError("replace denied")

    def fail_replace(_source, _target) -> None:
        raise primary

    def fail_cleanup(_path, *args, **kwargs) -> None:
        raise OSError("cleanup denied")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_replace)
        if cleanup_fails:
            patch.setattr(Path, "unlink", fail_cleanup)
        with pytest.raises(AgentStateSaveError) as caught:
            write_agent_state_snapshot(path, AgentState(), metadata={"saved": False})

    assert caught.value.__cause__ is primary
    assert path.read_bytes() == previous
    if not cleanup_fails:
        assert list(tmp_path.iterdir()) == [path]


def test_snapshot_parent_creation_failure_raises_save_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "missing" / "state.json"
    real_mkdir = Path.mkdir

    def fail_mkdir(directory, *args, **kwargs) -> None:
        if directory == path.parent:
            raise PermissionError("mkdir denied")
        real_mkdir(directory, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    with pytest.raises(AgentStateSaveError, match="mkdir denied"):
        write_agent_state_snapshot(path, AgentState())


def test_atomic_snapshot_is_flushed_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def sync(descriptor: int) -> None:
        real_fsync(descriptor)
        observed.append("fsync")

    def replace(source, target):
        observed.append("replace")
        return real_replace(source, target)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", replace)
    path = write_agent_state_snapshot(tmp_path / "state.json", AgentState())

    assert observed == ["fsync", "replace"]
    assert read_agent_state_snapshot(path).state == AgentState()
    assert list(tmp_path.iterdir()) == [path]


def test_snapshot_closes_temp_descriptor_if_file_wrapper_creation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    write_agent_state_snapshot(path, AgentState())
    previous = path.read_bytes()
    descriptors: list[int] = []

    def failed_fdopen(descriptor: int, *args, **kwargs):
        descriptors.append(descriptor)
        raise OSError("file wrapper failed")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "fdopen", failed_fdopen)
            with pytest.raises(AgentStateSaveError, match="file wrapper failed"):
                write_agent_state_snapshot(path, AgentState())
        assert len(descriptors) == 1
        with pytest.raises(OSError):
            os.fstat(descriptors[0])
        assert path.read_bytes() == previous
        assert list(tmp_path.iterdir()) == [path]
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
