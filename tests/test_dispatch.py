"""URL classification — pure regex matching, no I/O."""
from __future__ import annotations

import pytest

from spider.dispatch import classify


@pytest.mark.parametrize(
    "url,expected",
    [
        # github repo
        ("https://github.com/rooootdev/lara", "github"),
        ("https://github.com/zakirullin/files.md", "github"),
        ("https://github.com/owner/repo", "github"),
        ("https://github.com/owner/repo/", "github"),
        ("https://github.com/owner/repo?foo=bar", "github"),
        ("http://github.com/owner/repo", "github"),
        # github issue
        ("https://github.com/devlikeapro/waha/issues/1076", "github-issue"),
        ("https://github.com/o/r/issues/1", "github-issue"),
        # github sub-pages = generic (we don't have fetchers for these)
        ("https://github.com/owner/repo/pulls", "generic"),
        ("https://github.com/owner/repo/tree/main/src", "generic"),
        # hn
        ("https://news.ycombinator.com/item?id=12345", "hn"),
        ("https://news.ycombinator.com/from?site=example.com", "hn"),
        ("http://news.ycombinator.com/", "hn"),
        # reddit
        ("https://www.reddit.com/r/python/", "reddit"),
        ("https://old.reddit.com/r/python/", "reddit"),
        ("https://reddit.com/r/python/comments/abc/", "reddit"),
        ("https://www.reddit.com/r/AgentsOfAI/s/SHORT", "reddit"),
        # docling (URL extension)
        ("https://arxiv.org/pdf/1706.03762.pdf", "docling"),
        ("https://example.com/spec.docx", "docling"),
        ("https://example.com/deck.pptx", "docling"),
        ("https://example.com/sheet.xlsx", "docling"),
        ("https://example.com/paper.PDF?ref=hn", "docling"),  # case + query
        # generic
        ("https://example.com/", "generic"),
        ("https://waha.devlike.pro/docs/", "generic"),
        ("https://example.com/page.html", "generic"),  # html doesn't route to docling
        ("ftp://example.com/file", "generic"),
        # garbage
        ("", "generic"),
        ("not-a-url", "generic"),
    ],
)
def test_classify(url: str, expected: str) -> None:
    assert classify(url) == expected


def test_classify_returns_generic_for_non_string() -> None:
    """Defensive: pure function shouldn't blow up on bad input."""
    assert classify(None) == "generic"  # type: ignore[arg-type]
    assert classify(123) == "generic"   # type: ignore[arg-type]
