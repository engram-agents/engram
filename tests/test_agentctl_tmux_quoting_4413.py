"""#4413: the managed-Codex tmux launch is POSIX single-quoted with the hoisted `_shq`.

Contract (written before the implementation):
  C1  `_shq` is defined ONCE, at file scope in tools/agentctl, its line byte-identical to the
      #4410 text; cmd_share's `at` line still calls it.
  C2  the launch loop builds every word (codex, --remote, unix://SOCK, every "$@" word) with
      `_shq`, after a leading `exec`, one space before each word. The string is safe under ANY
      POSIX shell, not only bash.
  C3  the comment above the loop names the shell (tmux default-shell, the user's login shell)
      and the reason (POSIX quoting; a %q word is $'...' and dash does not read that).

The behavioural cells run the real shell helper end to end around fake codex/tmux binaries.
The fake tmux records the new-session command argument BYTE-EXACT; the cell then runs that
string under bash and under dash, each BY NAME, and compares the argv the fake codex got.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import warnings
from pathlib import Path

import pytest

from _short_socket import short_socket_path

AGENTCTL = Path(__file__).resolve().parent.parent / "tools" / "agentctl"
# the #4410 definition line, pinned literally
SHQ_LINE = """_shq() { printf "'%s'" "${1//\\'/\\'\\\\\\'\\'}"; }"""
HAVE_DASH = shutil.which("dash") is not None

HOSTILE = {
    "tab_quote_touch": "\t'; touch MARK; #",
    "newline_quote_touch": "\n'; touch MARK; #",
    "expansions": "a $HOME `id` \\ * ? z",
    "leading_dash": "-x",
    "empty": "",
    "single_quote": "'",
    "dollar_quote": "$'x'",
    # a pasted prompt ends in a newline; a path-like word ends in a backslash: a quoting step that
    # strips trailing newlines (command substitution does) or eats a trailing backslash corrupts them
    "trailing_newline": "x\n",
    "trailing_backslash": "x\\",
}


def _stubs(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "codex").write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        "if [[ \"${1:-}\" == app-server && \"${2:-}\" == daemon && \"${3:-}\" == start ]]; then exit 0; fi\n"
        "if [[ \"${1:-}\" == app-server && \"${2:-}\" == daemon && \"${3:-}\" == version ]]; then\n"
        "  printf '{\"socketPath\": \"%s\"}\\n' \"$FAKE_SOCKET\"; exit 0\n"
        "fi\n"
        "if [[ \"${1:-}\" == --remote ]]; then printf '%s\\0' \"$@\" > \"$FAKE_ARGV\"; exit 0; fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    (bin_dir / "tmux").write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        "case \"${1:-}\" in\n"
        "  has-session) test -f \"$FAKE_TMUX_STATE\" ;;\n"
        "  new-session) printf '%s' \"${@: -1}\" > \"$FAKE_REMOTE_CMD\"; touch \"$FAKE_TMUX_STATE\" ;;\n"
        "  set-option|attach) exit 0 ;;\n"
        "  *) exit 98 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    for p in bin_dir.iterdir():
        p.chmod(0o755)
    return bin_dir


def _launch(tmp_path: Path, *args: str):
    """Run the real helper; return (remote_command bytes, socket path string, bin_dir)."""
    import socket as _socket

    with short_socket_path() as sock_path:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.bind(str(sock_path))
        try:
            bin_dir = _stubs(tmp_path)
            rc_file = tmp_path / "remote-command"
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "FAKE_SOCKET": str(sock_path),
                "FAKE_TMUX_STATE": str(tmp_path / "tmux-running"),
                "FAKE_REMOTE_CMD": str(rc_file),
                "FAKE_ARGV": str(tmp_path / "unused-argv"),
            }
            proc = subprocess.run(
                ["bash", str(AGENTCTL), "__codex-managed-session", "mira", "--cwd", str(tmp_path), "--", *args],
                capture_output=True, text=True, env=env, timeout=60,
            )
            assert proc.returncode == 0, proc.stderr
            return rc_file.read_bytes(), str(sock_path), bin_dir
        finally:
            sock.close()


def _replay(shell: str, command: bytes, bin_dir: Path, scratch: Path):
    """Run the recorded string under `shell` BY NAME; return (argv list, work dir)."""
    work = Path(tempfile.mkdtemp(dir=scratch))
    argv_file = work / "argv"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_ARGV": str(argv_file)}
    res = subprocess.run([shell, "-c", os.fsdecode(command)], cwd=work, env=env,
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, (shell, command, res.stdout, res.stderr)
    return argv_file.read_bytes().split(b"\0")[:-1], work


@pytest.fixture
def scratch(tmp_path):
    d = tmp_path / "scratch"
    d.mkdir()
    return d


def _skip_without_dash(shell):
    if shell == "dash" and not HAVE_DASH:
        warnings.warn("dash not found: this #4413 cell is NOT exercised on this host; "
                      "a green run here is not evidence the injection was refused")
        pytest.skip("dash not found: not exercised on this host")


# ---- Q1: the recorded launch string carries every word as data under bash AND dash ----
CALLERS = {
    "resume_arg": lambda ws: ["resume", *ws],
    "prompt_after_dashdash": lambda ws: ["--", *ws],
}
COUNTS = {
    "single": lambda w: [w],
    "several": lambda w: ["alpha", w, "beta gamma"],
}


@pytest.mark.parametrize("shell", ["bash", "dash"])
@pytest.mark.parametrize("count", sorted(COUNTS))
@pytest.mark.parametrize("caller", sorted(CALLERS))
@pytest.mark.parametrize("word_id", sorted(HOSTILE))
def test_Q1_the_launch_string_runs_every_word_as_data_under_bash_and_dash(
        tmp_path, scratch, word_id, caller, count, shell):
    _skip_without_dash(shell)
    args = CALLERS[caller](COUNTS[count](HOSTILE[word_id]))
    command, sock, bin_dir = _launch(tmp_path, *args)
    assert command.startswith(b"exec "), command
    argv, work = _replay(shell, command, bin_dir, scratch)
    assert not (work / "MARK").exists(), (shell, command, "a word ran as code")
    expected = [b"--remote", b"unix://" + os.fsencode(sock)] + [os.fsencode(a) for a in args]
    assert argv == expected, (shell, command)


@pytest.mark.parametrize("shell", ["bash", "dash"])
def test_Q1_zero_extra_words_is_exactly_the_two_fixed_words(tmp_path, scratch, shell):
    _skip_without_dash(shell)
    command, sock, bin_dir = _launch(tmp_path)
    argv, _ = _replay(shell, command, bin_dir, scratch)
    assert argv == [b"--remote", b"unix://" + os.fsencode(sock)]


def test_Q1_the_string_is_exec_then_one_space_before_each_single_quoted_word(tmp_path):
    # Pinning the leading `exec` is load-bearing: removing it would turn a mangled-argv bug
    # (under a non-bash default-shell) back into code execution (see Q2b).
    command, sock, _ = _launch(tmp_path, "resume", "it's")
    words = ["codex", "--remote", "unix://" + sock, "resume", "it's"]
    expect = "exec" + "".join(" '" + w.replace("'", "'\\''") + "'" for w in words)
    assert command.decode() == expect


# ---- Q2: positive controls on the OLD %q-built line (a test fixture, not read from graph code) ----
def _old_line(word, strip_exec=False):
    out = subprocess.run(
        ["bash", "-c",
         'c=exec; for w in codex --remote unix://s "$@"; do printf -v q " %q" "$w"; c+=$q; done; printf "%s" "$c"',
         "x", word], capture_output=True, text=True, check=True).stdout
    return out[len("exec "):] if strip_exec else out


def _run_old(line, tmp_path, scratch):
    bin_dir = _stubs(tmp_path)
    work = Path(tempfile.mkdtemp(dir=scratch))
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_ARGV": str(work / "argv")}
    subprocess.run(["dash", "-c", line], cwd=work, env=env, capture_output=True, text=True, timeout=60)
    argv = (work / "argv").read_bytes().split(b"\0")[:-1] if (work / "argv").exists() else None
    return work, argv


@pytest.mark.skipif(not HAVE_DASH, reason="dash not found: the %q positive control is NOT exercised on this host")
def test_Q2a_control_the_old_percent_q_line_hands_codex_different_words_under_dash(tmp_path, scratch):
    word = HOSTILE["tab_quote_touch"]
    _, argv = _run_old(_old_line(word), tmp_path, scratch)
    assert argv != [b"--remote", b"unix://s", os.fsencode(word)], "the %q control did not corrupt argv on this dash"


@pytest.mark.skipif(not HAVE_DASH, reason="dash not found: the %q positive control is NOT exercised on this host")
def test_Q2b_control_the_old_line_without_its_leading_exec_creates_the_marker_under_dash(tmp_path, scratch):
    # documents why the real line is a wrong-words bug today, not an injection: `exec` replaces
    # the shell at the first `;`. Without exec the same words run as code.
    work, _ = _run_old(_old_line(HOSTILE["tab_quote_touch"], strip_exec=True), tmp_path, scratch)
    assert (work / "MARK").exists(), "the %q control did not reproduce the injection on this dash"


# ---- Q3-Q6: the text of tools/agentctl ----
def _lines():
    return AGENTCTL.read_text(encoding="utf-8").splitlines()


def test_Q3_shq_is_defined_once_at_file_scope_with_the_4410_text():
    # ANY quoting helper of this shape counts, not only the exact name: a copy under another
    # name (say _shq2) would be the second quoting idiom the issue's decision rules out.
    defs = [(i, l) for i, l in enumerate(_lines(), 1) if re.search(r"\b\w*shq\w*\s*\(\)\s*\{", l)]
    assert len(defs) == 1, defs
    assert defs[0][1] == SHQ_LINE, defs[0]          # column 0: file scope, not indented in a function


def test_Q4_cmd_share_at_line_still_builds_its_command_with_shq():
    at_lines = [l for l in _lines() if 'at "now + $ttl"' in l]
    assert len(at_lines) == 1, at_lines
    assert at_lines[0].count("$(_shq ") == 3, at_lines[0]


def _loop_region():
    lines = _lines()
    start = next(i for i, l in enumerate(lines) if 'local remote_command="exec"' in l)
    end = next(i for i in range(start, len(lines)) if 'tmux new-session -d' in lines[i])
    c0 = start
    while lines[c0 - 1].lstrip().startswith("#"):
        c0 -= 1
    return "\n".join(lines[c0:start]), "\n".join(lines[start:end + 1])


def test_Q5_the_comment_above_the_loop_names_the_shell_and_the_reason():
    comment, _ = _loop_region()
    assert comment.strip(), "no comment above the loop"
    for needle in ("default-shell", "$SHELL", "login shell", "POSIX", "exec"):
        assert needle in comment, needle
    assert "Quote each word with printf %q" not in comment


def test_Q6_the_launch_loop_uses_shq_and_no_longer_builds_words_with_percent_q():
    _, loop = _loop_region()
    assert "printf -v quoted_word" not in loop
    assert "%q" not in loop
    assert re.search(r"\$\(_shq ", loop), "the loop must call the one hoisted _shq (word boundary: _shq2 is not it)"
