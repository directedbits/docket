# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""SQLite FTS5 store + async-index job queue.

Single file, WAL mode so the background indexer (writer) and the MCP server
(readers) coexist in one process. Connections are opened per-call: SQLite is
cheap to open and per-call connections sidestep cross-thread sharing issues.
"""
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

try:
    import sqlite_vec
    _HAS_SQLITE_VEC = True
except ImportError:  # vector search simply stays off — keyword-only
    sqlite_vec = None
    _HAS_SQLITE_VEC = False

log = logging.getLogger(__name__)
_init_lock = threading.Lock()


def _maybe_load_vec(c):
    """Load the sqlite-vec extension into a connection. On any failure (extension
    missing, or a Python built without loadable-extension support) we flip vector
    search off globally and stay keyword-only."""
    global _HAS_SQLITE_VEC
    if not _HAS_SQLITE_VEC:
        return
    try:
        c.enable_load_extension(True)
        sqlite_vec.load(c)
        c.enable_load_extension(False)
    except Exception as e:  # noqa: BLE001
        _HAS_SQLITE_VEC = False
        log.warning("sqlite-vec unavailable (%s) — vector search disabled", e)


def vectors_supported() -> bool:
    return _HAS_SQLITE_VEC


def _connect():
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(
        config.DB_PATH,
        timeout=config.DB_BUSY_TIMEOUT_MS / 1000,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={config.DB_BUSY_TIMEOUT_MS}")
    _maybe_load_vec(conn)
    return conn


@contextmanager
def conn():
    c = _connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with _init_lock, conn() as c:
        _migrate_for_versioning(c)
        # docs columns: source, version, url, heading_path, body  (body = column index 4)
        c.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5("
            "source, version, url, heading_path, body, tokenize='porter')"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS jobs("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "url TEXT NOT NULL, source TEXT NOT NULL,"
            "version TEXT NOT NULL DEFAULT 'latest',"
            "state TEXT NOT NULL DEFAULT 'queued',"  # queued | running | done | error
            "error TEXT, created REAL, updated REAL)"
        )
        # jobs table predating versioning: add the column in place (regular table)
        if "version" not in _table_cols(c, "jobs"):
            c.execute("ALTER TABLE jobs ADD COLUMN version TEXT NOT NULL DEFAULT 'latest'")
        c.execute("CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state)")
        # catalog of indexed (source, version) pairs — backs list_sources / cooldown.
        c.execute(
            "CREATE TABLE IF NOT EXISTS sources("
            "source TEXT, version TEXT, url TEXT, last_indexed REAL, chunk_count INTEGER,"
            "PRIMARY KEY(source, version))"
        )
        # key/value metadata — stamps the embedding model + dim behind the vec index.
        c.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")


def _table_cols(c, table):
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]


def _migrate_for_versioning(c):
    """Pre-versioning docs/sources were keyed by source only, with no `version`
    column. FTS5 can't ALTER ADD COLUMN, so rebuild: drop the index + catalog (the
    cache is reconstructable by re-indexing). One-time, on first run after upgrade.
    """
    has_docs = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='docs'"
    ).fetchone()
    if has_docs and "version" not in _table_cols(c, "docs"):
        log.warning(
            "upgrading schema for versioning — existing index dropped; re-index your sources"
        )
        c.execute("DROP TABLE docs")
        c.execute("DROP TABLE IF EXISTS sources")


# ---------- search / sections ----------

def _fts_query(q: str) -> str:
    """Quote each whitespace token as an FTS5 phrase.

    Makes the query robust to punctuation and stray operators (e.g. `useState()`),
    which would otherwise raise an FTS5 syntax error. Tokens are implicitly ANDed.
    """
    toks = [t for t in q.split() if t.strip()]
    return " ".join('"' + t.replace('"', '""') + '"' for t in toks)


def _sv_filter(source, version):
    """SQL WHERE fragments + args for optional source/version filtering.
    A falsy or '*' version means "no version constraint"."""
    clauses, args = [], []
    if source:
        clauses.append("source = ?")
        args.append(source)
    if version and version != "*":
        clauses.append("version = ?")
        args.append(version)
    return clauses, args


def search(query: str, k: int = None, source: str = None, version: str = None):
    """Full-text search. Filters by `version` (default config.DEFAULT_VERSION,
    i.e. 'latest'); pass version='*' for all versions, or a pin like '19'. Optional
    `source` filter. Each hit carries its source + version.
    """
    fts = _fts_query(query)
    if not fts:
        return []
    if k is None:
        k = config.DEFAULT_K
    k = max(1, min(int(k), config.MAX_K))
    if version is None:
        version = config.DEFAULT_VERSION
    clauses, sv_args = _sv_filter(source, version)
    where = " AND ".join(["docs MATCH ?"] + clauses)
    args = [fts] + sv_args + [k]
    sql = (
        "SELECT rowid AS id, source, version, url, heading_path, "
        f"snippet(docs, 4, '[', ']', '…', {config.SNIPPET_TOKENS}) AS snippet "  # 4 = body column
        f"FROM docs WHERE {where} ORDER BY rank LIMIT ?"
    )
    with conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_section(section_id: int):
    with conn() as c:
        row = c.execute("SELECT body FROM docs WHERE rowid=?", (section_id,)).fetchone()
    return row["body"] if row else None


def replace_source(source: str, chunks, url: str = None, version: str = None):
    """Atomically rebuild a (source, version): delete its existing chunks, insert
    the new set, and upsert its `sources` catalog row.

    Scoped by (source, version) so refreshing one version never touches another
    (e.g. react '18' vs '19'). version defaults to config.DEFAULT_VERSION
    ('latest'). Re-indexing repopulates a whole (source, version) in one call, so
    we replace rather than append. The caller guards against empty `chunks`, so a
    failed/empty crawl never wipes a good index (and the catalog row keeps its
    prior timestamp). Delete + insert + catalog upsert share one transaction.
    """
    if version is None:
        version = config.DEFAULT_VERSION
    rows = [(source, version, u, h, b) for (u, h, b) in chunks]
    if not rows:
        return 0
    now = time.time()
    with conn() as c:
        old = [r[0] for r in c.execute(
            "SELECT rowid FROM docs WHERE source=? AND version=?", (source, version))]
        c.execute("DELETE FROM docs WHERE source=? AND version=?", (source, version))
        _delete_vec_rows(c, old)  # new chunks get fresh rowids → re-embedded by the indexer
        c.executemany(
            "INSERT INTO docs(source, version, url, heading_path, body) VALUES (?,?,?,?,?)",
            rows,
        )
        c.execute(
            "INSERT INTO sources(source, version, url, last_indexed, chunk_count) "
            "VALUES (?,?,?,?,?) ON CONFLICT(source, version) DO UPDATE SET "
            "url=excluded.url, last_indexed=excluded.last_indexed, "
            "chunk_count=excluded.chunk_count",
            (source, version, url, now, len(rows)),
        )
    return len(rows)


def list_sources():
    """Read the sources catalog (one row per source+version)."""
    with conn() as c:
        rows = c.execute(
            "SELECT source, version, url, last_indexed, chunk_count "
            "FROM sources ORDER BY source, version"
        ).fetchall()
    return [dict(r) for r in rows]


def get_source(source: str, version: str = None):
    """One catalog row for (source, version), or None. Backs the cooldown check."""
    if version is None:
        version = config.DEFAULT_VERSION
    with conn() as c:
        row = c.execute(
            "SELECT source, version, url, last_indexed, chunk_count "
            "FROM sources WHERE source=? AND version=?",
            (source, version),
        ).fetchone()
    return dict(row) if row else None


def optimize():
    with conn() as c:
        c.execute("INSERT INTO docs(docs) VALUES('optimize')")


def delete_source(source: str, version: str = None):
    """Cleanup: remove chunks, job rows, and catalog rows. Returns chunks removed.

    version=None → the DEFAULT_VERSION ('latest') bucket only; version='*' → ALL
    versions; else that specific (source, version). Consistent with the rest of the
    API where None means 'latest'. A job mid-run could re-create chunks on
    completion via replace_source — acceptable for a manual cleanup. One transaction.
    """
    if version is None:
        version = config.DEFAULT_VERSION
    with conn() as c:
        if version == "*":
            old = [r[0] for r in c.execute("SELECT rowid FROM docs WHERE source=?", (source,))]
            n = len(old)
            c.execute("DELETE FROM docs WHERE source=?", (source,))
            c.execute("DELETE FROM jobs WHERE source=?", (source,))
            c.execute("DELETE FROM sources WHERE source=?", (source,))
        else:
            old = [r[0] for r in c.execute(
                "SELECT rowid FROM docs WHERE source=? AND version=?", (source, version))]
            n = len(old)
            c.execute("DELETE FROM docs WHERE source=? AND version=?", (source, version))
            c.execute("DELETE FROM jobs WHERE source=? AND version=?", (source, version))
            c.execute("DELETE FROM sources WHERE source=? AND version=?", (source, version))
        _delete_vec_rows(c, old)
        if n:
            c.execute("INSERT INTO docs(docs) VALUES('optimize')")
    return n


# ---------- vectors (sqlite-vec) ----------

def get_meta(key: str):
    with conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(key: str, value):
    with conn() as c:
        c.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def _vec_table_exists(c) -> bool:
    return bool(c.execute(
        "SELECT 1 FROM sqlite_master WHERE name='vec_docs'").fetchone())


def _delete_vec_rows(c, rowids):
    """Best-effort delete of vec rows by docs rowid (no-op if no vec table)."""
    if not rowids or not _HAS_SQLITE_VEC or not _vec_table_exists(c):
        return
    ph = ",".join("?" * len(rowids))
    c.execute(f"DELETE FROM vec_docs WHERE rowid IN ({ph})", list(rowids))


def ensure_vec_table(dim: int):
    with conn() as c:
        c.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_docs USING vec0(embedding float[{int(dim)}])"
        )


def drop_vectors():
    with conn() as c:
        c.execute("DROP TABLE IF EXISTS vec_docs")


def has_vectors() -> bool:
    if not _HAS_SQLITE_VEC:
        return False
    with conn() as c:
        if not _vec_table_exists(c):
            return False
        return c.execute("SELECT count(*) FROM vec_docs").fetchone()[0] > 0


def chunks_for_embed(source: str, version: str):
    with conn() as c:
        rows = c.execute(
            "SELECT rowid, body FROM docs WHERE source=? AND version=?", (source, version)
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def vec_write(rows):
    """rows: iterable of (rowid, vector). INSERT OR REPLACE so re-embeds are idempotent."""
    data = [(rid, sqlite_vec.serialize_float32(list(v))) for rid, v in rows]
    if not data:
        return 0
    with conn() as c:
        c.executemany("INSERT OR REPLACE INTO vec_docs(rowid, embedding) VALUES (?, ?)", data)
    return len(data)


def vec_search(query_vec, k: int):
    """KNN over the vec table → [(rowid, distance)] (no source/version filter)."""
    blob = sqlite_vec.serialize_float32(list(query_vec))
    with conn() as c:
        rows = c.execute(
            "SELECT rowid, distance FROM vec_docs WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT ?",
            (blob, int(k)),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _bm25_ids(query, n, source, version):
    fts = _fts_query(query)
    if not fts:
        return []
    clauses, sv_args = _sv_filter(source, version)
    where = " AND ".join(["docs MATCH ?"] + clauses)
    args = [fts] + sv_args + [int(n)]
    with conn() as c:
        rows = c.execute(
            f"SELECT rowid FROM docs WHERE {where} ORDER BY rank LIMIT ?", args
        ).fetchall()
    return [r[0] for r in rows]


def _vec_ids(query_vec, n, source, version):
    hits = vec_search(query_vec, n)
    if not hits:
        return []
    ids = [rid for rid, _ in hits]
    ph = ",".join("?" * len(ids))
    clauses, sv_args = _sv_filter(source, version)
    where = " AND ".join([f"rowid IN ({ph})"] + clauses)
    args = list(ids) + sv_args
    with conn() as c:
        allowed = {r[0] for r in c.execute(f"SELECT rowid FROM docs WHERE {where}", args)}
    return [rid for rid in ids if rid in allowed]  # keep vector rank order


def _rrf(lists, k):
    """Reciprocal Rank Fusion: combine ranked id lists by rank, not score."""
    cst = config.RRF_K
    scores = {}
    for lst in lists:
        for rank, rid in enumerate(lst, 1):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (cst + rank)
    return sorted(scores, key=scores.get, reverse=True)[:k]


def _fetch_display(ids):
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    with conn() as c:
        rows = {r["id"]: dict(r) for r in c.execute(
            f"SELECT rowid AS id, source, version, url, heading_path, "
            f"substr(body, 1, 240) AS snippet FROM docs WHERE rowid IN ({ph})", list(ids))}
    return [rows[i] for i in ids if i in rows]


def hybrid_search(query, query_vec, k=None, source=None, version=None):
    """BM25 + vector candidates fused via RRF. Same hit shape as search() — the
    snippet is a plain excerpt here (no FTS highlight for vector-only hits)."""
    if k is None:
        k = config.DEFAULT_K
    k = max(1, min(int(k), config.MAX_K))
    if version is None:
        version = config.DEFAULT_VERSION
    n = config.VECTOR_CANDIDATES
    bm = _bm25_ids(query, n, source, version)
    vec = _vec_ids(query_vec, n, source, version)
    return _fetch_display(_rrf([bm, vec], k))


# ---------- job queue ----------

def enqueue_job(url: str, source: str, version: str = None):
    """Dedup on (source, version) in-flight: return existing job, else create one."""
    if version is None:
        version = config.DEFAULT_VERSION
    now = time.time()
    with conn() as c:
        existing = c.execute(
            "SELECT id FROM jobs WHERE source=? AND version=? "
            "AND state IN ('queued','running') LIMIT 1",
            (source, version),
        ).fetchone()
        if existing:
            return existing["id"], "existing"
        cur = c.execute(
            "INSERT INTO jobs(url, source, version, state, created, updated) "
            "VALUES (?,?,?, 'queued', ?, ?)",
            (url, source, version, now, now),
        )
        return cur.lastrowid, "queued"


def claim_next_job():
    """Atomically take the oldest queued job and mark it running. Single worker."""
    with conn() as c:
        row = c.execute(
            "SELECT id, url, source, version FROM jobs WHERE state='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        c.execute(
            "UPDATE jobs SET state='running', updated=? WHERE id=?",
            (time.time(), row["id"]),
        )
        return dict(row)


def finish_job(job_id: int, error: str = None):
    with conn() as c:
        c.execute(
            "UPDATE jobs SET state=?, error=?, updated=? WHERE id=?",
            ("error" if error else "done", error, time.time(), job_id),
        )


def list_jobs(state: str = "active", limit: int = 20):
    cols = "id AS job_id, url, source, version, state, error"
    with conn() as c:
        if state == "active":
            rows = c.execute(
                f"SELECT {cols} FROM jobs WHERE state IN ('queued','running') "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        elif state == "all":
            rows = c.execute(
                f"SELECT {cols} FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = c.execute(
                f"SELECT {cols} FROM jobs WHERE state=? ORDER BY id DESC LIMIT ?",
                (state, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def get_job(job_id: int):
    with conn() as c:
        row = c.execute(
            "SELECT id AS job_id, url, source, version, state, error FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    return dict(row) if row else None


def prune_jobs(max_age: float = None):
    """Drop terminal (done/error) jobs older than max_age. Active jobs untouched."""
    if max_age is None:
        max_age = config.JOB_RETENTION_SECS
    cutoff = time.time() - max_age
    with conn() as c:
        c.execute(
            "DELETE FROM jobs WHERE state IN ('done','error') AND updated < ?",
            (cutoff,),
        )
