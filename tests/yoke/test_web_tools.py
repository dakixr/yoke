from __future__ import annotations

# ruff: noqa: ANN401, D100, D101, D102, D103, S101

import json
import sys
from types import ModuleType
from typing import Any
from typing import cast

from yoke.agent.models import Message
from yoke.agent.tools.web import WebFetchTool
from yoke.agent.tools.web import WebResearchTool
from yoke.agent.tools.web.fetch import clear_fetch_cache
from yoke.agent.tools.web.fetch import web_search


class FakeResponse:
    def __init__(
        self,
        text: str,
        *,
        url: str = "https://example.test/page",
        content_type: str = "text/html; charset=utf-8",
        status_code: int = 200,
    ) -> None:
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": content_type}
        self.status_code = status_code
        self.url = url

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        import json

        return json.loads(self.text)


class HostedSearchProvider:
    provider_name = "codex"
    supports_image_inputs = True
    max_images_per_message = None

    def __init__(self) -> None:
        self.calls: list[tuple[list[Message], list[dict[str, object]]]] = []

    def complete_with_cancel(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested,
    ) -> Message:
        assert cancel_requested() is False
        self.calls.append((messages, tools))
        return Message.assistant(
            "Current answer with source: https://example.test/current"
        )


def test_web_fetch_returns_agent_metadata(monkeypatch: Any) -> None:
    clear_fetch_cache()

    class FakeMarkItDown:
        def convert_stream(self, *args: object, **kwargs: object) -> object:
            class Converted:
                text_content = (
                    "# Install\n\nUse pip install example.\n\n"
                    "[API reference](/api)\n\n```\npip install example\n```"
                )

            return Converted()

    fake_markitdown = ModuleType("markitdown")
    cast(Any, fake_markitdown).MarkItDown = FakeMarkItDown
    monkeypatch.setitem(sys.modules, "markitdown", fake_markitdown)

    html = """
    <html><head><title>Example Docs</title></head><body>
    <h1>Install</h1><p>Use pip install example.</p>
    <a href="/api">API reference</a><pre>pip install example</pre>
    </body></html>
    """

    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        return FakeResponse(html, url="https://docs.example.test/install")

    monkeypatch.setattr("httpx.get", fake_get)

    result = WebFetchTool(
        url="https://docs.example.test/install",
        mode="main_content",
        limit=2000,
    ).execute()

    assert result["ok"] is True
    assert "Install" in str(result["content"])
    assert "Use pip install example." in str(result["content"])
    details = cast(dict[str, object], result["details"])
    assert details["title"] == "Example Docs"


def test_web_fetch_pages_cached_raw_content_by_character(
    monkeypatch: Any,
) -> None:
    clear_fetch_cache()
    calls = 0

    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse(
            "abcdefghij",
            url="https://example.test/data.js",
            content_type="application/javascript",
        )

    monkeypatch.setattr("httpx.get", fake_get)

    first = WebFetchTool(
        url="https://example.test/data.js",
        mode="raw",
        limit=4,
    ).execute()
    second = WebFetchTool(
        url="https://example.test/data.js",
        mode="raw",
        offset=5,
        limit=4,
    ).execute()

    assert first["content"] == "abcd"
    assert first["next_offset"] == 5
    assert first["total_chars"] == 10
    assert second["content"] == "efgh"
    assert second["next_offset"] == 9
    assert calls == 1


def test_web_fetch_does_not_duplicate_large_raw_content(
    monkeypatch: Any,
) -> None:
    clear_fetch_cache()
    raw = "x" * 1_000_000

    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        return FakeResponse(
            raw,
            url="https://example.test/bundle.min.js",
            content_type="application/javascript",
        )

    monkeypatch.setattr("httpx.get", fake_get)
    result = WebFetchTool(
        url="https://example.test/bundle.min.js",
        mode="raw",
        limit=500,
    ).execute()

    assert result["content"] == "x" * 500
    assert result["next_offset"] == 501
    assert result["total_chars"] == 1_000_000
    assert "chunks" not in result
    assert "summary" not in result
    assert len(json.dumps(result)) < 2_000


def test_web_fetch_schema_has_simple_paging_api() -> None:
    properties = WebFetchTool.model_json_schema()["properties"]

    assert set(properties) == {"url", "mode", "offset", "limit", "timeout_s"}
    assert properties["offset"]["default"] == 1
    assert properties["limit"]["default"] == 20_000


def test_web_research_executes_in_process_for_provider_synthesis() -> None:
    assert WebResearchTool.execute_in_process is True


def test_web_research_prefers_codex_hosted_search() -> None:
    provider = HostedSearchProvider()
    tool = WebResearchTool.bind(provider=provider)
    parsed = tool.parse_arguments({"question": "What changed?"})

    result = parsed.execute()

    assert result["ok"] is True
    assert result["provider"] == "codex-hosted"
    assert "https://example.test/current" in str(result["answer"])
    messages, tools = provider.calls[0]
    assert messages[-1].plain_text_content == "What changed?"
    assert tools == [
        {
            "type": "web_search",
            "external_web_access": True,
            "search_context_size": "high",
        }
    ]


def test_web_search_closes_http_client(monkeypatch: Any) -> None:
    closed = False

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            assert "verify" not in kwargs

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            nonlocal closed
            closed = True

        def close(self) -> None:
            nonlocal closed
            closed = True

        def get(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse(
                '<a class="result__a" href="https://example.test">Example</a>'
            )

    monkeypatch.setattr("httpx.Client", FakeClient)

    result = web_search("example")

    assert result["ok"] is True
    assert closed is True


def test_web_research_applies_aggregate_source_budget(monkeypatch: Any) -> None:
    search_results = [
        {
            "title": f"Source {index}",
            "url": f"https://example{index}.test/page",
            "snippet": "snippet",
        }
        for index in range(2)
    ]
    fetch_limits: list[int] = []

    monkeypatch.setattr(
        "yoke.agent.tools.web.research.web_search",
        lambda *args, **kwargs: {"ok": True, "results": search_results},
    )

    def fake_fetch(tool: WebFetchTool) -> dict[str, object]:
        fetch_limits.append(tool.limit)
        return {
            "ok": True,
            "content": "x" * tool.limit,
            "details": {},
        }

    monkeypatch.setattr(WebFetchTool, "execute", fake_fetch)
    monkeypatch.setattr(WebResearchTool, "source_character_budget", 6)

    result = WebResearchTool(question="example").execute()

    assert result["ok"] is True
    assert fetch_limits == [6]
