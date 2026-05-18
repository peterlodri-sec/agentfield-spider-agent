"""Helpers shared across specialized fetchers.

Centralizes the bits each fetcher needs:
- a deterministic result builder that matches ``fetch_one``'s shape
- a "first meaningful paragraph" helper for README / selftext / body
  excerpts
- the HTTP user-agent string the fetchers use against third-party APIs
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any

# UA string for outbound HTTP. Some endpoints (Reddit, HN search) reject
# the urllib default; we identify as a real browser-like agent.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36 "
    "agentfield-spider/0.2"
)

# Bytes cap on responses we'll body-read into memory.
RESPONSE_CAP_BYTES = 4 * 1024 * 1024  # 4 MiB


def now_ms() -> int:
    return int(time.time() * 1000)


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


def base_result(url: str) -> dict[str, Any]:
    """Start a result dict with the keys the dispatcher contract guarantees.

    Specialized fetchers extend this with ``kind``, ``description``,
    ``title``, ``text``, etc. before returning.
    """
    return {
        "id": new_job_id(),
        "url": url,
        "fetch_url": url,
        "ts": now_ms(),
    }


def ok_result(
    url: str,
    *,
    title: str | None,
    text: str | None,
    t0: float,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ``status="ok"`` result. ``t0`` is a ``time.perf_counter()``
    sentinel taken at the start of the fetch."""
    out = base_result(url)
    out.update({
        "status": "ok",
        "title": title or None,
        "text": text or None,
        "ms": int((time.perf_counter() - t0) * 1000),
    })
    if extras:
        out.update(extras)
    return out


def error_result(
    url: str,
    *,
    error: str,
    t0: float,
    status: str = "error",
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a non-ok result (status defaults to ``"error"``)."""
    out = base_result(url)
    out.update({
        "status": status,
        "error": error,
        "ms": int((time.perf_counter() - t0) * 1000),
    })
    if extras:
        out.update(extras)
    return out


_BADGE_RE = re.compile(r"^\s*\[!\[.+\]\(.+\)\]\(.+\)\s*$")


def first_meaningful_paragraph(text: str, *, min_len: int = 50) -> str:
    """Return the first prose paragraph from ``text`` >= ``min_len`` chars.

    Skips headers (``#``), code fences, blockquotes / lists / tables,
    HTML tags, and shield-badge link-lines. Falls back to the longest
    paragraph seen if nothing reaches ``min_len``.

    Same heuristic as the one in scripts/gather_projects.py — kept
    here so the fetchers don't depend on a script outside the package.
    """
    paragraphs: list[str] = []
    current: list[str] = []
    in_code = False
    for raw in text.split("\n"):
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if in_code:
            continue
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        # Skip headers, image/link badges, HTML, blockquotes, lists, tables.
        if line.startswith(("#", "!", "<", ">", "|", "-")):
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if _BADGE_RE.match(line):
            continue
        current.append(line)
    if current:
        paragraphs.append(" ".join(current))

    def _visible_len(p: str) -> int:
        # Strip markdown link syntax so [text](url) counts as len(text).
        return len(re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", p))

    for p in paragraphs:
        if _visible_len(p) >= min_len:
            return p
    return max(paragraphs, key=_visible_len, default="")


_HTML_SCRIPT_STYLE = re.compile(
    r"<(script|style)\b[^<]*(?:(?!</\1>)<[^<]*)*</\1>",
    re.IGNORECASE,
)


def strip_html(html: str, *, cap: int = 4000) -> str:
    """Drop ``<script>`` / ``<style>`` blocks and tags, collapse
    whitespace. Returns at most ``cap`` chars."""
    h = _HTML_SCRIPT_STYLE.sub(" ", html)
    h = re.sub(r"<[^>]+>", " ", h)
    h = re.sub(r"\s+", " ", h).strip()
    return h[:cap]


def extract_html_title(html: str) -> str:
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(
        r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    return ""


__all__ = [
    "RESPONSE_CAP_BYTES",
    "USER_AGENT",
    "base_result",
    "error_result",
    "extract_html_title",
    "first_meaningful_paragraph",
    "new_job_id",
    "now_ms",
    "ok_result",
    "strip_html",
]
