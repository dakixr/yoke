"""Lazy public exports retain their original objects."""

from __future__ import annotations


def test_lazy_public_exports_retain_the_original_objects() -> None:
    import yoke.mcp_server as package
    from yoke.mcp_server.config import MCPServerConfig
    from yoke.mcp_server.server import MCPService, create_service

    assert package.MCPServerConfig is MCPServerConfig
    assert package.MCPService is MCPService
    assert package.create_service is create_service
    assert package.create_service is package.create_service
    assert set(package.__all__) == {"MCPServerConfig", "MCPService", "create_service"}
