"""Regenerate the checked-in Yoke HTTP OpenAPI contract."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from yoke.http.app import HttpAppSettings
from yoke.http.app import create_app


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "yoke" / "http" / "golden" / "openapi.json"


def main() -> None:
    """Write a deterministic OpenAPI JSON artifact used by contract tests."""
    with TemporaryDirectory(prefix="yoke-openapi-") as directory:
        app = create_app(
            HttpAppSettings(
                auth_token="openapi-snapshot",
                session_directory=Path(directory),
            )
        )
        payload = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    main()
