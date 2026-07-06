# Docket

A local-first MCP server that caches documentation sites and code repos behind a
search interface — so an agent retrieves only the relevant chunks instead of
dumping whole pages into context. The index lives on your machine; no third-party
service sees your queries.

## Why

Reading docs into context costs tokens linear in page size. Search over a
pre-indexed local corpus costs tokens linear in *what's relevant* — you pay the
indexing cost once (compute, not context tokens) and amortize it over many queries.

## Quick start

Create a `docker-compose.yml`:

```yaml
services:
  mcp:
    image: ghcr.io/directedbits/docket:latest
    ports:
      - "127.0.0.1:8765:8765"   # loopback only
    volumes:
      - data:/data              # persist the index
volumes:
  data:
```

Start the server (pulls the published image; binds `127.0.0.1:8765`):

```sh
docker compose up -d
```

> Building from source instead? Use the dev override:
> `docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d`

Index a source — a docs site, a git repo, or a local path/file:

```sh
docker compose exec mcp python -m docket.cli ingest react https://react.dev/llms-full.txt
docker compose exec mcp python -m docket.cli ingest somerepo https://github.com/user/repo.git
```

…or just call the `index` tool from your agent — it crawls in the background and
the next `search` is a hit.

Point any MCP client at the streamable-HTTP endpoint:

```
http://127.0.0.1:8765/mcp
```

Tools: `search`, `get_section` (retrieve) · `index` (async crawl) · `list_jobs`,
`index_status` (track crawls) · `list_sources`, `delete_source` (manage the cache).

## What it indexes

- **Doc sites** via the ingest ladder: `llms-full.txt` → `llms.txt` manifest →
  single page. **Repos** via shallow clone. **Local files and directories**
  (text-based: markdown, code, config, HTML — HTML is converted to markdown so
  headings survive).
- **Directory ignores:** a baked-in global default list plus an optional
  per-directory **`.docketignore`** (gitignore-style globs).
- **Versioning:** pin docs by version (e.g. `react@18` vs `19`); `latest` is the
  default bucket. Re-indexing refreshes a source in place (no duplicates); a
  cooldown avoids re-crawling on every call.

## Search

BM25 keyword search by default. Optionally add semantic search (below); results
are then BM25 + vector fused via Reciprocal Rank Fusion.

## Semantic search (optional)

Off by default. To enable, point Docket at a self-hosted
[Ollama](https://ollama.com) (same machine or LAN — nothing leaves your
infrastructure):

```
EMBED_ENABLED=true
EMBED_OLLAMA_URL=http://ollama:11434    # or your host/LAN Ollama
EMBED_MODEL=nomic-embed-text            # ollama pull nomic-embed-text
```

Strictly additive and graceful: if Ollama is unreachable or mid-rebuild, search
falls back to keyword-only. The index is stamped with its embedding model — change
`EMBED_MODEL` and it rebuilds in the background. The MCP tool contract never
changes. See the commented `ollama` service and `EMBED_*` block in
`docker-compose.yml`.

## Configuration

All configuration is environment variables (defaults shown). Under Docker, set them
on the `mcp` service's `environment:` in `docker-compose.yml` (a commented list
mirrors these); the source of truth is `docket/config.py`.

**Server**

| Var | Default | Purpose |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address. `0.0.0.0` opts into LAN exposure (then add your own auth/TLS). |
| `PORT` | `8765` | HTTP port. |
| `LOG_LEVEL` | `WARNING` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR`. |
| `SEED_FILE` | `/data/sources.yml` | Optional startup seed list; absent → no seeding. |

**Storage & jobs**

| Var | Default | Purpose |
|---|---|---|
| `DB_PATH` | `/data/docs.db` | SQLite index file. |
| `DB_BUSY_TIMEOUT_MS` | `30000` | SQLite busy timeout. |
| `JOB_RETENTION_SECS` | `86400` | Prune finished jobs older than this. |
| `PRUNE_INTERVAL_SECS` | `3600` | How often the worker prunes. |
| `WORKER_IDLE_WAIT_SECS` | `2` | Worker poll interval when idle. |
| `REINDEX_COOLDOWN_SECS` | `3600` | Skip re-index within this window unless `force`. |

**Crawl & fetch safety**

| Var | Default | Purpose |
|---|---|---|
| `REQUEST_TIMEOUT_SECS` | `30` | Per-request HTTP timeout. |
| `USER_AGENT` | `…docket/0.1` | Fetch User-Agent. |
| `MAX_PAGES` | `200` | Cap on `llms.txt` manifest links fetched per index. |
| `MAX_TOTAL_BYTES` | `50000000` | Cap on total ingested content per source. |
| `MAX_RESPONSE_BYTES` | `10000000` | Per-response download cap (memory/DoS guard). |
| `MAX_REDIRECTS` | `5` | Redirect hop cap. |
| `CLONE_TIMEOUT_SECS` | `300` | `git clone` hard timeout. |
| `ALLOW_INTERNAL_IPS` | `false` | **SSRF guard.** `false` refuses fetching/cloning hosts that resolve to internal addresses (private/loopback/link-local). Set `true` to allow LAN/localhost — e.g. a local git server behind nginx. |

**Chunking & search**

| Var | Default | Purpose |
|---|---|---|
| `CHUNK_MAX_CHARS` | `4000` | Target chunk size (heading-split markdown). |
| `CODE_FILE_CAP` | `20000` | Max chars ingested from a non-markdown file. |
| `DEFAULT_K` | `5` | Default results per search. |
| `MAX_K` | `50` | Hard cap on `k`. |
| `SNIPPET_TOKENS` | `12` | Snippet length for keyword hits. |
| `DEFAULT_VERSION` | `latest` | Bucket for unversioned content. |

**Local files & ignores**

| Var | Default | Purpose |
|---|---|---|
| `LOCAL_ROOT_ALLOWLIST` | *(empty)* | If set (os.pathsep-separated roots), restrict local file/dir ingest to under them. Empty = any local path. |
| `IGNORE_FILE` | `.docketignore` | Per-directory ignore filename (gitignore-style globs). |
| `DEFAULT_IGNORE_FILE` | `docket/default_ignores.txt` | Baked-in global ignore list. |

**Embeddings** (see [Semantic search](#semantic-search-optional))

| Var | Default | Purpose |
|---|---|---|
| `EMBED_ENABLED` | `false` | Turn on vector/hybrid search. |
| `EMBED_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama endpoint. |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model; change → background rebuild. |
| `EMBED_TIMEOUT_SECS` | `60` | Ollama request timeout. |
| `VECTOR_CANDIDATES` | `50` | Per-list candidates before RRF fusion. |
| `RRF_K` | `60` | Reciprocal Rank Fusion constant. |

## Security / network exposure

Docket is a **local-machine tool** and binds **loopback by default**
(`HOST=127.0.0.1`); under Docker the port is published only on `127.0.0.1`. There
is intentionally **no auth or TLS** — nothing off-machine can connect.

**Opt-in escape hatch:** set `HOST=0.0.0.0` for LAN exposure — then it's on you to
add auth/TLS (e.g. behind a reverse proxy).

## License

[Mozilla Public License 2.0](./LICENSE).
