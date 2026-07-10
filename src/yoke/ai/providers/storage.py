"""Private atomic JSON persistence shared by provider credential stores."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path


def write_private_json(path: Path, payload: object) -> None:
    """Atomically write JSON with private directory and file permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            with suppress(OSError, AttributeError):
                os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
