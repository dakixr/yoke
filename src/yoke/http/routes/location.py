"""Workspace location routes."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request

from yoke.http.auth import require_auth
from yoke.http.models.location import LocationResponse
from yoke.http.models.location import RecentLocationsResponse
from yoke.http.services.location_service import LocationService


router = APIRouter(dependencies=[Depends(require_auth)])


def _service(request: Request) -> LocationService:
    return request.app.state.location_service


@router.get("/location", response_model=LocationResponse, operation_id="resolveLocation")
def resolve_location(
    request: Request,
    directory: str | None = Query(default=None),
) -> LocationResponse:
    return LocationResponse(data=_service(request).resolve(directory))


@router.get(
    "/location/recent",
    response_model=RecentLocationsResponse,
    operation_id="recentLocations",
)
def recent_locations(request: Request) -> RecentLocationsResponse:
    return RecentLocationsResponse(data=_service(request).recent())

