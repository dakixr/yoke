"""Build identity freezes the checkout identity and includes root-level dirt."""

from __future__ import annotations

import subprocess

from yoke._version import __version__
from yoke.daemon import build


def test_build_identity_contains_head_and_any_checkout_dirt(tmp_path, monkeypatch):
    source = tmp_path / "src" / "yoke" / "daemon" / "build.py"
    source.parent.mkdir(parents=True)
    source.touch()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init")
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    head = git("rev-parse", "HEAD")[:12]
    monkeypatch.setattr(build, "__file__", str(source))
    monkeypatch.delenv(build.BUILD_ID_ENV, raising=False)
    build._source_build_id.cache_clear()
    try:
        assert build.build_id() == f"{__version__}+g{head}"
        (tmp_path / "pyproject.toml").write_text("# changed outside src/yoke\n")
        # An already running process keeps its original identity.
        assert build.build_id() == f"{__version__}+g{head}"
        build._source_build_id.cache_clear()
        assert build.build_id() == f"{__version__}+g{head}.dirty"
    finally:
        build._source_build_id.cache_clear()


def test_runtime_preload_imports_turn_and_provider_modules():
    import sys
    from yoke.daemon.server import preload_runtime

    preload_runtime()
    for name in (
        "yoke.http.runtime.runtime",
        "yoke.http.runtime.factory",
        "yoke.agent.loop.agent",
        "yoke.ai.providers.codex.subscription.provider",
    ):
        assert name in sys.modules
