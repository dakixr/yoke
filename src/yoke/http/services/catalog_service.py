"""Read-only provider and model catalogs for browser clients."""

from __future__ import annotations

from pathlib import Path

from yoke.ai.providers.resolution import available_provider_names
from yoke.ai.providers.resolution import list_provider_models
from yoke.ai.providers.resolution import list_provider_readiness
from yoke.http.errors import ApiError
from yoke.http.models.catalog import ModelCapabilities
from yoke.http.models.catalog import ModelInfo
from yoke.http.models.catalog import ModelListResponse
from yoke.http.models.catalog import ProviderInfo
from yoke.http.models.catalog import ProviderListResponse
from yoke.http.models.common import LocationInfo
from yoke.http.services.redaction import redact_public_value


class CatalogService:
    """Project provider registry data into stable HTTP transport models."""

    def providers(self, *, directory: str | None) -> ProviderListResponse:
        location = _location(directory)
        readiness = list_provider_readiness(home=Path.home())
        return ProviderListResponse(
            location=location,
            data=[
                ProviderInfo(
                    id=item.provider_name,
                    ready=item.ready,
                    reason=_safe_reason(item.reason),
                    current_model=item.model,
                    current_reasoning_effort=item.reasoning_effort,
                )
                for item in readiness
            ],
        )

    def models(
        self,
        *,
        directory: str | None,
        provider: str | None,
        search: str | None,
    ) -> ModelListResponse:
        location = _location(directory)
        providers = (
            [provider.strip().lower()]
            if isinstance(provider, str) and provider.strip()
            else available_provider_names(home=Path.home())
        )
        data: list[ModelInfo] = []
        for provider_name in providers:
            try:
                models = list_provider_models(provider_name, home=Path.home())
            except Exception as exc:
                if provider is not None:
                    raise ApiError(
                        503,
                        "model_catalog_unavailable",
                        f"Model catalog for {provider_name!r} is unavailable.",
                    ) from exc
                continue
            for model in models or []:
                data.append(
                    ModelInfo(
                        id=model.id,
                        provider=provider_name,
                        name=model.display_name,
                        reasoning_efforts=list(model.thinking_levels),
                        capabilities=ModelCapabilities(
                            images=model.supports_image_inputs,
                            tools=True,
                        ),
                        context_window_tokens=model.context_window_tokens,
                    )
                )
        if search:
            needle = search.casefold()
            data = [
                item
                for item in data
                if needle in item.id.casefold()
                or needle in item.name.casefold()
                or needle in item.provider.casefold()
            ]
        data.sort(key=lambda item: (item.provider, item.name.casefold(), item.id))
        return ModelListResponse(location=location, data=data)


def _location(directory: str | None) -> LocationInfo:
    root = Path(directory or Path.cwd()).resolve()
    return LocationInfo(directory=str(root))


def _safe_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    redacted = redact_public_value(reason)
    return redacted if isinstance(redacted, str) else "Provider is not ready."
