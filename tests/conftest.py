from __future__ import annotations

import os
import pwd
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_yoke_local_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("YOKE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("YOKE_PROVIDER_LOGS_DIR", str(tmp_path / "provider-logs"))


@pytest.fixture(autouse=True, scope="session")
def forbid_real_database_writes() -> Iterator[None]:
    """Fail the run if any test, or a subprocess it starts, creates ~/.yoke/yoke.db."""
    real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".yoke" / "yoke.db"
    existed = real.exists()
    yield
    if not existed and real.exists():
        pytest.fail(
            f"A test created the real database {real}; isolate HOME/YOKE_DB_PATH."
        )
