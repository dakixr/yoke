"""Default response-timeout coverage for built-in providers."""

from __future__ import annotations

from yoke.ai.providers.openai_compat import OpenAICompatibleConfig
from yoke.ai.providers.opencode_go.catalog import OpenCodeGoConfig
from yoke.ai.providers.zai.models import ZAIConfig


def test_builtin_provider_response_timeouts_default_to_fifteen_minutes() -> None:
    """Long-running generations share a 15-minute response budget."""
    assert OpenAICompatibleConfig(api_key="key", model="model").timeout_seconds == 900
    assert OpenCodeGoConfig(api_key="key").timeout_seconds == 900
    zai_config = ZAIConfig(api_key="key")
    assert zai_config.read_idle_timeout_seconds == 900
    assert zai_config.total_timeout_seconds == 900
