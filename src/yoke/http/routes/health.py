"""Health and capability discovery routes."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends

from yoke._version import __version__
from yoke.http.auth import require_auth
from yoke.http.models.capabilities import CapabilitiesData
from yoke.http.models.capabilities import CapabilitiesResponse
from yoke.http.models.capabilities import CapabilityLimits
from yoke.http.models.capabilities import FeatureFlags
from yoke.http.models.capabilities import HealthData
from yoke.http.models.capabilities import HealthResponse


router = APIRouter()


@router.get("/health", response_model=HealthResponse, operation_id="health")
def health() -> HealthResponse:
    return HealthResponse(
        data=HealthData(healthy=True, version=__version__, protocol_version="1")
    )


@router.get(
    "/capabilities",
    response_model=CapabilitiesResponse,
    dependencies=[Depends(require_auth)],
    operation_id="capabilities",
)
def capabilities() -> CapabilitiesResponse:
    return CapabilitiesResponse(
        data=CapabilitiesData(
            features=FeatureFlags(),
            limits=CapabilityLimits(),
        )
    )

