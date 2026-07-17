"""Web fetching tool and search helpers."""

from __future__ import annotations

import io
import html
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any
from typing import BinaryIO
from typing import Protocol
import warnings
from urllib.parse import urljoin
from urllib.parse import urlparse

from pydantic import Field

from yoke.agent.tools.base import LocalTool
from yoke.agent.tools.output import save_markdown_tool_output
from yoke.agent.tools.web.common import chunk_text
from yoke.agent.tools.web.common import html_to_text_blocks
from yoke.agent.tools.web.common import http_user_agent
from yoke.agent.tools.web.common import ReadableHTMLParser
from yoke.agent.tools.web.common import select_fetch_content
from yoke.agent.tools.web.common import source_type_for
from yoke.agent.tools.web.common import summarize_text
from yoke.agent.tools.web.search import web_search
from yoke.agent.truncate import DEFAULT_MAX_BYTES
from yoke.agent.truncate import truncate_head

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_FETCH_CHARS = 50_000

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


class WebSearchTool(LocalTool):
    """Run a simple search and return raw result links/snippets."""

    name = "web_search"
    description = (
        "Run a quick keyless web search and return raw result links/snippets. "
        "Use web_research instead when you need synthesized answers, source "
        "fetching, broader coverage, or current web research."
    )

    query: str = Field(min_length=1)
    max_results: int = Field(default=10, ge=1, le=50)
    timeout_s: int = Field(default=30, ge=1, le=180)

    def execute(self) -> dict[str, object]:
        """Run a keyless web search and return parsed results."""
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
        "or metadata. The complete extracted content is saved as Markdown under "
        "the global Yoke directory and returned as an absolute path so file tools "
        "can inspect it beyond model-facing limits. Responses over 5 MiB are "
        "rejected and model-facing output is capped at 50 KiB. Use web_search to "
        "discover URLs and web_research for multi-source researched answers."
    )

    url: str = Field(min_length=1)
    mode: str = "main_content"
    timeout_s: int = Field(default=30, ge=1, le=180)
    max_chars: int = Field(default=20_000, ge=500, le=MAX_FETCH_CHARS)

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
            _validate_response_size(response)

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
                    _validate_response_size(response)
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

            saved_path = self._save_complete_text(text, str(response.url))
            html_info = ReadableHTMLParser(str(response.url))
            if is_markup:
                html_info.feed(raw_text)

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

            content, truncated = _limit_content(text_for_limit, self.max_chars)
            result: dict[str, object] = {
                "ok": True,
                "path": saved_path,
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
            return _bound_fetch_result(result)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "url": self.url}

    def _use_markitdown(self) -> bool:
        configured = self._context.get("use_markitdown")
        return configured if isinstance(configured, bool) else True

    def _save_complete_text(self, text: str, source: str) -> str | None:
        home = self._context.get("home")
        if not isinstance(home, Path):
            return None
        return save_markdown_tool_output(home=home, source=source, content=text)


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


def _validate_response_size(response: object) -> None:
    headers = getattr(response, "headers", {})
    content_length = headers.get("content-length") if hasattr(headers, "get") else None
    if content_length:
        try:
            parsed_content_length = int(content_length)
        except (TypeError, ValueError):
            parsed_content_length = 0
        if parsed_content_length > MAX_RESPONSE_BYTES:
            raise ValueError("response too large (exceeds 5 MiB limit)")
    content = getattr(response, "content", b"")
    if isinstance(content, bytes) and len(content) > MAX_RESPONSE_BYTES:
        raise ValueError("response too large (exceeds 5 MiB limit)")


def _limit_content(content: str, max_chars: int) -> tuple[str, bool]:
    limited = content[:max_chars]
    truncated_by_chars = len(content) > max_chars
    truncation = truncate_head(limited)
    if truncation.first_line_exceeds_limit:
        return limited, True
    if truncation.truncated:
        return truncation.content, True
    if truncated_by_chars:
        return limited.rstrip() + "\n\n[Output truncated by max_chars.]", True
    return limited, False


def _bound_fetch_result(result: dict[str, object]) -> dict[str, object]:
    if _serialized_size(result) <= DEFAULT_MAX_BYTES:
        return result

    bounded = dict(result)
    bounded["truncated"] = True
    bounded["chunks"] = []
    if _serialized_size(bounded) <= DEFAULT_MAX_BYTES:
        return bounded

    bounded["links"] = []
    content = bounded.get("content")
    if not isinstance(content, str):
        bounded["content"] = str(content)
        content = str(content)

    marker = (
        "\n\n[Output truncated to the 50 KiB tool-result limit. "
        "Use read or rg on the saved path to inspect the complete content.]"
    )
    start = 0
    end = len(content)
    best = marker
    while start <= end:
        middle = (start + end) // 2
        candidate = content[:middle].rstrip() + marker
        bounded["content"] = candidate
        if _serialized_size(bounded) <= DEFAULT_MAX_BYTES:
            best = candidate
            start = middle + 1
        else:
            end = middle - 1
    bounded["content"] = best
    return bounded


def _serialized_size(payload: dict[str, object]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


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
