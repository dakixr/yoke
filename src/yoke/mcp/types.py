"""Shared MCP value types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

JSON = dict[str, Any]


@dataclass(slots=True, frozen=True)
class McpToolInfo:
    """Compact MCP tool metadata."""

    name: str
    description: str
    input_schema: JSON

    def without_schema(self) -> McpToolInfo:
        """Return this tool metadata without input schema details."""
        if not self.input_schema:
            return self
        return McpToolInfo(
            name=self.name,
            description=self.description,
            input_schema={},
        )
