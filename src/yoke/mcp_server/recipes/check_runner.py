"""Child-side check runner; emits a final report after all requested checks."""

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path
from typing import Any

from yoke.mcp_server.recipes.workspace import file_hash


def run(
    root: str, checks: list[dict[str, Any]], timeout: int, before: dict[str, str]
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    failed = False
    for check in checks:
        if failed:
            results.append({"name": check["name"], "status": "skipped"})
            continue
        try:
            completed = subprocess.run(
                check["argv"], cwd=root, timeout=timeout, check=False
            )
            failed = completed.returncode != 0
            results.append(
                {
                    "name": check["name"],
                    "status": "error" if failed else "ok",
                    "exit_code": completed.returncode,
                }
            )
        except subprocess.TimeoutExpired:
            failed = True
            results.append(
                {
                    "name": check["name"],
                    "status": "unknown",
                    "error": "Check timed out; side effects may have occurred",
                }
            )
        except OSError as exc:
            failed = True
            results.append(
                {"name": check["name"], "status": "error", "error": str(exc)}
            )
    diff: list[str] = []
    hashes = {}
    for name, text in before.items():
        path = (Path(root) / name).resolve()
        after = (
            path.read_text()
            if path.is_file() and path.stat().st_size <= 4 * 1024 * 1024
            else ""
        )
        diff.extend(
            difflib.unified_diff(
                text.splitlines(True),
                after.splitlines(True),
                fromfile=name,
                tofile=name,
            )
        )
        hashes[name] = file_hash(path)
    return {
        "ok": not failed,
        "status": "complete",
        "recipe_version": 1,
        "checks": results,
        "diff": "".join(diff),
        "hashes": hashes,
    }
