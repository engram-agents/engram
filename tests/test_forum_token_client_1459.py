"""#1459 PR A, clients: one token source, sent on every request, loud on 401/403.

Contract (stated before the implementation):

* ``load_forum_token(environ=None)``: ``ENGRAM_FORUM_TOKEN`` wins when set and
  non-empty; else the file ``<engram_home()>/forum-token``, whitespace-stripped.
  An existing file with ``mode & 0o077`` is REFUSED with a ForumApiError subclass
  naming the path and ``chmod 600``. No env and no file is None.
* ``ForumClient`` sends ``Authorization: Bearer <t>`` on every request when a token is
  available (resolved once per client; an explicit ``token=`` overrides) and sends no
  Authorization header when there is none.
* the ``forum`` CLI: a 401 prints ONE line naming the token path and exits non-zero (no
  traceback); a 403 identity mismatch names the two agents.
* each monitor script: the token never appears in the argv of the curl it runs, and the
  header does reach curl (on stdin, as a curl config line).
"""
import io
import json
import os
import shutil
import tempfile
import stat
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools import forum_api
from tools.forum_api import (
    ForumApiError,
    ForumAuthError,
    ForumClient,
    ForumHttpError,
    ForumTokenFileError,
    auth_failure_line,
    load_forum_token,
)

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "src" / "engram" / "tools"


@pytest.fixture
def owned_tmp():
    """A scratch dir this test owns (mkdtemp, removed afterwards). Not pytest's shared
    numbered tmp_path base, which a concurrent pytest session can prune mid-run (#1459 round 1c)."""
    d = Path(tempfile.mkdtemp(prefix="forum1459-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def home(owned_tmp, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(owned_tmp))
    monkeypatch.delenv("ENGRAM_FORUM_TOKEN", raising=False)
    return owned_tmp


def _token_file(home, text="file-token\n", mode=0o600):
    p = home / "forum-token"
    p.write_text(text)
    os.chmod(p, mode)
    return p


# 16 -----------------------------------------------------------------
def test_env_wins_over_file(home, monkeypatch):
    _token_file(home, "file-token\n")
    monkeypatch.setenv("ENGRAM_FORUM_TOKEN", "env-token")
    assert load_forum_token() == "env-token"


def test_file_used_when_env_unset_and_stripped(home):
    _token_file(home, "  file-token \n\n")
    assert load_forum_token() == "file-token"


def test_empty_env_falls_through_to_file(home, monkeypatch):
    _token_file(home, "file-token\n")
    monkeypatch.setenv("ENGRAM_FORUM_TOKEN", "")
    assert load_forum_token() == "file-token"


def test_group_or_world_readable_file_is_refused_naming_path(home):
    for mode in (0o644, 0o640, 0o604):
        p = _token_file(home, "file-token\n", mode)
        with pytest.raises(ForumTokenFileError) as ei:
            load_forum_token()
        assert str(p) in str(ei.value) and "chmod 600" in str(ei.value)
        assert isinstance(ei.value, ForumApiError)


def test_missing_file_and_no_env_is_none(home):
    assert load_forum_token() is None


# R5 (round 2): a symlinked token file is judged by its TARGET, in every client -----------------
def _link_to(home, target_mode):
    real = home / "real-token"
    real.write_text("linked-token\n")
    os.chmod(real, target_mode)
    link = home / "forum-token"
    link.symlink_to(real)
    return link


def test_symlink_to_a_0600_file_is_accepted(home):
    _link_to(home, 0o600)
    assert load_forum_token() == "linked-token"


def test_symlink_to_a_0644_file_is_refused_naming_the_link_path(home):
    link = _link_to(home, 0o644)
    with pytest.raises(ForumTokenFileError) as ei:
        load_forum_token()
    assert str(link) in str(ei.value) and "chmod 600" in str(ei.value)


def test_dangling_symlink_is_refused_with_a_clear_message_not_read_as_no_token(home):
    link = home / "forum-token"
    link.symlink_to(home / "does-not-exist")
    with pytest.raises(ForumTokenFileError) as ei:
        load_forum_token()
    assert str(link) in str(ei.value) and "dangling symlink" in str(ei.value)
    assert isinstance(ei.value, ForumApiError)


def test_explicit_environ_mapping(owned_tmp):
    p = owned_tmp / "forum-token"
    p.write_text("mapped\n")
    os.chmod(p, 0o600)
    assert load_forum_token({"ENGRAM_HOME": str(owned_tmp)}) == "mapped"
    assert load_forum_token({"ENGRAM_FORUM_TOKEN": "x", "ENGRAM_HOME": str(owned_tmp)}) == "x"


# 17 -----------------------------------------------------------------
def _capture_request(client, verb, *args):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        resp = MagicMock()
        resp.read.return_value = b"{}"
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: False
        return resp

    with patch("tools.forum_api.forum_urlopen", fake_urlopen):
        getattr(client, verb)(*args)
    return captured["req"]


def test_client_sends_bearer_on_get_and_post(home):
    _token_file(home, "tok-123\n")
    c = ForumClient("http://x:5002")
    assert _capture_request(c, "get", "/api/dm", {"agent": "a"}).get_header("Authorization") == "Bearer tok-123"
    assert _capture_request(c, "post", "/api/post", {"agent": "a"}).get_header("Authorization") == "Bearer tok-123"


def test_client_sends_no_header_without_a_token(home):
    c = ForumClient("http://x:5002")
    assert _capture_request(c, "get", "/api/threads").get_header("Authorization") is None


def test_explicit_token_overrides(home, monkeypatch):
    monkeypatch.setenv("ENGRAM_FORUM_TOKEN", "env-token")
    c = ForumClient("http://x:5002", token="explicit")
    assert _capture_request(c, "get", "/api/dm").get_header("Authorization") == "Bearer explicit"


def test_token_resolved_once_per_client(home):
    _token_file(home, "first\n")
    c = ForumClient("http://x:5002")
    _capture_request(c, "get", "/api/dm")
    _token_file(home, "second\n")
    assert _capture_request(c, "get", "/api/dm").get_header("Authorization") == "Bearer first"


def test_refused_token_file_surfaces_as_forum_api_error_on_first_request(home):
    _token_file(home, "tok\n", 0o644)
    c = ForumClient("http://x:5002")  # construction must not raise
    with pytest.raises(ForumTokenFileError):
        c.get("/api/dm")


def _http_error(code, body):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body.encode()))


def test_forum_401_raises_auth_error_naming_token_path(home):
    c = ForumClient("http://x:5002")
    body = json.dumps({"error": "authentication required", "hint": "h"})
    with patch("tools.forum_api.forum_urlopen", side_effect=_http_error(401, body)):
        with pytest.raises(ForumAuthError) as ei:
            c.get("/api/dm")
    assert ei.value.status == 401
    assert str(home / "forum-token") in ei.value.body
    assert "\n" not in ei.value.body
    assert isinstance(ei.value, ForumHttpError)


def test_forum_403_names_both_agents(home):
    body = json.dumps({"error": "identity mismatch", "token_agent": "alice", "named_agent": "bob"})
    line = auth_failure_line(403, body)
    assert "'alice'" in line and "'bob'" in line


def test_app_level_403_keeps_its_own_body(home):
    c = ForumClient("http://x:5002")
    body = json.dumps({"error": "agent not authorized"})
    with patch("tools.forum_api.forum_urlopen", side_effect=_http_error(403, body)):
        with pytest.raises(ForumHttpError) as ei:
            c.post("/api/projects/x/colleague", {})
    assert not isinstance(ei.value, ForumAuthError) and ei.value.body == body


# 18 -----------------------------------------------------------------
def _run_forum_cli(home, args, status, body):
    """Run tools/forum.py main() with a urlopen that answers `status`/`body`."""
    code = textwrap.dedent(f"""
        import io, json, sys, urllib.error, urllib.request
        sys.path.insert(0, {str(TOOLS)!r})
        import forum
        def fake(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, {status}, "x", {{}}, io.BytesIO({body!r}.encode()))
        forum.forum_urlopen = fake
        sys.argv = ["forum"] + {args!r}
        forum.main()
    """)
    env = dict(os.environ, ENGRAM_HOME=str(home), FORUM_URL="http://127.0.0.1:9", USER="alice")
    env.pop("ENGRAM_FORUM_TOKEN", None)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)


def test_cli_401_is_one_line_naming_the_token_path_and_exits_nonzero(home):
    (home / "config.json").write_text(json.dumps({"agent_name": "alice"}))
    body = json.dumps({"error": "authentication required", "hint": "h"})
    r = _run_forum_cli(home, ["online"], 401, body)
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1, r.stderr
    assert str(home / "forum-token") in lines[0] and "401" in lines[0]


def test_cli_403_names_both_agents(home):
    (home / "config.json").write_text(json.dumps({"agent_name": "alice"}))
    body = json.dumps({"error": "identity mismatch", "token_agent": "alice", "named_agent": "bob"})
    r = _run_forum_cli(home, ["online"], 403, body)
    assert r.returncode != 0 and "Traceback" not in r.stderr
    assert "'alice'" in r.stderr and "'bob'" in r.stderr


def test_cli_refuses_world_readable_token_file(home):
    (home / "config.json").write_text(json.dumps({"agent_name": "alice"}))
    _token_file(home, "t\n", 0o644)
    r = _run_forum_cli(home, ["online"], 200, "{}")
    assert r.returncode != 0 and "chmod 600" in r.stderr and "Traceback" not in r.stderr


# 19 -----------------------------------------------------------------
FAKE_CURL = """#!/bin/sh
# records argv (one per line) and stdin; answers a minimal /api/updates + mentions body
{ for a in "$@"; do printf '%s\\n' "$a"; done; } > "$FAKE_CURL_OUT/argv"
cat > "$FAKE_CURL_OUT/stdin"
printf '%s' '{"updates": [], "as_of": 1, "mentions": []}'
printf '\\n0.01 0.0 0.0 0.0 0.0 200'
"""


def _functions(script):
    """The monitor's own _forum_token .. _curl_detail block, sliced out of the script text."""
    src = script.read_text()
    start = src.index("_TOKEN_WARNED=0")
    marker = "printf 'curl %s: %s%s, %s' \"$_CURL_RC\" \"$_m\" \"$_p\" \"$_t\"\n}\n"
    end = src.index(marker, start) + len(marker)
    return src[start:end]


def _fetch_with_fake_curl(script, owned_tmp, token_env=None, token_file=None, token_symlink=None):
    bindir = owned_tmp / "bin"
    bindir.mkdir()
    out = owned_tmp / "out"
    out.mkdir()
    curl = bindir / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(0o755)
    home = owned_tmp / "eh"
    home.mkdir()
    if token_file is not None:
        f = home / "forum-token"
        f.write_text(token_file + "\n")
        f.chmod(0o600)
    if token_symlink is not None:  # a mode for a real target, or "dangling"
        link = home / "forum-token"
        if token_symlink == "dangling":
            link.symlink_to(owned_tmp / "no-such-target")
        else:
            real = owned_tmp / "real-token"
            real.write_text("linked-token\n")
            real.chmod(token_symlink)
            link.symlink_to(real)
    # Source the script's _forum_token / _curl_fetch (everything from the token block to the
    # end of _curl_fetch) and call it: the same function the poll loop calls.
    harness = owned_tmp / "h.sh"
    harness.write_text(_functions(script) + '\n_curl_fetch 5 "http://127.0.0.1:1/x"; echo "rc=$_CURL_RC"\n')
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", FAKE_CURL_OUT=str(out),
               ENGRAM_HOME=str(home))
    env.pop("ENGRAM_FORUM_TOKEN", None)
    if token_env:
        env["ENGRAM_FORUM_TOKEN"] = token_env
    r = subprocess.run(["bash", str(harness)], capture_output=True, text=True, env=env)
    return r, out


@pytest.mark.parametrize("script", [TOOLS / "forum-updates-monitor.sh", TOOLS / "forum-mention-monitor.sh"],
                         ids=["updates", "mention"])
@pytest.mark.parametrize("how", ["env", "file"])
def test_monitor_token_never_in_argv_and_reaches_curl_on_stdin(script, how, owned_tmp):
    secret = "S3cretTok-en_value.1234"
    r, out = _fetch_with_fake_curl(
        script, owned_tmp,
        token_env=secret if how == "env" else None,
        token_file=secret if how == "file" else None)
    assert "rc=0" in r.stdout, (r.stdout, r.stderr)
    argv = (out / "argv").read_text()
    assert secret not in argv
    assert "-K" in argv.splitlines()
    assert f"header = \"Authorization: Bearer {secret}\"" in (out / "stdin").read_text()
    assert secret not in r.stdout and secret not in r.stderr


@pytest.mark.parametrize("script", [TOOLS / "forum-updates-monitor.sh", TOOLS / "forum-mention-monitor.sh"],
                         ids=["updates", "mention"])
def test_monitor_without_token_sends_no_header(script, owned_tmp):
    r, out = _fetch_with_fake_curl(script, owned_tmp)
    assert "rc=0" in r.stdout
    assert "Authorization" not in (out / "stdin").read_text()
    assert "-K" not in (out / "argv").read_text().splitlines()


@pytest.mark.parametrize("script", [TOOLS / "forum-updates-monitor.sh", TOOLS / "forum-mention-monitor.sh"],
                         ids=["updates", "mention"])
def test_monitor_refuses_world_readable_token_file(script, owned_tmp):
    bindir = owned_tmp / "bin"
    r, out = _fetch_with_fake_curl(script, owned_tmp, token_file="tok")
    # now loosen the file and run again: curl must not be called at all
    f = owned_tmp / "eh" / "forum-token"
    f.chmod(0o644)
    (out / "argv").unlink()
    h = owned_tmp / "h2.sh"
    h.write_text(_functions(script) + '\n_curl_fetch 5 "http://x/y"; echo "rc=$_CURL_RC"; _curl_detail\n')
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", FAKE_CURL_OUT=str(out),
               ENGRAM_HOME=str(owned_tmp / "eh"))
    env.pop("ENGRAM_FORUM_TOKEN", None)
    r2 = subprocess.run(["bash", str(h)], capture_output=True, text=True, env=env)
    assert "rc=77" in r2.stdout
    assert not (out / "argv").exists()  # curl never ran
    assert "chmod 600" in r2.stderr and r2.stderr.count("forum monitor:") == 1
    assert "group- or world-readable" in r2.stdout


# R5 (round 2): the monitors follow a symlinked token file (stat -L) and judge the TARGET -------
_BOTH = pytest.mark.parametrize(
    "script", [TOOLS / "forum-updates-monitor.sh", TOOLS / "forum-mention-monitor.sh"],
    ids=["updates", "mention"])


@_BOTH
def test_monitor_accepts_a_symlink_to_a_0600_file(script, owned_tmp):
    r, out = _fetch_with_fake_curl(script, owned_tmp, token_symlink=0o600)
    assert "rc=0" in r.stdout, (r.stdout, r.stderr)
    assert 'header = "Authorization: Bearer linked-token"' in (out / "stdin").read_text()
    assert "linked-token" not in (out / "argv").read_text()


@_BOTH
def test_monitor_refuses_a_symlink_to_a_0644_file(script, owned_tmp):
    r, out = _fetch_with_fake_curl(script, owned_tmp, token_symlink=0o644)
    assert "rc=77" in r.stdout, (r.stdout, r.stderr)
    assert not (out / "argv").exists()  # curl never ran
    assert "group- or world-readable" in r.stderr and "chmod 600" in r.stderr


@_BOTH
def test_monitor_refuses_a_dangling_symlink_with_a_clear_message(script, owned_tmp):
    r, out = _fetch_with_fake_curl(script, owned_tmp, token_symlink="dangling")
    assert "rc=77" in r.stdout, (r.stdout, r.stderr)  # not rc=0: it is not "no token configured"
    assert not (out / "argv").exists()
    assert "dangling symlink" in r.stderr and r.stderr.count("forum monitor:") == 1
