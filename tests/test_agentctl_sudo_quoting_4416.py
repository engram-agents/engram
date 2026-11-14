"""#4416: the words `agentctl session` builds into the `sudo -u USER -i bash -c STRING` command
are quoted with the octal ANSI-C helper `_oct_quote`, not `printf '%q'`.

Why: `sudo -i` backslash-escapes every character of the command words except alphanumerics,
`_`, `-` and `$`, joins the words with spaces, and hands the result to the login shell, which
parses it again. A `printf '%q'` word holding `$` does not survive that (`a$HOME` is lost); the
octal form (every byte as a backslash and exactly three octal digits inside `$'...'`) survives
under bash AND dash as the outer shell. The inner consumer is `bash -c`, so the `$'...'` form
(a bash extension) is allowed here.

Bound: the evidence for the sudo cells is sudo 1.9.15p5's DOCUMENTED escaping rule applied by
hand in a simulation (the C2 cells), not a run of real sudo.

Contract (written before the implementation):
  C1  `_oct_quote WORD` (one definition, file scope) prints `$'` + every byte of WORD as `\\ooo`
      (exactly 3 octal digits) + `'`; the empty word prints `$''`; the output is pure ASCII and
      `printf '<%s>'` of it, evaluated by bash, gives back exactly `<WORD>` (compared as bytes).
      Bytes, not characters: a multibyte character comes out as its byte escapes.
  C2  Under the simulated `sudo -i` rule, `<outer> -c "<joined escaped words>"` with the words
      [bash -c "printf '<%s>' QUOTED \\"\\$@\\"" -- 'arg one' 'arg two'] prints
      `<word><arg one><arg two>` for the 7-case set, for both outers (bash, dash). A control
      with `printf '%q'` must FAIL `a$HOME` under both outers (the harness can fail).
  C3  Site cells: the scope_words assignment uses `_oct_quote` for both words and no `printf '%q'`;
      each of the two SUDO cd words uses `_oct_quote`; each of the two AS-SELF cd words keeps
      `printf '%q'` (one argv word to `exec bash -c`, no rejoin); each changed site carries
      `inner consumer is bash -c` within 3 lines above; `_oct_quote` is defined once at file
      scope; exactly one `scope_words=` builder, used by every launch command (C3f).
  C4  End to end with a fake sudo: a hostile --cd directory reaches the recorded `bash -c`
      string as the octal form only.

The `tail_args` words after `--` are not touched here: tracked separately.

The simulated sudo escaping is applied with a Python regex (the same rule as the sed one-liner
`sed -z -E 's/([^A-Za-z0-9_$-])/\\\\\\1/g'`), so the cells also run where sed has no `-z`.
Set ENGRAM_TEST_BASH to run the inner/outer bash cells against another bash (a Mac seat: /bin/bash).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

AGENTCTL = Path(__file__).resolve().parent.parent / "tools" / "agentctl"
BASH = os.environ.get("ENGRAM_TEST_BASH", "bash")
HAVE_DASH = shutil.which("dash") is not None
TEXT = AGENTCTL.read_text()
LINES = TEXT.splitlines()

HOSTILE = {
    "plain": b"plain", "dash_x": b"-x", "empty": b"", "dollar_home": b"a$HOME",
    "newline": b"a\nb", "quote": b"a'b", "space": b"a b", "trailing_dollar": b"a$",
    "brace_home": b"${HOME}", "backslash": b"a\\b", "tab_quote": b"a\t'b",
    "dquote": b'a"b', "backquote": b"a`id`b", "cmdsub": b"a$(id)b", "var_x": b"$X",
    "byte01_then_1": b"\x01" + b"1", "utf8": "é".encode(),
}
SIM = {k: HOSTILE[k] for k in
       ("plain", "dash_x", "empty", "dollar_home", "newline", "quote", "space")}


def _helper_text() -> str:
    m = re.search(r"^_oct_quote\(\) \{.*\}$", TEXT, re.M)
    assert m, "no file-scope one-line _oct_quote definition in tools/agentctl"
    return m.group(0)


def _utf8_locale():
    out = subprocess.run(["locale", "-a"], capture_output=True, text=True).stdout.split()
    for n in out:
        if re.search(r"utf-?8", n, re.I):
            return n
    return None


def _quote(word: bytes, how: str = "oct") -> bytes:
    if how == "oct":
        script = _helper_text() + '\n_oct_quote "$1"'
    else:
        script = "printf '%q' \"$1\""
    return subprocess.run([BASH, "-c", script, "x", word], capture_output=True,
                          check=True).stdout


@pytest.mark.parametrize("name", list(HOSTILE))
def test_c1_helper_roundtrips_bytes(name):
    word = HOSTILE[name]
    loc = _utf8_locale()
    if name == "utf8" and not loc:
        pytest.skip("no UTF-8 locale installed; the multibyte cell cannot discriminate")
    env = dict(os.environ, **({"LC_ALL": loc} if loc else {}))
    pre = _helper_text() + "\n"
    q = subprocess.run([BASH, "-c", pre + '_oct_quote "$1"', "x", word],
                       capture_output=True, check=True, env=env).stdout
    assert q.startswith(b"$'") and q.endswith(b"'"), q
    q.decode("ascii")
    assert re.fullmatch(rb"\$'(\\[0-7]{3})*'", q), q
    if word == b"":
        assert q == b"$''"
    assert len(q) == 3 + 4 * len(word)
    got = subprocess.run([BASH, "-c", pre + 'q="$(_oct_quote "$1")"; eval "printf \'<%s>\' $q"',
                          "x", word], capture_output=True, check=True, env=env).stdout
    assert got == b"<" + word + b">"


def _esc(word: bytes) -> bytes:
    # sudo -i's documented rule: a backslash before every char except [A-Za-z0-9_$-]
    return re.sub(rb"([^A-Za-z0-9_$-])", rb"\\\1", word)


def _simulate(outer: str, quoted: bytes) -> bytes:
    string = b"printf '<%s>' " + quoted + b' "$@"'
    words = [BASH.encode(), b"-c", string, b"--", b"arg one", b"arg two"]
    joined = b" ".join(_esc(w) for w in words)
    return subprocess.run([outer, "-c", joined], capture_output=True,
                          env={"PATH": os.environ["PATH"], "HOME": "/simhome",
                               "X": "EXPANDED"}).stdout


def _outers():
    return [pytest.param("bash", id="outer-bash"),
            pytest.param("dash", id="outer-dash",
                         marks=pytest.mark.skipif(not HAVE_DASH, reason="dash is not installed"))]


@pytest.mark.parametrize("outer", _outers())
@pytest.mark.parametrize("name", list(SIM))
def test_c2_octal_survives_simulated_sudo_i(outer, name):
    word = SIM[name]
    assert _simulate(outer, _quote(word)) == b"<" + word + b"><arg one><arg two>"


@pytest.mark.parametrize("outer", _outers())
def test_c2_control_printf_q_loses_dollar_word(outer):
    word = SIM["dollar_home"]
    assert _simulate(outer, _quote(word, "q")) != b"<" + word + b"><arg one><arg two>"


# --- C3: the sites -------------------------------------------------------------------------

def _cmd_session():
    start = next(i for i, l in enumerate(LINES) if l.startswith("cmd_session() {"))
    end = next(i for i in range(start + 1, len(LINES)) if LINES[i] == "}")
    return start, end


def _cd_sites(kind: str):
    s, e = _cmd_session()
    marker = 'sudo -u "$user" -i bash -c' if kind == "sudo" else "exec bash -c"
    return [i for i in range(s, e) if "cd $(" in LINES[i] and marker in LINES[i - 1]]


def _scope_line():
    s, e = _cmd_session()
    hits = [i for i in range(s, e) if re.match(r"\s*scope_words=", LINES[i])]
    assert len(hits) == 1, hits
    return hits[0]


def test_c3a_scope_words_uses_oct_quote_for_both_words():
    line = LINES[_scope_line()]
    assert line.count("_oct_quote") == 2, line
    assert "printf '%q'" not in line, line


@pytest.mark.parametrize("n", [0, 1])
def test_c3b_sudo_cd_word_uses_oct_quote(n):
    sites = _cd_sites("sudo")
    assert len(sites) == 2, sites
    assert '_oct_quote "${cwd_arg[0]}"' in LINES[sites[n]], LINES[sites[n]]


@pytest.mark.parametrize("n", [0, 1])
def test_c3c_as_self_cd_word_keeps_printf_q(n):
    sites = _cd_sites("self")
    assert len(sites) == 2, sites
    assert "cd $(printf '%q' \"${cwd_arg[0]}\")" in LINES[sites[n]], LINES[sites[n]]


@pytest.mark.parametrize("site", ["scope_words", "sudo_cd_1", "sudo_cd_2"])
def test_c3d_changed_site_carries_the_comment(site):
    if site == "scope_words":
        at = _scope_line()
    else:
        sites = _cd_sites("sudo")
        assert len(sites) == 2, sites
        at = sites[int(site[-1]) - 1] - 1  # the `sudo ... bash -c \` line above the cd word
    assert "inner consumer is bash -c" in "\n".join(LINES[max(0, at - 3):at]), site


def test_c3e_oct_quote_defined_once_at_file_scope():
    defs = [i for i, l in enumerate(LINES) if re.match(r"_oct_quote\(\)", l)]
    assert len(defs) == 1, defs
    assert not [l for l in LINES if re.match(r"\s+_oct_quote\(\)", l)]
    assert defs[0] < _cmd_session()[0]


def test_c3f_one_scope_words_builder_used_by_every_launch():
    s, e = _cmd_session()
    body = [l for l in LINES[s:e] if not l.lstrip().startswith("#")]
    assert sum("scope_words=" in l for l in body) == 1
    assert sum("__scope-exec" in l for l in body) == 1
    assert sum("$scope_words" in l for l in body) == 8


# --- C4: end to end with a fake sudo -------------------------------------------------------

def test_c4_hostile_cd_dir_reaches_sudo_as_octal(tmp_path):
    rec, bindir = tmp_path / "rec", tmp_path / "bin"
    rec.mkdir()
    bindir.mkdir()
    for name, text in (("id", '#!/bin/sh\nif [ "$1" = "-un" ]; then echo agent-zed; '
                              'elif [ "$1" = "-u" ]; then exec /usr/bin/id -u; else exit 0; fi\n'),
                       ("sudo", '#!/bin/bash\nprintf \'%s\\0\' "$@" >> "$FAKE_REC/argv"\nexit 0\n')):
        (bindir / name).write_text(text)
        (bindir / name).chmod(0o755)
    for t in ("cat", "head", "sed", "readlink", "dirname", "grep", "tr", "ls", "mkdir",
              "stat", "env", "sort", "wc", "uniq"):
        if shutil.which(t) and not (bindir / t).exists():
            (bindir / t).symlink_to(shutil.which(t))
    hostile = tmp_path / "a$HOME'b\nc"
    hostile.mkdir()
    proc = subprocess.run(
        [shutil.which(BASH) or BASH, str(AGENTCTL), "session", "zed", "--platform", "claude", "--no-share",
         "--cd", str(hostile), "-c"], capture_output=True, text=True, timeout=30,
        env={"PATH": str(bindir), "HOME": str(tmp_path / "home"), "FAKE_REC": str(rec)})
    assert proc.returncode == 0, proc.stderr
    argv = (rec / "argv").read_bytes().split(b"\0")[:-1]
    s = next(a for a in argv if b"exec" in a).decode()
    m = re.search(r"cd (\$'(?:\\[0-7]{3})*') &&", s)
    assert m, s
    decoded = bytes(int(x, 8) for x in re.findall(r"\\([0-7]{3})", m.group(1))).decode()
    assert decoded in (str(hostile), os.path.realpath(hostile))
    assert "a$HOME" not in s and "\n" not in s.split(m.group(1))[1]
