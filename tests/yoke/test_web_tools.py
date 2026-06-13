from __future__ import annotations

# ruff: noqa: ANN401, D100, D101, D102, D103, S101

from typing import Any
from typing import cast

from yoke.agent.models import Message
from yoke.agent.tools.web import WebFetchTool
from yoke.agent.tools.web import WebResearchTool
from yoke.agent.tools.web import _search_terms
from yoke.agent.tools.web import _web_search


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


class SynthesizingProvider:
    supports_image_inputs = False
    max_images_per_message = None

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        functions = [cast(dict[str, object], tool["function"]) for tool in tools]
        assert [function["name"] for function in functions] == ["web_fetch"]
        prompt = str(messages[-1].content)
        assert "Fetched source payload" in prompt
        return Message(
            role="assistant",
            content=(
                '{"answer":"Use Path.resolve() to make a path absolute '
                'and resolve symlinks.",'
                '"notes":["Use official docs when available."],'
                '"sources":[{"title":"pathlib docs",'
                '"url":"https://docs.python.org/3/library/pathlib.html",'
                '"quote":"Make the path absolute, resolving any symlinks."}]}'
            ),
        )


def test_web_fetch_returns_agent_metadata(monkeypatch: Any) -> None:
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
        mode="metadata",
        max_chars=2000,
    ).execute()

    assert result["ok"] is True
    assert "Example Docs" in str(result["content"])
    assert result["summary"]
    assert result["chunks"]
    assert result["links"] == [
        {"url": "https://docs.example.test/api", "text": "API reference"}
    ]
    details = cast(dict[str, object], result["details"])
    assert details["title"] == "Example Docs"


def test_web_fetch_find_filters_content(monkeypatch: Any) -> None:
    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        return FakeResponse(
            "<html><body><p>Alpha setup notes.</p>"
            "<p>Beta deploy guide.</p></body></html>"
        )

    monkeypatch.setattr("httpx.get", fake_get)

    result = WebFetchTool(
        url="https://example.test/page",
        find="deploy",
        max_chars=2000,
    ).execute()

    assert result["ok"] is True
    assert "deploy" in str(result["content"]).lower()
    assert "Alpha" not in str(result["content"])


def test_web_fetch_find_keeps_nearby_heading_context(monkeypatch: Any) -> None:
    html = (
        "<html><body><h2>Install</h2><p>General setup.</p>"
        "<h2>Path resolve</h2><p>Resolve removes dot segments.</p>"
        "<p>Use strict when missing paths should fail.</p></body></html>"
    )

    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        return FakeResponse(html)

    monkeypatch.setattr("httpx.get", fake_get)

    result = WebFetchTool(
        url="https://example.test/page",
        find="Path resolve strict",
        max_chars=2000,
    ).execute()

    content = str(result["content"])
    assert result["ok"] is True
    assert "Path resolve" in content
    assert "strict" in content


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


def test_internal_web_search_returns_empty_results_list(
    monkeypatch: Any,
) -> None:
    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse("<html><body>No matches</body></html>")

    monkeypatch.setattr("httpx.Client", FakeClient)

    result = _web_search("query with no matches", max_results=1)

    assert result == {"ok": True, "results": []}


def test_web_research_combines_search_and_fetch(monkeypatch: Any) -> None:
    monkeypatch.setattr(WebResearchTool, "fetched_source_target", 1)
    search_html = """
    <a class="result__a" href="https://docs.example.test/guide">Guide</a>
    <a class="result__snippet">Official guide text.</a>
    """
    page_html = (
        "<html><head><title>Guide</title></head>"
        "<body><p>Use example safely.</p></body></html>"
    )

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse(search_html)

    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        return FakeResponse(page_html, url="https://docs.example.test/guide")

    monkeypatch.setattr("httpx.Client", FakeClient)
    monkeypatch.setattr("httpx.get", fake_get)

    result = WebResearchTool(question="How to use example?").execute()

    assert result["ok"] is True
    assert "Research brief" in str(result["answer"])
    assert result["notes"]
    sources = cast(list[dict[str, object]], result["sources"])
    assert sources[0]["url"] == "https://docs.example.test/guide"


def test_web_research_falls_back_to_raw_question(monkeypatch: Any) -> None:
    monkeypatch.setattr(WebResearchTool, "fetched_source_target", 1)
    calls: list[str] = []

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, *args: object, **kwargs: object) -> FakeResponse:
            params = cast(dict[str, str], kwargs.get("params", {}))
            query = params.get("q", "")
            calls.append(query)
            html = ""
            if query == "What is ExampleThing?":
                html = (
                    '<a class="result__a" href="https://docs.example.test/example">'
                    "ExampleThing docs</a>"
                )
            return FakeResponse(html)

    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        return FakeResponse(
            "<html><body><h1>ExampleThing</h1>"
            "<p>ExampleThing is documented here.</p></body></html>",
            url="https://docs.example.test/example",
        )

    monkeypatch.setattr("httpx.Client", FakeClient)
    monkeypatch.setattr("httpx.get", fake_get)

    result = WebResearchTool(question="What is ExampleThing?").execute()

    assert calls[-1] == "What is ExampleThing?"
    assert result["sources"]


def test_search_terms_prioritize_symbol_like_terms() -> None:
    terms = _search_terms(
        "What is Python pathlib Path.resolve() and when should it be used?"
    )

    assert terms[0] == "path.resolve"
    assert "python" in terms
    assert "pathlib" in terms
    assert "used" not in terms


def test_web_research_uses_precise_find_terms_for_docs_pages(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(WebResearchTool, "fetched_source_target", 1)
    search_html = """
    <a class="result__a" href="https://docs.python.org/3/library/pathlib.html">
      pathlib — Object-oriented filesystem paths
    </a>
    <a class="result__snippet">
      Path.resolve() resolves symbolic links and removes "."/".." components.
    </a>
    """
    page_html = """
    <html><body>
      <h1>pathlib — Object-oriented filesystem paths</h1>
      <nav><h2>This page</h2><p>Path.resolve() | Path.glob()</p></nav>
      <h2>Path.resolve()</h2>
      <p>Path.resolve() makes the path absolute and resolves symlinks.</p>
      <p>Use strict=True to raise an error for missing paths.</p>
      <h2>Table of contents</h2>
      <p>Path.expanduser() | Path.glob()</p>
    </body></html>
    """

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse(search_html)

    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        return FakeResponse(
            page_html,
            url="https://docs.python.org/3/library/pathlib.html",
        )

    monkeypatch.setattr("httpx.Client", FakeClient)
    monkeypatch.setattr("httpx.get", fake_get)

    result = WebResearchTool(
        question=("What is Python pathlib Path.resolve() and when should it be used?"),
    ).execute()

    sources = cast(list[dict[str, object]], result["sources"])
    quote = str(sources[0]["quote"])
    assert "Path.resolve()" in quote
    assert "Table of contents" not in quote


def test_web_research_uses_provider_for_structured_synthesis(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(WebResearchTool, "fetched_source_target", 1)
    search_html = """
    <a class="result__a" href="https://docs.python.org/3/library/pathlib.html">
      pathlib docs
    </a>
    <a class="result__snippet">Path.resolve() official docs.</a>
    """

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse(search_html)

    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        return FakeResponse(
            "<html><body><h2>Path.resolve()</h2>"
            "<p>Make the path absolute, resolving symlinks.</p></body></html>",
            url="https://docs.python.org/3/library/pathlib.html",
        )

    monkeypatch.setattr("httpx.Client", FakeClient)
    monkeypatch.setattr("httpx.get", fake_get)
    tool = WebResearchTool(question="What is Python pathlib Path.resolve()?")
    tool._bind_context(provider=SynthesizingProvider())

    result = tool.execute()

    assert result["ok"] is True
    assert "resolve symlinks" in str(result["answer"])
    assert result["notes"] == ["Use official docs when available."]
    sources = cast(list[dict[str, object]], result["sources"])
    assert sources[0] == {
        "title": "pathlib docs",
        "url": "https://docs.python.org/3/library/pathlib.html",
        "quote": "Make the path absolute, resolving any symlinks.",
    }
