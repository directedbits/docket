# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Central configuration — env vars with defaults (12-factor).

Every scalar tunable lives here so nothing else hardcodes constants; set any of
these in the environment (e.g. via docker-compose) to override. Nothing here
changes the MCP tool contract — config is internal.
"""
import logging
import os

_DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "docs.db")


def _str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v is not None and v.strip() else default


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    try:
        return int(v) if v is not None and v.strip() else default
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ---- Storage ----
DB_PATH = _str("DB_PATH", _DEFAULT_DB)
DB_BUSY_TIMEOUT_MS = _int("DB_BUSY_TIMEOUT_MS", 30000)

# ---- Server ----
# Local-only by design: bind loopback so the endpoint is unreachable off-machine.
# Set HOST=0.0.0.0 explicitly to opt into LAN exposure (and add your own auth/TLS).
HOST = _str("HOST", "127.0.0.1")
PORT = _int("PORT", 8765)
LOG_LEVEL = _str("LOG_LEVEL", "WARNING")
SEED_FILE = _str("SEED_FILE", "/data/sources.yml")  # optional startup seed list; absent → no seeding

# ---- Jobs / indexer ----
JOB_RETENTION_SECS = _int("JOB_RETENTION_SECS", 86400)   # prune terminal jobs older than this
PRUNE_INTERVAL_SECS = _int("PRUNE_INTERVAL_SECS", 3600)  # how often the worker prunes
WORKER_IDLE_WAIT_SECS = _int("WORKER_IDLE_WAIT_SECS", 2)  # sleep when the queue is empty
REINDEX_COOLDOWN_SECS = _int("REINDEX_COOLDOWN_SECS", 3600)  # skip re-index within this window unless force

# ---- Crawl / ingest ----
REQUEST_TIMEOUT_SECS = _int("REQUEST_TIMEOUT_SECS", 30)
USER_AGENT = _str("USER_AGENT", "Mozilla/5.0 (X11; Linux x86_64) docket/0.1")
CHUNK_MAX_CHARS = _int("CHUNK_MAX_CHARS", 4000)
CODE_FILE_CAP = _int("CODE_FILE_CAP", 20000)
MAX_PAGES = _int("MAX_PAGES", 200)                    # cap llms.txt manifest links fetched per index
MAX_TOTAL_BYTES = _int("MAX_TOTAL_BYTES", 50_000_000)  # cap total ingested content per source
MAX_RESPONSE_BYTES = _int("MAX_RESPONSE_BYTES", 10_000_000)  # per-response download cap (DoS guard)
MAX_REDIRECTS = _int("MAX_REDIRECTS", 5)
CLONE_TIMEOUT_SECS = _int("CLONE_TIMEOUT_SECS", 300)  # git clone hard timeout
# SSRF guard. Default false → refuse fetching URLs (and cloning git hosts) that
# resolve to internal addresses (private/loopback/link-local). Set true to skip the
# check — valid when you index from your own LAN/localhost (e.g. a local git server
# behind nginx).
ALLOW_INTERNAL_IPS = _bool("ALLOW_INTERNAL_IPS", False)

# ---- Search ----
DEFAULT_K = _int("DEFAULT_K", 5)
MAX_K = _int("MAX_K", 50)        # hard cap so a caller can't request an unbounded result set
SNIPPET_TOKENS = _int("SNIPPET_TOKENS", 12)

# ---- Versioning ----
DEFAULT_VERSION = _str("DEFAULT_VERSION", "latest")  # sentinel bucket for unversioned content

# ---- Embeddings / vector search (Phase 2) ----
# Off by default → pure keyword (BM25). Turn on + point at a self-hosted Ollama to
# add semantic search, fused with BM25 via RRF. Vectors are strictly additive:
# if Ollama is unreachable or mid-rebuild, search falls back to keyword-only.
EMBED_ENABLED = _bool("EMBED_ENABLED", False)
EMBED_OLLAMA_URL = _str("EMBED_OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = _str("EMBED_MODEL", "nomic-embed-text")
EMBED_TIMEOUT_SECS = _int("EMBED_TIMEOUT_SECS", 60)
VECTOR_CANDIDATES = _int("VECTOR_CANDIDATES", 50)  # per-list candidates before RRF
RRF_K = _int("RRF_K", 60)                          # Reciprocal Rank Fusion constant

# ---- Local files ----
# If set (os.pathsep-separated roots), local file/dir ingest is restricted to under
# these roots. Default empty = no restriction (local-only tool you drive yourself).
LOCAL_ROOT_ALLOWLIST = [p for p in _str("LOCAL_ROOT_ALLOWLIST", "").split(os.pathsep) if p.strip()]
# Per-directory ignore file (gitignore-ish globs) honored during directory ingest.
IGNORE_FILE = _str("IGNORE_FILE", ".docketignore")
# Global default ignore patterns, baked into the image. Override with an env path.
DEFAULT_IGNORE_FILE = _str(
    "DEFAULT_IGNORE_FILE", os.path.join(os.path.dirname(__file__), "default_ignores.txt")
)


def setup_logging():
    """Configure logging once, at process entry (server or CLI), honoring LOG_LEVEL."""
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("docket").setLevel(level)
