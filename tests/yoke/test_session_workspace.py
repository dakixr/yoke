from __future__ import annotations

from pathlib import Path

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
from tests.yoke.session_rows import stored_entry_bytes, stored_session_bytes
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


def test_workspace_unreadable_is_not_reported_as_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("yoke.session.workspace.os.access", lambda *_args: False)
    assert inspect_workspace(tmp_path).status == "unreadable"


def test_history_is_readable_after_deletion_and_relocation_only_appends_metadata(
    tmp_path,
):
    store, original, old, new = saved(tmp_path)
    before = stored_session_bytes(store, "saved")
    history = stored_entry_bytes(store, "saved")
    old.rmdir()
    loaded = store.load("saved")
    assert loaded.messages == original.messages
    assert store.list(root=old)[0].id == "saved"
    assert stored_session_bytes(store, "saved") == before
    with pytest.raises(WorkspaceUnavailable):
        require_session_workspace(loaded)

    moved = relocate_session_workspace(store, "saved", new, expected_root=str(old))
    assert stored_entry_bytes(store, "saved") == history
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
    before = stored_session_bytes(store, "saved")
    assert (
        relocate_session_workspace(store, "saved", new, expected_root=str(old)) == moved
    )
    assert stored_session_bytes(store, "saved") == before
    with pytest.raises(WorkspaceConflict):
        relocate_session_workspace(store, "saved", old, expected_root=str(old))
    assert stored_session_bytes(store, "saved") == before


def test_invalid_destination_and_failed_append_leave_history_and_binding_unchanged(
    tmp_path, monkeypatch
):
    store, record, _, new = saved(tmp_path)
    before = stored_session_bytes(store, "saved")
    with pytest.raises(WorkspaceUnavailable):
        relocate_session_workspace(store, "saved", tmp_path / "absent")
    assert stored_session_bytes(store, "saved") == before

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(SessionStore, "update_metadata", fail)
    with pytest.raises(OSError, match="disk full"):
        relocate_session_workspace(store, "saved", new)
    assert stored_session_bytes(store, "saved") == before
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
    before = stored_session_bytes(store, "saved")
    with pytest.raises(WorkspaceBusy, match="pending"):
        relocate_session_workspace(store, "saved", new)
    assert stored_session_bytes(store, "saved") == before
    assert store.load("saved").root == record.root


def test_invalid_session_id_cannot_escape_workspace_lock_directory(tmp_path):
    store = SessionStore(tmp_path / "store")
    with pytest.raises(ValueError, match="Session id"):
        with workspace_lease(store, "../outside"):
            pytest.fail("invalid id acquired a lease")
    assert not (tmp_path / "outside.lock").exists()


def test_workspace_lease_can_transfer_to_cleanup_thread_without_files(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store, _, _, new = saved(tmp_path)
    lease = workspace_lease(store, "saved")
    lease.__enter__()
    try:
        with pytest.raises(WorkspaceBusy):
            relocate_session_workspace(store, "saved", new)
    finally:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(lease.__exit__, None, None, None).result(timeout=2)
    assert relocate_session_workspace(store, "saved", new).root == str(new)
    assert not (store.directory / "workspaces").exists()


def test_workspace_lease_keys_include_database_and_session(tmp_path):
    first = SessionStore(tmp_path / "first")
    second = SessionStore(tmp_path / "second")
    with workspace_lease(first, "same"):
        with workspace_lease(second, "same", exclusive=True):
            pass
        with workspace_lease(first, "other", exclusive=True):
            pass
        with pytest.raises(WorkspaceBusy):
            with workspace_lease(first, "same", exclusive=True):
                pytest.fail("relocation acquired a leased workspace")
    with workspace_lease(first, "same", exclusive=True):
        with pytest.raises(WorkspaceBusy):
            with workspace_lease(first, "same"):
                pytest.fail("execution entered during relocation")
    with workspace_lease(first, "same"):
        pass
    assert not (first.directory / "workspaces").exists()
    assert not (second.directory / "workspaces").exists()
