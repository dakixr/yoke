"""Explicitly configured, schema-pinned downstream wrappers."""

from pathlib import Path
import json
from copy import deepcopy
from urllib.parse import urljoin
from typing import Any

from mcp.types import Tool, ToolAnnotations
from pydantic import Field

from yoke.mcp_server.execution.gateway import schema_hash
from yoke.mcp_server.execution.models import Request


class Wrapper(Request):
    name: str = Field(pattern=r"^downstream_[a-z0-9_]+$")
    server: str
    tool: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    read_only: bool = False

    def descriptor(self) -> Tool:
        return Tool(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            output_schema={
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "structuredContent": embed_output_schema(self.output_schema),
                },
                "required": ["ok"],
            },
            annotations=ToolAnnotations(
                read_only_hint=self.read_only,
                destructive_hint=not self.read_only,
                idempotent_hint=self.read_only,
                open_world_hint=True,
            ),
        )

    @property
    def digest(self) -> str:
        return schema_hash(self.input_schema)


def load(path: Path | None) -> dict[str, Wrapper]:
    if path is None:
        return {}
    values = json.loads(path.expanduser().read_text())
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError(
            "Wrapper configuration must be a list of at most 32 reviewed tools"
        )
    wrappers = [Wrapper.model_validate(value) for value in values]
    if len({w.name for w in wrappers}) != len(wrappers):
        raise ValueError("Wrapper names must be unique")
    from jsonschema import Draft202012Validator

    for wrapper in wrappers:
        Draft202012Validator.check_schema(wrapper.input_schema)
    return {wrapper.name: wrapper for wrapper in wrappers}


def embed_output_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Make the nested schema a resource, preserving its local reference scope.

    An absolute root $id stays unchanged. Relative root IDs resolve against a
    stable hierarchical base; nested IDs, anchors and recursive refs retain
    their normal JSON Schema semantics without rewriting annotations or refs.
    """
    if schema is None:
        return {}
    embedded = deepcopy(schema)
    base = (
        f"https://schemas.yoke.invalid/wrapper-output/{schema_hash(schema)}/schema.json"
    )
    embedded["$id"] = urljoin(base, str(schema.get("$id", "")))
    return embedded
