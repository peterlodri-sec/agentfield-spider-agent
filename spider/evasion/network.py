"""Network-level evasion: WebRTC IP-leak block.

WebRTC IP leak: when a page calls ``new RTCPeerConnection()``, the
browser learns its own real LAN + WAN IPs from the OS network stack
and reports them via STUN ICE candidates. This BYPASSES any HTTP
proxy. The block prevents creation of RTCPeerConnection entirely, so
a misconfigured site that gathers ICE candidates gets nothing.

Why block instead of fake: returning fake candidates is detectable —
real Chrome always returns SOME local/private IPs. Returning nothing
is also detectable but indistinguishable from "user disabled WebRTC",
a real (small) population.

Applied via ``context.add_init_script(WEBRTC_BLOCK_JS)`` so the
override is installed before any page script runs.
"""
from __future__ import annotations

# Replaces RTC-related globals with getters that throw. Runs once per
# page navigation as an init script (configured at context level), so
# the override is in place before document_start. ``try/catch`` per
# define so a partial failure (e.g. property already non-configurable)
# doesn't abort the rest.
WEBRTC_BLOCK_JS = """
(() => {
    const block = () => { throw new Error('WebRTC disabled'); };
    if (typeof window === 'undefined') return;
    const names = [
        'RTCPeerConnection',
        'webkitRTCPeerConnection',
        'mozRTCPeerConnection',
        'RTCSessionDescription',
        'RTCIceCandidate',
    ];
    for (const n of names) {
        try {
            Object.defineProperty(window, n, {
                get: block,
                configurable: true,
            });
        } catch (e) { /* ignore */ }
    }
})();
"""


__all__ = ["WEBRTC_BLOCK_JS"]
