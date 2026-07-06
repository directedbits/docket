"""Startup seeding + the MCP tool wrappers (keyword-only path; no Ollama)."""
from docket import seed


def test_seed_missing_file_is_noop(store, tmp_path):
    seed.seed_from_file(str(tmp_path / "nope.yml"))
    assert store.list_jobs("all") == []


def test_seed_enqueues_and_skips_present(store, tmp_path):
    y = tmp_path / "s.yml"
    y.write_text("sources:\n  - source: react\n    url: http://x/llms-full.txt\n    version: '19'\n")
    seed.seed_from_file(str(y))
    jobs = store.list_jobs("all")
    assert any(j["source"] == "react" and j["version"] == "19" for j in jobs)
    # already-present source is skipped on a second pass (no duplicate job beyond dedup)
    seed.seed_from_file(str(y))
    assert len([j for j in store.list_jobs("all") if j["source"] == "react"]) == 1


def test_seed_malformed_yaml_is_safe(store, tmp_path):
    (tmp_path / "bad.yml").write_text("this: [is: not valid\n")
    seed.seed_from_file(str(tmp_path / "bad.yml"))  # must not raise


def test_search_tool_hit_and_miss(store):
    import docket.server as srv
    store.replace_source("s", [("u", "H", "alpha content")], url="u", version="latest")
    assert srv.search("alpha")["status"] == "hit"
    assert srv.search("zzznotpresent")["status"] == "miss"


def test_index_tool_cooldown_and_wildcard_reject(store):
    import docket.server as srv
    assert srv.index("http://x/y", "s", "latest")["status"] in ("queued", "existing")
    assert srv.index("http://x/y", "s", "*")["status"] == "error"  # reserved wildcard


def test_delete_tool(store):
    import docket.server as srv
    store.replace_source("s", [("u", "H", "body")], url="u", version="latest")
    assert srv.delete_source("s")["deleted_chunks"] >= 1
