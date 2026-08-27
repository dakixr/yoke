"""Run the synthetic Yoke HTTP performance benchmark."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC
from datetime import datetime
import json
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any
from urllib.parse import urlencode

from benchmark_http_performance_support import ServerProcess
from benchmark_http_performance_support import Timing
from benchmark_http_performance_support import _append_session_entry
from benchmark_http_performance_support import _json_request
from benchmark_http_performance_support import _make_index_stale
from benchmark_http_performance_support import _request
from benchmark_http_performance_support import build_fixture


def benchmark_server(session_dir: Path, fixture: dict[str, Any]) -> dict[str, Any]:
    process = ServerProcess(session_dir, open_browser=False)
    process.start()
    try:
        process.wait_ready()
        assert process.port is not None and process.listening_at is not None
        startup = process.listening_at - process.started_at
        _request(process.port, "/api/v1/health")
        session_id = fixture["largeSessionID"]
        root = fixture["root"]

        cold_paths = [
            f"/api/v1/session/{session_id}",
            f"/api/v1/session/{session_id}/message?limit=100&order=desc&branch=active",
            f"/api/v1/session/{session_id}/queue",
            f"/api/v1/session/{session_id}/permission",
            f"/api/v1/session/{session_id}/question",
        ]
        concurrent_start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=len(cold_paths)) as executor:
            cold = list(
                executor.map(lambda path: _request(process.port or 0, path), cold_paths)
            )
        concurrent_seconds = time.perf_counter() - concurrent_start

        warm = [_request(process.port, path) for path in cold_paths]
        prior_leaf = f"entry-{fixture['largeEntries'] - 1:08d}"
        appended_id = f"entry-{fixture['largeEntries']:08d}"
        _append_session_entry(
            session_dir,
            session_id=session_id,
            parent_id=prior_leaf,
            entry_id=appended_id,
        )
        append_refresh = _request(
            process.port,
            f"/api/v1/session/{session_id}/message?limit=100&order=desc&branch=active",
        )
        metadata_mutations = [
            _json_request(
                process.port,
                f"/api/v1/session/{session_id}",
                method="PATCH",
                payload={"title": "Benchmark rename"},
                label="rename",
            ),
            _json_request(
                process.port,
                f"/api/v1/session/{session_id}",
                method="PATCH",
                payload={"pinned": True},
                label="pin",
            ),
            _json_request(
                process.port,
                f"/api/v1/session/{session_id}",
                method="PATCH",
                payload={"archived": True},
                label="archive",
            ),
            _json_request(
                process.port,
                f"/api/v1/session/{session_id}",
                method="PATCH",
                payload={"archived": False},
                label="unarchive",
            ),
        ]
        list_calls = [
            _request(
                process.port,
                "/api/v1/session?archived=false&limit=100&order=updatedDesc",
            ),
            _request(
                process.port, "/api/v1/session?archived=true&limit=30&order=updatedDesc"
            ),
            _request(process.port, "/api/v1/location/recent"),
        ]

        history_pages: list[Timing] = []
        after = 0
        history_start = time.perf_counter()
        for _ in range(10):
            path = f"/api/v1/session/{session_id}/history?after={after}&limit=200"
            timing = _request(process.port, path)
            history_pages.append(timing)
            after += 200
        history_seconds = time.perf_counter() - history_start

        catalog = [
            _request(process.port, "/api/v1/provider"),
            _request(process.port, "/api/v1/model?provider=codex"),
            _request(
                process.port, f"/api/v1/location?{urlencode({'directory': root})}"
            ),
        ]
        inspectors = [
            _request(process.port, f"/api/v1/session/{session_id}/context"),
            _request(process.port, f"/api/v1/session/{session_id}/tree"),
            _request(process.port, f"/api/v1/session/{session_id}/skill"),
            _request(process.port, f"/api/v1/session/{session_id}/tool-call?limit=100"),
        ]
        preview_target = f"entry-{max(0, fixture['largeEntries'] - 1000):08d}"
        tree_preview = _request(
            process.port,
            f"/api/v1/session/{session_id}/tree/navigation-preview?"
            + urlencode(
                {
                    "targetID": preview_target,
                    "includeAbandoned": "true",
                }
            ),
        )
        fork = _json_request(
            process.port,
            f"/api/v1/session/{session_id}/fork",
            method="POST",
            payload={},
            label="fork",
        )
        return {
            "startupSeconds": startup,
            "coldOpenConcurrentSeconds": concurrent_seconds,
            "coldOpen": [asdict(item) for item in cold],
            "warmOpen": [asdict(item) for item in warm],
            "appendRefresh": asdict(append_refresh),
            "metadataMutations": [asdict(item) for item in metadata_mutations],
            "lists": [asdict(item) for item in list_calls],
            "history10PagesSeconds": history_seconds,
            "historyPages": [asdict(item) for item in history_pages],
            "catalog": [asdict(item) for item in catalog],
            "inspectors": [asdict(item) for item in inspectors],
            "treePreview": asdict(tree_preview),
            "fork": asdict(fork),
        }
    finally:
        process.close()


def benchmark_open_startup(session_dir: Path) -> dict[str, float]:
    process = ServerProcess(session_dir, open_browser=True)
    process.start()
    try:
        process.wait_ready()
        assert process.listening_at is not None and process.opening_at is not None
        return {
            "listenSeconds": process.listening_at - process.started_at,
            "browserOpenSeconds": process.opening_at - process.started_at,
        }
    finally:
        process.close()


def benchmark_stale_startup_open(
    session_dir: Path,
    fixture: dict[str, Any],
) -> dict[str, Any]:
    """Measure first browser-like requests while an existing index needs repair."""
    process = ServerProcess(session_dir, open_browser=True)
    process.start()
    try:
        process.wait_ready()
        assert process.port is not None
        assert process.listening_at is not None and process.opening_at is not None
        session_id = fixture["largeSessionID"]
        paths = [
            "/api/v1/session?archived=false&limit=100&order=updatedDesc",
            "/api/v1/session?archived=true&limit=30&order=updatedDesc",
            "/api/v1/location/recent",
            f"/api/v1/session/{session_id}",
            f"/api/v1/session/{session_id}/message?limit=100&order=desc&branch=active",
            f"/api/v1/session/{session_id}/queue",
            f"/api/v1/session/{session_id}/permission",
            f"/api/v1/session/{session_id}/question",
        ]
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=len(paths)) as executor:
            timings = list(
                executor.map(lambda path: _request(process.port or 0, path), paths)
            )
        return {
            "listenSeconds": process.listening_at - process.started_at,
            "browserOpenSeconds": process.opening_at - process.started_at,
            "firstBundleSeconds": time.perf_counter() - started,
            "requests": [asdict(item) for item in timings],
        }
    finally:
        process.close()


def benchmark_restart_message(
    session_dir: Path,
    fixture: dict[str, Any],
) -> dict[str, Any]:
    process = ServerProcess(session_dir, open_browser=False)
    process.start()
    try:
        process.wait_ready()
        assert process.port is not None and process.listening_at is not None
        session_id = fixture["largeSessionID"]
        message = _request(
            process.port,
            f"/api/v1/session/{session_id}/message?limit=100&order=desc&branch=active",
        )
        return {
            "startupSeconds": process.listening_at - process.started_at,
            "message": asdict(message),
        }
    finally:
        process.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=500)
    parser.add_argument("--large-mib", type=int, default=64)
    parser.add_argument("--events", type=int, default=20_000)
    parser.add_argument("--fixture-dir", type=Path)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    created_temp = args.fixture_dir is None
    fixture_dir = args.fixture_dir or Path(tempfile.mkdtemp(prefix="yoke-http-perf-"))
    try:
        fixture = build_fixture(
            fixture_dir,
            session_count=args.sessions,
            large_mib=args.large_mib,
            event_count=args.events,
        )
        result = {
            "time": datetime.now(UTC).isoformat(),
            "fixture": fixture,
            "api": benchmark_server(fixture_dir, fixture),
            "restart": benchmark_restart_message(fixture_dir, fixture),
            "serveOpen": benchmark_open_startup(fixture_dir),
        }
        _make_index_stale(fixture_dir)
        result["serveOpenStaleIndex"] = benchmark_stale_startup_open(
            fixture_dir,
            fixture,
        )
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps(result, indent=2))
    finally:
        if created_temp and not args.keep:
            shutil.rmtree(fixture_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
