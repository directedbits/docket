# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Content acquisition + chunking.

Ingest ladder for URLs:
  1. llms-full.txt  — entire docs as one clean markdown file (direct)
  2. llms.txt       — a manifest of links (fetch each)
  3. fallback       — fetch the single page, strip boilerplate
Repos: shallow clone (for .git URLs) or walk a local path.

Returns a list of (url, heading_path, body) chunks.
"""
import fnmatch
import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as _md

from . import config

log = logging.getLogger(__name__)

DOC_EXTS = {".md", ".mdx", ".rst"}
HTML_EXTS = {".html", ".htm"}
CONFIG_EXTS = {".yaml", ".yml", ".toml", ".json", ".ini", ".cfg"}
CODE_EXTS = ({".md", ".mdx", ".rst", ".txt", ".py", ".js", ".ts", ".tsx",
              ".go", ".rs", ".java", ".rb", ".c", ".h", ".cpp"}
             | HTML_EXTS | CONFIG_EXTS)


def ingest(target: str):
    return list(_cap(_ingest_raw(target)))


def _ingest_raw(target: str):
    target = _strip_file_uri(target)
    # Local existence + allowlist FIRST, so a local path ending in .git can't skip
    # the allowlist by falling into the git branch.
    if os.path.isdir(target):
        _check_local_allowed(target)
        yield from _ingest_repo(target)
    elif os.path.isfile(target):
        _check_local_allowed(target)
        log.info("ingest local file %s", target)
        yield from _ingest_one_file(target, os.path.basename(target))
    elif _looks_like_git(target):
        yield from _ingest_git(target)
    else:
        yield from _ingest_url(target)


def _looks_like_git(target: str) -> bool:
    if target.startswith("git@") or target.startswith("ssh://"):
        return True
    return urlparse(target).scheme in ("http", "https", "git") and target.endswith(".git")


def _git_host(target: str):
    """Host of a git target — scp-like (git@host:path) or a URL."""
    if target.startswith("git@") or ("@" in target and "://" not in target and ":" in target):
        return target.split("@", 1)[1].split(":", 1)[0]
    return urlparse(target).hostname


def _ingest_git(target: str):
    """Shallow-clone a git target and ingest it. Hardened: reject option-like
    targets, block dangerous transports (ext::/file::), hard timeout, and always
    clean up the temp clone. `target` may be attacker-influenced via tool args."""
    if target.startswith("-"):
        raise ValueError(f"refusing option-like git target: {target!r}")
    if not config.ALLOW_INTERNAL_IPS:
        host = _git_host(target)
        if host:
            _guard_host(host)  # SSRF: don't clone from internal hosts
    tmp = tempfile.mkdtemp(prefix="docket-")
    try:
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--", target, tmp],
                check=True, capture_output=True, text=True,
                timeout=config.CLONE_TIMEOUT_SECS,
                env={**os.environ,
                     "GIT_ALLOW_PROTOCOL": "http:https:git:ssh",  # blocks ext::/file::
                     "GIT_TERMINAL_PROMPT": "0"},                 # never hang on credentials
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"git clone failed: {(e.stderr or '').strip()[:300]}") from e
        yield from _ingest_repo(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _strip_file_uri(target: str) -> str:
    if target.startswith("file://"):
        return unquote(urlparse(target).path)
    return target


def _check_local_allowed(path: str):
    """If LOCAL_ROOT_ALLOWLIST is set, local file/dir ingest is restricted to under
    those roots (blocks an injected "index ~/.ssh"). Default empty = no restriction
    — it's a local-only tool you drive yourself.
    """
    roots = config.LOCAL_ROOT_ALLOWLIST
    if not roots:
        return
    rp = os.path.realpath(path)
    for root in roots:
        rr = os.path.realpath(root)
        if rp == rr or rp.startswith(rr + os.sep):
            return
    raise PermissionError(f"local path not under LOCAL_ROOT_ALLOWLIST: {path}")


def _cap(chunks):
    """Guardrail: stop once total ingested body bytes exceed MAX_TOTAL_BYTES.

    Applies uniformly across every ingest path (llms-full, manifest, page, repo)
    so a giant source can't blow up storage. MAX_PAGES caps manifest fetches
    separately, in _ingest_url.
    """
    total = 0
    for url, heading, body in chunks:
        total += len(body.encode("utf-8", "ignore"))
        if total > config.MAX_TOTAL_BYTES:
            log.warning("MAX_TOTAL_BYTES (%d) reached — ingest truncated", config.MAX_TOTAL_BYTES)
            break
        yield (url, heading, body)


# ---------- URLs ----------

def _base(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


class _Resp:
    """Minimal fetch result — just the fields callers use (.text, .headers)."""
    __slots__ = ("text", "headers")

    def __init__(self, text, headers):
        self.text = text
        self.headers = headers


def _guard_host(host: str, port: int = 80):
    """Reject a host that resolves to any internal address. Unwraps IPv4-mapped
    IPv6 (::ffff:a.b.c.d) so it can't sneak past the classifier.

    Caveat: this validates at resolve time; a DNS-rebinding attacker with a
    short-TTL record could still differ at httpx connect time. Acceptable residual
    for a local, owner-driven tool (set ALLOW_INTERNAL_IPS to skip entirely)."""
    try:
        infos = socket.getaddrinfo(host, port)
    except OSError as e:
        raise ValueError(f"DNS resolution failed for {host}: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise ValueError(f"blocked internal address {ip} for host {host}")


def _guard_public(url: str):
    """SSRF guard for http(s) URLs. Bypassed when ALLOW_INTERNAL_IPS is set."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError(f"blocked non-http(s) URL: {url}")
    if not p.hostname:
        raise ValueError(f"URL has no host: {url}")
    _guard_host(p.hostname, p.port or (443 if p.scheme == "https" else 80))


def _fetch(url: str, client: httpx.Client, _depth: int = 0) -> _Resp:
    """Fetch a URL with SSRF checks on every hop, a redirect cap, and a streamed
    size cap (MAX_RESPONSE_BYTES) so a huge/redirected body can't exhaust memory."""
    if _depth > config.MAX_REDIRECTS:
        raise ValueError(f"too many redirects: {url}")
    if not config.ALLOW_INTERNAL_IPS:
        _guard_public(url)
    with client.stream(
        "GET", url, follow_redirects=False,
        timeout=config.REQUEST_TIMEOUT_SECS,
        headers={"User-Agent": config.USER_AGENT},
    ) as r:
        if r.is_redirect:
            loc = r.headers.get("location")
            if not loc:
                r.raise_for_status()
            return _fetch(urljoin(url, loc), client, _depth + 1)
        r.raise_for_status()
        total = 0
        parts = []
        for chunk in r.iter_bytes():
            total += len(chunk)
            if total > config.MAX_RESPONSE_BYTES:
                raise ValueError(
                    f"response exceeds MAX_RESPONSE_BYTES ({config.MAX_RESPONSE_BYTES}): {url}")
            parts.append(chunk)
        text = b"".join(parts).decode(r.encoding or "utf-8", "replace")
        return _Resp(text, dict(r.headers))


def _html_to_md(html: str) -> str:
    """Strip boilerplate, then convert to markdown preserving <h1..h3> as `#`
    headings (so the chunker can split on them). Used for fetched and on-disk HTML.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    return _md(str(soup), heading_style="ATX")


def _to_markdownish(r: httpx.Response) -> str:
    if "html" in r.headers.get("content-type", ""):
        return _html_to_md(r.text)
    return r.text


def _try_llms_full(url: str, client: httpx.Client):
    candidates = []
    if url.rstrip("/").endswith(".txt"):
        candidates.append(url)  # url itself is an llms*.txt
    candidates.append(_base(url) + "/llms-full.txt")
    for cand in candidates:
        try:
            r = _fetch(cand, client)
        except Exception:
            continue
        body = r.text.strip()
        ctype = r.headers.get("content-type", "")
        # Reject SPA catch-alls: many sites return index.html (200) for unknown
        # paths. Require a non-HTML body so we don't ingest the app shell as docs.
        if body and "html" not in ctype and not body.lstrip().startswith("<"):
            return r.text, cand
    return None


def _try_llms_manifest(url: str, client: httpx.Client):
    try:
        r = _fetch(_base(url) + "/llms.txt", client)
    except Exception:
        return None
    links = re.findall(r"\]\((https?://[^)]+)\)", r.text)
    rel = re.findall(r"\]\((/[^)]+)\)", r.text)
    links += [_base(url) + x for x in rel]
    return list(dict.fromkeys(links)) or None


def _ingest_url(url: str):
    with httpx.Client() as client:
        full = _try_llms_full(url, client)
        if full:
            log.info("ingest %s via llms-full.txt (%s)", url, full[1])
            yield from _chunk_markdown(full[0], full[1])
            return
        manifest = _try_llms_manifest(url, client)
        if manifest:
            log.info(
                "ingest %s via llms.txt manifest (%d links, cap %d)",
                url, len(manifest), config.MAX_PAGES,
            )
            for link in manifest[:config.MAX_PAGES]:
                try:
                    yield from _chunk_markdown(_to_markdownish(_fetch(link, client)), link)
                except Exception as e:
                    log.warning("manifest link failed %s: %s", link, e)
                    continue
            return
        # fallback: the single page
        log.info("ingest %s via single-page fallback", url)
        yield from _chunk_markdown(_to_markdownish(_fetch(url, client)), url)


# ---------- repos ----------

def _read_ignore_file(path: str):
    """Parse an ignore file into a set of basename globs (`#` comments and blank
    lines skipped). Missing file → empty set."""
    patterns = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.add(line.rstrip("/"))
    except OSError:
        pass
    return patterns


def _load_ignores(root: str):
    """Global baked-in defaults (config.DEFAULT_IGNORE_FILE) unioned with the
    per-directory ignore file (config.IGNORE_FILE at the target root). Matched by
    basename via fnmatch — simple, gitignore-ish.
    """
    return _read_ignore_file(config.DEFAULT_IGNORE_FILE) | _read_ignore_file(
        os.path.join(root, config.IGNORE_FILE)
    )


def _ignored(name: str, patterns) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _ingest_repo(path: str):
    patterns = _load_ignores(path)
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not _ignored(d, patterns)]  # prune, don't descend
        for f in files:
            if _ignored(f, patterns):
                continue
            if os.path.splitext(f)[1].lower() not in CODE_EXTS:
                continue
            fp = os.path.join(root, f)
            yield from _ingest_one_file(fp, os.path.relpath(fp, path))


def _ingest_one_file(path: str, label: str):
    """Route one file by extension: html → markdown (markdownify), markdown/rst →
    chunked, everything else → a single size-capped text chunk.
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except Exception:
        return
    if ext in HTML_EXTS:
        yield from _chunk_markdown(_html_to_md(text), label)
    elif ext in DOC_EXTS:
        yield from _chunk_markdown(text, label)
    else:
        yield (label, label, text[:config.CODE_FILE_CAP])


# ---------- chunking ----------

def _chunk_markdown(text: str, url: str, max_chars: int = None):
    if max_chars is None:
        max_chars = config.CHUNK_MAX_CHARS
    chunks = []
    stack = []          # list of (level, title) building the breadcrumb
    cur_heading = ""
    buf = []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            chunks.append((url, cur_heading or "(top)", body))

    for line in text.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            flush()
            buf.clear()
            level = len(m.group(1))
            title = m.group(2).strip()
            stack[:] = [(lvl, t) for (lvl, t) in stack if lvl < level]
            stack.append((level, title))
            cur_heading = " > ".join(t for _, t in stack)
        else:
            buf.append(line)
            if sum(len(x) for x in buf) > max_chars:
                flush()
                buf.clear()
    flush()
    return chunks
