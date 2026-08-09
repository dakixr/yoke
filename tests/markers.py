from __future__ import annotations

import os

import pytest

skip_in_ci = pytest.mark.skipif(
    any(
        os.environ.get(name)
        for name in (
            "CI",
            "TF_BUILD",
            "BUILD_BUILDID",
            "BUILD_BUILDNUMBER",
            "AGENT_ID",
            "AGENT_NAME",
            "SYSTEM_TEAMFOUNDATIONCOLLECTIONURI",
            "SYSTEM_TEAMPROJECT",
        )
    ),
    reason="local-only test skipped in CI",
)
