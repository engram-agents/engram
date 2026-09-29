"""#2906 -- ``forum reply`` must not block on a stdin that can never deliver EOF.

In the Claude Code Bash harness fd 0 is a SOCKET: ``sys.stdin.read()`` blocks
forever.  Fake-stdin objects cannot model that, so these tests run
``src/engram/tools/forum.py`` in a SUBPROCESS with a real fd 0 of each kind.

Contract:
  S1  socket stdin + positional body -> never reads stdin; exits (not hangs).
  S2  socket stdin, no positional    -> refuses at once with the no-body message.
  S3  FIFO stdin "b" + positional "a" -> proceeds, with a "stdin ignored" note
      (a FIFO is never read on the positional path: it may never reach EOF).
  S4  /dev/null stdin + positional   -> proceeds past body handling.

Fold 2 (reviewer round 1):
  N1  argparse errors never echo long user text (redacted to a char count).
  N2  short values stay visible (no over-redaction).
  N3  positional body + never-closed FIFO -> exits, with the note.
  N4  positional body + regular-file stdin "b" -> both-sources refusal.
  N5  positional body + closed FIFO "b" -> proceeds, with the note.
  N6/N7  post / verify on a never-closing socket stdin refuse at once.
  N8  unrecognized args on a non-body subcommand carry the count only.

Never touches a real forum or ~/.engram: ENGRAM_HOME is a temp dir whose
config points the forum at a closed localhost port (a connection error proves
the process got past body handling).
"""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

_FORUM_PY = Path(__file__).resolve().parents[1] / "src" / "engram" / "tools" / "forum.py"
TIMEOUT = 5
NO_BODY = ("forum reply: no body — pass it as an argument "
           '(forum reply ID "text") or pipe it on stdin')


NOTE = "forum reply: note: stdin ignored because a body argument was given"
NO_BODY_STDIN = "no body on stdin — pipe it in (e.g. heredoc)"
SENTINELS = ["ZQ" + "abcdefghij" * 5 + "YZ", "it's" + "abcdefghij" * 5 + 'a"b']


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "engram_home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "agent_name": "tester",
        "forum": {"url": f"http://127.0.0.1:{_closed_port()}"},
    }))
    e = {k: v for k, v in os.environ.items() if k != "FORUM_URL"}
    e["ENGRAM_HOME"] = str(home)
    return e


def _run(env, argv, stdin):
    return subprocess.run(
        [sys.executable, str(_FORUM_PY), "reply", *argv],
        stdin=stdin, capture_output=True, text=True, timeout=TIMEOUT, env=env,
    )


def test_s1_socket_stdin_with_positional_does_not_hang(env):
    a, b = socket.socketpair()  # b stays open and silent: read() would block
    try:
        r = _run(env, ["1", "hello"], a.fileno())
    finally:
        a.close()
        b.close()
    assert "not reachable" in r.stderr  # got past body handling to the API call
    assert "both as argument and on stdin" not in r.stderr


def test_s2_socket_stdin_without_positional_refuses_at_once(env):
    a, b = socket.socketpair()
    try:
        r = _run(env, ["1"], a.fileno())
    finally:
        a.close()
        b.close()
    assert r.returncode == 2
    assert NO_BODY in r.stderr


def test_s3_fifo_stdin_plus_positional_proceeds_with_note(env):
    # Was: both-sources refusal.  A FIFO is never read on the positional path
    # (fold 2 / N5): the positional body wins and a note is printed.
    rfd, wfd = os.pipe()
    os.write(wfd, b"b")
    os.close(wfd)
    try:
        r = _run(env, ["1", "a"], rfd)
    finally:
        os.close(rfd)
    assert "not reachable" in r.stderr
    assert NOTE in r.stderr
    assert "both as argument and on stdin" not in r.stderr


def test_s4_devnull_stdin_with_positional_proceeds(env):
    with open(os.devnull) as f:
        r = _run(env, ["1", "a"], f)
    assert "not reachable" in r.stderr
    assert "both as argument and on stdin" not in r.stderr


def test_regular_file_stdin_without_positional_is_read(env, tmp_path):
    p = tmp_path / "body.txt"
    p.write_text("from a file\n")
    with open(p) as f:
        r = _run(env, ["1"], f)
    assert "not reachable" in r.stderr  # body accepted, API attempted


# ---- fold 2 ----------------------------------------------------------------

def _run_argv(env, argv, stdin=subprocess.DEVNULL):
    return subprocess.run(
        [sys.executable, str(_FORUM_PY), *argv],
        stdin=stdin, capture_output=True, text=True, timeout=TIMEOUT, env=env,
    )


@pytest.mark.parametrize("sentinel", SENTINELS)
@pytest.mark.parametrize("argv", [
    ["reply", "{S}"], ["read", "{S}"], ["verify", "{S}"], ["mark-read", "{S}"],
    ["{S}"], ["pack", "{S}"],
])
def test_n1_argparse_errors_never_echo_long_text(env, argv, sentinel):
    r = _run_argv(env, [a.replace("{S}", sentinel) for a in argv])
    assert r.returncode == 2
    assert sentinel not in r.stderr
    assert sentinel[10:40] not in r.stderr  # no 30-char run of it either
    assert "chars, not shown" in r.stderr


AMBIG_SENTINELS = ["Z" * 60, "the quick brown fox's tale of many words and more"]


@pytest.mark.parametrize("sentinel", AMBIG_SENTINELS)
@pytest.mark.parametrize("sub", ["list", "cursor"])
def test_n1_ambiguous_option_value_is_redacted(env, sub, sentinel):
    # argparse's ambiguous-option message is UNQUOTED:
    #   ambiguous option: --s=<value> could match --sort, --since
    r = _run_argv(env, [sub, "--s=" + sentinel])
    assert r.returncode == 2
    assert sentinel[10:40] not in r.stderr  # no 30-char run of it
    assert "chars, not shown" in r.stderr
    assert "could match" in r.stderr
    assert "--s=" in r.stderr  # option prefix stays visible


def test_n1_ambiguous_option_short_value_stays_visible(env):
    r = _run_argv(env, ["list", "--s=ab"])
    assert r.returncode == 2
    assert "--s=ab could match" in r.stderr
    assert "not shown" not in r.stderr


def test_n2_short_invalid_choice_still_visible(env):
    r = _run_argv(env, ["pakc"])
    assert r.returncode == 2
    assert "'pakc'" in r.stderr
    assert "not shown" not in r.stderr


def test_n3_positional_with_never_closed_fifo_does_not_hang(env):
    rfd, wfd = os.pipe()  # wfd stays open and silent: read() would block
    try:
        r = _run(env, ["1", "a"], rfd)
    finally:
        os.close(rfd)
        os.close(wfd)
    assert "not reachable" in r.stderr  # got past body handling
    assert NOTE in r.stderr


def test_n4_positional_with_nonempty_regular_file_stdin_refused(env, tmp_path):
    p = tmp_path / "b.txt"
    p.write_text("b")
    with open(p) as f:
        r = _run(env, ["1", "a"], f)
    assert r.returncode == 2
    assert "body given both as argument and on stdin; use one" in r.stderr


def test_n5_positional_with_closed_fifo_holding_b_proceeds(env):
    rfd, wfd = os.pipe()
    os.write(wfd, b"b")
    os.close(wfd)
    try:
        r = _run(env, ["1", "a"], rfd)
    finally:
        os.close(rfd)
    assert "not reachable" in r.stderr
    assert NOTE in r.stderr
    assert "both as argument and on stdin" not in r.stderr


def test_positional_with_socket_stdin_prints_no_note(env):
    a, b = socket.socketpair()
    try:
        r = _run(env, ["1", "hello"], a.fileno())
    finally:
        a.close()
        b.close()
    assert "not reachable" in r.stderr
    assert "stdin ignored" not in r.stderr


@pytest.mark.parametrize("argv,label", [
    (["post", "--category", "c", "--title", "t"], "post"),      # N6
    (["verify", "5"], "verify"),                                 # N7
])
def test_n6_n7_post_verify_socket_stdin_refuse_at_once(env, argv, label):
    a, b = socket.socketpair()
    try:
        r = _run_argv(env, argv, a.fileno())
    finally:
        a.close()
        b.close()
    assert r.returncode == 2
    assert f"forum {label}: {NO_BODY_STDIN}" in r.stderr


def test_n8_unrecognized_on_other_subcommand_is_count_only(env):
    r = _run_argv(env, ["status", "junk"])
    assert r.returncode == 2
    assert "1 unexpected argument" in r.stderr
    assert "junk" not in r.stderr.replace("unexpected", "")
    assert "stdin" not in r.stderr
    assert "post/verify" not in r.stderr


def test_unrecognized_hint_names_body_contract_for_reply_and_post(env):
    r = _run_argv(env, ["reply", "1", "b", "x"])
    assert "positional argument" in r.stderr
    r = _run_argv(env, ["post", "--category", "c", "--title", "t", "x"])
    assert "on stdin" in r.stderr


def test_reply_dashdash_body_starting_with_dash(env):
    r = _run(env, ["1", "--", "-1 from me"], subprocess.DEVNULL)
    assert "not reachable" in r.stderr  # parsed as a body, reached the API


def test_reply_help_documents_dashdash(env):
    r = _run_argv(env, ["reply", "--help"])
    assert "forum reply 42 -- " in " ".join(r.stdout.split())
