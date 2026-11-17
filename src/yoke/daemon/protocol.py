"""Validate the state contract before a client starts consuming daemon events."""

from collections.abc import Mapping

from yoke.daemon.build import PROTOCOL_VERSION
from yoke.daemon.client import DaemonIncompatible

REQUIRED_FEATURES = (
    "globalEvents",
    "promptAdmission",
    "inputLifecycle",
    "steering",
    "queueEditor",
)


def require_capabilities(data: Mapping[str, object]) -> None:
    """Refuse incompatible daemons without entering a reconnect loop."""
    features = data.get("features")
    missing = [
        name
        for name in REQUIRED_FEATURES
        if not isinstance(features, Mapping) or features.get(name) is not True
    ]
    protocol = data.get("protocolVersion")
    if protocol != PROTOCOL_VERSION or missing:
        details = f"protocol {protocol!r}"
        if missing:
            details += f"; missing required features: {', '.join(missing)}"
        raise DaemonIncompatible(
            f"Yoke requires daemon protocol {PROTOCOL_VERSION} ({details}). "
            "Upgrade the older client or daemon and restart the daemon explicitly."
        )
