#!/usr/bin/env bash
# Install or update the local SearXNG (Docker) in ~/searxng, bound to 127.0.0.1:8888.
# Re-running keeps the existing secret key and settings; it pulls the newest image and restarts.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${SEARXNG_HOME:-$HOME/searxng}"
mkdir -p "$DEST/config"
chmod 700 "$DEST"
cp "$HERE/searxng/docker-compose.yml" "$DEST/docker-compose.yml"
if [ ! -f "$DEST/config/settings.yml" ]; then
  KEY="$(openssl rand -hex 32)"
  sed "s/CHANGE_ME_BY_INSTALL/$KEY/" "$HERE/searxng/settings.yml" > "$DEST/config/settings.yml"
  echo "created $DEST/config/settings.yml"
fi
# The container's own user must read it; $DEST (mode 700) keeps other host users out.
chmod 644 "$DEST/config/settings.yml" 2>/dev/null || true   # after the first start the container owns it
cd "$DEST"
docker compose pull -q
docker compose up -d
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:8888/healthz" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "http://127.0.0.1:8888/search?q=arista+eos+bgp+evpn&format=json" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin).get("results",[]); e=set(); [e.update(x.get("engines",[])) for x in r]; print("SearXNG OK:", len(r), "results; engines:", sorted(e))'
docker image inspect --format '{{index .RepoDigests 0}}' docker.io/searxng/searxng:latest
