from __future__ import annotations

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
    monkeypatch.setenv(
        "YOKE_USAGE_METRIC_LOG_DIR",
        str(tmp_path / "usage-metric-logs"),
    )
    monkeypatch.setenv("YOKE_PROVIDER_LOGS_DIR", str(tmp_path / "provider-logs"))
