#!/usr/bin/env bash
# Run all 6 spider-agent live smoke checks in sequence.
# Each prints PASS / WARN / FAIL; first FAIL exits non-zero.
#
# Override targets:
#   SMOKE_FETCH_URL=...       (default: https://example.com)
#   SMOKE_SITEMAP_URLS=u1,u2  (multi-target; defaults to a curated list of
#                              our regularly-crawled domains)
#   SMOKE_SITEMAP_URL=...     (single override, legacy)
#   SMOKE_CRAWL_URL=...       (default: https://example.com)
#   SMOKE_CRAWL_MAX_PAGES=N   (default: 3)
#   SMOKE_CRAWL_TIMEOUT_S=N   (default: 60)
#   SMOKE_SINK_URL=...        (chromadb-sink target page, default example.com)
#
# Skip individual checks:
#   SKIP_CRAWL=1 ./check-all.sh    # skip the longest one (needs CloakBrowser)
#   SKIP_SITEMAP=1 ./check-all.sh
#   SKIP_SINK=1 ./check-all.sh     # skip chromadb (auto-skips if SPIDER_SINK_ENABLED unset)
#   SKIP_MULTI=1 ./check-all.sh    # sitemap runs only the reference target
#   SKIP_REUSE=1 ./check-all.sh    # skip HTTP/TLS connection-reuse probe
#   SKIP_ACT=1 ./check-all.sh      # skip /act (requires [browser-use] extra for full pass)

set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

echo
"$SCRIPT_DIR/check-services.sh"
echo
"$SCRIPT_DIR/check-health.sh"
echo
"$SCRIPT_DIR/check-fetch.sh"
echo

if [[ "${SKIP_SITEMAP:-0}" == "1" ]]; then
    echo "═══ D. sitemap ═══"
    echo "SKIP: SKIP_SITEMAP=1"
else
    "$SCRIPT_DIR/check-sitemap.sh"
fi
echo

if [[ "${SKIP_CRAWL:-0}" == "1" ]]; then
    echo "═══ E. crawl ═══"
    echo "SKIP: SKIP_CRAWL=1"
else
    "$SCRIPT_DIR/check-crawl.sh"
fi
echo

if [[ "${SKIP_SINK:-0}" == "1" ]]; then
    echo "═══ F. chromadb sink ═══"
    echo "SKIP: SKIP_SINK=1"
else
    "$SCRIPT_DIR/check-chroma-sink.sh"
fi
echo

if [[ "${SKIP_REUSE:-0}" == "1" ]]; then
    echo "═══ G. network reuse ═══"
    echo "SKIP: SKIP_REUSE=1"
else
    "$SCRIPT_DIR/check-network-reuse.sh"
fi
echo

if [[ "${SKIP_ACT:-0}" == "1" ]]; then
    echo "═══ H. /act ═══"
    echo "SKIP: SKIP_ACT=1"
else
    "$SCRIPT_DIR/check-act.sh"
fi

echo
echo "═════════════════════════════════"
echo "All spider-agent smoke checks completed."
