"""Build identity freezes the checkout identity and includes root-level dirt."""

from __future__ import annotations


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


def test_fresh_build_tracks_dirty_contents_and_disk_version(tmp_path, monkeypatch):
    import subprocess
    from yoke.daemon import build

    from itertools import count
    from types import SimpleNamespace

    ticks = count(100, 3)
    monkeypatch.setattr(build, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    root = tmp_path / "checkout"
    source = root / "src" / "yoke" / "daemon"
    source.mkdir(parents=True)
    version = source.parent / "_version.py"
    version.write_text('__version__ = "1.0.0"\n')
    marker = root / "config.txt"
    marker.write_text("clean")
    for args in (
        ["init"],
        ["add", "."],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "fixture",
        ],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    monkeypatch.delenv(build.BUILD_ID_ENV, raising=False)
    monkeypatch.setattr(build, "__file__", str(source / "build.py"))
    build._source_build_id.cache_clear()
    try:
        original = build.build_id()
        marker.write_text("dirty one")
        first = build.fresh_build_id()
        marker.write_text("dirty two")
        second = build.fresh_build_id()
        assert original != first != second
        assert build.build_id() == original
        version.write_text('__version__ = "2.0.0"\n')
        assert build.fresh_build_id().startswith("2.0.0+")
        untracked = root / "plugin.py"
        untracked.write_text("one")
        third = build.fresh_build_id()
        untracked.write_text("two")
        assert build.fresh_build_id() != third
    finally:
        build._source_build_id.cache_clear()
