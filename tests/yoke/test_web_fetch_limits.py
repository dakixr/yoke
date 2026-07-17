from __future__ import annotations

# ruff: noqa: ANN401, D100, D101, D102, D103, S101

import json
from typing import Any

from yoke.agent.tools.web import WebFetchTool
from yoke.agent.truncate import DEFAULT_MAX_BYTES


class FakeResponse:
    def __init__(
        self,
        text: str,
        *,
        content: bytes | None = None,
        content_type: str = "application/json",
    ) -> None:
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.headers = {"content-type": content_type}
        self.status_code = 200
        self.url = "https://api.example.test/records"

    def raise_for_status(self) -> None:
        pass

    def json(self) -> object:
        return json.loads(self.text)


def test_web_fetch_bounds_complete_result_for_large_single_line_api(
    monkeypatch: Any,
) -> None:
    api_body = json.dumps({"records": ["x" * 4000] * 100})

    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        del args, kwargs
        return FakeResponse(api_body)

    monkeypatch.setattr("httpx.get", fake_get)

    result = WebFetchTool(
        url="https://api.example.test/records",
        max_chars=50_000,
    ).execute()

    serialized = json.dumps(result, ensure_ascii=False).encode("utf-8")
    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["content"]
    assert result["chunks"] == []
    assert len(serialized) <= DEFAULT_MAX_BYTES


def test_web_fetch_rejects_response_over_five_mib(monkeypatch: Any) -> None:
    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        del args, kwargs
        return FakeResponse(
            "oversized",
            content=b"x" * (5 * 1024 * 1024 + 1),
        )

    monkeypatch.setattr("httpx.get", fake_get)

    result = WebFetchTool(url="https://api.example.test/all").execute()

    assert result["ok"] is False
    assert result["error"] == "response too large (exceeds 5 MiB limit)"
