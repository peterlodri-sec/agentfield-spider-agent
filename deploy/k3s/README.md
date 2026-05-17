# k3s deployment

One Deployment, one pod with two containers:

- **agent** — `python main.py` on `:8005`. AgentField skill surface
  (`fetch`, `feed_add`). Talks to the http container via `localhost:11700`.
- **http** — `python spider_server.py` on `:11700`. Owns the
  CloakBrowser pool, Redis client, dispatcher, and feed poller.

A `Service` exposes both ports (`agent` and `http`) at ClusterIP so
other agents in the fleet can reach `spider-agent.<ns>:8005` for the
AgentField surface and `spider-agent.<ns>:11700` for the rag-stack-
compatible HTTP surface.

## Prerequisites

- A k3s cluster (or any Kubernetes cluster — nothing here is k3s-specific).
- A reachable Redis. The default `ConfigMap` sets
  `REDIS_URL=redis://redis.default.svc.cluster.local:6379/0` — replace
  with your Redis Service name. To bundle redis in the same pod, add a
  third container and set `REDIS_URL=redis://localhost:6379/0`.
- The container image built from this repo and pushed somewhere the
  cluster can pull from. Default in `deployment.yaml`:
  `ghcr.io/peterlodri-sec/agentfield-spider-agent:latest`. Edit the
  `image:` fields if you push elsewhere.

## Build + push

```bash
# Build for the cluster's architecture (k3s on Raspberry Pi -> arm64,
# k3s on a normal Linux box -> amd64). Multi-arch with buildx:
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t ghcr.io/peterlodri-sec/agentfield-spider-agent:latest \
  --push .
```

## Deploy

```bash
# Optionally copy + edit the secret stub (v1 doesn't need it):
cp deploy/k3s/secret.example.yaml deploy/k3s/secret.yaml
# ...edit, then:
kubectl apply -f deploy/k3s/secret.yaml

# Apply the base:
kubectl apply -k deploy/k3s/

# Watch rollout:
kubectl rollout status deployment/spider-agent

# Smoke:
kubectl port-forward svc/spider-agent 11700:11700 &
curl -X POST http://localhost:11700/fetch/sync \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}'
```

The first `/fetch/sync` against a fresh pod triggers the ~200 MB
CloakBrowser Chromium download — expect a ~30s wait. Subsequent calls
in the same pod's lifetime are fast (browser stays warm for
`BROWSER_IDLE_S` seconds).

## Cross-pod-restart Chromium cache (optional)

The default `cloak-cache` volume is `emptyDir`, so the Chromium binary
is re-downloaded on every pod restart. To persist it, swap to a PVC:

```yaml
# deployment.yaml — replace the emptyDir block with:
volumes:
- name: cloak-cache
  persistentVolumeClaim:
    claimName: spider-cloak-cache

# Add the PVC manifest (own file or appended to kustomization.yaml):
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: spider-cloak-cache
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 1Gi
```

You'll need a PV provisioner in your cluster (`local-path` is shipped
with k3s by default).

## Scaling

Don't increase `replicas` past 1 without changing the dispatcher loop
— it BRPOPs on the queue and assumes a single leader. Adding replicas
would double-process some jobs because two pods would race to pop the
same items. A leader-election layer (e.g. `leases.coordination.k8s.io`)
is the natural fix; out of v1 scope.

If you need more crawler throughput, increase `CRAWL_CONCURRENCY` in
the ConfigMap (default 3) and bump the http container's CPU + memory
limits. Each in-flight fetch uses one tab in the CloakBrowser pool, so
also raise `BROWSER_MAX_PAGES` to match.

## Observability

Both containers log to stdout in the canonical
`<action> k1=v1 k2=v2` shape. Wire to your aggregator of choice
(Loki, ELK, Promtail+Grafana, etc.) via the pod label selector
`app=spider-agent`.

For health, use the `/health` endpoint:

```bash
curl http://spider-agent:11700/health
# {"ok": true, "service": "spider-http", "browser": "open|closed|absent",
#  "fetch_timeout_ms": 30000, "queue": N, "feeds": N, "seen": N, "jobs": N}
```
