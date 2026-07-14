"""Keyless web search transport and result parsing."""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs
from urllib.parse import unquote
from urllib.parse import urljoin
from urllib.parse import urlparse
from xml.etree import ElementTree

from yoke.agent.tools.web.common import domain_for
from yoke.agent.tools.web.common import DuckDuckGoHTMLParser
from yoke.agent.tools.web.common import http_user_agent
from yoke.agent.tools.web.common import source_type_for


def web_search(
    query: str, *, max_results: int = 5, timeout_s: int = 20
) -> dict[str, object]:
    """Search the web using DuckDuckGo HTML with a Bing RSS fallback."""
    try:
        import httpx

        client = httpx.Client(
            follow_redirects=True,
            headers={"User-Agent": http_user_agent()},
            timeout=timeout_s,
            verify=False,  # noqa: S501
        )
        try:
            duckduckgo_failed = False
            duckduckgo_blocked = False
            try:
                duckduckgo_response = client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query.strip()},
                )
                duckduckgo_response.raise_for_status()
                duckduckgo_blocked = _duckduckgo_challenge(duckduckgo_response)
                duckduckgo_results = (
                    []
                    if duckduckgo_blocked
                    else _duckduckgo_search_results(
                        duckduckgo_response.text, max_results=max_results
                    )
                )
                fallback_reason = (
                    "DuckDuckGo blocked the automated request"
                    if duckduckgo_blocked
                    else "DuckDuckGo returned no parseable results"
                )
            except Exception as exc:
                duckduckgo_failed = True
                duckduckgo_results = []
                fallback_reason = f"DuckDuckGo request failed: {exc}"

            if duckduckgo_results:
                return _search_result_payload(
                    duckduckgo_results,
                    provider="duckduckgo",
                    max_results=max_results,
                )

            try:
                bing_response = client.get(
                    "https://www.bing.com/search",
                    params={"q": query.strip(), "format": "rss"},
                )
                bing_response.raise_for_status()
                bing_results = _bing_rss_search_results(
                    bing_response.text, max_results=max_results
                )
            except Exception as exc:
                if duckduckgo_failed or duckduckgo_blocked:
                    return {
                        "ok": False,
                        "error": f"{fallback_reason}; Bing RSS fallback failed: {exc}",
                        "query": query,
                    }
                return _search_result_payload(
                    [],
                    provider="duckduckgo",
                    max_results=max_results,
                )

            return _search_result_payload(
                bing_results,
                provider="bing",
                max_results=max_results,
                fallback_reason=fallback_reason,
            )
        finally:
            getattr(client, "close", lambda: None)()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "query": query}


def _duckduckgo_challenge(response: object) -> bool:
    status_code = getattr(response, "status_code", None)
    text = str(getattr(response, "text", "")).lower()
    return status_code == 202 or any(
        marker in text
        for marker in (
            "anomaly-modal",
            "challenge-form",
            "bots use duckduckgo too",
        )
    )


def _duckduckgo_search_results(
    response_text: str, *, max_results: int
) -> list[dict[str, str]]:
    parser = DuckDuckGoHTMLParser()
    parser.feed(response_text)
    return _normalize_search_results(parser.results, max_results=max_results)


def _bing_rss_search_results(
    response_text: str, *, max_results: int
) -> list[dict[str, str]]:
    root = ElementTree.fromstring(response_text)
    parsed_results = [
        {
            "title": item.findtext("title", default=""),
            "url": item.findtext("link", default=""),
            "snippet": re.sub(
                r"<[^>]+>", " ", item.findtext("description", default="")
            ),
        }
        for item in root.findall("./channel/item")
    ]
    return _normalize_search_results(parsed_results, max_results=max_results)


def _normalize_search_results(
    parsed_results: list[dict[str, str]], *, max_results: int
) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for parsed_result in parsed_results:
        title = " ".join(html.unescape(parsed_result.get("title", "")).split())
        raw_url = html.unescape(parsed_result.get("url", "").strip())
        if raw_url.startswith("//"):
            raw_url = f"https:{raw_url}"
        elif raw_url.startswith("/"):
            raw_url = urljoin("https://duckduckgo.com", raw_url)
        parsed = urlparse(raw_url)
        if parsed.netloc.lower().endswith("duckduckgo.com"):
            parsed_query = parse_qs(parsed.query)
            for key in ("uddg", "rut"):
                values = parsed_query.get(key)
                if values and values[0]:
                    raw_url = unquote(values[0]).strip()
                    parsed = urlparse(raw_url)
                    break
        if not title or parsed.scheme not in {"http", "https"} or raw_url in seen:
            continue
        seen.add(raw_url)
        results.append(
            {
                "title": title,
                "url": raw_url,
                "domain": domain_for(raw_url),
                "sourceType": source_type_for(raw_url),
                "snippet": " ".join(
                    html.unescape(parsed_result.get("snippet", "")).split()
                ),
            }
        )
        if len(results) >= max_results:
            break
    return results


def _search_result_payload(
    results: list[dict[str, str]],
    *,
    provider: str,
    max_results: int,
    fallback_reason: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "ok": True,
        "results": results,
        "provider": provider,
    }
    notes: list[str] = []
    if fallback_reason:
        notes.append(f"{fallback_reason}; used Bing RSS fallback.")
    if max_results > len(results):
        result["exhausted"] = True
        result["requestedResults"] = max_results
        result["returnedResults"] = len(results)
        notes.append(
            f"{provider.title()} returned fewer results than requested; use "
            "web_research for broader multi-query research."
        )
    if notes:
        result["note"] = " ".join(notes)
    return result
