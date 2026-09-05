"""File-backed verification jobs that acknowledge readiness before mutation."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any


def prepare(root: Path, specification: dict[str, Any]) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="patch-", dir=root))
    try:
        path = directory / "job.json"
        path.write_text(json.dumps(specification), encoding="utf-8")
        path.chmod(0o600)
        return directory
    except BaseException:
        shutil.rmtree(directory)
        raise


def run_job(directory: str) -> None:
    # Import and deserialize before acknowledging readiness. The parent keeps
    # the patch mutex until it has applied the patch and released this gate.
    from yoke.mcp_server.execution.client import output
    from yoke.mcp_server.recipes.check_runner import run

    job = Path(directory)
    try:
        specification = json.loads((job / "job.json").read_text(encoding="utf-8"))
        (job / "ready").touch()
        deadline = time.monotonic() + 30
        while not (job / "go").exists():
            if not job.exists() or time.monotonic() >= deadline:
                raise RuntimeError("Patch verification was not released by its parent")
            time.sleep(0.02)
        output.emit(run(**specification))
    finally:
        shutil.rmtree(job, ignore_errors=True)
