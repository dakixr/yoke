from __future__ import annotations

# ruff: noqa: ANN401, D100, D101, D102, D103, S101

from collections.abc import Callable, Iterator
from typing import Any
from typing import cast

import httpx
import pytest

from yoke.agent.tools.web import _web_search


type ResponseHandler = Callable[[httpx.Request], httpx.Response]
type MockHTTP = Callable[[ResponseHandler], None]


@pytest.fixture
def mock_http(monkeypatch: pytest.MonkeyPatch) -> Iterator[MockHTTP]:
    client_type = httpx.Client
    clients: list[httpx.Client] = []

    def install(handler: ResponseHandler) -> None:
        def create_client(**kwargs: Any) -> httpx.Client:
            client = client_type(transport=httpx.MockTransport(handler), **kwargs)
            clients.append(client)
            return client

        monkeypatch.setattr(httpx, "Client", create_client)

    yield install
    try:
        assert all(client.is_closed for client in clients)
    finally:
        for client in clients:
            client.close()


def test_internal_web_search_returns_agent_fields(mock_http: MockHTTP) -> None:
    html = """
    <a class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.test%2Fguide">
       Guide
    </a>
    <a class="result__snippet">Official guide text.</a>
    """

    mock_http(lambda _request: httpx.Response(200, text=html))

    result = _web_search("example guide", max_results=1)

    assert result["ok"] is True
    results = cast(list[dict[str, object]], result["results"])
    first = results[0]
    assert first["url"] == "https://docs.example.test/guide"
    assert first["domain"] == "docs.example.test"
    assert first["sourceType"] == "docs"


def test_internal_web_search_skips_duckduckgo_ad_links(mock_http: MockHTTP) -> None:
    html = """
    <a class="result__a" href="https://duckduckgo.com/y.js?ad_provider=bingv7aa">
      Sponsored result
    </a>
    <a class="result__snippet">Sponsored text.</a>
    <a class="result__a" href="https://docs.example.test/guide">Guide</a>
    <a class="result__snippet">Official guide text.</a>
    """

    mock_http(lambda _request: httpx.Response(200, text=html))

    result = _web_search("example guide", max_results=1)

    results = cast(list[dict[str, str]], result["results"])
    assert [item["url"] for item in results] == ["https://docs.example.test/guide"]


def test_internal_web_search_returns_empty_results_list(mock_http: MockHTTP) -> None:
    mock_http(
        lambda _request: httpx.Response(
            200, text="<html><body>No matches</body></html>"
        )
    )

    result = _web_search("query with no matches", max_results=1)

    assert result["ok"] is True
    assert result["results"] == []
    assert result["exhausted"] is True
    assert result["requestedResults"] == 1
    assert result["returnedResults"] == 0


def test_internal_web_search_falls_back_when_duckduckgo_challenges(
    mock_http: MockHTTP,
) -> None:
    calls: list[str] = []
    bing_rss = """
    <rss><channel><item>
      <title>Python &amp; Documentation</title>
      <link>https://docs.python.org/3/</link>
      <description>Official &lt;b&gt;Python&lt;/b&gt; documentation.</description>
    </item></channel></rss>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.copy_with(query=None)))
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(
                202, text='<div class="anomaly-modal">bots use DuckDuckGo too</div>'
            )
        return httpx.Response(200, text=bing_rss)

    mock_http(handler)

    result = _web_search("Python documentation", max_results=3)

    assert result["ok"] is True
    assert result["provider"] == "bing"
    assert calls == [
        "https://html.duckduckgo.com/html/",
        "https://www.bing.com/search",
    ]
    results = cast(list[dict[str, str]], result["results"])
    assert results == [
        {
            "title": "Python & Documentation",
            "url": "https://docs.python.org/3/",
            "domain": "docs.python.org",
            "sourceType": "docs",
            "snippet": "Official Python documentation.",
        }
    ]
    assert "blocked" in str(result["note"])


def test_internal_web_search_reports_failed_challenge_fallback(
    mock_http: MockHTTP,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(202, text="challenge")
        return httpx.Response(503, text="unavailable")

    mock_http(handler)

    result = _web_search("Python documentation", max_results=3)

    assert result["ok"] is False
    assert "DuckDuckGo blocked" in str(result["error"])
    assert "Bing RSS fallback failed" in str(result["error"])


def test_internal_web_search_reports_partial_results(mock_http: MockHTTP) -> None:
    html = """
    <a class="result__a" href="https://docs.example.test/guide">Guide</a>
    <a class="result__snippet">Official guide text.</a>
    """

    mock_http(lambda _request: httpx.Response(200, text=html))

    result = _web_search("example guide", max_results=3)

    assert result["ok"] is True
    assert len(cast(list[object], result["results"])) == 1
    assert result["exhausted"] is True
    assert result["requestedResults"] == 3
    assert result["returnedResults"] == 1
