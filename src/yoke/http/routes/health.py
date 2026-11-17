"""Health and capability discovery routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Depends

from yoke._version import __version__
from yoke.daemon.build import build_id, protocol_version
from yoke.daemon.identity import identity_proof
from yoke.http.models.daemon import DaemonIdentityData, DaemonIdentityResponse
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
        data=HealthData(
            healthy=True,
            version=__version__,
            protocol_version=protocol_version(),
            build_id=build_id(),
        )
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
            protocol_version=protocol_version(),
            features=FeatureFlags(),
            limits=CapabilityLimits(),
        )
    )


@router.get(
    "/daemon/identity",
    response_model=DaemonIdentityResponse,
    operation_id="daemonIdentity",
)
def daemon_identity(
    request: Request,
    nonce: str = Query(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64),
) -> DaemonIdentityResponse:
    """Prove possession of the discovery token before a client discloses it."""
    token = request.app.state.auth_token
    server = request.scope.get("server")
    if (
        getattr(request.app.state, "daemon", None) is None
        or not token
        or server is None
        or server[1] is None
    ):
        raise HTTPException(404, "Daemon identity is unavailable.")
    return DaemonIdentityResponse(
        data=DaemonIdentityData(proof=identity_proof(token, nonce, server[1]))
    )
