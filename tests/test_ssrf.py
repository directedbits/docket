"""SSRF guard — blocks internal/loopback/link-local and non-http(s)."""
import pytest

from docket import ingest as ing


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x",
    "http://localhost/x",
    "http://10.0.0.1/x",
    "http://192.168.1.1/x",
    "http://169.254.169.254/",   # cloud metadata
    "http://[::1]/",
    "ftp://host/x",              # non-http(s) scheme
    "http://",                   # no host
])
def test_guard_public_rejects(url):
    with pytest.raises(ValueError):
        ing._guard_public(url)


def test_guard_host_unwraps_ipv4_mapped_ipv6():
    # ::ffff:169.254.169.254 must be caught as link-local after unwrapping
    with pytest.raises(ValueError):
        ing._guard_host("::ffff:169.254.169.254")
