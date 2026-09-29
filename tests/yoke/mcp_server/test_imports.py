"""A Python bridge subprocess must not initialize an unused MCP server."""

from __future__ import annotations

import subprocess
import sys


def test_bridge_import_loads_only_its_client_dependencies() -> None:
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from yoke.mcp_server.execution import client; "
            "assert callable(client.configure); "
            "assert callable(client.output.emit); "
            "assert 'yoke.mcp_server.server' not in sys.modules; "
            "assert 'mcp' not in sys.modules; "
            "assert 'pydantic' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr


def test_lazy_public_exports_retain_the_original_objects() -> None:
    import yoke.mcp_server as package
    from yoke.mcp_server.config import MCPServerConfig
    from yoke.mcp_server.server import MCPService, create_service

    assert package.MCPServerConfig is MCPServerConfig
    assert package.MCPService is MCPService
    assert package.create_service is create_service
    assert package.create_service is package.create_service
    assert set(package.__all__) == {"MCPServerConfig", "MCPService", "create_service"}
