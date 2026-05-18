"""Docling integration — PDF / Word / PowerPoint / HTML → structured.

`Docling <https://github.com/docling-project/docling>`_ is IBM's
Python library for parsing office documents into structured Markdown
+ tables. We use it as a side-channel for URLs that point to document
files. The dispatcher routes here when the URL matches a recognized
document extension; per-fetcher integration (Content-Type sniff) is a
follow-up.

Heavy dependency: docling downloads ~1.5 GB of ML models on first use.
Make it opt-in via the ``[docling]`` extra in ``pyproject.toml``. If
``docling`` isn't installed when the parser is invoked, the function
returns a clean ``status="error"`` envelope rather than raising —
matches the rest of the dispatcher contract.

Why a side-channel and not the regular browser path
---------------------------------------------------

Chromium can render PDFs but only into a single canvas — no structured
text, no table extraction. arxiv papers, RFCs, and slide decks need
structure, so the spider routes them away from the browser pool.
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any

from .log import log

# PEP 695 type alias.
type DoclingResult = dict[str, Any]


# Recognized URL extensions that hint at document content.
DOC_EXTS: frozenset[str] = frozenset({
    ".pdf",
    ".docx", ".doc",
    ".pptx", ".ppt",
    ".xlsx", ".xls",
    ".odt", ".odp", ".ods",
    ".rtf",
})

# Content-Type prefixes that route to docling. (Used by fetcher
# implementations doing a HEAD probe before deciding the path.)
DOC_CONTENT_TYPE_PREFIXES: tuple[str, ...] = (
    "application/pdf",
    "application/x-pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.",
    "application/vnd.ms-",
    "application/vnd.oasis.opendocument.",
)


class DoclingDependencyError(RuntimeError):
    """``docling`` isn't installed; surfaced via the status dict in
    most paths (this exception is only thrown if the caller wants to
    know directly).
    """


def is_docling_url(url: str) -> bool:
    """Cheap URL-extension test; no I/O.

    Strips query/fragment, lowercases path, checks the extension
    against ``DOC_EXTS``.
    """
    if not isinstance(url, str) or not url:
        return False
    path = re.sub(r"[?#].*$", "", url).lower()
    return any(path.endswith(ext) for ext in DOC_EXTS)


def is_docling_content_type(content_type: str | None) -> bool:
    """Test whether a Content-Type indicates a docling-parseable doc."""
    if not content_type:
        return False
    return content_type.lower().startswith(DOC_CONTENT_TYPE_PREFIXES)


def _new_job_id() -> str:
    return uuid.uuid4().hex[:12]


def _first_heading(markdown: str) -> str:
    """Return the first ``# `` or ``## `` heading from markdown, stripped."""
    for raw in markdown.split("\n"):
        line = raw.strip()
        if line.startswith("# "):
            return line.lstrip("#").strip()
        if line.startswith("## "):
            return line.lstrip("#").strip()
    return ""


def _error_envelope(
    url: str,
    *,
    error: str,
    t0: float,
    ts: int,
    job_id: str,
) -> DoclingResult:
    return {
        "id": job_id,
        "url": url,
        "fetch_url": url,
        "ts": ts,
        "status": "error",
        "error": error,
        "ms": int((time.perf_counter() - t0) * 1000),
        "kind": "docling-document",
    }


async def fetch_via_docling(
    url: str,
    *,
    proxy: str | None = None,
) -> DoclingResult:
    """Parse the document at ``url`` via docling.

    Returns the dispatcher-standard flat dict (status / id / url /
    fetch_url / ts / ms / kind) plus ``title`` / ``text`` (markdown) /
    ``description`` / ``metadata`` on success. Errors land in
    ``status="error"`` with an ``error`` message.

    The ``proxy`` kwarg is accepted for API symmetry but docling's
    document_converter fetches the URL internally and doesn't expose
    a per-call proxy hook in v2. Setting it logs a warning and
    proceeds without proxy. To use a proxy, set ``HTTP_PROXY`` env or
    fetch the doc separately and pass a local path (out of v1 scope).
    """
    t0 = time.perf_counter()
    ts = int(time.time() * 1000)
    job_id = _new_job_id()

    if proxy:
        log(
            "docling",
            state="warn",
            msg="proxy kwarg ignored; docling fetches URLs directly",
            url=url,
        )

    # Lazy import — docling pulls in transformers + torch and is
    # ~1.5 GB after first model download.
    try:
        from docling.document_converter import DocumentConverter
    except ImportError:
        return _error_envelope(
            url,
            error=(
                "docling not installed. Install with: "
                "pip install agentfield-spider-agent[docling]"
            ),
            t0=t0,
            ts=ts,
            job_id=job_id,
        )

    loop = asyncio.get_running_loop()

    def _parse() -> dict[str, Any]:
        converter = DocumentConverter()
        result = converter.convert(url)
        doc = result.document
        markdown = doc.export_to_markdown()

        tables_md: list[str] = []
        tables_attr = getattr(doc, "tables", None) or []
        # Cap to keep response sane; users wanting full tables can
        # call docling directly.
        for t in tables_attr[:10]:
            try:
                tables_md.append(t.export_to_markdown())
            except Exception:  # pragma: no cover - per-table failure
                continue

        return {
            "markdown": markdown,
            "tables": tables_md,
            "n_pages": getattr(doc, "num_pages", None),
        }

    try:
        parsed = await loop.run_in_executor(None, _parse)
    except Exception as e:
        return _error_envelope(
            url,
            error=f"docling parse failed: {type(e).__name__}: {e}",
            t0=t0,
            ts=ts,
            job_id=job_id,
        )

    md: str = parsed["markdown"]
    title = _first_heading(md) or url
    description = "Document parsed by Docling"
    if parsed.get("n_pages"):
        description += f" ({parsed['n_pages']} page(s))"

    return {
        "id": job_id,
        "url": url,
        "fetch_url": url,
        "ts": ts,
        "status": "ok",
        "kind": "docling-document",
        "title": title,
        "name": title,
        "description": description,
        "text": md or None,
        "ms": int((time.perf_counter() - t0) * 1000),
        "metadata": {
            "n_pages": parsed.get("n_pages"),
            "n_tables": len(parsed["tables"]),
            "tables_preview": parsed["tables"][:3],
        },
    }


__all__ = [
    "DOC_CONTENT_TYPE_PREFIXES",
    "DOC_EXTS",
    "DoclingDependencyError",
    "DoclingResult",
    "fetch_via_docling",
    "is_docling_content_type",
    "is_docling_url",
]
