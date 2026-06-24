from __future__ import annotations

# ruff: noqa: ANN401, D100, D101, D102, D103, S101

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

from yoke.agent.models import Message
from yoke.agent.tools import ModelIdentity
from yoke.agent.tools import ToolRuntimeContext
from yoke.agent.tools.web import WebFetchTool
from yoke.agent.tools.web import WebResearchTool
from yoke.agent.tools.web import WebSearchTool
from yoke.agent.tools.web import _search_terms
from yoke.agent.tools.web import _web_search
from yoke.agent.tools.web import recent_research_context
from yoke.ai.providers.codex.subscription import CodexSubscriptionProvider


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
    config = SimpleNamespace(timeout_seconds=600.0)

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        functions = [cast(dict[str, object], tool["function"]) for tool in tools]
        assert [function["name"] for function in functions] == [
            "web_fetch",
            "web_search",
        ]
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


class UnavailableSynthesisProvider:
    supports_image_inputs = False
    max_images_per_message = None

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del messages, tools
        raise RuntimeError("Synthesis unavailable")


class HostedSearchProvider(CodexSubscriptionProvider):
    def __init__(self) -> None:
        self.config = cast(Any, SimpleNamespace(timeout_seconds=600.0))
        self.calls: list[tuple[list[Message], list[dict[str, object]]]] = []

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.calls.append((messages, tools))
        return Message.assistant("Codex searched the web. Source: https://example.test")


class FailingHostedSearchProvider(HostedSearchProvider):
    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.calls.append((messages, tools))
        raise RuntimeError("hosted unavailable")


def bind_web_research(
    tool: WebResearchTool,
    provider: object | None = None,
    recent_messages: list[Message] | None = None,
) -> WebResearchTool:
    resolved_provider = provider or UnavailableSynthesisProvider()
    tool.bind_runtime_context(
        ToolRuntimeContext(
            root=Path.cwd(),
            home=Path.home(),
            provider=cast(Any, resolved_provider),
            model=ModelIdentity(provider_name="test", model_id="test-model"),
            recent_messages=tuple(recent_messages or ()),
        )
    )
    return tool


def test_web_research_uses_codex_hosted_web_search() -> None:
    provider = HostedSearchProvider()
    result = bind_web_research(
        WebResearchTool(question="what is current?"),
        provider,
    ).execute()

    assert result["ok"] is True
    assert result["answer"] == "Codex searched the web. Source: https://example.test"
    assert provider.calls
    messages, tools = provider.calls[0]
    assert messages[0].role == "system"
    assert "hosted web_search" in (messages[1].text_content() or "")
    assert tools == [
        {
            "type": "web_search",
            "external_web_access": True,
            "search_context_size": "high",
        }
    ]
    notes = cast(list[str], result["notes"])
    assert "Used Codex hosted web_search." in notes
    assert "sources" not in result


def test_web_research_passes_recent_context_to_codex_hosted_search() -> None:
    provider = HostedSearchProvider()
    recent_messages = [
        Message.system("hidden system"),
        Message.user("old user"),
        Message.assistant("old assistant"),
        Message.user("previous user"),
        Message.tool("call-1", "tool result"),
        Message.assistant("previous assistant"),
        Message.user("<environment_context>\n<cwd>/tmp</cwd>\n</environment_context>"),
        Message.user("current user"),
    ]

    result = bind_web_research(
        WebResearchTool(
            question="what changed?",
            research_context="Prefer official sources.",
            search_context_size="medium",
            web_search_mode="indexed",
            allowed_domains=["example.com"],
        ),
        provider,
        recent_messages=recent_messages,
    ).execute()

    assert result["ok"] is True
    messages, tools = provider.calls[0]
    prompt = messages[1].text_content() or ""
    assert "Prefer official sources." in prompt
    assert "User: previous user" in prompt
    assert "Assistant: previous assistant" in prompt
    assert "User: current user" in prompt
    assert "old user" not in prompt
    assert "hidden system" not in prompt
    assert "environment_context" not in prompt
    assert tools == [
        {
            "type": "web_search",
            "external_web_access": True,
            "search_context_size": "medium",
            "index_gated_web_access": True,
            "filters": {"allowed_domains": ["example.com"]},
        }
    ]


def test_recent_research_context_keeps_codex_style_text_tail() -> None:
    context = recent_research_context(
        [
            Message.user("old user"),
            Message.assistant("old assistant"),
            Message.user("previous user"),
            Message.assistant("previous assistant"),
            Message.user("<environment_context>\nignored\n</environment_context>"),
            Message.user("current user"),
        ]
    )

    assert context == (
        "User: previous user\nAssistant: previous assistant\nUser: current user"
    )


def test_web_research_executes_in_process_for_runtime_context() -> None:
    assert WebResearchTool.execute_in_process is True


def test_web_research_does_not_use_legacy_search_for_codex_failure(
    monkeypatch: Any,
) -> None:
    def fail_search(
        query: str, *, max_results: int, timeout_s: int
    ) -> dict[str, object]:
        del query, max_results, timeout_s
        raise AssertionError("legacy search should not run")

    monkeypatch.setattr("yoke.agent.tools.web.research.web_search", fail_search)

    result = bind_web_research(
        WebResearchTool(question="current info?"),
        FailingHostedSearchProvider(),
    ).execute()

    assert result["ok"] is False
    assert "Codex hosted web_search failed" in str(result["error"])


def test_web_research_unbound_tool_uses_legacy_fallback(monkeypatch: Any) -> None:
    monkeypatch.setattr(WebResearchTool, "fetched_source_target", 1)

    def fake_search(
        query: str, *, max_results: int, timeout_s: int
    ) -> dict[str, object]:
        del query, max_results, timeout_s
        return {
            "ok": True,
            "results": [
                {
                    "title": "Example",
                    "url": "https://example.test/docs",
                    "domain": "example.test",
                    "sourceType": "docs",
                    "snippet": "Example docs snippet.",
                }
            ],
        }

    def fake_fetch(self: WebFetchTool) -> dict[str, object]:
        return {
            "ok": True,
            "content": "Example docs fetched content.",
            "summary": "Example docs fetched content.",
        }

    monkeypatch.setattr("yoke.agent.tools.web.research.web_search", fake_search)
    monkeypatch.setattr(WebFetchTool, "execute", fake_fetch)

    result = WebResearchTool(question="Example docs?").execute()

    assert result["ok"] is True
    assert "fallback summary" in cast(list[str], result["notes"])[0]
    assert cast(list[dict[str, str]], result["sources"])[0]["url"] == (
        "https://example.test/docs"
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
    assert result["matched"] is True
    assert "deploy" in str(result["content"]).lower()
    assert "Alpha" not in str(result["content"])


def test_web_fetch_find_reports_no_match(monkeypatch: Any) -> None:
    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        return FakeResponse("<html><body><p>Alpha setup notes.</p></body></html>")

    monkeypatch.setattr("httpx.get", fake_get)

    result = WebFetchTool(
        url="https://example.test/page",
        find="deploy",
        max_chars=2000,
    ).execute()

    assert result["ok"] is True
    assert result["matched"] is False
    assert "No content matched" in str(result["note"])
    assert result["content"] == ""


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

    assert result["ok"] is True
    assert result["results"] == []
    assert result["exhausted"] is True
    assert result["requestedResults"] == 1
    assert result["returnedResults"] == 0


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


def test_web_search_tool_wraps_duckduckgo_search(monkeypatch: Any) -> None:
    calls: list[tuple[str, int, int]] = []

    def fake_search(
        query: str, *, max_results: int, timeout_s: int
    ) -> dict[str, object]:
        calls.append((query, max_results, timeout_s))
        return {"ok": True, "results": [{"title": "Example"}]}

    monkeypatch.setattr("yoke.agent.tools.web.fetch.web_search", fake_search)

    result = WebSearchTool(
        query="example",
        max_results=3,
        timeout_s=7,
    ).execute()

    assert result == {"ok": True, "results": [{"title": "Example"}]}
    assert calls == [("example", 3, 7)]


def test_web_research_combines_search_and_fetch(monkeypatch: Any) -> None:
    monkeypatch.setattr(WebResearchTool, "fetched_source_target", 1)
    fetch_kwargs: list[dict[str, object]] = []
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
        del args
        fetch_kwargs.append(dict(kwargs))
        return FakeResponse(page_html, url="https://docs.example.test/guide")

    monkeypatch.setattr("httpx.Client", FakeClient)
    monkeypatch.setattr("httpx.get", fake_get)

    result = bind_web_research(
        WebResearchTool(question="How to use example?")
    ).execute()

    assert result["ok"] is True
    assert "Research brief" in str(result["answer"])
    assert result["notes"]
    sources = cast(list[dict[str, object]], result["sources"])
    assert sources[0]["url"] == "https://docs.example.test/guide"
    assert fetch_kwargs[0]["timeout"] == 30


def test_web_research_uses_fast_fetch_without_markitdown(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(WebResearchTool, "fetched_source_target", 1)

    def fake_search(
        query: str, *, max_results: int, timeout_s: int
    ) -> dict[str, object]:
        del query, max_results, timeout_s
        return {
            "ok": True,
            "results": [
                {
                    "title": "Example",
                    "url": "https://example.test/docs",
                    "sourceType": "docs",
                    "snippet": "Example docs snippet.",
                }
            ],
        }

    seen_use_markitdown: list[bool] = []

    def fake_fetch(self: WebFetchTool) -> dict[str, object]:
        seen_use_markitdown.append(self.use_markitdown)
        return {
            "ok": True,
            "content": "Example docs fetched content.",
            "summary": "Example docs fetched content.",
        }

    monkeypatch.setattr("yoke.agent.tools.web.research.web_search", fake_search)
    monkeypatch.setattr(WebFetchTool, "execute", fake_fetch)

    result = WebResearchTool(question="Example docs?").execute()

    assert result["ok"] is True
    assert seen_use_markitdown == [False]


def test_web_research_keeps_fetching_until_source_target(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(WebResearchTool, "fetched_source_target", 2)

    def fake_search(
        query: str, *, max_results: int, timeout_s: int
    ) -> dict[str, object]:
        del query, max_results, timeout_s
        return {
            "ok": True,
            "results": [
                {"title": "One", "url": "https://one.test", "snippet": "one"},
                {"title": "Two", "url": "https://two.test", "snippet": "two"},
            ],
        }

    def fake_fetch(self: WebFetchTool) -> dict[str, object]:
        return {
            "ok": True,
            "content": f"fetched {self.url}",
            "summary": f"fetched {self.url}",
        }

    monkeypatch.setattr("yoke.agent.tools.web.research.web_search", fake_search)
    monkeypatch.setattr(WebFetchTool, "execute", fake_fetch)

    result = WebResearchTool(question="Example docs?").execute()

    assert result["ok"] is True
    sources = cast(list[dict[str, str]], result["sources"])
    assert len(sources) == 2
    assert sources[0]["url"] == "https://one.test"
    assert sources[1]["url"] == "https://two.test"


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

    result = bind_web_research(
        WebResearchTool(question="What is ExampleThing?")
    ).execute()

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

    result = bind_web_research(
        WebResearchTool(
            question=(
                "What is Python pathlib Path.resolve() and when should it be used?"
            ),
        )
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
    provider = SynthesizingProvider()
    tool = bind_web_research(
        WebResearchTool(question="What is Python pathlib Path.resolve()?"),
        provider,
    )

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
    assert provider.config.timeout_seconds == 600.0
