"""Codex subscription provider implementation."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from yoke.agent.models import Message
from yoke.ai.providers.base import (
    Provider,
    ProviderModelInfo,
)
from yoke.ai.providers.model_selection import set_config_model_from_catalog

from .catalog import MODEL_CATALOG, PROVIDER_NAME
from .completion import CodexCompletionMixin
from .config import CodexSubscriptionConfig
from .images import CodexImageMixin
from .request import CodexRequestMixin


class CodexSubscriptionProvider(
    CodexCompletionMixin, CodexImageMixin, CodexRequestMixin, Provider
):
    provider_name = PROVIDER_NAME
    supports_image_inputs = True
    max_images_per_message = None
    supports_image_generation = True

    def __init__(
        self,
        config: CodexSubscriptionConfig,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        set_config_model_from_catalog(
            self.config,
            MODEL_CATALOG,
            provider_name=PROVIDER_NAME,
            model_id=self.config.model,
            reasoning_effort=self.config.reasoning_effort,
        )
        self._sleep = sleep or time.sleep
        self._active_auth_profile: str | None = None
        self._last_logged_auth_profile: str | None = None
        self._prompt_cache_key = self._new_prompt_cache_key()
        self._turn_state: str | None = None
        self._owns_client = http_client is None
        self._client = http_client or self._new_client()

    def _new_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.config.timeout_seconds,
        )

    def list_models(self) -> list[ProviderModelInfo]:
        return [model.model_copy(deep=True) for model in MODEL_CATALOG]

    def current_model_id(self) -> str | None:
        model = self.config.model.strip()
        return model or None

    def current_model_info(self) -> ProviderModelInfo | None:
        current_model = self.current_model_id()
        if current_model is None:
            return None
        for model in self.list_models():
            if model.id == current_model:
                return model
        return None

    def set_model(self, model_id: str, *, reasoning_effort: str | None = None) -> None:
        set_config_model_from_catalog(
            self.config,
            MODEL_CATALOG,
            provider_name=PROVIDER_NAME,
            model_id=model_id,
            reasoning_effort=reasoning_effort,
        )

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        return self.complete_with_cancel(
            messages,
            tools,
            cancel_requested=lambda: False,
        )

    def complete_with_cancel(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        return self._with_request_cancellation(
            lambda: self._complete_with_cancel_impl(
                messages,
                tools,
                cancel_requested=cancel_requested,
            ),
            cancel_requested=cancel_requested,
        )
