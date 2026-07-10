"""Local credential storage for provider login commands."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from yoke.ai.providers.storage import write_private_json


def provider_credentials_path(home: Path) -> Path:
    """Return the user-local provider credential store path."""
    return home.expanduser().resolve() / ".yoke" / "providers" / "credentials.json"


def load_provider_credentials(*, home: Path) -> dict[str, str]:
    """Load provider environment credentials from the private user store."""
    path = provider_credentials_path(home)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Could not read provider credentials `{path}`: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Provider credentials `{path}` must contain a JSON object.")
    credentials: dict[str, str] = {}
    for key, value in payload.items():
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
        ):
            raise ValueError(
                f"Provider credentials `{path}` must map non-empty names to strings."
            )
        credentials[key] = value
    return credentials


def provider_environment(
    *,
    home: Path,
    env: Mapping[str, str],
) -> dict[str, str]:
    """Merge stored credentials underneath explicit process environment values."""
    merged = load_provider_credentials(home=home)
    merged.update(env)
    return merged


def save_provider_credential(*, home: Path, name: str, value: str) -> Path:
    """Atomically persist one provider credential with private permissions."""
    normalized_name = name.strip()
    if not normalized_name or not value:
        raise ValueError("Provider credential name and value must be non-empty.")
    path = provider_credentials_path(home)
    credentials = load_provider_credentials(home=home)
    credentials[normalized_name] = value
    write_private_json(path, credentials)
    return path
