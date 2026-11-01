"""One shipped helper writes the loop marker; the skill recipes call it [engram-alpha#4025].

Defect: the engram-loop skill carried two hand recipes (mktemp + jq + mv) that replace
``$ENGRAM_HOME/loop-mode.json`` through a NEW file.  A new inode comes back mode 0600 and
without the directory's default-ACL group entry, so a second account that reads the marker
loses access on every write; the recipes also need ``jq``, which a seat may not have.  The
shared writer ``engram_config_io.atomic_write_text`` (#4013) already keeps mode and group.
The fix ships ONE helper, ``tools/loop_marker_set.py``, built on it; every recipe that sets
a marker key is a one-liner that calls it.

CONTRACT (each clause is a test below; written before the helper):
  H1  an existing marker at mode 0660 is still 0660, same gid, after a set.
  H2  directory with a default ACL (``setfacl -d -m g::rw``): the marker's group entry survives.
  H3  ``KEY JSON-VALUE`` stores ``json.loads(JSON-VALUE)`` (``3300`` is a number);
      ``--string KEY TEXT`` stores TEXT literally.
  H4  runs with no ``jq`` on PATH.
  H5  only KEY changes; the other keys are equal and keep their order.
  H6  missing marker / invalid JSON / non-object top level / empty KEY / unparseable
      JSON-VALUE: nonzero exit, a one-line stderr message, original bytes untouched, no
      stray temp file.  The helper never creates a marker.
  H7  runs from a flattened deployed tree (``CLAUDE_PLUGIN_ROOT`` unset, ``python3 -I``)
      and from the source layout.
  H8  a symlinked marker is REPLACED, never followed.
  H9  success prints exactly one stdout line ``loop_marker_set: <KEY> set``, exit 0.
  H10 a relative ENGRAM_HOME is refused with the resolver's error; marker untouched.
  H11 the SKILL.md recipes run AS PRINTED (only ``<new state>`` substituted).
  H12 class guard: no shipped skill or autostart BLOCK replaces the marker through a new
      file (per-block scan, default-deny allow-list; its one allowance is the Codex-bridge
      arming script, which creates a marker rather than patching one).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
HELPER = SRC / "tools" / "loop_marker_set.py"
LOOP_SKILL = SRC / "skills" / "claude" / "engram-loop" / "SKILL.md"

SEED = {"FIRST_ACTION": "x", "kind": "loop", "state": "old", "cadence_seconds": 1800,
        "next_due_at": "2026-01-01T00:00:00Z", "zeta": [1, 2], "alpha": {"b": 1, "a": 2}}


def _seed(home: Path, data=None, raw: str | None = None) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    marker = home / "loop-mode.json"
    marker.write_text(raw if raw is not None else json.dumps(data or SEED, indent=2) + "\n", encoding="utf-8")
    return marker


def _env(home, path=None, **extra):
    env = {"HOME": str(home), "PATH": path or os.environ.get("PATH", "/usr/bin:/bin")}
    if home is not None:
        env["ENGRAM_HOME"] = str(home)
    env.update(extra)
    return env


def _run(args, home, helper=HELPER, env=None, isolated=True):
    cmd = [sys.executable] + (["-I"] if isolated else []) + [str(helper)] + list(args)
    return subprocess.run(cmd, env=env if env is not None else _env(home), capture_output=True,
                          text=True, timeout=60, cwd=str(home))


def _flatten(dest: Path) -> Path:
    """A plugin-root-shaped tree: tools/ and the core modules side by side (the deployed layout)."""
    (dest / "tools").mkdir(parents=True)
    for rel in ("tools/loop_marker_set.py", "tools/__init__.py", "tools/_owner_read.py",
                "engram_config_io.py", "engram_regular_io.py"):
        shutil.copy(SRC / rel, dest / rel)
    return dest


# --- H1 / H2: mode and group survive ---------------------------------------------------

def test_h1_mode_0660_and_gid_survive(tmp_path):
    home = tmp_path / "eg"
    marker = _seed(home)
    other = [g for g in os.getgroups() if g != os.getgid()]
    if other:
        os.chown(marker, -1, other[0])
    os.chmod(marker, 0o660)
    before = marker.stat()
    r = _run(["--string", "state", "new"], home)
    assert r.returncode == 0, r.stderr
    after = marker.stat()
    assert stat.S_IMODE(after.st_mode) == 0o660
    assert after.st_gid == before.st_gid
    assert json.loads(marker.read_text())["state"] == "new"


def test_h2_default_acl_group_entry_survives(tmp_path):
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("setfacl/getfacl not installed: cannot build a default-ACL directory")
    home = tmp_path / "eg"
    home.mkdir()
    p = subprocess.run(["setfacl", "-d", "-m", "g::rw", str(home)], capture_output=True, text=True)
    if p.returncode != 0:
        pytest.skip(f"setfacl refused on this filesystem: {p.stderr.strip()}")
    marker = _seed(home)  # created under the default ACL: inherits the group entry
    os.chmod(marker, 0o660)

    def group_entry():
        out = subprocess.run(["getfacl", "-p", str(marker)], capture_output=True, text=True).stdout
        return [ln for ln in out.splitlines() if ln.startswith("group::")], stat.S_IMODE(marker.stat().st_mode)

    before = group_entry()
    assert before[0] == ["group::rw-"], before
    r = _run(["--string", "state", "new"], home)
    assert r.returncode == 0, r.stderr
    assert group_entry() == before


# --- H3 / H5 / H9: values, only-KEY-changes, success line --------------------------------

def test_h3_json_value_is_parsed_and_string_flag_is_literal(tmp_path):
    home = tmp_path / "eg"
    marker = _seed(home)
    assert _run(["cadence_seconds", "3300"], home).returncode == 0
    data = json.loads(marker.read_text())
    assert data["cadence_seconds"] == 3300 and type(data["cadence_seconds"]) is int
    assert _run(["cadence_seconds", '"3300"'], home).returncode == 0
    assert json.loads(marker.read_text())["cadence_seconds"] == "3300"
    text = 'a "b" `c` d\nline2 $HOME'
    assert _run(["--string", "state", text], home).returncode == 0
    assert json.loads(marker.read_text())["state"] == text


def test_h5_only_key_changes_and_order_is_kept(tmp_path):
    home = tmp_path / "eg"
    marker = _seed(home)
    assert _run(["--string", "state", "new"], home).returncode == 0
    data = json.loads(marker.read_text())
    expect = dict(SEED)
    expect["state"] = "new"
    assert data == expect
    assert list(data) == list(SEED)
    assert list(data["alpha"]) == ["b", "a"]


def test_h5_new_key_is_appended_last(tmp_path):
    home = tmp_path / "eg"
    marker = _seed(home)
    assert _run(["--string", "extra", "v"], home).returncode == 0
    data = json.loads(marker.read_text())
    assert list(data) == list(SEED) + ["extra"] and data["extra"] == "v"


def test_h9_success_prints_exactly_one_marker_line(tmp_path):
    home = tmp_path / "eg"
    _seed(home)
    r = _run(["cadence_seconds", "3300"], home)
    assert (r.returncode, r.stdout, r.stderr) == (0, "loop_marker_set: cadence_seconds set\n", "")
    r = _run(["--string", "state", "x"], home)
    assert (r.returncode, r.stdout) == (0, "loop_marker_set: state set\n")


def test_h3_non_finite_json_value_is_refused(tmp_path):
    home = tmp_path / "eg"
    marker = _seed(home)
    before = marker.read_bytes()
    for bad in ("NaN", "Infinity", "1e999"):
        r = _run(["state", bad], home)
        assert r.returncode != 0 and r.stdout == "", bad
        assert len(r.stderr.strip().splitlines()) == 1 and r.stderr.startswith("loop_marker_set:"), r.stderr
        assert marker.read_bytes() == before
    assert sorted(os.listdir(home)) == ["loop-mode.json"]


# --- H4: no jq -------------------------------------------------------------------------

def test_h4_runs_with_no_jq_on_path(tmp_path):
    home = tmp_path / "eg"
    marker = _seed(home)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3").symlink_to(sys.executable)
    assert shutil.which("jq", path=str(bindir)) is None
    r = _run(["--string", "state", "x"], home, env=_env(home, path=str(bindir)))
    assert r.returncode == 0, r.stderr
    assert json.loads(marker.read_text())["state"] == "x"


# --- H6: refusals ---------------------------------------------------------------------

def _assert_refused(r, home, before_listing, before_bytes, marker):
    assert r.returncode != 0
    assert r.stdout == ""
    assert len(r.stderr.strip().splitlines()) == 1, r.stderr
    assert r.stderr.startswith("loop_marker_set:"), r.stderr
    assert sorted(os.listdir(home)) == before_listing, "stray file left behind"
    if before_bytes is not None:
        assert marker.read_bytes() == before_bytes


@pytest.mark.parametrize(
    "case,raw,args",
    [
        ("invalid-json", "{not json", ["state", '"x"']),
        ("array-top-level", "[1, 2]\n", ["state", '"x"']),
        ("scalar-top-level", "3\n", ["state", '"x"']),
        ("empty-key", None, ["", '"x"']),
        ("unparseable-value", None, ["state", "{bad"]),
        ("missing-value", None, ["state"]),
    ],
)
def test_h6_refusals_leave_bytes_and_directory_untouched(tmp_path, case, raw, args):
    home = tmp_path / "eg"
    marker = _seed(home, raw=raw)
    before = marker.read_bytes()
    listing = sorted(os.listdir(home))
    _assert_refused(_run(args, home), home, listing, before, marker)


def test_h6_string_flag_with_empty_key_is_refused(tmp_path):
    home = tmp_path / "eg"
    marker = _seed(home)
    _assert_refused(_run(["--string", "", "x"], home), home, ["loop-mode.json"], marker.read_bytes(), marker)


def test_h6_missing_marker_is_not_created(tmp_path):
    home = tmp_path / "eg"
    home.mkdir()
    r = _run(["state", '"x"'], home)
    _assert_refused(r, home, [], None, home / "loop-mode.json")
    assert "loop-mode.json" in r.stderr


# --- H7: layouts ----------------------------------------------------------------------

def test_h7_flattened_deployed_tree_without_plugin_root_under_dash_I(tmp_path):
    root = _flatten(tmp_path / "plugin")
    home = tmp_path / "eg"
    marker = _seed(home)
    env = _env(home)
    assert "CLAUDE_PLUGIN_ROOT" not in env
    r = _run(["--string", "state", "x"], home, helper=root / "tools" / "loop_marker_set.py", env=env)
    assert r.returncode == 0, r.stderr
    assert json.loads(marker.read_text())["state"] == "x"


def test_h7_source_layout(tmp_path):
    home = tmp_path / "eg"
    marker = _seed(home)
    r = _run(["--string", "state", "x"], home)
    assert r.returncode == 0, r.stderr
    assert json.loads(marker.read_text())["state"] == "x"


def test_h7_helper_reached_through_a_symlink_still_finds_its_package_root(tmp_path):
    root = _flatten(tmp_path / "plugin")
    link = tmp_path / "elsewhere" / "loop_marker_set.py"
    link.parent.mkdir()
    link.symlink_to(root / "tools" / "loop_marker_set.py")
    home = tmp_path / "eg"
    marker = _seed(home)
    r = _run(["--string", "state", "x"], home, helper=link)
    assert r.returncode == 0, r.stderr
    assert json.loads(marker.read_text())["state"] == "x"


# --- H8: symlinked marker is replaced, not followed -----------------------------------

def test_h8_symlinked_marker_is_replaced_and_target_untouched(tmp_path):
    home = tmp_path / "eg"
    home.mkdir()
    target = tmp_path / "victim.json"
    target.write_text(json.dumps(SEED) + "\n", encoding="utf-8")
    original = target.read_bytes()
    (home / "loop-mode.json").symlink_to(target)
    r = _run(["state", '"x"'], home)
    # The helper may refuse or replace; what it must never do is write through the link.
    assert target.read_bytes() == original
    if r.returncode == 0:
        marker = home / "loop-mode.json"
        assert not marker.is_symlink()
        assert json.loads(marker.read_text())["state"] == "x"


def test_h8_replaced_link_carries_the_targets_other_keys(tmp_path):
    home = tmp_path / "eg"
    home.mkdir()
    target = tmp_path / "victim.json"
    target.write_text(json.dumps(SEED) + "\n", encoding="utf-8")
    (home / "loop-mode.json").symlink_to(target)
    r = _run(["state", '"x"'], home)
    assert r.returncode == 0, r.stderr
    marker = home / "loop-mode.json"
    assert not marker.is_symlink() and marker.is_file()
    expect = dict(SEED)
    expect["state"] = "x"
    assert json.loads(marker.read_text()) == expect


# --- H10: relative ENGRAM_HOME --------------------------------------------------------

def test_h10_relative_engram_home_is_refused(tmp_path):
    home = tmp_path / "eg"
    marker = _seed(home)
    before = marker.read_bytes()
    env = _env(home)
    env["ENGRAM_HOME"] = "eg"
    r = _run(["state", '"x"'], home, env=env)
    assert r.returncode != 0
    assert "ENGRAM_HOME must be an absolute path" in r.stderr
    assert len(r.stderr.strip().splitlines()) == 1, r.stderr
    assert marker.read_bytes() == before


# --- H11: the SKILL.md recipes, run as printed ----------------------------------------

_FENCE = re.compile(r"^(?P<ind>[ \t]*)```(?P<lang>[A-Za-z0-9_-]*)[ \t]*\n(?P<body>.*?)^(?P=ind)```[ \t]*$", re.M | re.S)


def _recipe_blocks():
    import textwrap
    text = LOOP_SKILL.read_text(encoding="utf-8")
    out = []
    for m in _FENCE.finditer(text):
        if m.group("lang") == "bash" and "loop_marker_set.py" in m.group("body"):
            out.append(textwrap.dedent(m.group("body")))
    return out


def test_h11_skill_has_exactly_the_cadence_and_state_recipes():
    blocks = _recipe_blocks()
    assert len(blocks) == 2, blocks
    assert any("cadence_seconds" in b for b in blocks)
    assert any("--string state" in b and "<new state>" in b for b in blocks)


@pytest.mark.parametrize("layout", ["source", "flattened-plugin-root", "flattened-under-engram-home"])
def test_h11_recipes_run_as_printed(tmp_path, layout):
    home = tmp_path / "my home" / "eg"
    marker = _seed(home)
    env = {k: v for k, v in os.environ.items() if k not in ("ENGRAM_HOME", "CLAUDE_PLUGIN_ROOT")}
    env["HOME"] = str(tmp_path / "user")
    env["ENGRAM_HOME"] = str(home)
    if layout == "source":
        env["CLAUDE_PLUGIN_ROOT"] = str(SRC)
    elif layout == "flattened-plugin-root":
        env["CLAUDE_PLUGIN_ROOT"] = str(_flatten(tmp_path / "plugin"))
    else:  # CLAUDE_PLUGIN_ROOT unset: the recipe's own fallback expression must find the tree
        _flatten(home / "marketplace" / "plugins" / "engram")
    (tmp_path / "user").mkdir()
    # #4313: the recipes name the plugin venv's python under the login HOME; a wrapper onto THIS interpreter
    venv_py = tmp_path / "user" / ".engram" / "venv" / "bin" / "python3"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    venv_py.chmod(venv_py.stat().st_mode | stat.S_IXUSR)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    expect = dict(SEED)
    for body in _recipe_blocks():
        script = body.replace("<new state>", "iter 4 done")
        assert "<" not in script.replace("<<", ""), script  # only the documented placeholder existed
        r = subprocess.run(["bash", "-c", "set -e\n" + script], env=env, cwd=str(cwd),
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (script, r.stderr)
        assert "loop_marker_set:" in r.stdout
    data = json.loads(marker.read_text())
    expect["cadence_seconds"] = 3300
    expect["state"] = "iter 4 done"
    assert data == expect and type(data["cadence_seconds"]) is int
    assert os.listdir(cwd) == []
    assert sorted(p for p in os.listdir(home) if p != "marketplace") == ["loop-mode.json"]


# --- H12: class guard (default-deny) --------------------------------------------------

# The scan works per FENCED CODE BLOCK (any language tag, or none) and per bare line outside
# fences.  A block is flagged when it mentions the marker (the literal ``loop-mode.json``) AND
# holds a replace verb: mktemp, the XXXX template, ``mv``, ``sed -i``, ``cp`` whose
# destination is the marker or a shell variable assigned (directly or by chain) from it, or
# ``jq`` in a block that redirects output anywhere.  Per block, not per line: a recipe that
# keeps the path in a variable ($M) is the same defect as the literal-path one.
#
# What this guard does NOT see: a block that never mentions ``loop-mode.json`` (for example a
# marker path held only in a variable defined in prose or in another block, or a name or path
# assembled by concatenation); a replace done by a program the scan has no verb for (awk -i
# inplace, yq -i, ex, tar -x, git checkout --, and file calls from node or ruby were observed
# missing, and adding those verbs was decided against, not overlooked); a recipe in a file type
# or directory the scan does not walk (it walks src/engram/skills/**/SKILL.md and
# deploy/autostart/** only); and 4-space indented code blocks (markdown lists indent code
# legitimately, so only fenced blocks and bare lines are scanned).  CRLF line endings are
# normalized when a file is read; nothing else about line endings is.  It was measured on shapes
# its authors and reviewers chose (the numbers are in the PR discussion, #4055), so it is a
# tripwire for common shapes, not a proof that no replace-recipe exists.

_FENCE_ANY = re.compile(
    r"^(?P<ind>[ \t]*)(?P<fence>`{3,}|~{3,})[^\n]*\n(?P<body>.*?)^(?P=ind)(?P=fence)[ \t]*$", re.M | re.S)
_QUOTED = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'")
_ASSIGN = re.compile(
    r"(?<![\w$])([A-Za-z_]\w*)=((?:\"(?:[^\"\\]|\\.)*\"|'[^']*'|\$\([^()\n]*\)|[^\s;&|\"']+)+)")
_DEST_CMD = re.compile(r"(?<![\w-])(mv|cp|install|ln|rsync|sponge)\s+([^\n;&|]*)")
_SED_I = re.compile(r"(?<![\w-])sed\b([^\n;&|]*)")
_SED_I_FLAG = re.compile(r"(?:^|\s)(?:-[a-zA-Z]*i|--in-place)")  # -i, -i.bak, -i'', -ni, --in-place[=SUF]
_PERL = re.compile(r"(?<![\w-])perl\b([^\n;&|]*)")
_PERL_I_FLAG = re.compile(r"(?:^|\s)-[a-z]*i")  # -i, -pi, -i.bak, -pi.bak: perl -i replaces the inode too
_DD_OF = re.compile(r"(?<![\w-])dd\b[^\n;&|]*?\sof=(\"[^\"]*\"|'[^']*'|\S+)")
_PY_ASSIGN = re.compile(r"(?m)^[ \t]*([A-Za-z_]\w*)[ \t]*=(?!=)[ \t]*(.+)$")
_PY_MOVE = re.compile(r"(?<![\w])(?:os\.replace|os\.rename|shutil\.move)\((.*)$", re.M)
_PY_METHOD = re.compile(r"^(.*?)\.(?:replace|rename)\((.*)$", re.M)
_PY_TEMP = re.compile(r"\bmkstemp\b|\bNamedTemporaryFile\b")
_MKTEMP = re.compile(r"(?<![\w-])mktemp\b([^\n;&|]*)")
_MKTEMP_BESIDE = re.compile(r"loop-mode\.json\.[A-Za-z]*X{3,}")
_JQ = re.compile(r"(?<![\w-])jq\b")
_TARGET = r"(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s;&|]+)"
_REDIRECT_TARGET = re.compile(r"(?<![0-9&<>-])>>?(?![&=])\s*" + _TARGET)
_TEE_TARGET = re.compile(r"\|\s*tee\s+(?:-\w+\s+)*" + _TARGET)

# Every block the scan flags that is NOT a replace-recipe goes here, keyed by file, WITH its
# reason.  An entry is (distinctive substring, reason, verbs): the substring picks exactly ONE
# flagged block, and ``verbs`` is the exact set of verbs the scan reports for that block, so
# the allowance is a whole-block key: a replace verb added to the block later changes its verb
# set and the entry stops matching.  An entry that matches no flagged block (or more than one,
# or one with a different verb set) fails ``test_h12_every_shipped_block_is_clean``, so a stale
# allowance cannot silently widen later.
ALLOW = {
    "src/engram/skills/claude/engram-loop/SKILL.md": [
        (
            'mv "$pending" "$marker"',
            "the Codex-bridge arming script CREATES a new marker (it first refuses if one already "
            "exists and stages the new one as loop-mode.json.pending); it never patches an existing "
            "marker, so the mode/ACL loss of a replace-recipe does not apply (#4025)",
            ("jq output onto the marker", "mv onto the marker"),
        ),
    ],
}


def _read_text(path):
    """A file's text with CRLF line endings normalized to LF (a stray ``\\r`` breaks the fence scan)."""
    return Path(path).read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")


def _scan_files():
    """-> [(repo-relative posix path, text)] for every shipped skill and autostart file."""
    files = sorted((SRC / "skills").rglob("SKILL.md"))
    deploy = REPO / "deploy" / "autostart"
    files += sorted(p for p in deploy.rglob("*") if p.is_file())
    return [(p.relative_to(REPO).as_posix(), _read_text(p)) for p in files]


def _scan_roots():
    return [REPO / rel for rel, _ in _scan_files()]


_FENCE_LINE = re.compile(r"^([ \t]*)(`{3,}|~{3,})(.*)$")


def fence_problems(text):
    """-> [str]: unclosed fences, and fences whose closing line is indented differently from the
    opener (the block scan would not see such a block).  A fence line of the other kind inside an
    open fence is content, not an opener."""
    problems, opener = [], None
    for n, line in enumerate(text.split("\n"), 1):
        m = _FENCE_LINE.match(line)
        if m is None:
            continue
        ind, run, rest = m.groups()
        if opener is None:
            if run[0] == "`" and "`" in rest:
                continue  # an inline code span at line start, not a fence
            opener = (n, ind, run)
        elif run[0] == opener[2][0] and len(run) >= len(opener[2]) and not rest.strip():
            if ind != opener[1]:
                problems.append(f"line {n}: closing fence indent {len(ind)} differs from the opener's {len(opener[1])} (line {opener[0]})")
            opener = None
    if opener is not None:
        problems.append(f"line {opener[0]}: fence is never closed")
    return problems


def _blocks(text):
    """-> [(first line number, body)]: each fenced block whole, each bare line outside fences alone."""
    out, pos = [], 0

    def bare(chunk, first_line):
        for i, ln in enumerate(chunk.split("\n")):
            if ln.strip():
                out.append((first_line + i, ln))

    for m in _FENCE_ANY.finditer(text):
        bare(text[pos:m.start()], text.count("\n", 0, pos) + 1)
        out.append((text.count("\n", 0, m.start("body")) + 1, m.group("body")))
        pos = m.end()
    bare(text[pos:], text.count("\n", 0, pos) + 1)
    return sorted(out)


def _drop_comment(text):
    """``text`` up to its first unquoted ``#``."""
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return text[:i]
    return text


def _refs(name):
    return re.compile(r"\$\{?%s(?![A-Za-z0-9_])" % re.escape(name))


def _pyrefs(name):
    """A name used as a Python identifier, or as ``$name`` / ``${name}`` in an unquoted heredoc."""
    return re.compile(r"(?<![\w.])\$?\{?%s(?![A-Za-z0-9_])" % re.escape(name))


def _marker_vars(body):
    """Names assigned, in this block, from a string holding the marker or from another such name."""
    names = set()
    changed = True
    while changed:
        changed = False
        for name, rhs in _ASSIGN.findall(body):
            if name not in names and ("loop-mode.json" in rhs or any(_refs(n).search(rhs) for n in names)):
                names.add(name)
                changed = True
        for name, rhs in _PY_ASSIGN.findall(body):
            if name not in names and ("loop-mode.json" in rhs or any(_pyrefs(n).search(rhs) for n in names)):
                names.add(name)
                changed = True
    return names


def _replace_verbs(body):
    """-> sorted replace-verb names a block holds; [] unless it mentions the marker."""
    if "loop-mode.json" not in body:
        return []
    joined = re.sub(r"\\\n[ \t]*", " ", body)
    joined = "\n".join(ln for ln in joined.split("\n") if not ln.lstrip().startswith("#"))
    names = _marker_vars(joined)

    def is_marker(token):
        return "loop-mode.json" in token or any(_refs(n).search(token) for n in names)

    def operands(arg_text):
        try:
            toks = shlex.split(arg_text, comments=True)
        except ValueError:
            toks = _drop_comment(arg_text).split()
        return [t for t in toks if not t.startswith("-") and "<" not in t and ">" not in t]

    # A command is a replace only when its DESTINATION is the marker (a rewrite of an existing
    # file).  Creating a marker (``cat > marker <<EOF``, ``printf > marker``) is not flagged.
    verbs = set()
    for m in _DEST_CMD.finditer(joined):
        ops = operands(m.group(2))
        if ops and is_marker(ops[-1]):
            verbs.add(m.group(1) + " onto the marker")
    for m in _SED_I.finditer(joined):
        if _SED_I_FLAG.search(m.group(1)) and any(is_marker(t) for t in operands(m.group(1))):
            verbs.add("sed -i on the marker")
    for m in _PERL.finditer(joined):
        if _PERL_I_FLAG.search(m.group(1)) and any(is_marker(t) for t in operands(m.group(1))):
            verbs.add("perl -i on the marker")
    for m in _DD_OF.finditer(joined):
        if is_marker(m.group(1).strip("\"'")):
            verbs.add("dd of= the marker")

    def py_marker(text):
        return "loop-mode.json" in text or any(_pyrefs(n).search(text) for n in names)

    # Python: a call that moves/renames a file, with the marker as an argument (or as the
    # receiver of ``.replace`` / ``.rename``, the pathlib form).  A bare ``str.replace`` whose
    # receiver and arguments are not the marker is NOT a file replace.
    py_moved = False
    for m in _PY_MOVE.finditer(joined):
        py_moved = True
        if py_marker(m.group(1)):
            verbs.add("python file move onto the marker")
    for m in _PY_METHOD.finditer(joined):
        if re.search(r"os$|shutil$", m.group(1)):
            continue  # os.replace / os.rename: handled above
        if py_marker(m.group(1)) or py_marker(m.group(2)):
            verbs.add("python .replace/.rename on the marker")
    if py_moved and _PY_TEMP.search(joined):
        verbs.add("python temp-file then move, in a block naming the marker")
    for m in _MKTEMP.finditer(joined):
        if any(is_marker(t) for t in operands(m.group(1))):
            verbs.add("mktemp beside the marker")
    if _MKTEMP_BESIDE.search(joined):
        verbs.add("temp-file template beside the marker")
    for line in joined.split("\n"):
        if _JQ.search(_QUOTED.sub('""', line)):
            targets = [m.group(1) for m in _REDIRECT_TARGET.finditer(line)]
            targets += [m.group(1) for m in _TEE_TARGET.finditer(line)]
            if any(is_marker(t) for t in targets):
                verbs.add("jq output onto the marker")
    return sorted(verbs)


def flagged_blocks(text):
    """-> [(first line number, verbs, body)] for every block that mentions the marker and replaces it."""
    return [(ln, v, body) for ln, body in _blocks(text) for v in [_replace_verbs(body)] if v]


def _snippet(body):
    return next((ln.strip() for ln in body.split("\n") if ln.strip()), "")[:100]


def replace_recipe_hits(text):
    return [(", ".join(v), _snippet(body)) for _, v, body in flagged_blocks(text)]


def audit(files, allow):
    """-> (unallowed, unmatched).  ``unallowed``: flagged blocks no allowance covers.  ``unmatched``:
    allowances that do not match EXACTLY ONE flagged block of their file with EXACTLY their verb
    set (stale, too broad, or the block has since gained a verb)."""
    unallowed, unmatched, seen = [], [], set()
    for rel, text in files:
        seen.add(rel)
        entries = allow.get(rel, [])
        counts = [0] * len(entries)
        mismatched = {}
        for ln, verbs, body in flagged_blocks(text):
            hit = [i for i, (sub, _reason, want) in enumerate(entries) if sub in body and tuple(verbs) == tuple(want)]
            for i in hit:
                counts[i] += 1
            for i, (sub, _reason, want) in enumerate(entries):
                if sub in body and tuple(verbs) != tuple(want):
                    mismatched[i] = f"{rel}: allowance {sub!r} covers verbs {tuple(want)} but the block holds {tuple(verbs)}"
            if not hit:
                unallowed.append(f"{rel}:{ln}: {', '.join(verbs)}: {_snippet(body)}")
        for i, ((sub, _reason, _want), n) in enumerate(zip(entries, counts)):
            if i in mismatched:
                unmatched.append(mismatched[i])
            elif n != 1:
                unmatched.append(f"{rel}: allowance {sub!r} matches {n} blocks, expected exactly 1")
    for rel, entries in allow.items():
        if rel not in seen:
            unmatched.extend(f"{rel}: allowance {sub!r} names a file that was not scanned" for sub, _, _ in entries)
    return unallowed, unmatched


def test_h12_every_shipped_block_is_clean():
    unallowed, unmatched = audit(_scan_files(), ALLOW)
    assert not unallowed, (
        "a shipped block replaces loop-mode.json through a new file (mode 0600, ACL group dropped, "
        "needs jq); call tools/loop_marker_set.py instead (#4025):\n  " + "\n  ".join(unallowed)
    )
    assert not unmatched, "stale or too-broad ALLOW entries (default-deny):\n  " + "\n  ".join(unmatched)


def test_h12_every_allow_entry_states_a_reason():
    assert all(reason.strip() for entries in ALLOW.values() for _sub, reason, _verbs in entries)


def test_h12_scan_covers_the_loop_skill_and_the_boot_charges():
    rels = {p.relative_to(REPO).as_posix() for p in _scan_roots()}
    assert "src/engram/skills/claude/engram-loop/SKILL.md" in rels
    assert any(r.startswith("deploy/autostart/boot-charge") for r in rels)


@pytest.mark.parametrize(
    "line",
    [
        'tmp=$(mktemp "${ENGRAM_HOME:-$HOME/.engram}"/loop-mode.json.XXXXXX) && jq ... > "$tmp"',
        'mv "$tmp" "${ENGRAM_HOME:-$HOME/.engram}"/loop-mode.json',
        "jq '.state = 1' loop-mode.json > /tmp/x && cp /tmp/x loop-mode.json",
    ],
)
def test_h12_guard_flags_the_old_recipe_shapes(line):
    assert replace_recipe_hits(line)


@pytest.mark.parametrize(
    "line",
    [
        'cat "${ENGRAM_HOME:-$HOME/.engram}"/loop-mode.json | jq -r .state | head -20',
        "rm -f \"${ENGRAM_HOME:-$HOME/.engram}\"/loop-mode.json",
        'python3 "$ROOT/tools/loop_marker_set.py" cadence_seconds 3300',
    ],
)
def test_h12_guard_passes_reads_removal_and_the_helper(line):
    assert replace_recipe_hits(line) == []


# --- H12 (round 2): the guard covers the CLASS, not the old one-line shape ---------------
# The first guard needed ``loop-mode`` on the SAME LINE as mktemp / mv / jq >, so a recipe that
# keeps the path in a shell variable passed it.  The scan is per fenced block (and per bare
# line outside fences): a block is flagged when it mentions the marker (the literal, or a
# variable assigned from it in the same block) AND holds a replace verb.

_FENCED = "```bash\n{}\n```\n"

_GUARD_FLAGS = {
    "variable-path": 'M="${ENGRAM_HOME:-$HOME/.engram}/loop-mode.json"; tmp=$(mktemp); '
                     'jq \'.state = 1\' "$M" > "$tmp" && mv "$tmp" "$M"',
    "sed -i": 'M="${ENGRAM_HOME:-$HOME/.engram}/loop-mode.json"\nsed -i \'s/x/y/\' "$M"',
    "sed -i literal": "sed -i 's/x/y/' \"$ENGRAM_HOME/loop-mode.json\"",
    "original mktemp+mv": 'tmp=$(mktemp "${ENGRAM_HOME:-$HOME/.engram}"/loop-mode.json.XXXXXX)\n'
                          'jq \'.state = 1\' x > "$tmp"\nmv "$tmp" "${ENGRAM_HOME:-$HOME/.engram}"/loop-mode.json',
    "assigned early, mv later": 'MARKER="$ENGRAM_HOME/loop-mode.json"\necho start\nT=$(mktemp)\n'
                                'echo hi\nwork\nmv "$T" "$MARKER"',
    "variable chain": 'A="$ENGRAM_HOME/loop-mode.json"\nB="$A"\nsed -i s/x/y/ "$B"',
    "cp onto variable": 'M="$ENGRAM_HOME/loop-mode.json"\ncp /tmp/new "$M"',
    "mv onto the marker variable": 'M="${ENGRAM_HOME:-$HOME/.engram}/loop-mode.json"\nmv "$tmp" "$M"',
    "cp onto the marker literal": 'cp "$x" "${ENGRAM_HOME:-$HOME/.engram}/loop-mode.json"',
    "sed -i on the marker variable": 'M="${ENGRAM_HOME:-$HOME/.engram}/loop-mode.json"\nsed -i \'s/a/b/\' "$M"',
    "jq redirected onto the marker": 'M="${ENGRAM_HOME:-$HOME/.engram}/loop-mode.json"\njq \'.a=1\' "$M" > "$M"',
    "jq piped to tee onto the marker": 'M="$ENGRAM_HOME/loop-mode.json"\njq \'.a=1\' "$M" | tee "$M"',
    "sed -i.bak (glued suffix)": 'M="$H/loop-mode.json"\nsed -i.bak \'s/a/b/\' "$M"',
    "sed -i'' (empty glued suffix)": 'M="$H/loop-mode.json"\nsed -i\'\' \'s/a/b/\' "$M"',
    "sed --in-place=.bak": 'M="$H/loop-mode.json"\nsed --in-place=.bak \'s/a/b/\' "$M"',
    "sed -i -e": 'M="$H/loop-mode.json"\nsed -i -e \'s/a/b/\' "$M"',
    "sed --in-place": 'M="$H/loop-mode.json"\nsed --in-place \'s/a/b/\' "$M"',
    "perl -pi -e": 'M="$H/loop-mode.json"\nperl -pi -e \'s/a/b/\' "$M"',
    "perl -i.bak -pe": 'M="$H/loop-mode.json"\nperl -i.bak -pe \'s/a/b/\' "$M"',
    "python heredoc mkstemp + os.replace": 'python3 - <<EOF\nimport os,tempfile\n'
        't=tempfile.mkstemp(dir="$H")[1]\nos.replace(t,"$H/loop-mode.json")\nEOF',
    "python -c shutil.move": 'python3 -c "import shutil;shutil.move(\'/tmp/x\',\'$H/loop-mode.json\')"',
    "python os.rename onto a variable": 'M="$H/loop-mode.json"\npython3 -c "import os;os.rename(\'/tmp/x\',\'$M\')"',
    "python Path(...).replace": 'python3 - <<EOF\nfrom pathlib import Path\n'
        'Path("/tmp/x").replace(Path("$H/loop-mode.json"))\nEOF',
    "python Path(marker).replace": 'python3 - <<EOF\nfrom pathlib import Path\n'
        'Path("$H/loop-mode.json").replace("/tmp/gone")\nEOF',
    "python variable .rename": 'python3 - <<EOF\nfrom pathlib import Path\n'
        'p = Path("$H/loop-mode.json")\np.rename("/tmp/gone")\nEOF',
    "sponge onto the marker": 'M="$H/loop-mode.json"\njq \'.a=1\' "$M" | sponge "$M"',
    "ln -sf onto the marker": 'ln -sf /tmp/other "$H/loop-mode.json"',
    "rsync onto the marker": 'rsync /tmp/x "$H/loop-mode.json"',
    "dd of= the marker": 'M="$H/loop-mode.json"\ndd if=/tmp/x of="$M"',
    "mv onto the variable, trailing comment": 'M="$H/loop-mode.json"\nmv "$tmp" "$M" # swap in',
    "mv onto the literal, trailing comment": 'mv "$tmp" "$E/loop-mode.json"  # swap in',
    "cp -f onto the variable, trailing comment": 'M="$H/loop-mode.json"\ncp -f "$t" "$M"   # restore',
    "continued jq redirect": 'M="$ENGRAM_HOME/loop-mode.json"\njq -n --arg a 1 \\\n  \'{a:$a}\' > "$M.pending"',
}

_GUARD_PASSES = {
    "helper one-liner": 'python3 "${CLAUDE_PLUGIN_ROOT}/tools/loop_marker_set.py" --string state "x"',
    "helper one-liner naming the marker": 'M="$ENGRAM_HOME/loop-mode.json"\n'
                                         'python3 tools/loop_marker_set.py --marker "$M" state \'"x"\'',
    "cat | jq read": 'M="${ENGRAM_HOME:-$HOME/.engram}/loop-mode.json"\ncat "$M" | jq -r .state',
    "rm": 'rm -f "${ENGRAM_HOME:-$HOME/.engram}"/loop-mode.json',
    "mktemp for an unrelated file": 'tmp=$(mktemp)\necho hi > "$tmp"\nmv "$tmp" /tmp/other.txt',
    "jq comparison, not a redirect": 'M="$ENGRAM_HOME/loop-mode.json"\njq \'select(.n > 1)\' "$M"',
    "cp from the marker": 'M="$ENGRAM_HOME/loop-mode.json"\ncp "$M" /tmp/backup.json',
    "school-day cleanup block, verbatim":
        'rm -f "${ENGRAM_HOME:-$HOME/.engram}"/loop-mode.json\n'
        'mv "${ENGRAM_HOME:-$HOME/.engram}"/loop-scratch.md '
        '"${ENGRAM_HOME:-$HOME/.engram}"/reports/scratch_$(date +%Y-%m-%d_%H%M%S).md 2>/dev/null || true',
    "mv with a destination that is not the marker": 'M="$ENGRAM_HOME/loop-mode.json"\nmv "$a" "$b"',
    "jq reads the marker, redirects elsewhere": 'M="$ENGRAM_HOME/loop-mode.json"\njq -r .state "$M" > /tmp/x',
    "creation heredoc": 'cat > "${ENGRAM_HOME:-$HOME/.engram}"/loop-mode.json <<JSON\n{"a": 1}\nJSON',
    "creation printf": 'printf \'%s\\n\' x > "${ENGRAM_HOME:-$HOME/.engram}/loop-mode.json"',
    "sed -i on another file, marker mentioned elsewhere": 'M="$H/loop-mode.json"\ncat "$M"\nsed -i \'s/a/b/\' /tmp/other.txt',
    "sed without -i is a read": 'M="$H/loop-mode.json"\nsed \'s/a/b/\' "$M"',
    "perl without -i is a read": 'M="$H/loop-mode.json"\nperl -pe \'s/a/b/\' "$M"',
    "python str.replace, marker only in a print": 'python3 - <<EOF\nx = "x".replace("a", "b")\n'
        'print("reading $H/loop-mode.json", x)\nEOF',
    "python reads the marker": 'python3 -c "import json;print(json.load(open(\'$H/loop-mode.json\'))[\'state\'])"',
    "sponge to another file": 'M="$H/loop-mode.json"\njq . "$M" | sponge /tmp/other',
    "ln from the marker": 'M="$H/loop-mode.json"\nln -s "$M" /tmp/link',
    "rsync from the marker": 'M="$H/loop-mode.json"\nrsync "$M" /tmp/backup.json',
    "dd reads the marker": 'M="$H/loop-mode.json"\ndd if="$M" of=/tmp/backup.json',
    "marker named only inside a trailing comment": 'mv "$a" "$b"   # not touching loop-mode.json',
    "stderr redirect only": 'M="$ENGRAM_HOME/loop-mode.json"\ntest -f "$M" 2>/dev/null',
}


@pytest.mark.parametrize("name", sorted(_GUARD_FLAGS))
def test_h12_guard_flags_the_class_in_a_fenced_block(name):
    assert replace_recipe_hits(_FENCED.format(_GUARD_FLAGS[name])), name


@pytest.mark.parametrize("name", sorted(_GUARD_FLAGS))
def test_h12_guard_flags_the_class_in_an_untagged_fence(name):
    assert replace_recipe_hits("```\n" + _GUARD_FLAGS[name] + "\n```\n"), name


@pytest.mark.parametrize("name", sorted(_GUARD_PASSES))
def test_h12_guard_passes_non_replacing_blocks(name):
    assert replace_recipe_hits(_FENCED.format(_GUARD_PASSES[name])) == [], name


def test_h12_flag_is_per_block_not_per_file():
    """A marker mention in one block and a replace verb in ANOTHER block is not a recipe."""
    text = (_FENCED.format('M="$ENGRAM_HOME/loop-mode.json"\ncat "$M"')
            + "prose\n" + _FENCED.format("tmp=$(mktemp)\nmv \"$tmp\" /tmp/other"))
    assert replace_recipe_hits(text) == []


def test_h12_a_variable_assigned_in_another_block_does_not_leak():
    text = _FENCED.format('M="$ENGRAM_HOME/loop-mode.json"') + _FENCED.format('mv a "$M"')
    assert replace_recipe_hits(text) == []


# --- H12 (round 2): the allow-list is default-deny ---------------------------------------

_RECIPE_TEXT = _FENCED.format(_GUARD_FLAGS["variable-path"])
_RECIPE_VERBS = tuple(flagged_blocks(_RECIPE_TEXT)[0][1])  # read off the scan, not guessed


def test_h12_audit_reports_an_unallowed_flagged_block():
    unallowed, unmatched = audit([("a.md", _RECIPE_TEXT)], {})
    assert len(unallowed) == 1 and unallowed[0].startswith("a.md")
    assert unmatched == []


def test_h12_audit_lets_an_allowed_block_through_and_it_is_not_stale():
    allow = {"a.md": [("tmp=$(mktemp); jq", "builds a NEW marker, not a patch of an existing one", _RECIPE_VERBS)]}
    assert audit([("a.md", _RECIPE_TEXT)], allow) == ([], [])


def test_h12_audit_reports_a_stale_allow_entry():
    allow = {"a.md": [("a string no block holds", "stale", _RECIPE_VERBS)]}
    unallowed, unmatched = audit([("a.md", _RECIPE_TEXT)], allow)
    assert len(unallowed) == 1 and unmatched and "a string no block holds" in unmatched[0]


def test_h12_audit_reports_an_allow_entry_for_a_file_that_is_gone():
    allow = {"gone.md": [("tmp=$(mktemp)", "stale", _RECIPE_VERBS)]}
    assert audit([("a.md", "nothing here\n")], allow)[1]


def test_h12_audit_reports_an_allow_entry_matching_two_blocks_as_too_broad():
    allow = {"a.md": [("mktemp", "too broad", _RECIPE_VERBS)]}
    unallowed, unmatched = audit([("a.md", _RECIPE_TEXT + "\n" + _RECIPE_TEXT)], allow)
    assert unmatched and "2 blocks" in unmatched[0]


def test_h12_an_allowance_does_not_cover_a_different_file():
    allow = {"a.md": [("tmp=$(mktemp); jq", "reason", _RECIPE_VERBS)]}
    unallowed, _ = audit([("a.md", _RECIPE_TEXT), ("b.md", _RECIPE_TEXT)], allow)
    assert [u.split(":")[0] for u in unallowed] == ["b.md"]


# --- F2: what the helper says about a marker path that is not a readable regular file -------

def _assert_one_line_refusal(r, home, listing, prefix, path):
    assert r.returncode == 1 and r.stdout == ""
    assert len(r.stderr.strip().splitlines()) == 1, r.stderr
    assert r.stderr.startswith("loop_marker_set: " + prefix + str(path)), r.stderr
    assert sorted(os.listdir(home)) == listing, "stray file left behind"


def _run_with_timeout(args, home, timeout=15):
    cmd = [sys.executable, "-I", str(HELPER)] + list(args)
    return subprocess.run(cmd, env=_env(home), capture_output=True, text=True, timeout=timeout, cwd=str(home))


def test_f2_invalid_json_keeps_the_not_valid_json_wording(tmp_path):
    home = tmp_path / "eg"
    marker = _seed(home, raw="{not json")
    r = _run(["state", '"x"'], home)
    _assert_one_line_refusal(r, home, ["loop-mode.json"], "marker file is not valid JSON: ", marker)
    assert marker.read_bytes() == b"{not json"


def test_f2_non_utf8_file_keeps_the_not_valid_json_wording(tmp_path):
    home = tmp_path / "eg"
    home.mkdir()
    marker = home / "loop-mode.json"
    marker.write_bytes(b'{"a": "\xff\xfe"}\n')
    r = _run(["state", '"x"'], home)
    _assert_one_line_refusal(r, home, ["loop-mode.json"], "marker file is not valid JSON: ", marker)
    assert marker.read_bytes() == b'{"a": "\xff\xfe"}\n'


def test_f2_a_directory_at_the_marker_path_is_not_called_invalid_json(tmp_path):
    home = tmp_path / "eg"
    home.mkdir()
    marker = home / "loop-mode.json"
    marker.mkdir()
    r = _run(["state", '"x"'], home)
    _assert_one_line_refusal(r, home, ["loop-mode.json"], "marker path is not a readable regular file: ", marker)
    assert marker.is_dir() and os.listdir(marker) == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no os.mkfifo on this platform")
def test_f2_a_fifo_at_the_marker_path_is_not_called_invalid_json_and_does_not_hang(tmp_path):
    home = tmp_path / "eg"
    home.mkdir()
    marker = home / "loop-mode.json"
    os.mkfifo(marker)
    r = _run_with_timeout(["state", '"x"'], home)  # TimeoutExpired here would be the hang
    _assert_one_line_refusal(r, home, ["loop-mode.json"], "marker path is not a readable regular file: ", marker)
    assert stat.S_ISFIFO(marker.lstat().st_mode)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="running as root: mode 000 does not make a file unreadable, so the cell cannot build its case")
def test_f2_an_unreadable_file_is_not_called_invalid_json(tmp_path):
    home = tmp_path / "eg"
    marker = _seed(home)
    marker.chmod(0o000)
    try:
        before = marker.lstat()
        r = _run(["state", '"x"'], home)
        _assert_one_line_refusal(r, home, ["loop-mode.json"], "marker path is not a readable regular file: ", marker)
        assert marker.lstat().st_mtime_ns == before.st_mtime_ns and stat.S_IMODE(marker.lstat().st_mode) == 0
    finally:
        marker.chmod(0o600)


# --- F4: the option rule, pinned ---------------------------------------------------------

def test_f4_only_double_dash_tokens_are_options(tmp_path):
    home = tmp_path / "eg"
    marker = _seed(home)
    assert _run(["-x", "1"], home).returncode == 0
    assert json.loads(marker.read_text())["-x"] == 1
    assert _run(["--string", "--", "--odd", "v"], home).returncode == 0
    assert json.loads(marker.read_text())["--odd"] == "v"
    r = _run(["--oops", "1"], home)
    assert r.returncode == 1 and "unknown option --oops" in r.stderr


def test_f4_module_docstring_states_the_option_rule():
    import ast
    doc = " ".join((ast.get_docstring(ast.parse(HELPER.read_text(encoding="utf-8"))) or "").split())
    assert "only `--`-prefixed tokens are options" in doc, doc
    assert "a key beginning with a single `-` is accepted as a key" in doc, doc
    assert "`--` ends option parsing" in doc, doc


# --- H12 (round 4): fence balance, CRLF, and whole-block allowances ----------------------

@pytest.mark.parametrize(
    "name,text",
    [
        ("unclosed fence", "```bash\nmv a b\n"),
        ("closing fence at a different indent", "  ```bash\nmv a b\n```\n"),
        ("closing fence indented more than the opener", "```bash\nmv a b\n    ```\n"),
        ("unclosed tilde fence", "~~~\nx\n"),
        ("second fence unclosed", "```\nx\n```\n\n```bash\ny\n"),
    ],
)
def test_h12_fence_problems_reports_unbalanced_or_misindented_fences(name, text):
    assert fence_problems(text), name


@pytest.mark.parametrize(
    "name,text",
    [
        ("plain pair", "```bash\nx\n```\n"),
        ("indented pair, same indent", "  ```bash\nx\n  ```\n"),
        ("tilde pair", "~~~\nx\n~~~\n"),
        ("tilde line inside a backtick fence is content, not an opener", "```\n~~~\nx\n```\n"),
        ("backtick line inside a tilde fence is content", "~~~\n```\nx\n~~~\n"),
        ("longer opener closed by an equal-or-longer fence", "````\n```\nx\n````\n"),
        ("no fences", "just prose\n"),
    ],
)
def test_h12_fence_problems_is_quiet_on_balanced_fences(name, text):
    assert fence_problems(text) == [], name


def test_h12_every_walked_file_has_balanced_fences():
    files = _scan_files()
    assert len(files) > 40, len(files)
    bad = [f"{rel}: {p}" for rel, text in files for p in fence_problems(text)]
    assert not bad, "unbalanced or misindented fences: the block scan cannot be trusted there:\n  " + "\n  ".join(bad)


def test_h12_crlf_text_is_normalized_when_a_file_is_read(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_bytes(_FENCED.format(_GUARD_FLAGS["variable-path"].replace("; ", "\n")).replace("\n", "\r\n").encode())
    assert b"\r\n" in p.read_bytes()
    text = _read_text(p)
    assert "\r" not in text
    assert replace_recipe_hits(text), text


def _codex_block_text():
    text = LOOP_SKILL.read_text(encoding="utf-8")
    (ln, verbs, body), = [b for b in flagged_blocks(text) if 'mv "$pending" "$marker"' in b[2]]
    return body, tuple(verbs)


def test_h12_allow_entries_carry_the_verb_set_they_cover():
    for rel, entries in ALLOW.items():
        for entry in entries:
            assert len(entry) == 3, entry
            sub, reason, verbs = entry
            assert isinstance(verbs, tuple) and verbs and all(isinstance(v, str) and v for v in verbs), entry


def test_h12_allowance_is_a_whole_block_key_not_a_substring_pass():
    body, verbs = _codex_block_text()
    allow = {"a.md": [('mv "$pending" "$marker"', "builds a new marker", verbs)]}
    assert audit([("a.md", _FENCED.format(body))], allow) == ([], [])
    grown = _FENCED.format(body + '\ncp /tmp/x "$marker"')
    unallowed, unmatched = audit([("a.md", grown)], allow)
    assert unallowed or unmatched, "a replace verb appended to an allowed block must not ride the allowance"
    assert unmatched and "cp onto the marker" in unmatched[0], unmatched
