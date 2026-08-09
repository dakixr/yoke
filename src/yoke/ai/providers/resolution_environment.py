"""Environment and credential checks for provider resolution."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from yoke.ai.providers.credentials import provider_environment


def resolved_home(home: Path | str | None) -> Path:
    """Return the normalized home used for provider discovery."""
    return (Path.home() if home is None else Path(home)).resolve()


def resolved_env(
    env: Mapping[str, str] | None,
    *,
    home: Path,
) -> Mapping[str, str]:
    """Return an explicit or credential-enriched provider environment."""
    if env is not None:
        return env
    return provider_environment(home=home, env=os.environ)


def credential_issue(
    provider_name: str,
    *,
    env: Mapping[str, str],
    home: Path,
) -> str | None:
    """Return the missing-credential reason for a built-in provider."""
    if provider_name == "zai" and not env.get("ZAI_API_KEY"):
        return "zai provider requires ZAI_API_KEY."
    if provider_name == "opencode-go" and not env.get("OPENCODE_API_KEY"):
        return "opencode-go provider requires OPENCODE_API_KEY."
    if provider_name != "codex":
        return None
    if env.get("YOKE_CODEX_API_KEY"):
        return None
    if (home / ".codex" / "auth.json").is_file():
        return None
    if any((home / ".codex-auth" / "accounts").glob("*/auth.json")):
        return None
    auths_path = (
        Path(env["YOKE_CODEX_AUTHS_PATH"])
        if env.get("YOKE_CODEX_AUTHS_PATH")
        else home / ".yoke" / "providers" / "codex-auth" / "auths.json"
    )
    if auths_path.is_file():
        return None
    return "codex provider requires YOKE_CODEX_API_KEY or stored Codex auth."
