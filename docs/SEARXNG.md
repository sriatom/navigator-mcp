# SearXNG: free web search for the agents

## What it gives you

SearXNG is an open-source **meta search engine**. You send it a query; it asks several public search
engines (Brave, Google, DuckDuckGo, Bing and others) for you, merges the results and returns them, here as
JSON for the agents. It runs as one Docker container on navhost.

- **No API key and no account:** queries leave from navhost with nothing that identifies you, and there are no
  per-query costs. A paid API (Brave, Tavily, Exa) is only worth it if the free results are not good enough.
- **For NAVIGATOR:** the `research` tool searches through it and can read the top pages. In LOCAL projects it
  refuses queries that name your devices, addresses or projects, and every query is logged in the project.
- **For Hermes:** Hermes ships a SearXNG plugin, so its own `web_search` tool can use it as well
  (`web.search_backend: searxng` and `SEARXNG_URL=http://127.0.0.1:8888`).
- **Limits:** upstream engines sometimes rate-limit or show a "bot check" (Arista's site does). SearXNG then
  uses the other engines. Some vendor pages cannot be read by the page fetch for the same reason.

To read more:
- What SearXNG is, and its features: <https://docs.searxng.org/>
- Who does what (instances, engines, privacy): <https://docs.searxng.org/user/about.html>
- Source and releases: <https://github.com/searxng/searxng>
- The JSON search API used here: <https://docs.searxng.org/dev/search_api.html>

## On navhost

| What | Where |
| :--- | :--- |
| Container | `searxng` (image `searxng/searxng:latest`), restarts by itself (`restart: unless-stopped`) |
| Address | `http://127.0.0.1:8888`: **localhost only**, not reachable from the network |
| Files | `~/searxng/docker-compose.yml`, `~/searxng/config/settings.yml` (holds its own secret key) |
| Source in the repo | `scripts/searxng/`, `scripts/install_searxng.sh` |
| Query log | each project's `RECORDS/PUBLIC_QUERIES.md`; without a project `~/.navigator/PUBLIC_QUERIES.md` |

```bash
docker ps --filter name=searxng                       # running?
docker logs --tail 30 searxng                         # its log
cd ~/searxng && docker compose restart                # restart
~/navigator/scripts/install_searxng.sh                # update to the newest image (keeps the settings)
curl -s 'http://127.0.0.1:8888/search?q=test&format=json' | head -c 300   # quick check
```

To remove it: `cd ~/searxng && docker compose down`, then delete `~/searxng`.

NAVIGATOR settings (`navigator.yml`, optional; env `SEARXNG_URL` overrides `URL`):

```yaml
SEARCH:
  URL: http://127.0.0.1:8888
  MAX_RESULTS: 8
  MAX_FETCH: 3            # pages research may read per query
  MAX_PAGE_CHARS: 6000
```
