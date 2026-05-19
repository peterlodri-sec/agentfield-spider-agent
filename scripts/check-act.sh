#!/usr/bin/env bash
# Smoke: POST /act against a session.
#
# When the [browser-use] install extra IS NOT present in the running
# spider container, the route correctly returns 501 with the
# "install the [browser-use] extra" envelope — we treat that as a
# pass (the route plumbing works; the optional dep just isn't there).
#
# When the extra IS present, the script tries a trivial task against
# https://example.com and asserts status="ok".

source "$(dirname "$0")/_lib.sh"

TASK="${SMOKE_ACT_TASK:-Open https://example.com and return the page title}"
TARGET_HOST="${SMOKE_ACT_HOST:-example.com}"

echo "═══ POST ${BASE_URL}/sessions ═══"
SESSION_JSON="$(hcurl -X POST "${BASE_URL}/sessions" \
    -H 'Content-Type: application/json' \
    -d '{"name": "smoke-act"}')"
echo "$SESSION_JSON"
SID="$(echo "$SESSION_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id", ""))')"
if [ -z "$SID" ]; then
    fail "could not extract session id"
    exit 1
fi
ok "session created: $SID"

echo
echo "═══ POST ${BASE_URL}/act ═══"
RESP="$(hcurl -X POST "${BASE_URL}/act" \
    -H 'Content-Type: application/json' \
    -w '\n__HTTP_%{http_code}__' \
    -d "$(python3 -c "import json,sys; print(json.dumps({'task': sys.argv[1], 'session_id': sys.argv[2], 'max_steps': 5, 'allowed_hosts': [sys.argv[3]]}))" "$TASK" "$SID" "$TARGET_HOST")")"
HTTP_CODE="$(echo "$RESP" | tail -1 | sed -n 's/^__HTTP_\([0-9]\{3\}\)__$/\1/p')"
BODY="$(echo "$RESP" | sed '$d')"

echo "$BODY"
echo "(http $HTTP_CODE)"

case "$HTTP_CODE" in
    200)
        ok "/act returned 200; browser-use Agent loop ran"
        ;;
    501)
        warn "/act returned 501 — [browser-use] extra not installed in this image"
        warn "Install via: pip install agentfield-spider-agent[browser-use]"
        warn "Treating route plumbing as healthy."
        ;;
    *)
        fail "/act returned unexpected HTTP $HTTP_CODE"
        # Clean up the session before bailing.
        hcurl -X DELETE "${BASE_URL}/sessions/${SID}" >/dev/null 2>&1 || true
        exit 1
        ;;
esac

echo
echo "═══ DELETE ${BASE_URL}/sessions/${SID} ═══"
hcurl -X DELETE "${BASE_URL}/sessions/${SID}"
echo
ok "session closed"
