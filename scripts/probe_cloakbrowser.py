#!/usr/bin/env python3
"""Probe CloakBrowser end-to-end against the assumptions in
``spider/browser.py``.

Walks each step the spider's lease/fetch path makes, in order, and
fails fast at the first mismatch so the fix is obvious from the
output.

Usage
-----
::

    # default URL (https://example.com/)
    python3 scripts/probe_cloakbrowser.py

    # custom URL — try something JS-heavy / cookie-banner-heavy:
    python3 scripts/probe_cloakbrowser.py https://www.cloudflare.com/
    python3 scripts/probe_cloakbrowser.py https://news.ycombinator.com/

    # inside the docker image (validates the runtime env, not just the host):
    docker compose run --rm spider-http \\
        python3 scripts/probe_cloakbrowser.py

The first run triggers the ~200 MB Chromium binary auto-download, so
expect step 3 ("launch") to take 30 - 90 s. Subsequent runs are fast.

Exit codes
----------
::

    0   all steps pass
    10  cloakbrowser not importable  (pip install cloakbrowser)
    20  launch_async not found at expected import path
    21  no submodule fallback found either
    30  launch_async kwargs don't match our usage
    31  launch_async raised — usually Chromium binary issue
    40  browser.new_page missing
    41  new_page raised
    50  page.goto kwargs mismatch (wait_until / timeout)
    51  goto raised after kwargs retry
    60  page.evaluate raised

If you see a non-zero exit, the fix is almost always confined to
``spider/browser.py`` — that's the abstraction layer for exactly this
kind of API drift.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_URL = "https://example.com/"


def hdr(title: str) -> None:
    print(f"\n=== {title} ===")


def ok(msg: str) -> None:
    print(f"  [pass]  {msg}")


def fail(msg: str) -> None:
    print(f"  [fail]  {msg}")


def info(msg: str) -> None:
    print(f"          {msg}")


async def probe(url: str) -> int:
    # ── 1. import ──
    hdr("1. import cloakbrowser")
    try:
        cb = importlib.import_module("cloakbrowser")
    except ImportError as e:
        fail(f"could not import cloakbrowser: {e}")
        info("install with: pip install cloakbrowser")
        return 10
    ok(f"imported from {cb.__file__}")
    if hasattr(cb, "__version__"):
        info(f"version: {cb.__version__}")

    # ── 2. resolve launch_async ──
    hdr("2. resolve launch_async")
    launch_async = getattr(cb, "launch_async", None)
    if launch_async is None:
        # Some Playwright drop-ins expose launchers via submodules. If
        # that's where the function lives, the fix is one line in
        # spider/browser.py's import block — print enough info to
        # make that change obvious.
        public = sorted(n for n in dir(cb) if not n.startswith("_"))
        info(f"top-level dir(cloakbrowser): {public}")
        for sub_name in ("async_api", "async_playwright", "playwright_async"):
            try:
                sub = importlib.import_module(f"cloakbrowser.{sub_name}")
            except ImportError:
                continue
            for cand in ("launch_async", "launch"):
                la = getattr(sub, cand, None)
                if la is not None:
                    fail(
                        "top-level launch_async missing; "
                        f"found cloakbrowser.{sub_name}.{cand} instead"
                    )
                    info(
                        "fix: in spider/browser.py change "
                        f"`from cloakbrowser import launch_async` to "
                        f"`from cloakbrowser.{sub_name} import {cand}`"
                    )
                    return 20
        fail("cloakbrowser exports neither launch_async nor any known submodule launcher")
        return 21
    ok(f"resolved at {launch_async.__module__}.{launch_async.__name__}")
    try:
        sig = inspect.signature(launch_async)
        info(f"signature: {sig}")
    except (TypeError, ValueError):
        info("signature: (introspection failed; likely a C/builtin)")

    # ── 3. launch ──
    hdr("3. launch_async(headless=True)")
    t0 = time.perf_counter()
    info("(first run downloads ~200 MB Chromium; can take 30-90 s)")
    try:
        browser = await launch_async(headless=True)
    except TypeError as e:
        fail(f"signature mismatch: {e}")
        info("spider/browser.py calls launch_async(headless=HEADLESS, **kwargs)")
        info("check the resolved signature above and adjust the call site")
        return 30
    except Exception as e:
        fail(f"launch raised {type(e).__name__}: {e}")
        info("usually means the Chromium binary failed to download or run:")
        info("  - check ~/.cloakbrowser (binary path)")
        info("  - check disk space (~200 MB needed)")
        info("  - check chmod +x on the binary")
        info("  - on Linux, sandboxing may need --no-sandbox; try inside Docker")
        return 31
    info(f"launch took {time.perf_counter() - t0:.1f}s")
    info(f"browser type: {type(browser).__name__}")
    ok("browser launched")

    # ── 3b. confirm the kwargs spider/browser.py forwards ──
    # spider/browser.py's CloakBrowserPool(__init__, **kwargs) currently
    # passes max_pages=BROWSER_MAX_PAGES to launch_async. Verify that
    # launch_async tolerates unknown kwargs — if not, we'd need to
    # filter kwargs in browser.py.
    hdr("3b. extra kwargs tolerance (max_pages)")
    try:
        b2 = await launch_async(headless=True, max_pages=3)
        ok("launch_async accepts max_pages")
        await b2.close()
    except TypeError as e:
        fail(f"launch_async rejects max_pages: {e}")
        info(
            "fix: drop max_pages from CloakBrowserPool's kwargs in "
            "spider/browser.py — Scrapling accepted it but CloakBrowser doesn't"
        )

    # ── 4. new_page ──
    hdr("4. browser.new_page()")
    if not hasattr(browser, "new_page"):
        fail("browser has no .new_page method")
        attrs = sorted(a for a in dir(browser) if not a.startswith("_"))[:25]
        info(f"browser attrs (first 25): {attrs}")
        return 40
    try:
        page = await browser.new_page()
    except Exception as e:
        fail(f"new_page raised {type(e).__name__}: {e}")
        return 41
    info(f"page type: {type(page).__name__}")
    ok("page opened")

    # ── 5. goto ──
    hdr(f"5. page.goto({url!r})")
    t0 = time.perf_counter()
    try:
        await page.goto(url, wait_until="networkidle", timeout=30_000)
    except TypeError as e:
        # Some drop-ins use snake_case names or different kwargs.
        fail(f"goto kwargs mismatch: {e}")
        info("retrying without wait_until / timeout ...")
        try:
            await page.goto(url)
            ok("goto succeeded without kwargs (drop wait_until/timeout from browser.py)")
        except Exception as e2:
            fail(f"goto without kwargs also failed: {type(e2).__name__}: {e2}")
            await _safe_close(page, browser)
            return 50
    except Exception as e:
        fail(f"goto raised {type(e).__name__}: {e}")
        info("check the target URL is reachable from this host")
        await _safe_close(page, browser)
        return 51
    info(f"goto took {time.perf_counter() - t0:.1f}s")
    ok(f"navigated to {url}")

    # ── 6. evaluate ──
    hdr("6. page.evaluate(js)")
    try:
        title = await page.evaluate("() => document.title")
    except Exception as e:
        fail(f"evaluate raised {type(e).__name__}: {e}")
        await _safe_close(page, browser)
        return 60
    info(f"document.title = {title!r}")
    ok("page.evaluate returned a value")

    # ── 6b. realistic extraction (matches spider/extract.py shape) ──
    # Only available when this script is run from the repo root so
    # ``spider.extract`` is importable. We optionally exercise the real
    # extraction logic against this real page to validate the
    # JS-in-Python round trip beyond just `document.title`.
    hdr("6b. spider.extract.extract_title_text against a real page")
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    try:
        from spider import extract  # type: ignore[import-not-found]
    except ImportError:
        info(
            "spider.extract not importable from this CWD; "
            "skip 6b. (run from the agentfield-spider-agent repo root to "
            "exercise the real extraction)"
        )
    else:
        try:
            title_t, text_t = await extract.extract_title_text(page)
            info(f"title = {title_t!r}")
            preview = text_t[:120] + ("..." if len(text_t) > 120 else "")
            info(f"text  = {preview!r}  (len={len(text_t)})")
            if title_t or text_t:
                ok("spider.extract.extract_title_text works against CloakBrowser")
            else:
                fail("extract_title_text returned empty for both fields")
                info("the page may be JS-heavy and our JS heuristics missed it")
        except Exception as e:
            fail(f"extract_title_text raised {type(e).__name__}: {e}")

        # Bonus: cookie banner dismissal (no assertion — many test
        # pages don't have a banner; we just want to confirm the JS
        # round-trip doesn't error).
        try:
            await extract.dismiss_banners(page)
            info("dismiss_banners ran without raising")
        except Exception as e:
            fail(f"dismiss_banners raised {type(e).__name__}: {e}")

    # ── 7. cleanup ──
    hdr("7. cleanup")
    await _safe_close(page, browser)
    return 0


async def _safe_close(page: Any, browser: Any) -> None:
    try:
        await page.close()
        ok("page.close()")
    except Exception as e:
        fail(f"page.close raised {type(e).__name__}: {e}")
    try:
        await browser.close()
        ok("browser.close()")
    except Exception as e:
        fail(f"browser.close raised {type(e).__name__}: {e}")


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f"probing cloakbrowser against {url}")
    print(f"python {sys.version.split()[0]} on {sys.platform}")
    try:
        rc = asyncio.run(probe(url))
    except KeyboardInterrupt:
        return 130
    print()
    if rc == 0:
        print("all steps passed — spider/browser.py assumptions hold")
    else:
        print(f"failed with exit {rc}; see the failed section above for the fix")
    return rc


if __name__ == "__main__":
    sys.exit(main())
