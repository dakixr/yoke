"""Native MCP image result and bounded image validation tests."""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import math
from pathlib import Path

from mcp.types import ImageContent
from PIL import Image
import pytest

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.files import MAX_VIEW_IMAGE_BYTES
from yoke.mcp_server.files import MAX_VIEW_IMAGE_WIRE_BYTES
from yoke.mcp_server.files import MAX_VIEW_IMAGE_WIRE_OVERHEAD
from yoke.mcp_server.files import read_image_bytes
from yoke.mcp_server.files import validate_image_bytes
from yoke.mcp_server.files import ViewImageError
from yoke.mcp_server.server import create_service

from .helpers import memory_client
from .helpers import structured


def _image_bytes(image_format: str, *, size: tuple[int, int] = (3, 2)) -> bytes:
    output = BytesIO()
    with Image.new("RGB", size, color=(25, 100, 200)) as image:
        image.save(output, format=image_format)
    return output.getvalue()


def test_view_image_returns_exact_native_content_for_supported_formats(
    tmp_path: Path,
) -> None:
    cases = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "GIF": "image/gif",
        "WEBP": "image/webp",
    }
    files = {}
    for image_format in cases:
        data = _image_bytes(image_format)
        path = tmp_path / f"sample-{image_format.lower()}.bin"
        path.write_bytes(data)
        files[image_format] = (path, data)

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            for image_format, mime_type in cases.items():
                path, expected = files[image_format]
                result = await client.call_tool("view_image", {"path": path.name})
                assert result.is_error is False
                assert result.structured_content is None
                assert len(result.content) == 1
                content = result.content[0]
                assert isinstance(content, ImageContent)
                assert content.mime_type == mime_type
                assert base64.b64decode(content.data, validate=True) == expected

    asyncio.run(scenario())


def test_view_image_keeps_yoke_path_semantics_and_detects_mime_from_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "inside.png"
    outside = tmp_path / "outside.png"
    disguised = root / "actually-jpeg.png"
    inside.write_bytes(_image_bytes("PNG"))
    outside.write_bytes(_image_bytes("PNG"))
    disguised.write_bytes(_image_bytes("JPEG"))

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=root))
        async with memory_client(service) as client:
            calls = (
                ("inside.png", "image/png"),
                ("../outside.png", "image/png"),
                (str(outside), "image/png"),
                ("actually-jpeg.png", "image/jpeg"),
            )
            for path, mime_type in calls:
                result = await client.call_tool("view_image", {"path": path})
                assert result.is_error is False
                content = result.content[0]
                assert isinstance(content, ImageContent)
                assert content.mime_type == mime_type

    asyncio.run(scenario())


def test_view_image_failures_use_structured_json_errors(tmp_path: Path) -> None:
    (tmp_path / "directory").mkdir()
    (tmp_path / "text.png").write_text("not an image", encoding="utf-8")
    png = _image_bytes("PNG", size=(20, 20))
    (tmp_path / "truncated.png").write_bytes(png[: len(png) // 2])
    (tmp_path / "unsupported.bmp").write_bytes(_image_bytes("BMP"))
    with (tmp_path / "oversized.png").open("wb") as handle:
        handle.truncate(MAX_VIEW_IMAGE_BYTES + 1)

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            calls = (
                {},
                {"path": "missing.png"},
                {"path": "directory"},
                {"path": "text.png"},
                {"path": "truncated.png"},
                {"path": "unsupported.bmp"},
                {"path": "oversized.png"},
            )
            for arguments in calls:
                result = await client.call_tool("view_image", arguments)
                payload = structured(result)
                assert result.is_error is True
                assert payload["ok"] is False
                assert payload["error"]
                assert all(
                    not isinstance(content, ImageContent) for content in result.content
                )

    asyncio.run(scenario())


def test_view_image_does_not_change_json_tool_results(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(_image_bytes("PNG"))

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            text_result = await client.call_tool("read_file", {"path": "note.txt"})
            image_result = await client.call_tool("view_image", {"path": "image.png"})
            error_result = await client.call_tool("view_image", {"path": "note.txt"})
            assert structured(text_result)["content"] == "hello\n"
            assert image_result.structured_content is None
            assert isinstance(image_result.content[0], ImageContent)
            assert structured(error_result)["ok"] is False
            assert error_result.is_error is True

    asyncio.run(scenario())


def test_malformed_internal_image_result_becomes_a_safe_tool_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "image.png").write_bytes(_image_bytes("PNG"))
    service = create_service(MCPServerConfig(root=tmp_path))

    async def malformed_execute(*_args: object) -> dict[str, object]:
        return {
            "ok": True,
            "mime_type": "image/png",
            "data_base64": "sensitive-invalid-payload",
            "bytes": 10,
        }

    monkeypatch.setattr(service.runtime, "execute", malformed_execute)

    async def scenario() -> None:
        async with memory_client(service) as client:
            result = await client.call_tool("view_image", {"path": "image.png"})
            payload = structured(result)
            assert result.is_error is True
            assert payload["ok"] is False
            assert "Invalid internal tool result" in payload["error"]
            assert "sensitive-invalid-payload" not in str(payload)

    asyncio.run(scenario())


def test_view_image_enforces_independent_resource_limits(tmp_path: Path) -> None:
    image_path = tmp_path / "image.png"
    data = _image_bytes("PNG", size=(4, 4))
    image_path.write_bytes(data)

    with pytest.raises(ViewImageError, match="compressed size"):
        read_image_bytes(image_path, max_bytes=len(data) - 1)
    with pytest.raises(ViewImageError, match="too many pixels"):
        validate_image_bytes(data, max_pixels=15)
    with pytest.raises(ViewImageError, match="decoded size"):
        validate_image_bytes(data, max_decoded_bytes=47)

    encoded_size = 4 * math.ceil(MAX_VIEW_IMAGE_BYTES / 3)
    assert encoded_size + MAX_VIEW_IMAGE_WIRE_OVERHEAD <= MAX_VIEW_IMAGE_WIRE_BYTES
