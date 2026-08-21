"""Command-line entry point for the Yoke MCP server."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.config import env_bool
from yoke.mcp_server.config import env_hosts
from yoke.mcp_server.config import env_int
from yoke.mcp_server.server import create_service


def main() -> None:
    """Load configuration and run one async ASGI worker."""
    config = parse_config()
    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    service = create_service(config)
    logging.getLogger(__name__).info(
        "Starting Yoke MCP on %s:%s with root %s",
        config.host,
        config.port,
        config.root,
    )
    uvicorn.run(
        service.app,
        host=config.host,
        port=config.port,
        workers=1,
        log_level=config.log_level,
    )


def parse_config(argv: list[str] | None = None) -> MCPServerConfig:
    """Parse CLI flags with environment-backed defaults."""
    parser = argparse.ArgumentParser(prog="yoke-mcp")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("YOKE_MCP_ROOT", Path.cwd())),
    )
    parser.add_argument("--host", default=os.environ.get("YOKE_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=env_int("YOKE_MCP_PORT", 8765))
    parser.add_argument(
        "--default-yield-ms",
        type=int,
        default=env_int("YOKE_MCP_DEFAULT_YIELD_MS", 30_000),
    )
    parser.add_argument(
        "--python-timeout",
        type=int,
        default=env_int("YOKE_MCP_PYTHON_TIMEOUT", 180),
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=env_int("YOKE_MCP_MAX_OUTPUT_TOKENS", 20_000),
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        dest="allowed_hosts",
        default=None,
    )
    parser.add_argument(
        "--log-level", default=os.environ.get("YOKE_MCP_LOG_LEVEL", "info")
    )
    args = parser.parse_args(argv)
    return MCPServerConfig(
        root=args.root,
        host=args.host,
        port=args.port,
        default_yield_ms=args.default_yield_ms,
        python_timeout=args.python_timeout,
        max_output_tokens=args.max_output_tokens,
        allowed_hosts=tuple(args.allowed_hosts or env_hosts()),
        bearer_token=os.environ.get("YOKE_MCP_BEARER_TOKEN") or None,
        log_tool_inputs=env_bool("YOKE_MCP_LOG_TOOL_INPUTS"),
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
