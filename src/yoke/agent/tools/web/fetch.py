"""Web fetching tool and search helpers."""

from __future__ import annotations

import io
import json
import warnings
from urllib.parse import parse_qs
from urllib.parse import unquote
from urllib.parse import urljoin
from urllib.parse import urlparse

from pydantic import Field

from yoke.agent.tools.base import LocalTool
from yoke.agent.tools.web.common import chunk_text
from yoke.agent.tools.web.common import domain_for
from yoke.agent.tools.web.common import DuckDuckGoHTMLParser
from yoke.agent.tools.web.common import extract_raw_term_windows
from yoke.agent.tools.web.common import filter_text_blocks
from yoke.agent.tools.web.common import html_to_text_blocks
from yoke.agent.tools.web.common import http_user_agent
from yoke.agent.tools.web.common import ReadableHTMLParser
from yoke.agent.tools.web.common import search_terms
from yoke.agent.tools.web.common import select_fetch_content
from yoke.agent.tools.web.common import source_type_for
from yoke.agent.tools.web.common import summarize_text
from yoke.agent.truncate import truncate_head


def web_search(
    query: str, *, max_results: int = 5, timeout_s: int = 20
) -> dict[str, object]:
    """Search the web using DuckDuckGo HTML results."""
    try:
        import html

        import httpx

        client = httpx.Client(
            follow_redirects=True,
            headers={"User-Agent": http_user_agent()},
            timeout=timeout_s,
            verify=False,  # noqa: S501
        )
        response = client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query.strip()},
        )
        response.raise_for_status()

        parser = DuckDuckGoHTMLParser()
        parser.feed(response.text)

        results: list[dict[str, str]] = []
        seen: set[str] = set()
        for parsed_result in parser.results:
            title = " ".join(parsed_result.get("title", "").split())
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
                    "snippet": " ".join(parsed_result.get("snippet", "").split()),
                }
            )
            if len(results) >= max_results:
                break

        return {"ok": True, "results": results}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "query": query}


class WebFetchTool(LocalTool):
    """Fetch a URL and convert the page into readable text/markdown."""

    name = "web_fetch"
    description = "Fetch a URL and return readable markdown/text content."

    url: str = Field(min_length=1)
    mode: str = "main_content"
    find: str | None = Field(default=None, min_length=1)
    timeout_s: int = Field(default=30, ge=1, le=180)
    max_chars: int = Field(default=20_000, ge=500, le=200_000)

    def execute(self) -> dict[str, object]:
        """Fetch the URL and return its content as text or markdown."""
        try:
            import httpx

            parsed = urlparse(self.url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("url must use http/https and include a host")

            response = httpx.get(
                self.url.strip(),
                follow_redirects=True,
                timeout=self.timeout_s,
                headers={"User-Agent": http_user_agent()},
                verify=False,  # noqa: S501
            )
            response.raise_for_status()

            content_type = response.headers.get("content-type", "").lower()
            text: str
            converter = None
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        message=r".*Couldn't find ffmpeg or avconv.*",
                        category=RuntimeWarning,
                    )
                    from markitdown import (  # ty: ignore[unresolved-import]
                        MarkItDown,  # type: ignore[import-not-found]
                    )

                converter = MarkItDown()
            except ImportError:
                converter = None

            looks_like_markup = response.text.lstrip().startswith("<")
            is_markup = (
                "html" in content_type or "xml" in content_type or looks_like_markup
            )

            if converter is not None and is_markup:
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        message=r".*Couldn't find ffmpeg or avconv.*",
                        category=RuntimeWarning,
                    )
                    converted = converter.convert_stream(
                        io.BytesIO(response.content),
                        file_extension=".html",
                        url=self.url.strip(),
                    )
                text = (
                    getattr(converted, "text_content", None)
                    or getattr(converted, "markdown", None)
                    or str(converted)
                )
            elif "json" in content_type:
                text = json.dumps(response.json(), indent=2, ensure_ascii=False)
            elif is_markup:
                text = html_to_text_blocks(response.text)
            else:
                text = response.text

            html_info = ReadableHTMLParser(str(response.url))
            if is_markup:
                html_info.feed(response.text)

            filtered_text = filter_text_blocks(text, self.find)
            find_terms = search_terms(self.find or "")
            raw_windows = extract_raw_term_windows(response.text, self.find)
            if self.find and len(find_terms) > 1 and raw_windows:
                if not filtered_text:
                    text = raw_windows
                else:
                    text = filtered_text
            else:
                text = filtered_text

            chunks = chunk_text(text)
            summary = summarize_text(text)
            links = [
                link
                for link in html_info.links
                if link.get("url", "").startswith(("http://", "https://"))
            ][:100]

            selected = select_fetch_content(
                mode=self.mode,
                text=text,
                raw_text=response.text,
                summary=summary,
                chunks=chunks,
                links=links,
                html_info=html_info,
            )

            text_for_limit = (
                json.dumps(selected, indent=2, ensure_ascii=False)
                if not isinstance(selected, str)
                else selected
            )

            limited = (
                text_for_limit[: self.max_chars]
                if len(text_for_limit) > self.max_chars
                else text_for_limit
            )
            truncation = truncate_head(limited)
            content = truncation.content
            truncated = truncation.truncated or len(text_for_limit) > self.max_chars
            if len(text_for_limit) > self.max_chars and not truncation.truncated:
                content = limited.rstrip() + "\n\n[Output truncated by max_chars.]"
            result: dict[str, object] = {
                "ok": True,
                "content": content,
                "summary": summary,
                "chunks": chunks[:20],
                "links": links,
                "details": {
                    "contentType": content_type,
                    "finalUrl": str(response.url),
                    "mode": self.mode,
                    "statusCode": response.status_code,
                    "markitdownUsed": converter is not None,
                    "title": html_info.title,
                    "sourceType": source_type_for(str(response.url)),
                },
            }
            if truncated:
                result["truncated"] = True
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc), "url": self.url}
