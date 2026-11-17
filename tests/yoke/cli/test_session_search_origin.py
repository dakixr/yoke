"""The terminal chooser sends search through the daemon and preserves origin."""

from pathlib import Path

from typer.testing import CliRunner

from yoke.cli.main import app


def test_resume_search_option_reaches_chooser(tmp_path: Path, monkeypatch):
    from yoke.cli.backend import startup

    seen = {}

    def chooser(args, **options):
        seen.update(options)
        return 0

    monkeypatch.setattr(startup, "run_daemon_interactive", chooser)
    result = CliRunner().invoke(
        app, ["resume", "--root", str(tmp_path), "--all", "--search", "a phrase"]
    )
    assert result.exit_code == 0, result.output
    assert seen == {"picker": True, "all_sessions": True, "search": "a phrase"}
    assert (
        CliRunner().invoke(app, ["resume", "some-id", "--search", "text"]).exit_code
        == 2
    )
