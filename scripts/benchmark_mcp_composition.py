"""Measure local MCP envelopes for six known reads; no model latency claims."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import tempfile
import time

from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.server import create_service


async def benchmark(repeats: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="yoke-benchmark-") as directory:
        root = Path(directory)
        paths = [f"file-{i}.txt" for i in range(6)]
        for path in paths:
            (root / path).write_text("source evidence\n" * 20)
        service = create_service(MCPServerConfig(root=root))
        async with InMemoryTransport(service.server) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                results = {}
                for mode in ("parallel_direct", "batch"):
                    elapsed = []
                    response_bytes = []
                    for _ in range(repeats):
                        started = time.perf_counter()
                        if mode == "parallel_direct":
                            calls = await asyncio.gather(
                                *(
                                    client.call_tool("read_file", {"path": path})
                                    for path in paths
                                )
                            )
                        else:
                            calls = [
                                await client.call_tool(
                                    "batch_read",
                                    {
                                        "items": [
                                            {
                                                "id": str(i),
                                                "tool": "read_file",
                                                "arguments": {"path": path},
                                            }
                                            for i, path in enumerate(paths)
                                        ]
                                    },
                                )
                            ]
                        assert all(not result.is_error for result in calls)
                        if mode == "batch":
                            assert calls[0].structured_content is not None
                            assert all(
                                item["status"] == "ok"
                                for item in calls[0].structured_content["items"]
                            )
                        elapsed.append((time.perf_counter() - started) * 1000)
                        response_bytes.append(
                            sum(
                                len(result.model_dump_json().encode())
                                for result in calls
                            )
                        )
                    ordered = sorted(elapsed)
                    results[mode] = {
                        "outer_calls": 6 if mode == "parallel_direct" else 1,
                        "file_reads": 6,
                        "runs": repeats,
                        "p50_ms": round(statistics.median(elapsed), 3),
                        "p95_ms": round(
                            ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3
                        ),
                        "median_response_bytes": statistics.median(response_bytes),
                    }
                return {
                    "transport": "in-memory MCP SDK",
                    "task": "six known files",
                    "results": results,
                    "limits": "No ChatGPT/model latency or token measurements. Direct reads run in parallel.",
                }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=20)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    print(json.dumps(asyncio.run(benchmark(args.repeats)), indent=2))


if __name__ == "__main__":
    main()
