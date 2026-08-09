"""Web fetching tool and search helpers."""

from __future__ import annotations

import io
import json
import threading
import time
import warnings
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal
from typing import Protocol
from urllib.parse import urlparse

from pydantic import Field

from yoke.agent.tools.base import LocalTool
from yoke.agent.tools.web.common import http_user_agent
from yoke.agent.tools.web.common import ReadableHTMLParser
from yoke.agent.tools.web.common import source_type_for
from yoke.agent.tools.web.search import web_search as keyless_web_search

DEFAULT_FETCH_LIMIT = 20_000
MAX_FETCH_LIMIT = 50_000
FETCH_CACHE_TTL_SECONDS = 15 * 60
FETCH_CACHE_MAX_CHARS = 20_000_000
FETCH_CACHE_MAX_ENTRIES = 16


@dataclass(frozen=True, slots=True)
class FetchedResource:
    """A normalized remote resource held for paged continuation calls."""

    content: str
    details: dict[str, object]
    expires_at: float


class HttpResponse(Protocol):
    """HTTP response fields used during content selection."""

    @property
    def text(self) -> str:
        """Return decoded response text."""
        ...

    @property
    def content(self) -> bytes:
        """Return raw response bytes."""
        ...

    def json(self) -> object:
        """Decode the response body as JSON."""
        ...


_FETCH_CACHE: OrderedDict[tuple[str, str], FetchedResource] = OrderedDict()
_FETCH_CACHE_CHARS = 0
_FETCH_CACHE_LOCK = threading.RLock()


def clear_fetch_cache() -> None:
    """Clear cached fetch resources."""
    global _FETCH_CACHE_CHARS
    with _FETCH_CACHE_LOCK:
        _FETCH_CACHE.clear()
        _FETCH_CACHE_CHARS = 0


def _cached_resource(key: tuple[str, str]) -> FetchedResource | None:
    global _FETCH_CACHE_CHARS
    with _FETCH_CACHE_LOCK:
        resource = _FETCH_CACHE.get(key)
        if resource is None:
            return None
        if resource.expires_at <= time.monotonic():
            _FETCH_CACHE.pop(key)
            _FETCH_CACHE_CHARS -= len(resource.content)
            return None
        _FETCH_CACHE.move_to_end(key)
        return resource


def _cache_resource(key: tuple[str, str], resource: FetchedResource) -> None:
    global _FETCH_CACHE_CHARS
    size = len(resource.content)
    if size > FETCH_CACHE_MAX_CHARS:
        return
    with _FETCH_CACHE_LOCK:
        previous = _FETCH_CACHE.pop(key, None)
        if previous is not None:
            _FETCH_CACHE_CHARS -= len(previous.content)
        while _FETCH_CACHE and (
            len(_FETCH_CACHE) >= FETCH_CACHE_MAX_ENTRIES
            or _FETCH_CACHE_CHARS + size > FETCH_CACHE_MAX_CHARS
        ):
            _, evicted = _FETCH_CACHE.popitem(last=False)
            _FETCH_CACHE_CHARS -= len(evicted.content)
        _FETCH_CACHE[key] = resource
        _FETCH_CACHE_CHARS += size


def web_search(
    query: str, *, max_results: int = 5, timeout_s: int = 20
) -> dict[str, object]:
    """Run Yoke's keyless search with its Bing fallback."""
    return keyless_web_search(
        query,
        max_results=max_results,
        timeout_s=timeout_s,
    )


class WebSearchTool(LocalTool):
    """Run a keyless web search and return links and snippets."""

    name = "web_search"
    description = (
        "Run a quick keyless web search and return raw result links/snippets. "
        "Use web_research for synthesized multi-source answers."
    )
    execute_in_process = True

    query: str = Field(min_length=1)
    max_results: int = Field(default=10, ge=1, le=50)
    timeout_s: int = Field(default=30, ge=1, le=180)

    def execute(self) -> dict[str, object]:
        """Run the search unless this turn has been cancelled."""
        if self._is_cancel_requested():
            return {"ok": False, "cancelled": True}
        return web_search(
            self.query,
            max_results=self.max_results,
            timeout_s=self.timeout_s,
        )


class WebFetchTool(LocalTool):
    """Fetch and page through a readable representation of a URL."""

    name = "web_fetch"
    description = (
        "Fetch a URL and return readable text. Defaults to the first 20,000 "
        "characters. Use offset and limit to continue. Set mode='raw' to "
        "read the unprocessed response text."
    )
    execute_in_process = True

    url: str = Field(min_length=1, max_length=4096)
    mode: Literal["main_content", "raw"] = "main_content"
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=DEFAULT_FETCH_LIMIT, ge=1, le=MAX_FETCH_LIMIT)
    timeout_s: int = Field(default=30, ge=1, le=180)

    def execute(self) -> dict[str, object]:
        """Fetch the URL and return one character page."""
        try:
            url = self.url.strip()
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("url must use http/https and include a host")

            key = (url, self.mode)
            resource = _cached_resource(key) if self.offset > 1 else None
            if resource is None:
                resource = self._fetch_resource(url)
                _cache_resource(key, resource)
            return self._page(resource)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "url": self.url}

    def _fetch_resource(self, url: str) -> FetchedResource:
        import httpx

        response = httpx.get(
            url,
            follow_redirects=True,
            timeout=self.timeout_s,
            headers={"User-Agent": http_user_agent()},
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        final_url = str(response.url)
        parser = ReadableHTMLParser(final_url)
        if "html" in content_type or "xml" in content_type:
            parser.feed(response.text)

        content, markitdown_used = self._select_content(
            response=response,
            content_type=content_type,
        )
        details: dict[str, object] = {
            "content_type": content_type,
            "final_url": final_url,
            "mode": self.mode,
            "status_code": response.status_code,
            "markitdown_used": markitdown_used,
            "title": parser.title[:500],
            "source_type": source_type_for(final_url),
        }
        return FetchedResource(
            content=content,
            details=details,
            expires_at=time.monotonic() + FETCH_CACHE_TTL_SECONDS,
        )

    def _select_content(
        self, *, response: HttpResponse, content_type: str
    ) -> tuple[str, bool]:
        if self.mode == "raw":
            return response.text, False
        if "json" in content_type:
            return (
                json.dumps(
                    response.json(),
                    indent=2,
                    ensure_ascii=False,
                ),
                False,
            )
        if "html" not in content_type and "xml" not in content_type:
            return response.text, False

        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r".*Couldn't find ffmpeg or avconv.*",
                    category=RuntimeWarning,
                )
                from markitdown import MarkItDown  # type: ignore[import-not-found]

            converter = MarkItDown()
        except ImportError:
            return response.text, False

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
        content = (
            getattr(converted, "text_content", None)
            or getattr(converted, "markdown", None)
            or str(converted)
        )
        return str(content), True

    def _page(self, resource: FetchedResource) -> dict[str, object]:
        total_chars = len(resource.content)
        start = self.offset - 1
        if start >= total_chars and not (start == 0 and total_chars == 0):
            raise ValueError(
                f"Offset {self.offset} is beyond end of content "
                f"({total_chars} characters total)"
            )
        end = min(start + self.limit, total_chars)
        result: dict[str, object] = {
            "ok": True,
            "content": resource.content[start:end],
            "offset": self.offset,
            "limit": self.limit,
            "total_chars": total_chars,
            "details": resource.details,
        }
        if end < total_chars:
            result["next_offset"] = end + 1
        return result
