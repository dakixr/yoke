"""Web fetching tool and search helpers."""

from __future__ import annotations

import io
import html
import json
import re
from pathlib import PurePosixPath
from typing import Any
from typing import BinaryIO
from typing import Protocol
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

DOCUMENT_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".htm",
    ".html",
    ".json",
    ".md",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rst",
    ".txt",
    ".xls",
    ".xlsx",
    ".xml",
}

TEXT_CONTENT_TYPES = (
    "application/javascript",
    "application/json",
    "application/rss+xml",
    "application/xml",
    "text/",
)

CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.ms-word": ".doc",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/xhtml+xml": ".html",
    "application/xml": ".xml",
    "text/html": ".html",
    "text/markdown": ".md",
    "text/plain": ".txt",
}


class MarkItDownConverter(Protocol):
    """Minimal converter interface used by web_fetch."""

    def convert_stream(self, stream: BinaryIO, **kwargs: Any) -> object:
        """Convert a binary stream to readable document content."""


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

        result: dict[str, object] = {"ok": True, "results": results}
        if max_results > len(results):
            result["exhausted"] = True
            result["requestedResults"] = max_results
            result["returnedResults"] = len(results)
            result["note"] = (
                "DuckDuckGo HTML returned fewer parseable results than requested; "
                "use web_research for broader multi-query research."
            )
        return result
    except Exception as exc:
        return {"ok": False, "error": str(exc), "query": query}


class WebSearchTool(LocalTool):
    """Run a simple search and return raw result links/snippets."""

    name = "web_search"
    description = (
        "Run a quick DuckDuckGo HTML search and return raw result links/snippets. "
        "Use web_research instead when you need synthesized answers, source "
        "fetching, broader coverage, or current web research."
    )

    query: str = Field(min_length=1)
    max_results: int = Field(default=10, ge=1, le=50)
    timeout_s: int = Field(default=30, ge=1, le=180)

    def execute(self) -> dict[str, object]:
        """Run a DuckDuckGo HTML search and return parsed results."""
        if self._is_cancel_requested():
            return {"ok": False, "cancelled": True}
        return web_search(
            self.query,
            max_results=self.max_results,
            timeout_s=self.timeout_s,
        )


class WebFetchTool(LocalTool):
    """Fetch one URL and convert it into readable text/markdown."""

    name = "web_fetch"
    description = (
        "Fetch one known URL and return readable markdown/text, chunks, links, "
        "or metadata. Use web_search to discover URLs and web_research for "
        "multi-source researched answers."
    )

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

            request_url = self.url.strip()
            response = httpx.get(
                self.url.strip(),
                follow_redirects=True,
                timeout=self.timeout_s,
                headers={"User-Agent": http_user_agent()},
                verify=False,  # noqa: S501
            )
            response.raise_for_status()

            content_type = response.headers.get("content-type", "").lower()
            raw_text = _response_text(response)
            html_redirect_url = _html_redirect_url(
                raw_text,
                base_url=str(response.url),
                content_type=content_type,
            )
            html_redirect_error = ""
            if html_redirect_url and html_redirect_url != str(response.url):
                try:
                    response = httpx.get(
                        html_redirect_url,
                        follow_redirects=True,
                        timeout=self.timeout_s,
                        headers={"User-Agent": http_user_agent()},
                        verify=False,  # noqa: S501
                    )
                    response.raise_for_status()
                    request_url = html_redirect_url
                    content_type = response.headers.get("content-type", "").lower()
                    raw_text = _response_text(response)
                except Exception as exc:
                    html_redirect_error = str(exc)
            file_extension = _response_file_extension(
                str(response.url),
                content_type=content_type,
            )
            converter = _markitdown_converter() if self._use_markitdown() else None

            looks_like_markup = raw_text.lstrip().startswith("<")
            is_markup = (
                "html" in content_type or "xml" in content_type or looks_like_markup
            )
            is_json = "json" in content_type or file_extension == ".json"
            is_binary = _is_probably_binary(response.content, content_type)

            text: str | None = None
            extractor = "text"
            conversion_error = ""
            if (
                converter is not None
                and not is_json
                and _should_use_markitdown(
                    content_type=content_type,
                    file_extension=file_extension,
                    is_markup=is_markup,
                    is_binary=is_binary,
                )
            ):
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        message=r".*Couldn't find ffmpeg or avconv.*",
                        category=RuntimeWarning,
                    )
                    try:
                        converted = converter.convert_stream(
                            io.BytesIO(response.content),
                            file_extension=file_extension or None,
                            url=request_url,
                        )
                        text = _converted_text(converted)
                        extractor = "markitdown"
                    except Exception as exc:
                        conversion_error = str(exc)

            if text is None or not text.strip():
                if is_json:
                    text = _json_text(response, raw_text)
                    extractor = "json"
                elif is_markup:
                    text = html_to_text_blocks(raw_text)
                    extractor = "html"
                elif is_binary:
                    text = _binary_fallback_text(
                        content_type=content_type,
                        size_bytes=len(response.content),
                        conversion_error=conversion_error,
                    )
                    extractor = "binary"
                else:
                    text = raw_text
                    extractor = "text"

            html_info = ReadableHTMLParser(str(response.url))
            if is_markup:
                html_info.feed(raw_text)

            filtered_text = filter_text_blocks(text, self.find)
            find_terms = search_terms(self.find or "")
            raw_windows = extract_raw_term_windows(raw_text, self.find)
            if self.find and len(find_terms) > 1 and raw_windows:
                if not filtered_text:
                    text = raw_windows
                else:
                    text = filtered_text
            else:
                text = filtered_text

            find_miss = bool(self.find and not text.strip())

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
                raw_text=raw_text,
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
                    "extractor": extractor,
                    "fileExtension": file_extension,
                    "htmlRedirectUrl": html_redirect_url,
                    "markitdownUsed": extractor == "markitdown",
                    "title": html_info.title,
                    "sourceType": source_type_for(str(response.url)),
                },
            }
            if conversion_error:
                cast_details = result["details"]
                if isinstance(cast_details, dict):
                    cast_details["conversionError"] = conversion_error
            if html_redirect_error:
                cast_details = result["details"]
                if isinstance(cast_details, dict):
                    cast_details["htmlRedirectError"] = html_redirect_error
            if truncated:
                result["truncated"] = True
            if find_miss:
                result["matched"] = False
                result["note"] = f"No content matched find={self.find!r}."
            elif self.find:
                result["matched"] = True
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc), "url": self.url}

    def _use_markitdown(self) -> bool:
        configured = self._context.get("use_markitdown")
        return configured if isinstance(configured, bool) else True


def _markitdown_converter() -> MarkItDownConverter | None:
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r".*Couldn't find ffmpeg or avconv.*",
                category=RuntimeWarning,
            )
            from markitdown import MarkItDown

        return MarkItDown()
    except ImportError:
        return None


def _response_text(response: object) -> str:
    text = getattr(response, "text", "")
    if isinstance(text, str):
        return text
    content = getattr(response, "content", b"")
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return str(text)


def _json_text(response: Any, fallback: str) -> str:
    try:
        json_value = response.json()
    except Exception:
        return fallback
    return json.dumps(json_value, indent=2, ensure_ascii=False)


def _html_redirect_url(
    raw_html: str,
    *,
    base_url: str,
    content_type: str,
) -> str:
    if "html" not in content_type and not raw_html.lstrip().startswith("<"):
        return ""
    patterns = (
        r"""(?is)<meta\b[^>]*http-equiv=["']?refresh["']?[^>]*content=["'][^"']*?\burl=([^"'>\s]+)""",
        r"""(?is)<script\b[^>]*>.*?\blocation\.replace\((["'])(.*?)\1\)""",
    )
    for pattern in patterns:
        match = re.search(pattern, raw_html)
        if match is None:
            continue
        raw_url = match.group(2) if len(match.groups()) > 1 else match.group(1)
        candidate = html.unescape(raw_url).replace("\\/", "/").strip()
        redirected = urljoin(base_url, candidate)
        parsed = urlparse(redirected)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return redirected
    return ""


def _response_file_extension(url: str, *, content_type: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type in CONTENT_TYPE_EXTENSIONS:
        return CONTENT_TYPE_EXTENSIONS[media_type]
    suffix = PurePosixPath(urlparse(url).path).suffix.lower()
    return suffix if suffix in DOCUMENT_EXTENSIONS else ""


def _should_use_markitdown(
    *,
    content_type: str,
    file_extension: str,
    is_markup: bool,
    is_binary: bool,
) -> bool:
    if is_markup:
        return True
    if file_extension in DOCUMENT_EXTENSIONS:
        return True
    if is_binary and bool(content_type):
        return True
    return False


def _converted_text(converted: object) -> str:
    for attr in ("text_content", "markdown"):
        value = getattr(converted, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    text = str(converted)
    return text if text.strip() else ""


def _is_probably_binary(content: bytes, content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type.startswith(TEXT_CONTENT_TYPES):
        return False
    if media_type in CONTENT_TYPE_EXTENSIONS and media_type not in {
        "application/pdf",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.ms-word",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }:
        return False
    sample = content[:4096]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    textish = sum(byte in b"\t\n\r\f\b" or 32 <= byte <= 126 for byte in sample)
    return (textish / len(sample)) < 0.75


def _binary_fallback_text(
    *,
    content_type: str,
    size_bytes: int,
    conversion_error: str,
) -> str:
    parts = [
        "[No readable text extracted from binary response.]",
        f"Content-Type: {content_type or 'unknown'}",
        f"Size: {size_bytes} bytes",
    ]
    if conversion_error:
        parts.append(f"Conversion error: {conversion_error}")
    return "\n".join(parts)
