"""Local ingest — directories (with ignores), single files, file://, html, json."""
from docket import ingest as ing


def test_ingest_local_dir_honors_ignores(tmp_path):
    (tmp_path / "a.md").write_text("# A\n\nalpha content")
    (tmp_path / "skip.md").write_text("# S\n\nskip content")
    (tmp_path / ".docketignore").write_text("skip.md\n")
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "x.js").write_text("junk")   # pruned by baked-in defaults
    labels = sorted({c[0] for c in ing.ingest(str(tmp_path))})
    assert labels == ["a.md"]


def test_ingest_single_file_and_file_uri(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("# T\n\nbody here")
    assert any("T" in h for _, h, _ in ing.ingest(str(p)))
    assert any("T" in h for _, h, _ in ing.ingest("file://" + str(p)))


def test_ingest_html_converts_and_strips_boilerplate(tmp_path):
    p = tmp_path / "page.html"
    p.write_text("<html><body><nav>NAVJUNK</nav><h1>Head</h1><p>para body</p></body></html>")
    chunks = ing.ingest(str(p))
    assert any("Head" in h for _, h, _ in chunks)              # heading preserved
    assert "NAVJUNK" not in " ".join(b for _, _, b in chunks)  # nav stripped


def test_ingest_json_single_chunk(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"key": "value"}')
    chunks = ing.ingest(str(p))
    assert len(chunks) == 1 and "value" in chunks[0][2]
