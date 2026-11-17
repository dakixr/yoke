"""Protocol 3 refuses incompatible state contracts without retrying discovery."""

from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from yoke.daemon import client
from yoke.daemon.paths import DaemonPaths
from yoke.daemon.protocol import REQUIRED_FEATURES, require_capabilities
from yoke.http.models.capabilities import FeatureFlags


@pytest.mark.parametrize("feature", REQUIRED_FEATURES)
@pytest.mark.parametrize("value", [False, None])
def test_required_feature_cannot_fall_back(feature, value):
    features = FeatureFlags().model_dump(by_alias=True)
    if value is None:
        features.pop(feature)
    else:
        features[feature] = value
    with pytest.raises(client.DaemonIncompatible, match=f"{feature}.*Upgrade"):
        require_capabilities({"protocolVersion": "3", "features": features})
    with pytest.raises(ValidationError):
        FeatureFlags.model_validate({feature: False})


def test_current_capabilities_are_accepted():
    require_capabilities(
        {"protocolVersion": "3", "features": FeatureFlags().model_dump(by_alias=True)}
    )


@pytest.mark.parametrize("client_protocol,daemon_protocol", [("2", "3"), ("3", "2")])
@pytest.mark.parametrize("busy", [False, True])
def test_two_checkouts_with_protocol_skew_refuse_without_restart(
    tmp_path, monkeypatch, client_protocol, daemon_protocol, busy
):
    # Different checkouts at the same release must never ping-pong, even idle.
    monkeypatch.setattr(client, "build_id", lambda: "4.0.0+gclient")
    monkeypatch.setattr(client, "fresh_build_id", lambda: "4.0.0+gclient")
    monkeypatch.setattr(client, "protocol_version", lambda: client_protocol)
    discover = Mock(return_value=Path("fixture.sock"))
    restart = Mock(side_effect=AssertionError("protocol skew restarted a checkout"))
    monkeypatch.setattr(client, "find_daemon", discover)
    monkeypatch.setattr(client, "restart_daemon", restart)
    monkeypatch.setattr(
        httpx.Client,
        "get",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            json={
                "data": {
                    "buildID": "4.0.0+gdaemon",
                    "protocolVersion": daemon_protocol,
                    "busy": busy,
                    "pid": 123,
                }
            },
        ),
    )
    with pytest.raises(client.DaemonIncompatible, match="Upgrade/restart") as error:
        client.connect_or_spawn(DaemonPaths(tmp_path, tmp_path))
    assert f"protocol {client_protocol}" in str(error.value)
    assert f"protocol {daemon_protocol}" in str(error.value)
    discover.assert_called_once()
    restart.assert_not_called()
