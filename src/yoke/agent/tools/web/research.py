"""Web research tool built on top of search and fetch helpers."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import ClassVar
from typing import cast

from pydantic import BaseModel
from pydantic import Field
from typing_extensions import Literal

from yoke.agent.models import Message
from yoke.agent.tools.base import LocalTool
from yoke.agent.tools.web.common import domain_for
from yoke.agent.tools.web.common import search_terms
from yoke.agent.tools.web.common import source_type_for
from yoke.agent.tools.web.common import summarize_text
from yoke.agent.tools.web.fetch import WebFetchTool
from yoke.agent.tools.web.fetch import WebSearchTool
from yoke.agent.tools.web.fetch import web_search
from yoke.ai.providers.codex.subscription import CodexSubscriptionProvider

WebSearchContextSize = Literal["low", "medium", "high"]
WebSearchMode = Literal["cached", "indexed", "live"]

ASSISTANT_CONTEXT_CHAR_LIMIT = 4_000


class ResearchSource(BaseModel):
    """Structured synthesized source returned by web research."""

    title: str = ""
    url: str = ""
    quote: str = ""


class ResearchBrief(BaseModel):
    """Structured synthesized research answer."""

    answer: str = ""
    sources: list[ResearchSource] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


RESEARCH_AGENT_SYSTEM_PROMPT = """You are a focused web research synthesizer.
Use only the provided source payload and any web_fetch follow-up calls you make.
Ignore navigation, sidebars, headers, footers, and table-of-contents text.
Return concise, source-grounded structured output. Do not invent facts.
Prefer broad source coverage for non-trivial questions; consult 20+ sources
when useful instead of stopping at the first plausible answer.
"""


def rank_results_by_source_type(results: Sequence[object]) -> list[object]:
    """Prefer docs, GitHub, and academic results."""

    def score(result: object) -> int:
        if not isinstance(result, dict):
            return 0
        search_result = cast(dict[str, object], result)
        source_type = str(search_result.get("sourceType") or "")
        domain = str(search_result.get("domain") or "")
        value = 0
        if source_type == "docs":
            value += 6
        if source_type in {"github", "academic"}:
            value += 6
        if any(part in domain for part in ("docs.", "developer.", "github.com")):
            value += 2
        return value

    return sorted(results, key=score, reverse=True)


class WebResearchTool(LocalTool):
    """Research a question across sources and synthesize an answer."""

    name = "web_research"
    description = (
        "Autonomously research a question by searching the web, fetching top "
        "sources, and returning a concise answer with evidence and links. Prefer "
        "this over web_search/web_fetch for open-ended questions, current facts, "
        "or tasks needing multiple sources."
    )
    execute_in_process = True

    fetched_source_target: ClassVar[int] = 25
    search_result_target: ClassVar[int] = 30

    question: str = Field(min_length=1)
    research_context: str | None = Field(
        default=None,
        description="Relevant task context to disambiguate the research question.",
    )
    search_context_size: WebSearchContextSize = Field(
        default="high",
        description="How much context hosted web search should retrieve.",
    )
    web_search_mode: WebSearchMode = Field(
        default="live",
        description="Use cached, indexed, or live external web access when supported.",
    )
    allowed_domains: list[str] = Field(
        default_factory=list,
        description="Optional domains to restrict hosted web search to.",
    )

    def execute(self) -> dict[str, object]:
        """Execute a compact search+fetch research workflow."""
        hosted = self._research_with_codex_hosted_web_search()
        if hosted is not None:
            return hosted

        query, search = self._search_with_fallback()
        if not search.get("ok"):
            return search

        results = search.get("results")
        if not isinstance(results, list) or not results:
            return {
                "ok": True,
                "answer": "No search results were found.",
                "notes": [str(search["note"])] if search.get("note") else [],
                "sources": [],
            }

        sources: list[dict[str, object]] = []
        seen_domains: set[str] = set()
        for result in rank_results_by_source_type(results):
            if self._is_cancel_requested():
                return {"ok": False, "cancelled": True}
            if not isinstance(result, dict):
                continue
            search_result = cast(dict[str, object], result)
            url = str(search_result.get("url", ""))
            if not url:
                continue
            domain = domain_for(url)
            if (
                domain in seen_domains
                and len(sources) >= self.fetched_source_target // 2
            ):
                continue
            fetch_tool = WebFetchTool(
                url=url,
                mode="chunks",
                timeout_s=30,
                max_chars=5000,
            )
            fetch_tool._bind_context(use_markitdown=False)
            fetched = fetch_tool.execute()
            source: dict[str, object] = {
                "title": search_result.get("title", ""),
                "url": url,
                "domain": domain,
                "sourceType": search_result.get("sourceType") or source_type_for(url),
                "searchSnippet": search_result.get("snippet", ""),
                "ok": bool(fetched.get("ok")),
            }
            if fetched.get("ok"):
                source["summary"] = fetched.get("summary") or fetched.get("content", "")
                source["evidence"] = fetched.get("content", "")
                source["details"] = fetched.get("details", {})
            else:
                source["error"] = fetched.get("error", "fetch failed")
            sources.append(source)
            seen_domains.add(domain)
            if len(sources) >= self.fetched_source_target:
                break

        synthesized = self._synthesize_with_provider(query=query, sources=sources)
        if synthesized is not None:
            return synthesized

        return {
            "ok": True,
            "answer": self._brief_from_sources(sources),
            "notes": [
                "Provider synthesis was unavailable; returned a fallback summary."
            ],
            "sources": self._fallback_sources(sources),
        }

    def _search_with_fallback(self) -> tuple[str, dict[str, object]]:
        queries = self._queries_for_mode()
        last_search: dict[str, object] = {"ok": True, "results": []}
        for query in queries:
            if self._is_cancel_requested():
                return query, {"ok": False, "cancelled": True}
            search = web_search(
                query,
                max_results=self.search_result_target,
                timeout_s=30,
            )
            last_search = search
            if search.get("ok") and isinstance(search.get("results"), list):
                if search.get("results"):
                    return query, search
            elif not search.get("ok"):
                return query, search
        return queries[-1], last_search

    def _queries_for_mode(self) -> list[str]:
        question = self.question.strip()
        terms = " ".join(search_terms(question)) or question
        return [f"{terms} official docs", f"{terms} documentation", question]

    def _research_with_codex_hosted_web_search(self) -> dict[str, object] | None:
        provider = self._codex_provider()
        if provider is None:
            return None
        try:
            prompt = (
                "Research the question using the hosted web_search tool. "
                "Search live/current web sources as needed, then answer concisely. "
                "Include URLs for the best supporting sources and do not invent facts.\n\n"
                f"Question:\n{self.question.strip()}\n\n"
                f"Relevant context:\n{self._research_context_text()}"
            )
            message = self._complete_codex_hosted_search(provider, prompt)
            content = message.text_content() or ""
            return {
                "ok": True,
                "answer": content.strip(),
                "notes": ["Used Codex hosted web_search."],
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Codex hosted web_search failed: {exc}",
                "notes": [
                    "Skipped local web_research fallback because a Codex provider "
                    "should use hosted web_search."
                ],
            }

    def _codex_provider(self) -> CodexSubscriptionProvider | None:
        runtime_context = self.runtime_context
        if runtime_context is None:
            return None
        provider = runtime_context.provider
        return provider if isinstance(provider, CodexSubscriptionProvider) else None

    def _complete_codex_hosted_search(
        self,
        provider: CodexSubscriptionProvider,
        prompt: str,
    ) -> Message:
        return provider.complete_with_cancel(
            [
                Message.system(
                    "You are a concise web research tool. Use hosted web "
                    "search when available and cite source URLs in your answer."
                ),
                Message.user(prompt),
            ],
            [
                self._hosted_web_search_tool(),
            ],
            cancel_requested=self._is_cancel_requested,
        )

    def _hosted_web_search_tool(self) -> dict[str, object]:
        tool: dict[str, object] = {
            "type": "web_search",
            "external_web_access": self.web_search_mode != "cached",
            "search_context_size": self.search_context_size,
        }
        if self.web_search_mode == "indexed":
            tool["index_gated_web_access"] = True
        if self.allowed_domains:
            tool["filters"] = {"allowed_domains": self.allowed_domains}
        return tool

    def _synthesize_with_provider(
        self,
        *,
        query: str,
        sources: list[dict[str, object]],
    ) -> dict[str, object] | None:
        runtime_context = self.runtime_context
        if runtime_context is None:
            return None
        provider = runtime_context.provider
        try:
            from yoke.ai import Agent
            from yoke.ai import RunConfig

            prompt = self._research_agent_prompt(query=query, sources=sources)
            agent = Agent(
                provider=provider,
                config=RunConfig(
                    root=".",
                    tools=[
                        WebFetchTool.bind(
                            cancel_requested=self._is_cancel_requested,
                            use_markitdown=False,
                        ),
                        WebSearchTool.bind(cancel_requested=self._is_cancel_requested),
                    ],
                    max_iterations=8,
                    sys_prompt=RESEARCH_AGENT_SYSTEM_PROMPT,
                    include_agents_file=False,
                ),
            )
            result = agent.prompt(
                prompt,
                output_type=ResearchBrief,
                stop_requested=self._is_cancel_requested,
            )
            if result.structured is None:
                return None
            brief = result.structured
            return {
                "ok": True,
                "answer": brief.answer,
                "notes": brief.notes,
                "sources": [source.model_dump() for source in brief.sources],
            }
        except Exception as exc:
            return {
                "ok": True,
                "answer": self._brief_from_sources(sources),
                "notes": [f"Provider synthesis failed: {exc}"],
                "sources": self._fallback_sources(sources),
            }

    def _research_agent_prompt(
        self,
        *,
        query: str,
        sources: list[dict[str, object]],
    ) -> str:
        source_payload = json.dumps(
            sources,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        return (
            f"Question: {self.question}\n"
            f"Relevant context:\n{self._research_context_text()}\n\n"
            f"Search query used: {query}\n\n"
            "Use the fetched source payload below to answer the question. "
            "For non-trivial questions, review many sources rather than "
            "stopping early; aim to consult 20+ relevant sources when the "
            "available results support it. "
            "Prioritize directly relevant evidence over page navigation, "
            "headers, sidebars, table-of-contents text, and generic intros. "
            "If the fetched payload is insufficient, you may call web_fetch "
            "on the listed URLs for targeted follow-up. Return only facts "
            "supported by the sources. Keep the answer concise and put any "
            "caveats in notes. Include only the best quote per source.\n\n"
            f"Fetched source payload:\n{source_payload}"
        )

    def _research_context_text(self) -> str:
        parts: list[str] = []
        if self.research_context and self.research_context.strip():
            parts.append(self.research_context.strip())
        recent_context = recent_research_context(self._recent_messages())
        if recent_context:
            parts.append(recent_context)
        return "\n\n".join(parts) or "No additional context."

    def _recent_messages(self) -> Sequence[Message]:
        runtime_context = self.runtime_context
        if runtime_context is not None and runtime_context.recent_messages:
            return runtime_context.recent_messages
        messages = self._context.get("messages")
        if isinstance(messages, list) and all(
            isinstance(message, Message) for message in messages
        ):
            return cast(Sequence[Message], messages)
        return ()

    def _fallback_sources(
        self, sources: list[dict[str, object]]
    ) -> list[dict[str, str]]:
        fallback: list[dict[str, str]] = []
        for source in sources:
            fallback.append(
                {
                    "title": str(source.get("title") or ""),
                    "url": str(source.get("url") or ""),
                    "quote": summarize_text(
                        str(
                            source.get("searchSnippet")
                            or source.get("summary")
                            or source.get("evidence")
                            or ""
                        ),
                        max_chars=500,
                    ),
                }
            )
        return fallback

    def _brief_from_sources(self, sources: list[dict[str, object]]) -> str:
        usable = [source for source in sources if source.get("ok")]
        if not usable:
            return "No fetched source content was available. Inspect source errors."
        lines = [f"Research brief for: {self.question}"]
        for index, source in enumerate(usable, start=1):
            title = str(source.get("title") or source.get("url") or "source")
            summary = str(source.get("summary") or source.get("searchSnippet") or "")
            lines.append(f"{index}. {title}: {summarize_text(summary, max_chars=350)}")
        return "\n".join(lines)


def recent_research_context(messages: Sequence[Message]) -> str:
    """Return a Codex-style recent text tail for web research."""
    visible = _visible_research_messages(messages)
    user_indices = [
        index for index, message in enumerate(visible) if message.role == "user"
    ]
    if not user_indices:
        return ""
    start = user_indices[-2] if len(user_indices) >= 2 else user_indices[-1]
    retained = visible[start:]
    lines: list[str] = []
    assistant_chars = 0
    for message in retained:
        text = (message.text_content() or "").strip()
        if not text:
            continue
        if message.role == "assistant":
            remaining = ASSISTANT_CONTEXT_CHAR_LIMIT - assistant_chars
            if remaining <= 0:
                continue
            text = text[:remaining]
            assistant_chars += len(text)
        label = "User" if message.role == "user" else "Assistant"
        lines.append(f"{label}: {text}")
    return "\n".join(lines)


def _visible_research_messages(messages: Sequence[Message]) -> list[Message]:
    visible: list[Message] = []
    for message in messages:
        if message.role == "user":
            text = (message.text_content() or "").strip()
            if not text or text.startswith("<environment_context>"):
                continue
            visible.append(message)
        elif message.role == "assistant" and not message.tool_calls:
            text = (message.text_content() or "").strip()
            if text:
                visible.append(message)
    return visible
