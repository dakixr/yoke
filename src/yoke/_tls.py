"""Shared TLS verification policy for Yoke-owned outbound connections."""

from __future__ import annotations

import os
from collections.abc import Mapping

_FALSE_VALUES = {"0", "false", "no", "off"}


def tls_verification_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether Yoke-owned outbound TLS connections should be verified."""

    source = os.environ if env is None else env
    value = source.get("YOKE_DISABLE_TLS")
    if value is None:
        return True
    return value.strip().lower() in _FALSE_VALUES
