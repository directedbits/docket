"""Store integration — search, versioning, delete semantics, job queue."""


def _one(source, version, body="alpha beta gamma"):
    return [("u", "Heading", body)]


def test_replace_and_search(store):
    assert store.replace_source("s", _one("s", "latest"), url="u", version="latest") == 1
    hits = store.search("alpha")
    assert hits and hits[0]["source"] == "s" and hits[0]["version"] == "latest"


def test_versions_coexist_and_refresh_is_isolated(store):
    for v in ("latest", "18", "19"):
        store.replace_source("react", _one("react", v), url="u", version=v)
    assert sorted(r["version"] for r in store.list_sources()) == ["18", "19", "latest"]
    store.replace_source("react", _one("react", "18", "hook cleanup v2"), url="u", version="18")
    assert sorted(r["version"] for r in store.list_sources()) == ["18", "19", "latest"]


def test_search_version_filter(store):
    store.replace_source("s", _one("s", "18", "alpha one"), url="u", version="18")
    store.replace_source("s", _one("s", "19", "alpha two"), url="u", version="19")
    assert store.search("alpha", version="18")
    assert store.search("alpha", version="latest") == []       # nothing in latest bucket
    assert len(store.search("alpha", version="*", k=50)) == 2   # all versions


def test_delete_source_semantics(store):
    for v in ("latest", "18", "19"):
        store.replace_source("s", _one("s", v), url="u", version=v)
    store.delete_source("s")               # None -> latest only
    assert sorted(r["version"] for r in store.list_sources()) == ["18", "19"]
    store.delete_source("s", "18")         # a specific pin
    assert sorted(r["version"] for r in store.list_sources()) == ["19"]
    store.replace_source("s", _one("s", "latest"), url="u", version="latest")
    assert store.delete_source("s", "*") >= 1   # all versions
    assert store.list_sources() == []


def test_job_queue_dedup_and_lifecycle(store):
    jid, status = store.enqueue_job("u", "s", "latest")
    assert status == "queued"
    jid2, status2 = store.enqueue_job("u", "s", "latest")
    assert status2 == "existing" and jid2 == jid   # in-flight dedup
    job = store.claim_next_job()
    assert job and job["source"] == "s"
    store.finish_job(job["id"])
    assert store.get_job(job["id"])["state"] == "done"


def test_get_section_roundtrip(store):
    store.replace_source("s", [("u", "H", "the body text")], url="u", version="latest")
    hit = store.search("body")[0]
    assert store.get_section(hit["id"]) == "the body text"
    assert store.get_section(999999) is None
