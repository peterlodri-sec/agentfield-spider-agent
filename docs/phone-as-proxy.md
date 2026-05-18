# Phone as 4G/5G proxy for the spider

Datacenter IPs get bot-flagged. Mobile carrier IPs (4G/5G) rarely do
— Cloudflare's "bot" verdict differs by ASN, and a phone tethered on
a mobile network looks like a real human session to most anti-bot
heuristics.

The spider supports an outbound proxy via the `SPIDER_PROXY_URL` env
var. This guide walks the easiest setup paths for routing the spider's
HTTP + browser traffic through a phone's mobile connection.

You only need this when scraping sites that block datacenter IPs.
Most of the URL dispatcher (GitHub, HN, Reddit) speaks to the official
APIs which don't care about IP, so the proxy is most useful for the
browser-rendered path.

## Three setup paths

| Path | Phone | Pros | Cons |
|---|---|---|---|
| **A. Termux + SSH tunnel** | Android | Easiest; no extra services; survives reboot with `termux-boot` | Battery drain; phone has to be unlocked + plugged in |
| **B. Tailscale exit node** | Android (works on iOS too with caveats) | Mesh networking; works from anywhere on the tailnet; encrypted | Requires Tailscale account; bandwidth metered through Tailscale's DERP if direct fails |
| **C. WiFi hotspot to a small Linux box (Pi/old laptop)** | Any | Phone just tethers; the proxy lives on the box | Two devices; more moving parts |

All three end with the spider seeing a `SPIDER_PROXY_URL` it can
connect to. The differences are who runs the proxy daemon.

---

## Path A — Termux SOCKS5 over SSH (Android, recommended)

The spider will SSH to the phone and use SSH's `-D` flag to open a
local SOCKS5 port. Traffic egresses through the phone's mobile
connection.

### One-time setup on the phone

```bash
# Install Termux from F-Droid (the Play Store version is outdated).
# Inside Termux:

pkg update && pkg install openssh

# Set a password for incoming ssh:
passwd

# Start sshd:
sshd
# (Listens on port 8022 by default.)

# Find the phone's IP on your LAN (assuming you've turned on WiFi
# hotspot / connected to the same LAN as the spider host):
ifconfig | grep inet | grep -v 127.0.0.1
# Note the IP, e.g. 192.168.43.123
```

To keep sshd running after reboot, install `termux-boot` from F-Droid
and create `~/.termux/boot/start-sshd` with:

```bash
#!/data/data/com.termux/files/usr/bin/sh
sshd
```
(`chmod +x` it.)

### On the spider host

```bash
# Open a persistent SOCKS5 tunnel through the phone:
ssh -p 8022 -fNg -D 0.0.0.0:1080 u0_a123@192.168.43.123

# Replace u0_a123 with `whoami` output from Termux.
# -f = background, -N = no remote command, -g = allow other hosts to
# connect to the forwarded port, -D 0.0.0.0:1080 = SOCKS5 on local 1080.
```

Then in the spider's `.env`:

```
SPIDER_PROXY_URL=socks5://127.0.0.1:1080
```

Restart the spider. Verify with:

```bash
curl --socks5 127.0.0.1:1080 -fsS https://ifconfig.me
# Should print your phone's mobile carrier IP, not your home WAN IP.
```

### Caveats

- Phone must be unlocked and connected to mobile data (turn off WiFi
  or set carrier as preferred for that app via Android settings ->
  Network -> Mobile network -> Allowed networks for data).
- Termux + sshd uses ~30-50 MB RAM, ~negligible CPU.
- Battery drain is the limiting factor; keep the phone on a charger.

---

## Path B — Tailscale exit node

Tailscale lets you route the spider's traffic through the phone as if
the phone were a VPN gateway, with auto-routing on the mesh.

### On the phone

1. Install Tailscale (Android Play Store / F-Droid).
2. Log in with the same account / tailnet as the spider host.
3. Settings -> "Use as exit node" -> enable.
4. In the Tailscale admin console, approve the phone's exit-node
   advertisement.

### On the spider host

There are two ways to wire this up. The simpler one is to set the
phone as the exit node for the entire spider host:

```bash
sudo tailscale up --exit-node=phone-friendly-name
```

But that routes everything through the phone, not just the spider.
If you want only the spider to egress via the phone, run a per-
container SOCKS proxy:

```bash
# Install a tiny SOCKS proxy that lives on the spider host and
# routes only its connections through the tailscale exit node:
docker run -d --name socks-via-phone \
  --net=host \
  serjs/go-socks5-proxy
# Then `SPIDER_PROXY_URL=socks5://127.0.0.1:1080` and set the route
# at the OS level so connections from socks-via-phone egress through
# tailscale's exit-node interface.
```

### Caveats

- iOS Tailscale exit-node support is limited; works best with
  Android.
- Requires a Tailscale subscription only if you go over the free
  tier's device count.

---

## Path C — WiFi hotspot + Linux box as proxy

If the phone runs hot in Termux or you want a more "set and forget"
setup, dedicate a small Linux box (Raspberry Pi, old laptop, mini
PC) to running the proxy. The phone just provides internet to that
box via WiFi tethering.

### On the phone

Enable WiFi hotspot. Set a password. Note the SSID.

### On the small box

```bash
# Connect to the phone's hotspot.
# Then install something like tinyproxy:
sudo apt install tinyproxy
sudo systemctl enable --now tinyproxy

# Edit /etc/tinyproxy/tinyproxy.conf:
#   Port 8888
#   Allow <spider-host's LAN IP>
sudo systemctl restart tinyproxy
```

### On the spider host

```
SPIDER_PROXY_URL=http://<pi-lan-ip>:8888
```

### Caveats

- Phone has to stay near the box (within WiFi hotspot range).
- The box's own outbound goes through the phone; if you want the
  box's regular updates to use your home network, configure routes
  carefully.

---

## Verification

The spider logs `proxy=true` on `browser.opened` when it launches
CloakBrowser through a proxy. For HTTP fetchers there's no per-call
log, but you can confirm with:

```bash
# From any host that can reach the spider:
curl -fsS -X POST http://localhost:11700/fetch/sync \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://ifconfig.me", "use_browser": true}'
```

The `text` field in the response should be your phone's carrier IP.

For a per-call override (useful for testing one URL against the
proxy without changing the env):

```bash
curl -fsS -X POST http://localhost:11700/fetch/sync \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://github.com/zakirullin/files.md",
    "proxy": "socks5://127.0.0.1:1080"
  }'
```

(GitHub URLs use the HTTP fetcher, so per-call `proxy` works.
For the browser path, the env-level `SPIDER_PROXY_URL` applies.)

## When NOT to use the proxy

- GitHub / HN / Reddit fetchers hit official APIs. Those APIs don't
  care about IP (within rate limits). Routing API calls through a
  metered mobile connection wastes bandwidth.
- Internal-network URLs (the SSRF gate already blocks loopback /
  private IPs, but if you ever loosen the gate, the proxy would
  break those calls).

The intended target is the browser-rendered path on sites with
aggressive bot detection. Keep the proxy enabled for that, and
consider `proxy=""` (empty string) as a per-call override to
forcibly bypass it for known-friendly hosts.
