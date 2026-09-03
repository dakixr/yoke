"""OpenCode Go multi-protocol provider."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import httpx

from yoke.agent.models import Message
from yoke.ai.providers.base import Provider, ProviderCancelledError, ProviderModelInfo
from yoke.ai.providers.model_selection import (
    cloned_model_catalog,
    current_model_id_from_config,
    current_model_info_from_catalog,
    set_config_model_from_catalog,
)
from yoke.ai.providers.openai_compat import (
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
)
from yoke.ai.providers.responses import complete_response
from yoke.ai.providers.opencode_go.catalog import (
    MODEL_PROTOCOLS,
    OPENAI_BASE_URL,
    PROVIDER_NAME,
    OpenCodeGoConfig,
    _max_output_tokens,
    _normalize_model_id,
)


class OpenCodeGoProvider(Provider):
    provider_name = PROVIDER_NAME
    supports_image_inputs = True

    @property
    def max_images_per_message(self) -> int:
        """Return the active model's per-message image admission limit."""
        return 8 if self.config.model == "glm-5.3-flash" else 50

    @property
    def max_images_per_request(self) -> int | None:
        """Return the active model's request-wide image budget."""
        return 8 if self.config.model == "glm-5.3-flash" else None

    def __init__(
        self,
        config: OpenCodeGoConfig,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        set_config_model_from_catalog(
            self.config,
            self.config.model_catalog,
            provider_name=PROVIDER_NAME,
            model_id=self.config.model,
            reasoning_effort=self.config.reasoning_effort,
        )
        self._sleep = sleep or time.sleep
        self._owns_client = http_client is None
        self._client = http_client or self._new_client()
        self._openai_provider = self._build_openai_provider(config, http_client)

    def _new_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.config.timeout_seconds,
            headers={
                "Content-Type": "application/json",
                "x-opencode-session": self.config.session_id,
            },
        )

    def _build_openai_provider(
        self,
        config: OpenCodeGoConfig,
        http_client: httpx.Client | None,
    ) -> OpenAICompatibleProvider:
        openai_reasoning_effort = config.reasoning_effort
        if openai_reasoning_effort == "thinking":
            openai_reasoning_effort = None
        return OpenAICompatibleProvider(
            OpenAICompatibleConfig(
                api_key=config.api_key,
                model=config.model,
                base_url=OPENAI_BASE_URL,
                timeout_seconds=config.timeout_seconds,
                max_retries=config.max_retries,
                retry_backoff_seconds=config.retry_backoff_seconds,
                max_retry_backoff_seconds=config.max_retry_backoff_seconds,
                max_tokens=_max_output_tokens(config.model),
                reasoning_effort=openai_reasoning_effort,
                provider_name=PROVIDER_NAME,
                model_catalog=config.model_catalog,
                headers={"x-opencode-session": config.session_id},
            ),
            http_client=http_client,
            sleep=self._sleep,
        )

    def list_models(self) -> list[ProviderModelInfo]:
        return cloned_model_catalog(self.config.model_catalog)

    def current_model_id(self) -> str | None:
        return current_model_id_from_config(self.config)

    def current_model_info(self) -> ProviderModelInfo | None:
        return current_model_info_from_catalog(self.config, self.list_models())

    def set_model(self, model_id: str, *, reasoning_effort: str | None = None) -> None:
        set_config_model_from_catalog(
            self.config,
            self.list_models(),
            provider_name=PROVIDER_NAME,
            model_id=_normalize_model_id(model_id),
            reasoning_effort=reasoning_effort,
        )
        self._sync_openai_config()

    def set_session_id(self, session_id: str) -> None:
        """Bind subsequent requests to one stable OpenCode Go session."""
        normalized = session_id.strip()
        if not normalized:
            raise ValueError("session_id must not be empty")
        self.config.session_id = normalized
        self._openai_provider.set_request_header("x-opencode-session", normalized)

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self._sync_openai_config()
        if MODEL_PROTOCOLS.get(self.config.model) == "responses":
            return self._complete_responses(
                messages,
                tools,
                cancel_requested=lambda: False,
            )
        return self._openai_provider.complete(messages, tools)

    def complete_with_cancel(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        self._sync_openai_config()
        if MODEL_PROTOCOLS.get(self.config.model) == "responses":
            return self._with_request_cancellation(
                lambda: self._complete_responses(
                    messages,
                    tools,
                    cancel_requested=cancel_requested,
                ),
                cancel_requested=cancel_requested,
            )
        return self._openai_provider.complete_with_cancel(
            messages,
            tools,
            cancel_requested=cancel_requested,
        )

    def close(self) -> None:
        self._openai_provider.close()
        if self._owns_client:
            self._client.close()

    def _sync_openai_config(self) -> None:
        self._openai_provider.config.model = self.config.model
        self._openai_provider.config.max_tokens = _max_output_tokens(self.config.model)
        reasoning_effort = self.config.reasoning_effort
        if reasoning_effort == "thinking":
            reasoning_effort = None
        self._openai_provider.config.reasoning_effort = reasoning_effort

    def _complete_responses(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        return complete_response(
            client=self._client,
            url=f"{OPENAI_BASE_URL}/responses",
            api_key=self.config.api_key,
            provider_name=PROVIDER_NAME,
            model=self.config.model,
            messages=messages,
            tools=tools,
            reasoning_effort=self.config.reasoning_effort,
            max_output_tokens=_max_output_tokens(self.config.model),
            max_retries=self.config.max_retries,
            retry_backoff_seconds=self.config.retry_backoff_seconds,
            max_retry_backoff_seconds=self.config.max_retry_backoff_seconds,
            cancel_requested=cancel_requested,
            sleep=self._sleep,
            request_headers={"x-opencode-session": self.config.session_id},
        )

    def _with_request_cancellation(
        self,
        action: Callable[[], Message],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        if not self._owns_client:
            return action()
        finished = threading.Event()
        client_closed = threading.Event()

        def close_on_cancel() -> None:
            while not finished.wait(0.05):
                if cancel_requested():
                    client_closed.set()
                    self._client.close()
                    return

        threading.Thread(target=close_on_cancel, daemon=True).start()
        try:
            message = action()
            if cancel_requested():
                raise ProviderCancelledError()
            return message
        finally:
            finished.set()
            if client_closed.is_set():
                self._client = self._new_client()
