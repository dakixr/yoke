from __future__ import annotations

import os
from pathlib import Path

import pytest

from yoke import _file_io


@pytest.mark.parametrize("failure_stage", ["acquire", "body", "release"])
def test_file_lock_closes_descriptor_and_preserves_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    failure = OSError(f"{failure_stage} failed")
    acquired: list[int] = []
    released: list[int] = []

    def acquire(descriptor: int) -> None:
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
