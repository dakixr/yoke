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
