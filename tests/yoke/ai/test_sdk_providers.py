"""Tests for Axi-compatible SDK provider helpers."""

# ruff: noqa: D101,D103,S101

from __future__ import annotations

from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.resolution import ProviderReadiness
from yoke.ai.sdk import providers


class FakeProvider:
    pass


def readiness() -> list[ProviderReadiness]:
    return [
        ProviderReadiness(
            provider_name="demo",
            ready=True,
            model="demo-model",
            reasoning_effort="high",
            models=(
                ProviderModelInfo(
                    id="demo-model",
                    display_name="Demo Model",
                    context_window_tokens=10_000,
                    thinking_levels=("low", "high"),
                    default_thinking_level="high",
                ),
            ),
        )
    ]


def test_provider_status_and_builder_compatibility(monkeypatch, capsys) -> None:
    built: list[str] = []
    monkeypatch.setattr(providers, "provider_readiness", readiness)
    monkeypatch.setattr(
        providers,
        "build_provider",
        lambda selection: built.append(selection) or FakeProvider(),
    )

    statuses = providers.builtin_provider_status()
    provider = providers.build_builtin_provider()
    selected = providers.available_builtin_providers(["demo:demo-model:high"])
    providers.print_builtin_provider_status()

    assert statuses[0].default_selection == "demo:demo-model:high"
    assert statuses[0].models[0].selection == "demo:demo-model:high"
    assert isinstance(provider, FakeProvider)
    assert list(selected) == ["demo:demo-model:high"]
    assert built == ["demo:demo-model:high", "demo:demo-model:high"]
    assert "Ready providers:" in capsys.readouterr().out
