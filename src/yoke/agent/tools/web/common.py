"""Shared helpers for web search/fetch tools."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.parse import urlparse


class DuckDuckGoHTMLParser(HTMLParser):
    """Parse DuckDuckGo HTML results."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._in_title = False
        self._snippet_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track result title and snippet regions while parsing."""
        attrs_map = {key: value or "" for key, value in attrs}
        class_names = set(attrs_map.get("class", "").split())
        if tag == "a" and "result__a" in class_names:
            href = attrs_map.get("href", "").strip()
            if href:
                self._current = {"title": "", "url": href, "snippet": ""}
                self.results.append(self._current)
                self._in_title = True
            return
        if self._current is None:
            return
        if "result__snippet" in class_names:
            self._snippet_depth += 1

    def handle_endtag(self, tag: str) -> None:
        """Close title/snippet regions when their tags end."""
        if tag == "a" and self._in_title:
            self._in_title = False
            return
        if tag in {"a", "div", "span"} and self._snippet_depth > 0:
            self._snippet_depth -= 1

    def handle_data(self, data: str) -> None:
        """Capture title text and the first snippet text fragment."""
        if self._current is None:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self._current["title"] = " ".join(
                part for part in [self._current["title"], text] if part
            )
        elif self._snippet_depth > 0 and not self._current["snippet"]:
            self._current["snippet"] = " ".join(text.split())


class ReadableHTMLParser(HTMLParser):
    """Capture title, links, headings, and code blocks from HTML."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.links: list[dict[str, str]] = []
        self.headings: list[dict[str, str]] = []
        self.code_blocks: list[str] = []
        self._in_title = False
        self._current_heading_level: str | None = None
        self._current_heading_text: list[str] = []
        self._in_code = False
        self._current_code: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track title, heading, link, and code block regions."""
        attrs_map = {key: value or "" for key, value in attrs}
        if tag == "title":
            self._in_title = True
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._current_heading_level = tag
            self._current_heading_text = []
        elif tag == "a":
            href = attrs_map.get("href", "").strip()
            if href:
                self.links.append({"url": urljoin(self.base_url, href), "text": ""})
        elif tag in {"code", "pre"}:
            self._in_code = True
            self._current_code = []

    def handle_endtag(self, tag: str) -> None:
        """Finalize any tracked title, heading, or code block content."""
        if tag == "title":
            self._in_title = False
        elif tag == self._current_heading_level:
            text = " ".join(" ".join(self._current_heading_text).split())
            if text:
                self.headings.append(
                    {"level": self._current_heading_level or tag, "text": text}
                )
            self._current_heading_level = None
            self._current_heading_text = []
        elif tag in {"code", "pre"} and self._in_code:
            code = "".join(self._current_code).strip()
            if code:
                self.code_blocks.append(code)
            self._in_code = False
            self._current_code = []

    def handle_data(self, data: str) -> None:
        """Accumulate text content for the currently active regions."""
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title = " ".join(part for part in [self.title, text] if part)
        if self._current_heading_level is not None:
            self._current_heading_text.append(text)
        if self._in_code:
            self._current_code.append(data)
        if self.links and not self.links[-1]["text"]:
            self.links[-1]["text"] = " ".join(text.split())


def http_user_agent() -> str:
    """Return the shared browser-style user agent string."""
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36"
    )


def domain_for(url: str) -> str:
    """Return the normalized netloc for a URL."""
    return urlparse(url).netloc.lower()


def source_type_for(url: str) -> str:
    """Classify a URL into a source type."""
    domain = domain_for(url)
    if "github.com" in domain:
        return "github"
    if any(part in domain for part in ("docs.", "readthedocs", "developer.")):
        return "docs"
    if domain.endswith(".edu") or "arxiv.org" in domain:
        return "academic"
    return "web"


def summarize_text(text: str, *, max_chars: int = 700) -> str:
    """Collapse whitespace and return a truncated summary."""
    cleaned = " ".join(text.split())
    return cleaned[:max_chars].rstrip() + ("…" if len(cleaned) > max_chars else "")


def search_terms(text: str) -> list[str]:
    """Extract focused query terms."""
    stop_words = {
        "about",
        "after",
        "and",
        "are",
        "blog",
        "can",
        "docs",
        "documentation",
        "for",
        "from",
        "how",
        "into",
        "latest",
        "official",
        "should",
        "site",
        "the",
        "this",
        "use",
        "used",
        "what",
        "when",
        "where",
        "with",
    }
    terms: list[str] = []
    seen: set[str] = set()
    precise_terms: list[str] = []

    def add(term: str) -> None:
        normalized = term.strip(".:-_").lower().removesuffix("()")
        if len(normalized) < 3 or normalized in stop_words:
            return
        if normalized in seen:
            return
        seen.add(normalized)
        terms.append(normalized)

    precise_patterns = [
        r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+(?:\(\))?",
        r"\b[A-Za-z_][A-Za-z0-9_]*\(\)",
        r"#[A-Za-z0-9_.-]+",
    ]
    for pattern in precise_patterns:
        for match in re.finditer(pattern, text):
            precise_term = match.group(0).strip(".:-_").lower()
            precise_term = precise_term.removesuffix("()")
            precise_terms.append(precise_term)
            add(precise_term)

    precise_atoms = {
        atom
        for precise_term in precise_terms
        for atom in re.split(r"[.:-_]+", precise_term)
        if atom
    }

    generic_terms: list[str] = []
    for raw_term in re.findall(r"[A-Za-z0-9_.:-]+", text.lower()):
        term = raw_term.strip(".:-_")
        if len(term) < 3 or term in stop_words or term in seen:
            continue
        if term in precise_atoms or any(term in precise for precise in precise_terms):
            continue
        generic_terms.append(term)

    if terms:
        generic_terms = generic_terms[:4]

    for term in generic_terms:
        add(term)

    return terms[:10]


def block_score(block: str, terms: list[str]) -> int:
    """Score a text block for term relevance."""
    lowered = block.lower()
    score = sum(1 for term in terms if term in lowered)
    if is_heading_block(block):
        score += 1
    navigation_markers = (
        "table of contents",
        "this page",
        "report a bug",
        "improve this page",
        "show source",
        "documentation »",
    )
    if any(marker in lowered for marker in navigation_markers):
        score -= 3
    if lowered.count("#") > 4 or lowered.count("*") > 12:
        score -= 2
    return score


def is_heading_block(block: str) -> bool:
    """Return whether a block looks like a heading."""
    stripped = block.lstrip()
    return stripped.startswith("#") or (
        len(stripped) < 120 and "\n" not in stripped and stripped.endswith(":")
    )


def chunk_text(text: str, *, max_chunk_chars: int = 2500) -> list[dict[str, object]]:
    """Split text into paragraph chunks."""
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    chunks: list[dict[str, object]] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs or [text.strip()]:
        if current and current_len + len(paragraph) + 2 > max_chunk_chars:
            chunk_value = "\n\n".join(current)
            chunks.append({"id": f"chunk-{len(chunks) + 1}", "content": chunk_value})
            current = []
            current_len = 0
        current.append(paragraph)
        current_len += len(paragraph) + 2
    if current:
        chunks.append(
            {"id": f"chunk-{len(chunks) + 1}", "content": "\n\n".join(current)}
        )
    return chunks


def filter_text_blocks(text: str, find: str | None) -> str:
    """Filter text to the most relevant blocks."""
    if not find:
        return text
    terms = search_terms(find)
    if not terms:
        return text
    blocks = [block.strip() for block in text.split("\n\n") if block.strip()]
    scored = [
        (block_score(block, terms), index, block) for index, block in enumerate(blocks)
    ]
    best_score = max((score for score, _, _ in scored), default=0)
    if best_score <= 0:
        return ""
    selected_indexes: set[int] = set()
    minimum_score = max(1, min(2, best_score))
    for score, index, _ in scored:
        if score >= minimum_score:
            selected_indexes.add(index)
            if index > 0 and is_heading_block(blocks[index - 1]):
                selected_indexes.add(index - 1)
            if is_heading_block(blocks[index]) and index + 1 < len(blocks):
                selected_indexes.add(index + 1)
    selected = [blocks[index] for index in sorted(selected_indexes)][:12]
    return "\n\n".join(selected)


def html_to_text_blocks(raw_html: str) -> str:
    """Convert simple HTML markup to paragraph-separated plain text."""
    text = re.sub(r"<script\b.*?</script>", " ", raw_html, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(
        r"</(?:p|div|li|h[1-6]|tr|section|article|br)\s*>",
        "\n\n",
        text,
        flags=re.I,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    blocks = [" ".join(html.unescape(block).split()) for block in text.split("\n\n")]
    return "\n\n".join(block for block in blocks if block)


def extract_raw_term_windows(raw_html: str, find: str | None) -> str:
    """Extract raw HTML text windows around search terms."""
    if not find:
        return ""
    terms = search_terms(find)
    if not terms:
        return ""
    plain = re.sub(r"<script\b.*?</script>", " ", raw_html, flags=re.I | re.S)
    plain = re.sub(r"<style\b.*?</style>", " ", plain, flags=re.I | re.S)
    plain = re.sub(r"<[^>]+>", " ", plain)
    plain = html.unescape(" ".join(plain.split()))
    windows: list[tuple[int, str]] = []
    lowered = plain.lower()
    for term in terms:
        start_at = 0
        while True:
            index = lowered.find(term, start_at)
            if index < 0:
                break
            start = max(0, index - 250)
            end = min(len(plain), index + 900)
            window = plain[start:end].strip()
            windows.append((block_score(window, terms), window))
            start_at = index + max(1, len(term))
    selected = [window for _, window in sorted(windows, reverse=True)[:3]]
    return "\n\n".join(dict.fromkeys(selected))


def select_fetch_content(
    *,
    mode: str,
    text: str,
    raw_text: str,
    summary: str,
    chunks: list[dict[str, object]],
    links: list[dict[str, str]],
    html_info: ReadableHTMLParser,
) -> object:
    """Select the payload shape for a fetch mode."""
    if mode == "summary":
        return summary
    if mode == "links":
        return links
    if mode == "code_blocks":
        return html_info.code_blocks[:50]
    if mode == "metadata":
        return {
            "title": html_info.title,
            "headings": html_info.headings[:100],
            "summary": summary,
        }
    if mode == "chunks":
        return chunks
    if mode == "raw":
        return raw_text
    return text
