"""Provider and model catalog routes."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request

from yoke.http.auth import require_auth
from yoke.http.models.catalog import ModelListResponse
from yoke.http.models.catalog import ProviderListResponse
from yoke.http.services.catalog_service import CatalogService


router = APIRouter(dependencies=[Depends(require_auth)])


def _service(request: Request) -> CatalogService:
    return request.app.state.catalog_service


@router.get("/provider", response_model=ProviderListResponse, operation_id="listProviders")
def list_providers(
    request: Request,
    directory: str | None = Query(default=None),
) -> ProviderListResponse:
    return _service(request).providers(directory=directory)


@router.get("/model", response_model=ModelListResponse, operation_id="listModels")
def list_models(
    request: Request,
    directory: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    search: str | None = Query(default=None),
) -> ModelListResponse:
    return _service(request).models(
        directory=directory,
        provider=provider,
        search=search,
    )
