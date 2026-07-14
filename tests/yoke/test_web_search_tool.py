from __future__ import annotations

# ruff: noqa: ANN401, D100, D101, D102, D103, S101

from typing import Any
from typing import cast

from yoke.agent.tools.web import _web_search


class FakeResponse:
    def __init__(self, text: str, *, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_internal_web_search_returns_agent_fields(monkeypatch: Any) -> None:
    html = """
    <a class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.test%2Fguide">
       Guide
    </a>
    <a class="result__snippet">Official guide text.</a>
    """

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse(html)

    monkeypatch.setattr("httpx.Client", FakeClient)

    result = _web_search("example guide", max_results=1)

    assert result["ok"] is True
    results = cast(list[dict[str, object]], result["results"])
    first = results[0]
    assert first["url"] == "https://docs.example.test/guide"
    assert first["domain"] == "docs.example.test"
    assert first["sourceType"] == "docs"


def test_internal_web_search_skips_duckduckgo_ad_links(monkeypatch: Any) -> None:
    html = """
    <a class="result__a" href="https://duckduckgo.com/y.js?ad_provider=bingv7aa">
      Sponsored result
    </a>
    <a class="result__snippet">Sponsored text.</a>
    <a class="result__a" href="https://docs.example.test/guide">Guide</a>
    <a class="result__snippet">Official guide text.</a>
    """

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse(html)

    monkeypatch.setattr("httpx.Client", FakeClient)

    result = _web_search("example guide", max_results=1)

    results = cast(list[dict[str, str]], result["results"])
    assert [item["url"] for item in results] == ["https://docs.example.test/guide"]


def test_internal_web_search_returns_empty_results_list(monkeypatch: Any) -> None:
    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse("<html><body>No matches</body></html>")

    monkeypatch.setattr("httpx.Client", FakeClient)

    result = _web_search("query with no matches", max_results=1)

    assert result["ok"] is True
    assert result["results"] == []
    assert result["exhausted"] is True
    assert result["requestedResults"] == 1
    assert result["returnedResults"] == 0


def test_internal_web_search_falls_back_when_duckduckgo_challenges(
    monkeypatch: Any,
) -> None:
    calls: list[str] = []
    bing_rss = """
    <rss><channel><item>
      <title>Python &amp; Documentation</title>
      <link>https://docs.python.org/3/</link>
      <description>Official &lt;b&gt;Python&lt;/b&gt; documentation.</description>
    </item></channel></rss>
    """

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, url: str, **kwargs: object) -> FakeResponse:
            calls.append(url)
            if "duckduckgo" in url:
                return FakeResponse(
                    '<div class="anomaly-modal">bots use DuckDuckGo too</div>',
                    status_code=202,
                )
            return FakeResponse(bing_rss)

    monkeypatch.setattr("httpx.Client", FakeClient)

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
    monkeypatch: Any,
) -> None:
    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, url: str, **kwargs: object) -> FakeResponse:
            if "duckduckgo" in url:
                return FakeResponse("challenge", status_code=202)
            return FakeResponse("unavailable", status_code=503)

    monkeypatch.setattr("httpx.Client", FakeClient)

    result = _web_search("Python documentation", max_results=3)

    assert result["ok"] is False
    assert "DuckDuckGo blocked" in str(result["error"])
    assert "Bing RSS fallback failed" in str(result["error"])


def test_internal_web_search_reports_partial_results(monkeypatch: Any) -> None:
    html = """
    <a class="result__a" href="https://docs.example.test/guide">Guide</a>
    <a class="result__snippet">Official guide text.</a>
    """

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse(html)

    monkeypatch.setattr("httpx.Client", FakeClient)

    result = _web_search("example guide", max_results=3)

    assert result["ok"] is True
    assert len(cast(list[object], result["results"])) == 1
    assert result["exhausted"] is True
    assert result["requestedResults"] == 3
    assert result["returnedResults"] == 1
