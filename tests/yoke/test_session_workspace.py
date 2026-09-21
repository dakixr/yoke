from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from yoke.agent.models import Message
from yoke.agent.skills.models import ActiveSkill
from yoke.session import SessionStore
from yoke.session.admissions import AdmissionRecord, AdmissionSnapshot, AdmissionStore
from yoke.session.queue import (
    PersistedPendingInput,
    PersistedPromptQueue,
    write_prompt_queue_snapshot,
)
from yoke.session.workspace import (
    WorkspaceBusy,
    WorkspaceConflict,
    WorkspaceUnavailable,
    inspect_workspace,
    relocate_session_workspace,
    require_session_workspace,
    require_workspace,
    workspace_lease,
)


def saved(tmp_path: Path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    store = SessionStore(tmp_path / "store")
    record = store.save(
        "saved",
        [Message.user("original"), Message.assistant("answer")],
        root=old,
        title="Keep this title",
        provider_name="test",
        model_id="saved-model",
        active_skills=[
            ActiveSkill(
                name="saved-skill",
                description="Saved",
                source_path="<inline>",
                content="Keep these instructions",
            )
        ],
        skill_dirs=[str(old / "skills")],
    )
    return store, record, old, new


@pytest.mark.parametrize(
    "kind", ["missing", "not_directory", "unconfigured", "invalid"]
)
def test_workspace_errors_are_actionable_and_never_create_a_directory(tmp_path, kind):
    directory: Path | str | None = tmp_path / kind
    if kind == "not_directory":
        (tmp_path / kind).write_text("file")
    elif kind == "unconfigured":
        directory = None
    elif kind == "invalid":
        directory = "\0invalid"
    assert inspect_workspace(directory).status == kind
    with pytest.raises(WorkspaceUnavailable) as caught:
        require_workspace(directory, session_id="saved")
    assert caught.value.code == "session_workspace_unavailable"
    assert caught.value.details["status"] == kind
    assert "yoke resume saved --relocate" in str(caught.value)
    assert not (tmp_path / "missing").exists()


def test_workspace_detects_broken_symlinks_and_files_in_parent_path(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "absent", target_is_directory=True)
    assert inspect_workspace(link).status == "missing"
    file = tmp_path / "file"
    file.write_text("file")
    assert inspect_workspace(file / "child").status == "not_directory"


def test_workspace_detects_file_parent_when_stat_reports_missing(tmp_path, monkeypatch):
    file = tmp_path / "file"
    file.write_text("file")
    target = file / "child"
    original_stat = Path.stat

    def stat_with_windows_missing(path, *args, **kwargs):
        if path == target:
            raise FileNotFoundError(str(target))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_with_windows_missing)
    assert inspect_workspace(target).status == "not_directory"


def test_workspace_unreadable_is_not_reported_as_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("yoke.session.workspace.os.access", lambda *_args: False)
    assert inspect_workspace(tmp_path).status == "unreadable"


def test_history_is_readable_after_deletion_and_relocation_only_appends_metadata(
    tmp_path,
):
    store, original, old, new = saved(tmp_path)
    path = store.directory / "saved.jsonl"
    before = path.read_bytes()
    old.rmdir()
    loaded = store.load("saved")
    assert loaded.messages == original.messages
    assert store.list(root=old)[0].id == "saved"
    assert path.read_bytes() == before
    with pytest.raises(WorkspaceUnavailable):
        require_session_workspace(loaded)

    moved = relocate_session_workspace(store, "saved", new, expected_root=str(old))
    assert path.read_bytes().startswith(before)
    assert not old.exists()
    assert moved.id == original.id
    assert moved.root == str(new)
    assert moved.conversation_entries == original.conversation_entries
    assert moved.leaf_id == original.leaf_id
    assert moved.active_skills == original.active_skills
    assert moved.skill_dirs == []
    assert moved.created_at == original.created_at
    assert moved.title == original.title
    assert moved.provider_name == original.provider_name
    assert moved.model_id == original.model_id
    assert SessionStore(store.directory).load("saved") == moved
    entry = store.index_entry("saved")
    assert entry is not None and entry.root == str(new)
    assert entry.entry_count == len(original.conversation_entries)


def test_relocation_is_idempotent_but_rejects_a_stale_third_destination(tmp_path):
    store, _, old, new = saved(tmp_path)
    moved = relocate_session_workspace(store, "saved", new, expected_root=str(old))
    path = store.directory / "saved.jsonl"
    before = path.read_bytes()
    assert (
        relocate_session_workspace(store, "saved", new, expected_root=str(old)) == moved
    )
    assert path.read_bytes() == before
    with pytest.raises(WorkspaceConflict):
        relocate_session_workspace(store, "saved", old, expected_root=str(old))
    assert path.read_bytes() == before


def test_invalid_destination_and_failed_append_leave_history_and_binding_unchanged(
    tmp_path, monkeypatch
):
    store, record, _, new = saved(tmp_path)
    path = store.directory / "saved.jsonl"
    before = path.read_bytes()
    with pytest.raises(WorkspaceUnavailable):
        relocate_session_workspace(store, "saved", tmp_path / "absent")
    assert path.read_bytes() == before

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr("yoke.session.workspace.append_session_metadata", fail)
    with pytest.raises(OSError, match="disk full"):
        relocate_session_workspace(store, "saved", new)
    assert path.read_bytes() == before
    assert store.load("saved") == record


def test_nested_execution_leases_exclude_relocation_and_release_on_errors(tmp_path):
    store, _, _, new = saved(tmp_path)
    with workspace_lease(store, "saved"):
        with workspace_lease(store, "saved"):
            with pytest.raises(WorkspaceBusy):
                relocate_session_workspace(store, "saved", new)
    with pytest.raises(RuntimeError):
        with workspace_lease(store, "saved"):
            raise RuntimeError("aborted worker")
    assert relocate_session_workspace(store, "saved", new).root == str(new)


def test_another_process_cannot_relocate_under_an_execution_lease(tmp_path):
    store, _, _, new = saved(tmp_path)
    code = """
from pathlib import Path
import sys
from yoke.session import SessionStore
from yoke.session.workspace import WorkspaceBusy, relocate_session_workspace
try:
    relocate_session_workspace(SessionStore(Path(sys.argv[1])), 'saved', sys.argv[2])
except WorkspaceBusy:
    print('busy')
else:
    print('moved')
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    argv = [sys.executable, "-c", code, str(store.directory), str(new)]
    with workspace_lease(store, "saved"):
        result = subprocess.run(
            argv, env=env, text=True, capture_output=True, check=True, timeout=10
        )
    assert result.stdout.strip() == "busy"
    result = subprocess.run(
        argv, env=env, text=True, capture_output=True, check=True, timeout=10
    )
    assert result.stdout.strip() == "moved"


@pytest.mark.parametrize("kind", ["queued", "paused", "images", "promoted"])
def test_relocation_never_retargets_pending_work(tmp_path, kind):
    store, record, _, new = saved(tmp_path)
    queue = PersistedPromptQueue()
    if kind in {"queued", "paused"}:
        queue.prompts.append(
            PersistedPendingInput(
                id="input",
                prompt="touch files",
                created_at="now",
                paused=kind == "paused",
            )
        )
    elif kind == "images":
        queue.pending_images.append("/original/image.png")
    else:
        admission = AdmissionRecord(
            id="input",
            session_id="saved",
            prompt="touch files",
            delivery="steer",
            fingerprint="x",
            time_created="now",
            admitted_seq=1,
            state="promoted",
        )
        AdmissionStore(store.directory).save(
            "saved", AdmissionSnapshot(records={"input": admission})
        )
    write_prompt_queue_snapshot(store.directory, "saved", queue)
    before = (store.directory / "saved.jsonl").read_bytes()
    with pytest.raises(WorkspaceBusy, match="pending"):
        relocate_session_workspace(store, "saved", new)
    assert (store.directory / "saved.jsonl").read_bytes() == before
    assert store.load("saved").root == record.root


def test_invalid_session_id_cannot_escape_workspace_lock_directory(tmp_path):
    store = SessionStore(tmp_path / "store")
    with pytest.raises(ValueError, match="Session id"):
        with workspace_lease(store, "../outside"):
            pytest.fail("invalid id acquired a lease")
    assert not (tmp_path / "outside.lock").exists()
