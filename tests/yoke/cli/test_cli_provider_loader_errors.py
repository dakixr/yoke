"""Provider plugin error reporting tests for yoke CLI."""

# ruff: noqa: S101

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from yoke.cli.main import app


def test_providers_doctor_reports_human_readable_plugin_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broken provider plugins mention the file path and import failure."""
    home = tmp_path / "home"
    monkeypatch.setattr("yoke.cli.providers.registry.Path.home", lambda: home)
    monkeypatch.setattr("yoke.cli.providers.app.Path.home", lambda: home)
    provider_dir = home / ".yoke" / "providers"
    provider_dir.mkdir(parents=True)
    (provider_dir / "broken.py").write_text(
        "raise RuntimeError('boom during import')\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["providers", "doctor"])

    assert result.exit_code == 1
    assert "Provider loading completed with 1 failure(s)." in result.stdout
    assert "broken.py" in result.stdout
    assert "boom during import" in result.stdout
    assert "Provider loading OK." not in result.stdout
