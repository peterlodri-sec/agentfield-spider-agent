"""Cookie-banner dismissal + DOM extraction.

Two layers:

1. ``dismiss_banners(page)`` — selector + text-match rules ported from
   the Go spider's cookie_banner.go (OneTrust, Cookiebot, Quantcast,
   GDPR, CCPA, plus generic [id*=cookie] / [class*=cookie]). Fallback
   is a viewport-scan removal of any overlay matching the cookie /
   consent name patterns.

2. ``extract_title_text(page)`` — title + main-content text via a single
   ``page.evaluate()`` round-trip. Tries og:title / twitter:title /
   article h1 / main h1 / h1 / title in priority order, then article /
   main / [role=main] for the body. Falls back to ``document.body``
   innerText.

3. ``extract_structured(page)`` — JSON-LD, microdata, OpenGraph, links,
   images. JS snippets ported from the Go spider's dom_crawler.go.

All three return Python data structures via ``page.evaluate``, which
CloakBrowser inherits from Playwright. No Scrapling-specific CSS
selectors.
"""
from __future__ import annotations

from typing import Any

from .log import log

# Banner selectors / button texts. Order matters — specific vendors
# (OneTrust, Cookiebot, Quantcast) run before generic [id*=cookie]
# patterns because the vendor-specific rules are higher signal.
#
# action: "click" or "remove"
# button: comma-separated text labels to match, or "" if the selector
# itself is the button
_COOKIE_RULES: list[dict[str, str]] = [
    {
        "name": "onetrust",
        "selector": "#onetrust-accept-btn-handler, .onetrust-close-btn-handler",
        "button": "",
        "action": "click",
    },
    {
        "name": "cookiebot",
        "selector": (
            "#CybotCookiebotDialogBodyButtonAccept, "
            "#CybotCookiebotDialogBodyLevelButtonAccept"
        ),
        "button": "",
        "action": "click",
    },
    {
        "name": "quantcast",
        "selector": ".qc-cmp2-summary-buttons button[mode='primary']",
        "button": "",
        "action": "click",
    },
    {
        "name": "truste",
        "selector": "#truste-consent-button",
        "button": "",
        "action": "click",
    },
    {
        "name": "evidon",
        "selector": "#_evidon-accept-button",
        "button": "",
        "action": "click",
    },
    {
        "name": "generic_accept",
        "selector": "[id*='cookie'] button, [class*='cookie'] button",
        "button": "Accept,Got it,Allow,Agree,I understand,OK",
        "action": "click",
    },
    {
        "name": "gdpr_accept",
        "selector": "[class*='gdpr'] button, [id*='gdpr'] button",
        "button": "Accept all,Alle akzeptieren,Accepter tout,Accept",
        "action": "click",
    },
    {
        "name": "cmp_accept",
        "selector": "[class*='cmp'] button, [id*='cmp'] button",
        "button": "Accept,Allow all",
        "action": "click",
    },
    {
        "name": "ccpa",
        "selector": "[class*='ccpa'] button, [id*='ccpa'] button",
        "button": "",
        "action": "click",
    },
    {
        "name": "simple_remove",
        "selector": (
            "#cookie-banner, .cookie-banner, .cookie-consent, .gdpr-banner"
        ),
        "button": "",
        "action": "remove",
    },
]

# Fallback: any overlay with "cookie"/"consent" in its id/class that
# covers a meaningful chunk of viewport gets removed wholesale.
_FALLBACK_REMOVE_JS = """
() => {
    const all = document.querySelectorAll(
        '[id*="cookie"], [class*="cookie"], '
        + '[id*="consent"], [class*="consent"]'
    );
    let count = 0;
    for (const el of all) {
        const rect = el.getBoundingClientRect();
        if (rect.height > 50 && rect.width > 200 && rect.top < window.innerHeight) {
            el.style.display = 'none';
            el.remove();
            count++;
        }
    }
    return count;
}
"""

# Try-click via JS: scans the rule's selector for elements whose visible
# text contains any of the button-label fragments and clicks the first
# match. Returns the matched label, or "" if none.
_TRY_CLICK_JS = """
(args) => {
    const sel = args.selector;
    const labels = args.labels;  // [] = match by selector only
    const nodes = document.querySelectorAll(sel);
    for (const node of nodes) {
        const txt = (node.innerText || node.textContent || '').trim().toLowerCase();
        if (labels.length === 0) {
            node.click();
            return '_';
        }
        for (const label of labels) {
            if (txt.indexOf(label.toLowerCase()) !== -1) {
                node.click();
                return label;
            }
        }
    }
    return '';
}
"""

# Remove-via-JS for "action: remove" rules.
_REMOVE_JS = """
(selector) => {
    const els = document.querySelectorAll(selector);
    let count = 0;
    for (const el of els) {
        el.style.display = 'none';
        el.remove();
        count++;
    }
    return count;
}
"""

# Title + text extraction. Tries multiple selectors in priority order and
# returns the first match that looks substantive (text >= 200 chars for
# content; non-empty trimmed title).
_EXTRACT_JS = """
() => {
    let title = '';
    for (const sel of [
        'meta[property=\"og:title\"]',
        'meta[name=\"twitter:title\"]',
        'article h1', 'main h1', 'h1', 'title',
    ]) {
        const el = document.querySelector(sel);
        if (!el) continue;
        const v = sel.startsWith('meta')
            ? (el.getAttribute('content') || '')
            : ((el.innerText || el.textContent || '').trim());
        if (v) { title = v.trim(); break; }
    }
    let text = '';
    for (const sel of ['article', 'main', '[role=\"main\"]']) {
        const el = document.querySelector(sel);
        if (!el) continue;
        const v = (el.innerText || el.textContent || '').trim();
        if (v.length > 200) { text = v; break; }
    }
    if (!text) {
        text = document.body
            ? ((document.body.innerText || document.body.textContent || '').trim())
            : '';
    }
    return { title, text };
}
"""

# Structured-data extraction. Ports the Go dom_crawler.go JS snippets
# into a single round-trip so we don't pay multiple page.evaluate IPC
# costs.
_STRUCTURED_JS = """
() => {
    const jsonld = Array.from(
        document.querySelectorAll('script[type=\"application/ld+json\"]')
    ).map(s => {
        try { return JSON.parse(s.textContent); } catch (e) { return null; }
    }).filter(x => x !== null);

    const microdata = Array.from(document.querySelectorAll('[itemscope]'))
        .map(scope => {
            const type = scope.getAttribute('itemtype') || '';
            const props = { _type: type };
            scope.querySelectorAll('[itemprop]').forEach(el => {
                const key = el.getAttribute('itemprop');
                let val = el.getAttribute('content')
                    || (el.textContent || '').trim();
                if (el.tagName === 'A' && el.href) val = el.href;
                if (el.tagName === 'IMG' && el.src) val = el.src;
                if (el.tagName === 'TIME' && el.dateTime) val = el.dateTime;
                if (key) props[key] = val;
            });
            return props;
        });

    const og = {};
    document.querySelectorAll(
        'meta[property^=\"og:\"], meta[name^=\"og:\"]'
    ).forEach(m => {
        const key = m.getAttribute('property') || m.getAttribute('name');
        if (key) og[key] = m.getAttribute('content') || '';
    });

    const host = location.host;
    const links = Array.from(document.querySelectorAll('a[href]'))
        .map(a => ({
            href: a.href,
            text: (a.innerText || '').trim().substring(0, 200),
            title: a.title || '',
            rel: a.rel || '',
            isInternal: a.host === host,
            isNofollow: (a.rel || '').includes('nofollow'),
            isUGC: (a.rel || '').includes('ugc'),
            isSponsored: (a.rel || '').includes('sponsored'),
        }));

    const images = Array.from(document.querySelectorAll('img')).map(img => ({
        src: img.src || img.currentSrc || '',
        alt: img.alt || '',
        width: img.naturalWidth || img.width || 0,
        height: img.naturalHeight || img.height || 0,
        loading: img.loading || 'auto',
        isLazy: img.loading === 'lazy',
    }));

    return { jsonld, microdata, opengraph: og, links, images };
}
"""


async def dismiss_banners(page: Any) -> bool:
    """Click / remove the first matching cookie banner.

    Returns True on a successful dismissal, False if no rule matched
    (the fallback overlay-scan ran regardless).
    """
    handled = False
    for rule in _COOKIE_RULES:
        if rule["action"] == "remove":
            try:
                count = await page.evaluate(_REMOVE_JS, rule["selector"])
                if count and count > 0:
                    log("cookie", state="removed", rule=rule["name"], n=count)
                    handled = True
                    break
            except Exception:
                continue
            continue
        labels = (
            [s.strip() for s in rule["button"].split(",") if s.strip()]
            if rule["button"]
            else []
        )
        try:
            matched = await page.evaluate(
                _TRY_CLICK_JS,
                {"selector": rule["selector"], "labels": labels},
            )
        except Exception:
            continue
        if matched:
            log("cookie", state="clicked", rule=rule["name"], match=matched)
            handled = True
            break

    if not handled:
        try:
            removed = await page.evaluate(_FALLBACK_REMOVE_JS)
            if removed and removed > 0:
                log("cookie", state="fallback-removed", n=removed)
        except Exception:
            pass

    return handled


async def extract_title_text(page: Any) -> tuple[str, str]:
    """Return ``(title, text)``. Both may be empty strings.

    Single ``page.evaluate`` round-trip; the JS picks the highest-signal
    title selector and the longest substantive content block.
    """
    try:
        result = await page.evaluate(_EXTRACT_JS)
    except Exception as e:  # pragma: no cover
        log("extract", state="error", msg=f"{type(e).__name__}: {e}")
        return "", ""
    if not isinstance(result, dict):
        return "", ""
    title = str(result.get("title") or "").strip()
    text = str(result.get("text") or "").strip()
    return title, text


async def extract_structured(page: Any) -> dict:
    """Return ``{jsonld, microdata, opengraph, links, images}``.

    Ported from the Go spider's dom_crawler.go. Single round-trip;
    raises only on unrecoverable browser errors.
    """
    try:
        result = await page.evaluate(_STRUCTURED_JS)
    except Exception as e:  # pragma: no cover
        log("structured", state="error", msg=f"{type(e).__name__}: {e}")
        return {
            "jsonld": [], "microdata": [], "opengraph": {},
            "links": [], "images": [],
        }
    if not isinstance(result, dict):
        return {
            "jsonld": [], "microdata": [], "opengraph": {},
            "links": [], "images": [],
        }
    # Defensive normalisation — page.evaluate output is JSON-ish but
    # the test fakes may return slightly different shapes.
    return {
        "jsonld": result.get("jsonld") or [],
        "microdata": result.get("microdata") or [],
        "opengraph": result.get("opengraph") or {},
        "links": result.get("links") or [],
        "images": result.get("images") or [],
    }


__all__ = [
    "dismiss_banners",
    "extract_structured",
    "extract_title_text",
]
