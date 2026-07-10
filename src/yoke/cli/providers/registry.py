"""Compatibility exports for the AI-layer provider plugin registry."""

from pathlib import Path as Path

from yoke.ai.providers.plugins import LoadedProviderPlugin
from yoke.ai.providers.plugins import ProviderPluginContext
from yoke.ai.providers.plugins import ProviderPluginDiscovery
from yoke.ai.providers.plugins import ProviderPluginLoadFailure
from yoke.ai.providers.plugins import available_custom_provider_names
from yoke.ai.providers.plugins import create_custom_provider
from yoke.ai.providers.plugins import discover_global_provider_plugins
from yoke.ai.providers.plugins import list_custom_provider_models
from yoke.ai.providers.plugins import load_global_provider_plugins

__all__ = [
    "LoadedProviderPlugin",
    "Path",
    "ProviderPluginContext",
    "ProviderPluginDiscovery",
    "ProviderPluginLoadFailure",
    "available_custom_provider_names",
    "create_custom_provider",
    "discover_global_provider_plugins",
    "list_custom_provider_models",
    "load_global_provider_plugins",
]
