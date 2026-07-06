# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""FastMCP server — the stable tool contract over streamable HTTP.

Everything behind these seven tools (storage layout, ingest strategy, vector
backend) is an internal detail the agent never sees.
"""
import logging
import time
from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import config, db, embed, indexer, seed

log = logging.getLogger(__name__)

# Only pure definitions at import time — bootstrap (DB init, worker thread, seeding)
# happens in main(), so importing this module (tests, tooling) has no side effects.
mcp = FastMCP("docket", host=config.HOST, port=config.PORT)


@mcp.tool()
def search(query: str, k: int = config.DEFAULT_K, source: Optional[str] = None,
           version: str = config.DEFAULT_VERSION) -> dict:
    """Search the local doc/repo cache. Returns up to k cheap snippet hits.

    - version defaults to "latest"; pass a pin like "19", or "*" to search all versions.
    - optional source filter. Each hit carries its source + version.
    status is "hit" if anything matched, else "miss". On a miss, fetch the source
    live to answer now and call index(url) so the next query is a hit.

    Internally: if embeddings are enabled and vectors exist, results are BM25 +
    vector fused via RRF; otherwise (or if Ollama is unreachable) keyword-only.
    This routing is invisible — the tool signature and hit shape never change.
    """
    if embed.enabled() and db.has_vectors():
        try:
            hits = db.hybrid_search(query, embed.embed_query(query), k,
                                    source=source, version=version)
        except Exception as e:  # noqa: BLE001 - Ollama down / mid-rebuild → keyword
            log.warning("hybrid search failed, keyword fallback: %s", e)
            hits = db.search(query, k, source=source, version=version)
    else:
        hits = db.search(query, k, source=source, version=version)
    return {"status": "hit" if hits else "miss", "hits": hits}


@mcp.tool()
def get_section(id: int) -> str:
    """Return the full text of one indexed section, by an id from a search hit."""
    return db.get_section(id) or ""


@mcp.tool()
def index(url: str, source: str = "default", version: str = config.DEFAULT_VERSION,
          force: bool = False) -> dict:
    """Queue a URL or repo for background indexing into (source, version); returns immediately.

    - version defaults to "latest" (the unversioned bucket); pass e.g. "19" to pin.
    - Dedups on (source, version) in-flight: a repeat returns the same job_id with
      status "existing".
    - Cooldown: if (source, version) was indexed within REINDEX_COOLDOWN_SECS, the
      call is skipped with status "fresh" (pass force=true to override).
    Job completion is observed via index_status / list_jobs, not this return value.
    """
    if version == "*":
        return {"status": "error", "error": "version '*' is reserved (wildcard); use a concrete version"}
    if not force:
        row = db.get_source(source, version)
        if row and row["last_indexed"] and (
            time.time() - row["last_indexed"] < config.REINDEX_COOLDOWN_SECS
        ):
            log.info("index skipped (cooldown): source=%s version=%s", source, version)
            return {"status": "fresh", "source": source, "version": version,
                    "last_indexed": row["last_indexed"]}
    job_id, status = db.enqueue_job(url, source, version)
    return {"job_id": job_id, "status": status}


@mcp.tool()
def list_jobs(state: str = "active", limit: int = 20) -> list:
    """List indexing jobs. state: active | all | queued | running | done | error.

    Primary way to discover in-flight work — robust when a job_id has scrolled
    out of context across turns.
    """
    return db.list_jobs(state, limit)


@mcp.tool()
def index_status(job_id: int) -> dict:
    """Look up one indexing job by id."""
    return db.get_job(job_id) or {"job_id": job_id, "state": "unknown"}


@mcp.tool()
def list_sources() -> list:
    """List indexed sources: source, url, last_indexed, chunk_count."""
    return db.list_sources()


@mcp.tool()
def delete_source(source: str, version: Optional[str] = None) -> dict:
    """Delete indexed content (and jobs) from the cache. Cleanup counterpart to index().

    version=None removes just the "latest" bucket; pass a pin like "19" for one
    version, or "*" to remove ALL versions of the source. Returns chunks removed.
    """
    removed = db.delete_source(source, version)
    log.info("deleted source '%s' version=%s (%d chunks)", source, version, removed)
    return {"source": source, "version": version, "deleted_chunks": removed}


def main():
    config.setup_logging()
    db.init_db()
    indexer.start()
    log.info("docket starting on %s:%s", config.HOST, config.PORT)
    seed.seed_from_file(config.SEED_FILE)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
