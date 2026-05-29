"""
Tests for evidence URL validation (Layer 3 honesty enforcement).

Validates structural checks, TLD requirements, DNS resolution,
scheme restrictions, and honesty axiom reminder presence.
"""

import atexit
import json
import os
import shutil
import socket
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_url_validation_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _setup():
    """Reset test DB and reimport server module.

    Uses _configure_paths(TEST_DIR) — the canonical isolation pattern — instead of
    the ENGRAM_DB_DIR env var (a no-op: server reads ENGRAM_DATA_DIR).  See
    test_cascade_semantics.py::fresh_server() for the reference implementation.
    """
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _add_evidence(server, url, title="Test"):
    """Call engram_add_evidence directly and return parsed result.

    Wraps args into payload_json per wave-3 single-payload migration (issue #99).
    """
    return json.loads(server.engram_add_evidence(
        payload_json=json.dumps({"url": url, "title": title})
    ))


def _add_observation(db_dir, **kwargs):
    """Call engram_add_observation via EngramClient."""
    if "server" in sys.modules:
        del sys.modules["server"]
    from engram_client import EngramClient
    client = EngramClient(db_dir=db_dir)
    return client.call("engram_add_observation", kwargs)


# ── Structural validation ────────────────────────────────────────────────────

def test_no_hostname_rejected():
    """URL with no hostname should be rejected."""
    server = _setup()
    r = _add_evidence(server, "https://")
    assert "error" in r, f"Expected error, got {r}"
    assert "no hostname" in r["error"]


def test_no_tld_rejected():
    """Single-label hostname (no dot) should be rejected — catches fabricated domains."""
    server = _setup()
    r = _add_evidence(server, "https://session-transcript/abc")
    assert "error" in r, f"Expected error, got {r}"
    assert "no TLD" in r["error"]


def test_no_tld_other_fabrications():
    """Other fabricated single-label hostnames should also be rejected."""
    server = _setup()
    for hostname in ["conversation", "measurement", "localhost-fake"]:
        r = _add_evidence(server, f"https://{hostname}/data")
        assert "error" in r, f"Expected error for '{hostname}', got {r}"
        assert "no TLD" in r["error"], f"Expected TLD error for '{hostname}'"


# ── DNS resolution ───────────────────────────────────────────────────────────

@pytest.mark.real_dns
def test_dns_failure_rejected():
    """Domain with TLD but no DNS record should be rejected."""
    server = _setup()
    r = _add_evidence(server, "https://totally-fake-domain-xyz123456.com/article")
    assert "error" in r, f"Expected error, got {r}"
    assert "DNS resolution failed" in r["error"]


@pytest.mark.real_dns
def test_real_domain_accepted():
    """A real, resolvable domain should be accepted."""
    server = _setup()
    r = _add_evidence(server, "https://arxiv.org/abs/2401.00001")
    assert r.get("status") in ("created", "already_exists"), f"Expected success, got {r}"


@pytest.mark.real_dns
def test_real_domain_with_subdomain():
    """Subdomains of real domains should work."""
    server = _setup()
    r = _add_evidence(server, "https://www.reuters.com/article/test")
    assert r.get("status") in ("created", "already_exists"), f"Expected success, got {r}"


# ── Scheme validation ────────────────────────────────────────────────────────

def test_synthetic_scheme_rejected():
    """Invented URL schemes should be rejected."""
    server = _setup()
    for scheme in ["conversation://session123", "measurement://data",
                   "memory://agent/thought", "transcript://2026-04-10"]:
        r = _add_evidence(server, scheme)
        assert "error" in r, f"Expected error for '{scheme}', got {r}"
        assert "Unrecognized URL scheme" in r["error"]


# ── Honesty axiom reminder ───────────────────────────────────────────────────

def test_axiom_reminder_in_web_url_errors():
    """All web URL validation errors should include the ax_0002 honesty reminder."""
    server = _setup()
    # No hostname
    r = _add_evidence(server, "https://")
    assert "ax_0002" in r.get("error", ""), "Missing axiom reminder in no-hostname error"

    # No TLD
    r = _add_evidence(server, "https://session-transcript/abc")
    assert "ax_0002" in r.get("error", ""), "Missing axiom reminder in no-TLD error"

    # DNS failure
    r = _add_evidence(server, "https://totally-fake-domain-xyz999.com/x")
    assert "ax_0002" in r.get("error", ""), "Missing axiom reminder in DNS error"


def test_axiom_reminder_in_scheme_errors():
    """Synthetic scheme errors should include the ax_0002 honesty reminder."""
    server = _setup()
    r = _add_evidence(server, "conversation://session")
    assert "ax_0002" in r.get("error", ""), "Missing axiom reminder in scheme error"


def test_axiom_reminder_in_file_errors():
    """File-based evidence errors should include the ax_0002 honesty reminder."""
    server = _setup()
    r = _add_evidence(server, "file:///nonexistent/path/file.txt")
    assert "ax_0002" in r.get("error", ""), "Missing axiom reminder in file-not-found error"


# ── Integration: observation auto-creates evidence ───────────────────────────

def test_observation_with_fake_url_blocked():
    """engram_add_observation with a fabricated URL should fail at evidence creation."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    r = _add_observation(
        TEST_DIR,
        url="https://session-transcript/2026-04-10",
        title="Fake Transcript",
        quoted_text="Some quote",
        interpretation="Testing",
        claim="Test claim",
        quote_type="hard_data",
    )
    assert "error" in r, f"Expected error, got {r}"


def test_observation_with_real_url_succeeds():
    """engram_add_observation with a real URL should succeed."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    r = _add_observation(
        TEST_DIR,
        url="https://arxiv.org/abs/2401.00001",
        title="Real Paper",
        quoted_text="test quote",
        interpretation="Testing real URL",
        claim="Real URLs should work",
        quote_type="editorial",
    )
    assert r.get("status") == "created", f"Expected created, got {r}"


# ── DNS errno discrimination ─────────────────────────────────────────────────

@pytest.mark.real_dns
def test_dns_eai_again_passes_silently(monkeypatch):
    """EAI_AGAIN (transient DNS failure) should not block evidence creation."""
    server = _setup()

    def raise_eai_again(host, *args, **kwargs):
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr("socket.getaddrinfo", raise_eai_again)
    r = _add_evidence(server, "https://example.com/dns-errno-eai-again-test")
    assert "error" not in r, f"EAI_AGAIN should not block, got {r}"
    assert r.get("status") in ("created", "already_exists"), f"Expected success, got {r}"


@pytest.mark.real_dns
def test_dns_eai_fail_passes_silently(monkeypatch):
    """EAI_FAIL (non-recoverable DNS server failure) should not block evidence creation."""
    server = _setup()

    def raise_eai_fail(host, *args, **kwargs):
        raise socket.gaierror(socket.EAI_FAIL, "Non-recoverable failure in name resolution")

    monkeypatch.setattr("socket.getaddrinfo", raise_eai_fail)
    r = _add_evidence(server, "https://example.com/dns-errno-eai-fail-test")
    assert "error" not in r, f"EAI_FAIL should not block, got {r}"
    assert r.get("status") in ("created", "already_exists"), f"Expected success, got {r}"


@pytest.mark.real_dns
def test_dns_eai_noname_still_blocks(monkeypatch):
    """EAI_NONAME (definitive name-not-found) should still block evidence creation."""
    server = _setup()

    def raise_eai_noname(host, *args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr("socket.getaddrinfo", raise_eai_noname)
    r = _add_evidence(server, "https://example.com/dns-errno-eai-noname-test")
    assert "error" in r, f"EAI_NONAME should block, got {r}"
    assert "DNS resolution failed" in r["error"], f"Expected DNS error message, got {r}"


# ── Run all tests ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed out of {passed + failed}")
    if failed:
        sys.exit(1)
