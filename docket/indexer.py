# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Background indexing worker.

A single daemon thread drains the jobs table: claim queued job -> run the ingest
ladder -> insert chunks -> optimize -> mark done/error. Periodically prunes old
terminal jobs. Synchronous httpx inside the thread keeps this simple (no async).
"""
import logging
import threading
import time

from . import config
from . import db
from . import embed
from . import ingest as ingest_mod

log = logging.getLogger(__name__)


def _embed_source(source, version):
    """Vector-backfill: embed a (source, version)'s chunks via Ollama and stamp the
    model/dim. Best-effort — any failure (Ollama down, disabled) leaves the source
    keyword-only; BM25 is never blocked on this."""
    if not embed.enabled() or not db.vectors_supported():
        return
    try:
        rows = db.chunks_for_embed(source, version)
        if not rows:
            return
        vectors = embed.embed([body for _, body in rows])
        dim = len(vectors[0])
        db.ensure_vec_table(dim)
        db.set_meta("embed_model", config.EMBED_MODEL)
        db.set_meta("embed_dim", str(dim))
        db.vec_write((rid, v) for (rid, _), v in zip(rows, vectors))
        log.info("embedded %d chunks for %s v=%s", len(rows), source, version)
    except Exception as e:  # noqa: BLE001
        log.warning("embedding failed for %s v=%s (keyword-only): %s", source, version, e)


def reconcile_embeddings():
    """On startup: if EMBED_MODEL changed, drop the incompatible vec index and
    re-embed; if embeddings were just enabled (no vectors yet), embed existing
    sources. Runs in the worker thread — keyword search works throughout."""
    if not db.vectors_supported() or not embed.enabled():
        return
    stamped = db.get_meta("embed_model")
    if stamped == config.EMBED_MODEL and db.has_vectors():
        return  # already current
    if stamped and stamped != config.EMBED_MODEL:
        log.warning("EMBED_MODEL changed %s -> %s: rebuilding vector index",
                    stamped, config.EMBED_MODEL)
        db.drop_vectors()
    for row in db.list_sources():
        _embed_source(row["source"], row["version"])


def _run_job(job):
    log.info("job %s: indexing %s (source=%s version=%s)",
             job["id"], job["url"], job["source"], job["version"])
    try:
        chunks = ingest_mod.ingest(job["url"])
        if chunks:
            db.replace_source(job["source"], chunks, url=job["url"], version=job["version"])
            db.optimize()
            _embed_source(job["source"], job["version"])  # vector-backfill
        log.info("job %s: done, %d chunks for source '%s' version=%s",
                 job["id"], len(chunks), job["source"], job["version"])
        db.finish_job(job["id"])
    except Exception as e:  # noqa: BLE001 - record any failure on the job row
        log.warning("job %s: failed — %s", job["id"], e)
        db.finish_job(job["id"], error=str(e)[:500])


def _loop(stop: threading.Event):
    try:
        reconcile_embeddings()
    except Exception as e:  # noqa: BLE001
        log.warning("embedding reconcile failed: %s", e)
    last_prune = 0.0
    while not stop.is_set():
        try:
            job = db.claim_next_job()
            if job:
                _run_job(job)
                continue
            now = time.time()
            if now - last_prune > config.PRUNE_INTERVAL_SECS:
                db.prune_jobs()
                last_prune = now
                log.debug("pruned terminal jobs older than %ss", config.JOB_RETENTION_SECS)
        except Exception as e:  # noqa: BLE001 - a DB hiccup must not kill the worker
            log.exception("indexer loop error: %s", e)
        stop.wait(config.WORKER_IDLE_WAIT_SECS)


def start() -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=_loop, args=(stop,), daemon=True).start()
    log.info("indexer worker started")
    return stop
