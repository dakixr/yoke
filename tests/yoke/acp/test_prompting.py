"""Strict inline decoding, with no URI dereferencing or silent omissions."""

from __future__ import annotations

import base64

import pytest
from acp import RequestError
from acp import schema as s

from yoke.acp.prompting import decode_prompt
from yoke.agent.attachments import MAX_ATTACHMENT_BYTES, attachment_name
from tests.yoke.http.test_attachment_continuation import png


def resource(data: bytes = b"data", *, name: str = "file.bin") -> dict:
    return {
        "type": "resource",
        "resource": {
            "uri": "file:///must/never/read",
            "mimeType": "application/octet-stream",
            "blob": base64.b64encode(data).decode(),
        },
        "_meta": {"yokeAttachmentName": name},
    }


def test_text_is_exact_and_continuation_is_explicit() -> None:
    result = decode_prompt(
        [s.TextContentBlock(type="text", text=" \n exact  ")], {}, images=False
    )
    assert result.text == " \n exact  "
    assert not result.continuation
    result = decode_prompt([], {"yokeContinuation": True}, images=False)
    assert result.continuation and result.text == "" and not result.attachments


@pytest.mark.parametrize(
    ("blocks", "meta"),
    [
        ([], {}),
        ([{"type": "text", "text": "\n "}], {}),
        ([{"type": "text", "text": ""}], {"yokeContinuation": True}),
        ([resource()], {"yokeContinuation": True}),
        ([], {"yokeContinuation": "true"}),
        ([{"type": "audio"}], {}),
        ([{"type": "resource_link", "uri": "file:///etc/passwd"}], {}),
        ([{"type": "unknown"}], {}),
    ],
)
def test_invalid_and_unsupported_prompts(blocks, meta) -> None:
    with pytest.raises(RequestError):
        decode_prompt(blocks, meta, images=True)


@pytest.mark.parametrize(
    "encoded",
    ["%%bad", "a", "YQ==\n", "YQ===", "\ud800", "data:text/plain;base64,YQ=="],
)
def test_invalid_base64(encoded: str) -> None:
    block = resource()
    block["resource"]["blob"] = encoded
    with pytest.raises(RequestError):
        decode_prompt([block], {}, images=True)


def test_inline_resource_uri_is_metadata_only(tmp_path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("do not read")
    block = resource(b"inline", name="../../evil\r\n.bin")
    for uri in (secret.as_uri(), "https://untrusted.invalid/secret"):
        block["resource"]["uri"] = uri
        result = decode_prompt([block], {}, images=False)
        assert result.attachments[0].data == b"inline"
        assert result.attachments[0].name == "evil__.bin"
    block["resource"] = {
        "uri": secret.as_uri(),
        "text": "embedded\ntext",
        "mimeType": "text/plain",
    }
    assert (
        decode_prompt([block], {}, images=False).attachments[0].data
        == b"embedded\ntext"
    )


@pytest.mark.parametrize(
    "name", ["../..", "..\\..\\metadata.json", "/etc/passwd", "\x00\r\n", "x" * 300]
)
def test_sanitization_cannot_form_storage_paths(name: str) -> None:
    safe = attachment_name(name)
    assert safe not in {"", ".", ".."}
    assert len(safe) <= 128
    assert all(c not in safe for c in "/\\\x00\r\n")


def test_images_require_valid_bytes_and_model_support() -> None:
    block = {
        "type": "image",
        "mimeType": "image/png",
        "data": base64.b64encode(png()).decode(),
        "_meta": {"yokeAttachmentName": "original.png"},
    }
    with pytest.raises(RequestError, match="does not support"):
        decode_prompt([block], {}, images=False)
    assert decode_prompt([block], {}, images=True).attachments[0].data == png()
    block["mimeType"] = "image/jpeg"
    with pytest.raises(RequestError, match="MIME"):
        decode_prompt([block], {}, images=True)
    block["mimeType"] = "image/png"
    block["data"] = base64.b64encode(b"not an image").decode()
    with pytest.raises(RequestError, match="image"):
        decode_prompt([block], {}, images=True)


def test_limits_are_enforced_before_admission(monkeypatch) -> None:
    assert MAX_ATTACHMENT_BYTES == 20 * 1024 * 1024
    with pytest.raises(RequestError, match="20 attachments"):
        decode_prompt([resource()] * 21, {}, images=False)
    oversized = resource()
    oversized["resource"]["blob"] = "A" * (4 * ((MAX_ATTACHMENT_BYTES + 2) // 3) + 4)
    with pytest.raises(RequestError, match="oversized"):
        decode_prompt([oversized], {}, images=False)
    monkeypatch.setattr("yoke.acp.prompting.MAX_INLINE_BYTES", 6)
    with pytest.raises(RequestError, match="aggregate"):
        decode_prompt([resource(), resource()], {}, images=False)
    monkeypatch.setattr("yoke.acp.prompting.MAX_PROMPT_JSON_BYTES", 10)
    with pytest.raises(RequestError, match="JSON limit"):
        decode_prompt([resource()], {}, images=False)
