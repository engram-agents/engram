"""Cells for the live-forum connect guard (#4475), src/forum/tests/ tree. The guard's ONE definition is the
repo-root conftest.py; this file runs its contract in this tree so a missing wiring here is red.

Safety: the connect cells really try port 5002, so each first fails (no network) unless the guard
wrapper is installed. Run mutants of the guard LOGIC only under a recorder that refuses 5002 beneath it.
"""

from __future__ import annotations

import asyncio
import errno
import http.client
import importlib.util
import json
import shutil
import socket
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CONFTEST = REPO_ROOT / "conftest.py"
_WRAPPER_ATTR = "_live_forum_guard_4475"


def _load_guard_module():
    """The ONE definition lives in the root conftest.py; load it by file path (never sys.path)."""
    spec = importlib.util.spec_from_file_location("_live_forum_guard_probe_4475", _CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


G = _load_guard_module()


def _require_guard():
    """Fail (no network) when this tree is not wired. The connect cells below really try the live
    forum port, so they must never run with the guard absent: a missing wiring is red HERE."""
    if getattr(socket.socket.connect, _WRAPPER_ATTR, None) is None or getattr(
        socket.socket.connect_ex, _WRAPPER_ATTR, None
    ) is None:
        pytest.fail("#4475: the live-forum guard is not installed in this test tree")


def _refused_count():
    return len(getattr(socket.socket.connect, _WRAPPER_ATTR))


# ---------------------------------------------------------------- pure function (G1, G2)

def test_G1_env_forum_url_is_in_the_endpoint_set(tmp_path):
    eps = G.live_forum_endpoints({"FORUM_URL": "http://localhost:5099"}, tmp_path / "absent.json")
    assert ("localhost", 5099) in eps


def test_G1_default_pair_always_present_and_read_from_the_source_files(tmp_path):
    eps = G.live_forum_endpoints({}, tmp_path / "absent.json")
    assert ("localhost", 5002) in eps
    # the default is READ from the code's constant, not retyped: a repo file whose constant says 5077 moves it
    fake = tmp_path / "repo"
    for rel in G.DEFAULT_URL_SOURCES:
        p = fake / rel
        p.parent.mkdir(parents=True)
        p.write_text('_DEFAULT_FORUM_URL = "http://localhost:5077"\n')
    assert ("localhost", 5077) in G.live_forum_endpoints({}, tmp_path / "absent.json", repo_root=fake)


def test_G2_config_url_on_a_non_loopback_host_is_in_the_set_and_matches(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"forum": {"url": "http://forum.example.test:5002/"}}))
    eps = G.live_forum_endpoints({}, cfg)
    assert ("forum.example.test", 5002) in eps
    assert ("localhost", 5002) in eps
    assert G.is_live_forum_target(("forum.example.test", 5002), eps)
    assert G.is_live_forum_target(("FORUM.example.test", 5002), eps)  # host case is not a way round
    assert not G.is_live_forum_target(("forum.example.test", 5003), eps)
    assert not G.is_live_forum_target(("other.example.test", 5002), eps)


def test_G2_unreadable_or_malformed_config_is_ignored(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text("{not json")
    assert ("localhost", 5002) in G.live_forum_endpoints({}, bad)
    bad.write_text(json.dumps({"forum": {"url": 12}}))
    assert ("localhost", 5002) in G.live_forum_endpoints({}, bad)


def test_matcher_covers_loopback_names_and_leaves_other_ports_alone():
    eps = {("localhost", 5002)}
    for host in ("localhost", "127.0.0.1", "::1"):
        assert G.is_live_forum_target((host, 5002), eps), host
        assert not G.is_live_forum_target((host, 5999), eps), host
    assert not G.is_live_forum_target("/tmp/some.sock", eps)  # a unix address


# ---------------------------------------------------------------- the installed guard (G1, G4, G7)

def test_guard_is_active_in_this_tree():
    _require_guard()


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_G1_connect_to_the_live_port_is_refused_and_names_the_guard(host):
    _require_guard()
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        s = socket.socket(fam, socket.SOCK_STREAM)
    except OSError:
        pytest.skip("no IPv6 sockets on this host")
    before = _refused_count()
    with s:
        with pytest.raises(ConnectionRefusedError) as ei:
            s.connect((host, 5002))
        assert "#4475" in str(ei.value) and "guard" in str(ei.value)
    assert _refused_count() == before + 1


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_G1_connect_ex_to_the_live_port_returns_econnrefused(host):
    _require_guard()
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        s = socket.socket(fam, socket.SOCK_STREAM)
    except OSError:
        pytest.skip("no IPv6 sockets on this host")
    before = _refused_count()
    with s:
        assert s.connect_ex((host, 5002)) == errno.ECONNREFUSED
    # ECONNREFUSED alone cannot tell the guard from a refusing recorder beneath it: the guard counts its own
    assert _refused_count() == before + 1


def test_G4_unmocked_urlopen_of_the_default_forum_url_is_refused_by_the_guard():
    _require_guard()
    before = _refused_count()
    with pytest.raises(urllib.error.URLError) as ei:
        urllib.request.urlopen("http://localhost:5002/api/agents/status", timeout=2)
    reason = ei.value.reason
    assert isinstance(reason, ConnectionRefusedError)
    assert "#4475" in str(reason) and "guard" in str(reason)
    assert _refused_count() == before + 1  # the guard refused it; no connect ever ran


def test_other_stdlib_connect_paths_are_refused():
    _require_guard()
    with pytest.raises(ConnectionRefusedError, match="#4475"):
        socket.create_connection(("127.0.0.1", 5002), timeout=2)
    conn = http.client.HTTPConnection("localhost", 5002, timeout=2)
    with pytest.raises(ConnectionRefusedError, match="#4475"):
        conn.request("GET", "/")
    conn.close()
    sconn = http.client.HTTPSConnection("localhost", 5002, timeout=2)
    with pytest.raises(ConnectionRefusedError, match="#4475"):
        sconn.connect()
    sconn.close()

    async def _go():
        await asyncio.open_connection("127.0.0.1", 5002)

    with pytest.raises(ConnectionRefusedError, match="#4475"):
        asyncio.run(_go())


def test_G3_a_loopback_server_on_an_ephemeral_port_is_not_disturbed():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    assert port != 5002

    def _echo():
        c, _ = srv.accept()
        with c:
            c.sendall(c.recv(1))

    t = threading.Thread(target=_echo, daemon=True)
    t.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as cli:
            cli.sendall(b"x")
            assert cli.recv(1) == b"x"
    finally:
        t.join(5)
        srv.close()


def test_G3_connect_ex_to_a_non_forum_loopback_port_is_the_real_answer():
    # nothing listens on this port once the probe's server is closed: the REAL connect_ex answers
    # ECONNREFUSED itself, which the guard must not be the source of (refusal counter unmoved).
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    before = _refused_count() if getattr(socket.socket.connect, _WRAPPER_ATTR, None) is not None else 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        assert s.connect_ex(("127.0.0.1", port)) == errno.ECONNREFUSED
    after = _refused_count() if getattr(socket.socket.connect, _WRAPPER_ATTR, None) is not None else 0
    assert after == before


def test_G7_a_unix_socket_is_not_refused():
    d = tempfile.mkdtemp(prefix="lfg")
    try:
        path = str(Path(d) / "s")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)

        def _echo():
            c, _ = srv.accept()
            with c:
                c.sendall(c.recv(1))

        t = threading.Thread(target=_echo, daemon=True)
        t.start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as cli:
            cli.settimeout(5)
            cli.connect(path)
            cli.sendall(b"y")
            assert cli.recv(1) == b"y"
        t.join(5)
        srv.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_wiring_does_not_depend_on_the_root_conftest():
    """The forum tests may be run from inside src/forum, where the runner treats them as a separate
    universe: the forum conftest must install the guard by itself. Run two cells with the conftest
    search cut at this directory (--confcutdir), so only the forum conftest can supply the guard."""
    import os
    import subprocess
    import sys

    if os.environ.get("LIVE_FORUM_GUARD_4475_INNER"):
        pytest.skip("the inner run")
    here = Path(__file__).resolve()
    env = dict(os.environ, LIVE_FORUM_GUARD_4475_INNER="1")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--confcutdir", str(here.parent),
         str(here) + "::test_guard_is_active_in_this_tree",
         str(here) + "::test_G4_unmocked_urlopen_of_the_default_forum_url_is_refused_by_the_guard"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-1000:]

