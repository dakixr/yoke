from __future__ import annotations

import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from yoke import _file_io


@pytest.mark.parametrize("failure_stage", ["acquire", "body", "release"])
def test_file_lock_closes_descriptor_and_preserves_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    failure = OSError(f"{failure_stage} failed")
    acquired: list[int] = []
    released: list[int] = []

    def acquire(
        descriptor: int, *, shared: bool = False, blocking: bool = True
    ) -> None:
        assert not shared and blocking
        acquired.append(descriptor)
        if failure_stage == "acquire":
            raise failure

    def release(descriptor: int) -> None:
        released.append(descriptor)
        if failure_stage == "release":
            raise failure

    monkeypatch.setattr(_file_io, "_lock_descriptor", acquire)
    monkeypatch.setattr(_file_io, "_unlock_descriptor", release)

    with pytest.raises(OSError) as caught:
        with _file_io.exclusive_file_lock(tmp_path / "test.lock"):
            if failure_stage == "body":
                raise failure

    assert caught.value is failure
    assert len(acquired) == 1
    assert released == ([] if failure_stage == "acquire" else acquired)
    with pytest.raises(OSError):
        os.fstat(acquired[0])


def test_windows_reader_group_excludes_writers_until_last_cross_thread_reader(tmp_path):
    """Exercise the Windows ownership algorithm with the host's native lock."""
    path = tmp_path / "readers.lock"
    entered, release = Event(), Event()

    def second_reader():
        with _file_io._windows_shared_file_lock(path, blocking=False):
            entered.set()
            assert release.wait(3)

    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            with _file_io._windows_shared_file_lock(path, blocking=False):
                future = executor.submit(second_reader)
                assert entered.wait(2)
                with pytest.raises(OSError):
                    with _file_io.file_lock(path, blocking=False):
                        pytest.fail("writer entered while readers held the lease")
            with pytest.raises(OSError):
                with _file_io.file_lock(path, blocking=False):
                    pytest.fail(
                        "first reader incorrectly released the second reader's lease"
                    )
        finally:
            release.set()
        future.result(timeout=3)
    with _file_io.file_lock(path, blocking=False):
        pass
