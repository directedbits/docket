"""Pure-function tests — no DB, no network."""
from docket import config, db
from docket import ingest as ing


def test_fts_query_quotes_tokens():
    assert db._fts_query("useState() hook") == '"useState()" "hook"'
    assert db._fts_query("   ") == ""
    assert db._fts_query('a"b') == '"a""b"'  # internal quotes doubled


def test_sv_filter():
    assert db._sv_filter(None, None) == ([], [])
    assert db._sv_filter("react", "19") == (["source = ?", "version = ?"], ["react", "19"])
    assert db._sv_filter("react", "*") == (["source = ?"], ["react"])  # '*' = no version constraint
    assert db._sv_filter(None, "latest") == (["version = ?"], ["latest"])


def test_rrf_fuses_by_rank():
    # id 2 is ranked in both lists -> should win the fusion
    fused = db._rrf([[1, 2, 3], [2, 4]], k=3)
    assert fused[0] == 2
    assert set(fused) <= {1, 2, 3, 4}


def test_chunk_markdown_headings_and_breadcrumb():
    chunks = ing._chunk_markdown("# Title\n\nalpha\n\n## Sub\n\nbeta", "u")
    headings = [h for _, h, _ in chunks]
    assert "Title" in headings and "Title > Sub" in headings
    assert ing._chunk_markdown("# HeadingOnly", "u") == []  # no body -> no chunk


def test_config_coercers(monkeypatch):
    monkeypatch.setenv("DK_INT", "7")
    assert config._int("DK_INT", 1) == 7
    monkeypatch.setenv("DK_INT", "nope")
    assert config._int("DK_INT", 1) == 1  # bad value -> default
    assert config._int("DK_UNSET_ZZZ", 3) == 3
    monkeypatch.setenv("DK_B", "yes")
    assert config._bool("DK_B", False) is True
    monkeypatch.setenv("DK_B", "0")
    assert config._bool("DK_B", True) is False
    assert config._str("DK_UNSET_ZZZ", "d") == "d"


def test_strip_file_uri_and_git_detection():
    assert ing._strip_file_uri("file:///etc/hosts") == "/etc/hosts"
    assert ing._strip_file_uri("/plain/path") == "/plain/path"
    assert ing._looks_like_git("https://github.com/u/r.git")
    assert ing._looks_like_git("git@github.com:u/r.git")
    assert not ing._looks_like_git("https://example.com/page")
    assert ing._git_host("git@host.com:path.git") == "host.com"
    assert ing._git_host("https://github.com/u/r.git") == "github.com"


def test_ignore_file_parsing_and_match(tmp_path):
    (tmp_path / ".docketignore").write_text("*.log\nsecrets/\n# a comment\n\n")
    pats = ing._read_ignore_file(str(tmp_path / ".docketignore"))
    assert "*.log" in pats and "secrets" in pats
    assert not any(p.startswith("#") for p in pats)
    assert ing._ignored("app.log", pats)
    assert not ing._ignored("app.py", pats)
