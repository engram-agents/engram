"""Contract tests: setup never silently overwrites or deletes a user's file.

Written BEFORE the implementation. On the commit that adds this file most of these cells FAIL,
each for the contract reason it names; they pass once a fix implements the contract. Related
guards already exist (#4440 for CLAUDE.md, #4452 for AGENTS.md); this file pins the wider rule.

The rule: setup backs up first, asks, and acts only on the user's yes.

  C1. Protected = every existing file under ENGRAM_HOME, CLAUDE_HOME and CODEX_HOME that a run
      would overwrite, truncate, rename or delete, except a closed list of ENGRAM-owned artifacts.
  C2. Before the first write of the run there is a check-only pass. A differing existing
      protected file needs consent; an absent one is written; an identical one is left alone
      (no backup). Without consent the run REFUSES (exit != 0) BEFORE writing anything, and ONE
      refusal names EVERY path that needs consent.
  C3. Consent: `--replace PATH` (repeatable) consents for exactly that path; `--force` means
      consent for every listed path. A differing path that was not consented stays refused.
  C4. Every consented overwrite first copies the current bytes to
      `<name>.engram-backup.<UTC %Y%m%dT%H%M%SZ>` (a -N suffix if that name is taken). An
      existing `<name>.pre-engram` is used only when absent and is never modified.
  C5. Existing agent: if the home holds a non-empty knowledge.db, OR (knowledge.db absent or with
      0 nodes AND any of warm-briefing.md, seed-manifest.json, a non-empty history/ or diary/),
      bootstrap never seeds over it. Graph present: bootstrap writes nothing (the installer
      already skips bootstrap). Memory without a graph: bootstrap REFUSES even with --force,
      writes nothing, creates no knowledge.db and no first-run-pending, and the message names
      what it found and, if `<ENGRAM_HOME>/db-backup/` holds any `*.db`, the newest one.
  C6. The installer with no tty and no --yes never replaces an existing differing CLAUDE.md:
      DEFAULT_BACKUP_AND_REPLACE_CLAUDE_MD is 0.

Two MECHANICAL gates, so a write site added later is caught without anyone remembering:
  * test_differential_gate_no_consent builds its fixture FROM WHAT THE CODE WRITES: it runs
    bootstrap into a fresh home, records every regular file that run created, and plants a
    unique sentinel at each of those paths in a second home. A future write site shows up in the
    first run, so it is covered automatically.
  * test_static_write_sites_are_reviewed parses bootstrap.py and install.sh and lists every
    call that can overwrite, truncate, rename or delete a file. Each must sit inside a reviewed
    allowlist entry (with a one-line reason) or the test fails and names it.
A heuristic third gate, test_docs_never_tell_a_bare_delete, scans the agent-facing docs.

All runs use tmp homes through env; the real ~/.claude, ~/.codex and ~/.engram are never read or
written. Every "this content is gone / kept" assertion uses a unique SENTINEL string, and every
search for a sentinel is positive-controlled (the same search is run on a planted copy first),
because a broken search reads as "lost".
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bootstrap_no_clobber_4440 import (  # noqa: E402
    REPO_ROOT, RUNTIME_FILES, SRC, _db, _home, _run,
)
from test_install_sh_4097 import Sandbox  # noqa: E402

BOOTSTRAP_PY = SRC / "bootstrap.py"
INSTALL_SH = SRC / "tools" / "install.sh"
UNINSTALL_SH = SRC / "tools" / "uninstall-local-marketplace.sh"

BACKUP_SUFFIX_RE = re.compile(r"\.engram-backup\.\d{8}T\d{6}Z(-\d+)?$")


# ------------------------------------------------------------------------- helpers

def _sentinel(label: str, as_json: bool = False) -> bytes:
    token = f"SENTINEL-{label}-{uuid.uuid4().hex[:10]}"
    if as_json:  # a file the code parses: the sentinel must not make the fixture itself crash it
        return json.dumps({"sentinel": token, "note": "user content, keep it"}).encode()
    return f"{token}\nuser content, keep it\n".encode()


def _run_env(tmp_path: Path, target: str = "claude", args: list[str] | None = None,
             extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Same as the shared `_run`, plus extra env (the shared one pops FORCE_RESEED_EMPTY)."""
    _home(tmp_path)
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path),
        "ALPHA_DIR": str(REPO_ROOT),
        "ALPHA_TEMPLATES_DIR": str(SRC / "templates"),
        "ENGRAM_HOME": str(tmp_path / ".engram"),
        "CLAUDE_HOME": str(tmp_path / ".claude"),
        "CODEX_HOME": str(tmp_path / ".codex"),
        "PYTHON_BIN": sys.executable,
        "TARGET": target,
    })
    for k in ("ENGRAM_BOOTSTRAP", "FORCE", "FORCE_RESEED_EMPTY"):
        env.pop(k, None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(BOOTSTRAP_PY), *(args or [])],
        env=env, capture_output=True, text=True, timeout=300,
    )


def _text(r: subprocess.CompletedProcess) -> str:
    return r.stdout + r.stderr


def _names(text: str, path: Path) -> bool:
    return str(path) in text or str(path.resolve()) in text


def _make_empty_db(path: Path) -> None:
    """The aborted-prior-bootstrap state: a knowledge.db file with no tables, so 0 nodes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(str(path)).close()
    assert path.exists(), "the empty knowledge.db fixture was not created"


def _node_count(path: Path) -> int:
    con = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return 0
        raise
    finally:
        con.close()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _backups_of(path: Path) -> list[Path]:
    """Siblings named `<name>.engram-backup.<UTC stamp>[-N]`."""
    if not path.parent.is_dir():
        return []
    return sorted(
        p for p in path.parent.iterdir()
        if p.name.startswith(path.name) and BACKUP_SUFFIX_RE.search(p.name[len(path.name):])
    )


def _find_sentinel(roots: list[Path], needle: bytes) -> list[Path]:
    """Every regular file under `roots` whose bytes contain `needle`."""
    found = []
    for root in roots:
        for dirpath, _dirs, files in os.walk(root, followlinks=False):
            for name in files:
                p = Path(dirpath) / name
                if p.is_symlink():
                    continue
                try:
                    if needle in p.read_bytes():
                        found.append(p)
                except OSError:
                    pass
    return found


def _assert_search_works(tmp_path: Path, needle: bytes) -> None:
    """Positive control for _find_sentinel: plant the needle in a known file, find it."""
    control = tmp_path / "search-control"
    control.mkdir(exist_ok=True)
    (control / "planted.txt").write_bytes(b"prefix " + needle + b" suffix")
    assert _find_sentinel([control], needle) == [control / "planted.txt"], \
        "the sentinel search is broken (it cannot find a planted copy); a 'lost' verdict would be meaningless"


def _file_map(*roots: Path) -> dict[str, str]:
    """{label/relative path: sha256} for every regular (non-symlink) file under the roots."""
    out: dict[str, str] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(root, followlinks=False):
            for name in files:
                p = Path(dirpath) / name
                if p.is_symlink() or not p.is_file():
                    continue
                out[f"{root.name}/{p.relative_to(root).as_posix()}"] = _sha(p)
    return out


# --------------------------------------------------------------------- C5: existing agent

LETTER = _sentinel("warm-briefing-letter")


def _memory_without_graph(tmp_path: Path) -> Path:
    wb = tmp_path / ".engram" / "warm-briefing.md"
    wb.parent.mkdir(parents=True, exist_ok=True)
    wb.write_bytes(LETTER)
    return wb


def _assert_nothing_written_by_refusal(tmp_path: Path, r: subprocess.CompletedProcess, wb: Path) -> None:
    assert r.returncode != 0, "bootstrap must refuse to seed over an existing agent's memory:\n" + _text(r)
    assert wb.read_bytes() == LETTER, "warm-briefing.md was overwritten"
    assert not (tmp_path / ".engram" / "first-run-pending").exists()
    assert not (tmp_path / ".engram" / "seed-manifest.json").exists()
    assert "warm-briefing.md" in _text(r), "the refusal must name what it found:\n" + _text(r)


@pytest.mark.parametrize("cell", ["plain", "force_flag", "zero_node_db_with_reseed_env"])
def test_existing_warm_briefing_without_graph_is_refused_and_kept(tmp_path, cell):
    """C5: memory without a graph is never seeded over, even under --force / FORCE_RESEED_EMPTY."""
    wb = _memory_without_graph(tmp_path)
    db_before = None
    if cell == "plain":
        r = _run(tmp_path)
    elif cell == "force_flag":
        r = _run(tmp_path, args=["--force"])
    else:
        _make_empty_db(_db(tmp_path))
        db_before = _sha(_db(tmp_path))
        r = _run_env(tmp_path, extra_env={"FORCE_RESEED_EMPTY": "1"})
    _assert_nothing_written_by_refusal(tmp_path, r, wb)
    if cell == "zero_node_db_with_reseed_env":
        assert _sha(_db(tmp_path)) == db_before and _node_count(_db(tmp_path)) == 0, "the empty DB was seeded"
    else:
        assert not _db(tmp_path).exists(), "knowledge.db was created"


def test_refusal_names_newest_db_backup(tmp_path):
    """C5: when db-backup/ holds *.db files the refusal names the newest one."""
    wb = _memory_without_graph(tmp_path)
    older = tmp_path / ".engram" / "db-backup" / "knowledge-20261101.db"
    newer = tmp_path / ".engram" / "db-backup" / "knowledge-20261105.db"
    older.parent.mkdir(parents=True)
    older.write_bytes(b"older backup bytes")
    newer.write_bytes(b"newer backup bytes")
    os.utime(older, (1_000_000_000, 1_000_000_000))
    os.utime(newer, (1_100_000_000, 1_100_000_000))  # name order and mtime order agree
    r = _run(tmp_path)
    _assert_nothing_written_by_refusal(tmp_path, r, wb)
    msg = _text(r)
    assert "knowledge-20261105.db" in msg, "the refusal must name the newest db-backup file:\n" + msg
    assert "knowledge-20261101.db" not in msg, "only the newest backup should be named:\n" + msg
    assert older.read_bytes() == b"older backup bytes" and newer.read_bytes() == b"newer backup bytes"


# --------------------------------------------------------------------- C4: backups

def test_second_install_backs_up_current_identity_file(tmp_path):
    """C4: a second install must not lose the CURRENT CLAUDE.md just because .pre-engram exists.

    Today the first-ever backup (.pre-engram) is kept and the current file is overwritten with
    no copy anywhere, so the CURRENT sentinel is gone.
    """
    current = _sentinel("claude-md-CURRENT")
    old = _sentinel("claude-md-OLD")
    md = tmp_path / ".claude" / "CLAUDE.md"
    md.parent.mkdir()
    md.write_bytes(current)
    pre = tmp_path / ".claude" / "CLAUDE.md.pre-engram"
    pre.write_bytes(old)
    _assert_search_works(tmp_path, current)
    r = _run(tmp_path, args=["--force"])
    assert r.returncode == 0, _text(r)
    assert pre.read_bytes() == old, ".pre-engram is the first-ever backup and must never be modified"
    stamped = _backups_of(md)
    assert len(stamped) == 1, (
        f"expected exactly one CLAUDE.md.engram-backup.<UTC stamp> beside CLAUDE.md, found "
        f"{[p.name for p in stamped]}; siblings: {sorted(p.name for p in md.parent.iterdir())}"
    )
    assert stamped[0].read_bytes() == current, "the stamped backup must hold the CURRENT bytes"
    homes = [tmp_path / ".engram", tmp_path / ".claude", tmp_path / ".codex"]
    assert _find_sentinel(homes, current), "the CURRENT file's content survives nowhere under the homes"
    assert md.read_bytes() != current


@pytest.mark.parametrize("flags", [[], ["--force"]], ids=["no_flag", "force"])
def test_identical_file_is_not_backed_up(tmp_path, flags):
    """C2: an existing file identical to the render is left untouched and gets no backup.

    The identical file is the render of an earlier run into the same home path (the render
    embeds the home path, so the same tmp home is reused); the data home is then emptied, so
    this is not an existing-agent home. Today a CLAUDE.md with no seed manifest cannot be
    recognised as identical: it is refused (no flag) or backed up (--force).
    """
    first = _run(tmp_path)
    assert first.returncode == 0, _text(first)
    md = tmp_path / ".claude" / "CLAUDE.md"
    rendered = md.read_bytes()
    shutil.rmtree(tmp_path / ".engram")  # nothing of the earlier agent remains in the data home
    mtime_before = md.stat().st_mtime_ns
    r = _run(tmp_path, args=flags)
    assert r.returncode == 0, "an identical existing file must not need consent:\n" + _text(r)
    assert md.read_bytes() == rendered
    assert md.stat().st_mtime_ns == mtime_before, "an identical file must be left untouched"
    extra = sorted(p.name for p in md.parent.iterdir() if p.name != "CLAUDE.md")
    assert extra == [], f"no backup of any name may be made for an identical file, found {extra}"


# --------------------------------------------------------------------- C2 / C3: consent

def _codex_pair(tmp_path: Path) -> tuple[Path, Path, bytes, bytes]:
    compact = tmp_path / ".engram" / "codex-compact-prompt.md"
    agents = tmp_path / ".codex" / "AGENTS.md"
    compact.parent.mkdir(parents=True, exist_ok=True)
    agents.parent.mkdir(parents=True, exist_ok=True)
    c_bytes, a_bytes = _sentinel("codex-compact-prompt"), _sentinel("codex-agents-md")
    compact.write_bytes(c_bytes)
    agents.write_bytes(a_bytes)
    return compact, agents, c_bytes, a_bytes


def test_refusal_lists_every_path_needing_consent(tmp_path):
    """C2: ONE refusal names EVERY differing protected path (today: only AGENTS.md)."""
    compact, agents, c_bytes, a_bytes = _codex_pair(tmp_path)
    r = _run(tmp_path, target="codex")
    msg = _text(r)
    assert r.returncode != 0, msg
    assert compact.read_bytes() == c_bytes and agents.read_bytes() == a_bytes
    assert not _db(tmp_path).exists()
    assert msg.count("REFUSED") == 1, f"expected exactly one refusal:\n{msg}"
    assert _names(msg, agents), "the refusal does not name AGENTS.md:\n" + msg
    assert _names(msg, compact), "the refusal does not name codex-compact-prompt.md:\n" + msg


def test_replace_consents_for_exactly_one_path(tmp_path):
    """C3: `--replace PATH` consents for that path only; both paths consented -> backed up, written."""
    compact, agents, c_bytes, a_bytes = _codex_pair(tmp_path)
    one = _run(tmp_path, target="codex", args=["--replace", str(agents)])
    msg = _text(one)
    assert "unknown argument" not in msg, "--replace is not a recognised option:\n" + msg
    assert one.returncode != 0, "the compact prompt was not consented, so the run must refuse:\n" + msg
    assert compact.read_bytes() == c_bytes and agents.read_bytes() == a_bytes, "a file changed under a refusal"
    assert not _db(tmp_path).exists()
    assert _names(msg, compact), "the refusal must name the path that still needs consent:\n" + msg
    assert _backups_of(agents) == [] and _backups_of(compact) == []

    both = _run(tmp_path, target="codex", args=["--replace", str(agents), "--replace", str(compact)])
    assert both.returncode == 0, _text(both)
    for path, old in ((agents, a_bytes), (compact, c_bytes)):
        stamped = _backups_of(path)
        assert len(stamped) == 1, f"{path.name}: expected one engram-backup, found {[p.name for p in stamped]}"
        assert stamped[0].read_bytes() == old
        assert path.read_bytes() != old, f"{path.name} was not written after consent"


# --------------------------------------------------------------------- C1 / C2: differential gate

_SEEDED: dict[str, dict] = {}


def _seeded_reference(tmp_path_factory, target: str) -> dict:
    """Run bootstrap once into a fresh home; record every file that run created."""
    if target in _SEEDED:
        return _SEEDED[target]
    home = tmp_path_factory.mktemp(f"reference-{target}")
    r = _run(home, target=target)
    assert r.returncode == 0, _text(r)
    target_dir = ".claude" if target == "claude" else ".codex"
    # ENGRAM-owned artifacts (the closed list): the runtime copies, the graph and its sidecars, and
    # principle_triggers.json, a cache the server rebuilds from the graph whenever it creates one.
    owned_top = set(RUNTIME_FILES) | {"knowledge.db", "knowledge.db-wal", "knowledge.db-shm",
                                      "principle_triggers.json"}
    recorded: list[str] = []
    for sub in (".engram", target_dir):
        for dirpath, _dirs, files in os.walk(home / sub, followlinks=False):
            for name in files:
                p = Path(dirpath) / name
                if p.is_symlink():
                    continue
                parts = p.relative_to(home).parts
                if parts[0] == ".engram":
                    if len(parts) == 2 and parts[1] in owned_top:
                        continue
                    if parts[1] == "db-backup" or parts[1].startswith("knowledge.db."):
                        continue
                recorded.append("/".join(parts))
    recorded.sort()
    # Positive control: the recording sees the writes we know bootstrap makes.
    expected = {".engram/warm-briefing.md", ".engram/seed-manifest.json", ".engram/first-run-pending"}
    expected.add(".claude/CLAUDE.md" if target == "claude" else ".codex/AGENTS.md")
    assert expected <= set(recorded), f"the reference run did not record {expected - set(recorded)}; recorded {recorded}"
    _SEEDED[target] = {"home": home, "recorded": recorded, "target_dir": target_dir}
    return _SEEDED[target]


@pytest.mark.parametrize("identity_files", ["present", "absent"])
@pytest.mark.parametrize("db_state", ["absent", "zero_nodes", "seeded_copy"])
@pytest.mark.parametrize("target", ["claude", "codex"])
def test_differential_gate_no_consent(tmp_path, tmp_path_factory, target, db_state, identity_files):
    """C1/C2: with NO flags, no file a run would write may change when it already exists.

    Home B holds a unique sentinel at every path a fresh run writes (see the module docstring).
    `identity_files=absent` leaves the target-side files (CLAUDE.md / AGENTS.md / config.toml)
    out: those already have an early refusal, which would otherwise hide every other write site
    behind it. The exit status may be anything; the bytes may not change.
    """
    ref = _seeded_reference(tmp_path_factory, target)
    planted: dict[Path, bytes] = {}
    for rel in ref["recorded"]:
        if identity_files == "absent" and not rel.startswith(".engram/"):
            continue
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        data = _sentinel(rel.replace("/", "_"), as_json=rel.endswith(".json"))
        p.write_bytes(data)
        planted[p] = data
    assert planted, "nothing was planted"
    if db_state == "zero_nodes":
        _make_empty_db(_db(tmp_path))
    elif db_state == "seeded_copy":
        src = sqlite3.connect(str(ref["home"] / ".engram" / "knowledge.db"))
        dst = sqlite3.connect(str(_db(tmp_path)))
        src.backup(dst)
        src.close()
        dst.close()
    r = _run(tmp_path, target=target)
    changed = [str(p.relative_to(tmp_path)) for p, data in planted.items()
               if not p.exists() or p.read_bytes() != data]
    assert not changed, (
        f"a run with no consent changed {len(changed)} existing file(s): {changed}\n"
        f"(exit {r.returncode}) output tail:\n{_text(r)[-600:]}"
    )


# --------------------------------------------------------------------- static write-site gate

_PY_WRITE_METHODS_ANY_RECEIVER = {"write_text", "write_bytes", "symlink_to"}
_PY_SHUTIL = {"rmtree", "move", "copy", "copy2", "copyfile", "copytree"}
_PY_OS = {"replace", "rename", "remove", "unlink", "link", "symlink", "truncate", "removedirs", "rmdir"}
_WRITE_MODE_RE = re.compile(r"[wax+]")
_OS_OPEN_WRITE_RE = re.compile(r"O_(WRONLY|RDWR|CREAT|TRUNC|APPEND)")

# Reviewed write sites that the contract KEEPS. Anything else in bootstrap.py is a violation:
# it must go through the consent/backup helper instead. `call=None` allows the whole function.
BOOTSTRAP_ALLOWLIST = [
    {"func": "_copy_exclusive", "call": None,
     "reason": "exclusive create of a NEW backup/copy: mkstemp temp + os.link publish, never overwrites dst"},
    {"func": "_atomic_write_text", "call": None,
     "reason": "settings.json merge the user asked for with --retention-days: temp file + fsync + os.replace"},
    {"func": "main", "call": "engram_tools_link.unlink()",
     "reason": "removes the ENGRAM-made plugin symlink only (guarded by is_symlink())"},
    {"func": "main", "call": "engram_tools_link.rename(backup)",
     "reason": "moves an existing scatter-install dir aside to tools.scatter-backup, nothing deleted"},
    {"func": "main", "call": "engram_tools_link.symlink_to(plugin_tools)",
     "reason": "creates the plugin tools link after the old entry was removed or moved aside"},
    {"func": "main", "call": "engram_hooks_link.unlink()",
     "reason": "removes the ENGRAM-made plugin symlink only (guarded by is_symlink())"},
    {"func": "main", "call": "engram_hooks_link.rename(backup)",
     "reason": "moves an existing scatter-install dir aside to hooks.scatter-backup, nothing deleted"},
    {"func": "main", "call": "engram_hooks_link.symlink_to(plugin_hooks)",
     "reason": "creates the plugin hooks link after the old entry was removed or moved aside"},
]


def _py_write_sites(source: str) -> list[tuple[int, str, str]]:
    """(line, enclosing function, call text) for every call that can write, truncate, rename or delete."""
    tree = ast.parse(source)
    hits: list[tuple[int, str, str]] = []

    def receiver_name(func: ast.Attribute) -> str | None:
        return func.value.id if isinstance(func.value, ast.Name) else None

    def mode_is_write(call: ast.Call, positional_index: int) -> bool:
        mode = None
        if len(call.args) > positional_index:
            mode = call.args[positional_index]
        for kw in call.keywords:
            if kw.arg == "mode":
                mode = kw.value
        if mode is None:
            return False
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
            return bool(_WRITE_MODE_RE.search(mode.value))
        return True  # a mode we cannot read statically is not provably read-only

    def is_write(call: ast.Call) -> bool:
        f = call.func
        if isinstance(f, ast.Name):
            return f.id == "open" and mode_is_write(call, 1)
        if not isinstance(f, ast.Attribute):
            return False
        recv, attr = receiver_name(f), f.attr
        if attr in _PY_WRITE_METHODS_ANY_RECEIVER:
            return True
        if attr == "unlink":
            return True
        if recv == "shutil" and attr in _PY_SHUTIL:
            return True
        if recv == "os" and attr in _PY_OS:
            return True
        if recv == "os" and attr == "fdopen":
            return mode_is_write(call, 1)
        if recv == "os" and attr == "open":
            return bool(_OS_OPEN_WRITE_RE.search(ast.unparse(call)))
        if attr in ("replace", "rename") and recv != "os" and len(call.args) == 1 and not call.keywords:
            return True  # Path.replace(target) / Path.rename(target); str.replace takes two arguments
        if attr == "open" and recv != "os":
            return mode_is_write(call, 0)  # Path.open(mode)
        return False

    def walk(node: ast.AST, func: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if isinstance(child, ast.Call) and is_write(child):
                text = ast.unparse(child)
                hits.append((child.lineno, name, text if len(text) <= 90 else text[:87] + "..."))
            walk(child, name)

    walk(tree, "<module>")
    return hits


def _py_violations(source: str, allowlist: list[dict]) -> list[tuple[int, str, str]]:
    out = []
    for line, func, text in _py_write_sites(source):
        ok = any(a["func"] == func and (a["call"] is None or a["call"] == text) for a in allowlist)
        if not ok:
            out.append((line, func, text))
    return out


_PLANTED_PY = '''
import os, shutil
from pathlib import Path
def planted(p, q, s):
    p.write_text("x")
    p.write_bytes(b"x")
    open(p, "w")
    open(p, mode="a")
    Path(p).open("w")
    os.replace(p, q)
    os.rename(p, q)
    p.rename(q)
    p.replace(q)
    p.unlink()
    os.remove(p)
    shutil.rmtree(p)
    shutil.move(p, q)
    shutil.copy(p, q)
    shutil.copy2(p, q)
    shutil.copyfile(p, q)
    shutil.copytree(p, q)
    p.symlink_to(q)
def quiet(p, s):
    open(p)
    open(p, "rb")
    s.replace("a", "b")
    d = dict()
    d.copy()
    p.read_text()
'''


def _assert_python_scanner_controls() -> None:
    hits = _py_write_sites(_PLANTED_PY)
    assert len(hits) == 18, f"the AST scanner is broken: found {len(hits)} of 18 planted write sites: {hits}"
    assert all(func == "planted" for _l, func, _t in hits), "the scanner flagged a read-only call in quiet()"


_PROTECTED_VAR_RE = re.compile(
    r"\$\{?(?:CLAUDE_MD_BACKUP|CLAUDE_MD|DB_PATH|CONFIG_PATH)(?![A-Za-z0-9_])"
    r"|\$\{?ENGRAM_HOME\}?/(?!(?:marketplace|venv|install-logs|\.deployed-version)(?:/|$|[^A-Za-z0-9_.-]))[^\s\"']+"
    r"|\$\{?(?:CLAUDE_HOME|CODEX_HOME)\}?/[^\s\"']+"
)

# Reviewed shell lines that touch files but not a user's protected file. (They do not expand a
# protected variable, so the scanner would not flag them anyway; the entries record the review.)
INSTALL_SH_ALLOWLIST = [
    ('rm -rf "$WORK_DIR"', "the installer's own scratch directory"),
    ('cp -R "$WORK_DIR/." "$LOG_SAVE_DIR/"', "saves the failed run's log into $ENGRAM_HOME/install-logs (ENGRAM-owned)"),
]


def _mask_shell(text: str) -> tuple[str, str | None]:
    """Blank out quoted strings and comments (keeping length and newlines). Returns (masked, open quote)."""
    out: list[str] = []
    i, n, q = 0, len(text), None
    while i < n:
        c = text[i]
        if q is None:
            if c == "\\" and i + 1 < n:
                out.append(c + text[i + 1])
                i += 2
                continue
            if c == "#" and (i == 0 or text[i - 1] in " \t\n;&|("):
                j = text.find("\n", i)
                j = n if j == -1 else j
                out.append(" " * (j - i))
                i = j
                continue
            if c in "'\"":
                q = c
            out.append(c)
            i += 1
            continue
        if q == '"' and c == "\\" and i + 1 < n:
            out.append(" " + ("\n" if text[i + 1] == "\n" else " "))
            i += 2
            continue
        if c == q:
            q = None
            out.append(c)
        else:
            out.append("\n" if c == "\n" else " ")
        i += 1
    return "".join(out), q


_SHELL_CMD_RE = re.compile(r"(?<![\w./$-])(cp|mv|rm)(?=\s)")


def _sh_write_sites(text: str) -> list[tuple[int, str]]:
    """(line, command text) for cp/mv/rm outside quotes whose target expands a protected variable."""
    masked, open_quote = _mask_shell(text)
    assert open_quote is None, "the shell scanner lost track of quotes; its result would be meaningless"
    hits = []
    for lineno, (orig, mline) in enumerate(zip(text.split("\n"), masked.split("\n")), 1):
        for m in _SHELL_CMD_RE.finditer(mline):
            prefix = mline[: m.start()].rstrip()
            at_command = (
                prefix == "" or prefix[-1] in ";&|({`!"
                or re.search(r"(?:^|\s)(?:then|do|else|elif|if|while|until)$", prefix)
            )
            if not at_command:
                continue
            tail = mline[m.start():]
            end = len(tail)
            for t in (";", "&&", "||", "|"):
                k = tail.find(t)
                if k != -1:
                    end = min(end, k)
            seg = orig[m.start(): m.start() + end].rstrip().rstrip("\\").strip()
            try:
                tokens = shlex.split(seg)
            except ValueError:
                tokens = seg.split()
            args = [t for t in tokens[1:] if not (t.startswith("-") and len(t) > 1)]
            cmd = tokens[0]
            if cmd == "cp":
                targets = args[-1:]
            else:  # mv removes its source, rm removes everything it is given
                targets = args
            if any(_PROTECTED_VAR_RE.search(t) for t in targets):
                hits.append((lineno, seg))
    return hits


_PLANTED_SH = '''#!/usr/bin/env bash
cp -p "$CLAUDE_MD" "$CLAUDE_MD_BACKUP"
x=1; mv "$CONFIG_PATH" /tmp/x
test -e a && rm -f "$DB_PATH"
if rm "$ENGRAM_HOME/warm-briefing.md"; then :; fi
cp "$CLAUDE_MD" /tmp/copy-only-reads-the-protected-file
rm -rf "$ENGRAM_HOME/venv"
fail "remove the link (rm '$CLAUDE_MD') then re-run"   # quoted: message text, not a command
# rm "$CLAUDE_MD"  <- a comment
'''


def _assert_shell_scanner_controls() -> None:
    hits = _sh_write_sites(_PLANTED_SH)
    assert [ln for ln, _ in hits] == [2, 3, 4, 5], f"the shell scanner is broken; planted-script hits: {hits}"


def test_static_write_sites_are_reviewed():
    """Static gate: every file-writing call in bootstrap.py / install.sh is a reviewed, kept site.

    The allowlist holds only the sites the contract KEEPS; the helper that implements consent and
    backup will live in another module, so none of its calls appear here. Today's violations are
    the plain write_text calls (template renders, seed manifest, first-run marker, codex compact
    prompt, config.toml merge) and install.sh's `cp -p "$CLAUDE_MD" "$CLAUDE_MD_BACKUP"`.
    """
    _assert_python_scanner_controls()
    _assert_shell_scanner_controls()
    py_bad = _py_violations(BOOTSTRAP_PY.read_text(), BOOTSTRAP_ALLOWLIST)
    sh_bad = _sh_write_sites(INSTALL_SH.read_text())
    problems = [f"src/engram/bootstrap.py:{ln}: {text}   (in {func})" for ln, func, text in py_bad]
    problems += [f"src/engram/tools/install.sh:{ln}: {text}" for ln, text in sh_bad]
    assert not problems, (
        "file-writing calls outside the reviewed allowlist (each must go through the consent and "
        "backup helper, or be reviewed and added to the allowlist with a reason):\n  " + "\n  ".join(problems)
    )


# --------------------------------------------------------------------- C6: installer default

def test_install_sh_never_defaults_to_yes(tmp_path):
    """C6: no tty and no --yes must not replace an existing differing CLAUDE.md."""
    text = INSTALL_SH.read_text()
    m = re.search(r"^DEFAULT_BACKUP_AND_REPLACE_CLAUDE_MD=(\d+)", text, re.MULTILINE)
    assert m, "DEFAULT_BACKUP_AND_REPLACE_CLAUDE_MD is not defined in install.sh"
    assert m.group(1) == "0", f"DEFAULT_BACKUP_AND_REPLACE_CLAUDE_MD={m.group(1)}; with no one to ask it must be 0"

    # Behavioural cell: the installer harness with its fakes. stdin is /dev/null and stdout is a
    # pipe, so there is no tty; no --yes is given.
    sb = Sandbox(tmp_path)
    sb.claude_home.mkdir(parents=True)
    mine = _sentinel("installer-claude-md")
    md = sb.claude_home / "CLAUDE.md"
    md.write_bytes(mine)
    r = sb.run()
    assert md.read_bytes() == mine, "the installer replaced CLAUDE.md without anyone saying yes"
    assert r.returncode != 0
    assert "FAIL" in r.all, "the refusal must be a FAIL line:\n" + r.all


# --------------------------------------------------------------------- whole-flow cell

def _installer_with_real_bootstrap(tmp_path: Path) -> Sandbox:
    """The installer harness (fake claude/pip/network/marketplace) running the REAL bootstrap.py.

    The harness's own bootstrap is a fake; here it is replaced by a thin wrapper that execs the
    repo's bootstrap.py, with the real templates beside it, so step 6 decides and bootstrap acts
    exactly as they do in a real install.
    """
    sb = Sandbox(tmp_path)
    shutil.copytree(SRC / "templates", sb.plugin_src / "templates", dirs_exist_ok=True)
    (sb.plugin_src / "bootstrap.py").write_text(
        "import os, sys\n"
        "open(os.environ['CALLS_LOG'], 'a').write('bootstrap (real) ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        f"os.execv(sys.executable, [sys.executable, {str(BOOTSTRAP_PY)!r}, *sys.argv[1:]])\n"
    )
    return sb


def _existing_agent_home(sb: Sandbox, with_graph: bool) -> dict[Path, bytes]:
    sentinel_files = {
        sb.engram / "warm-briefing.md": _sentinel("letter"),
        sb.engram / "history" / "2026-11-01.md": _sentinel("history"),
        sb.engram / "diary" / "x.md": _sentinel("diary"),
        sb.claude_home / "CLAUDE.md": _sentinel("claude-md"),
    }
    cfg = (b'{"self_lineage": "anthropic:opus", "install_tier": "convenience", '
           + f'"note": "{uuid.uuid4().hex[:10]}"}}'.encode())
    sentinel_files[sb.engram / "config.json"] = cfg
    for path, data in sentinel_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    sb.make_db(4)
    con = sqlite3.connect(str(sb.engram / "knowledge.db"))
    con.execute("INSERT INTO nodes VALUES ('SENTINEL-node')")
    con.commit()
    con.close()
    if not with_graph:
        away = sb.root / "moved-away"
        away.mkdir()
        shutil.move(str(sb.engram / "knowledge.db"), str(away / "knowledge.db"))
    return sentinel_files


def _allowed_change(rel: str, config_tier_changed: bool) -> bool:
    """ENGRAM-owned artifacts a run may rewrite: marketplace/, .deployed-version, install-logs/, venv/."""
    label, _, inner = rel.partition("/")
    if label != ".engram":
        return False
    top = inner.split("/")[0]
    if top in ("marketplace", "install-logs", "venv") or inner == ".deployed-version":
        return True
    return inner == "config.json" and config_tier_changed


@pytest.mark.parametrize("with_graph", [True, False], ids=["graph_present", "knowledge_db_moved_away"])
def test_reinstall_over_existing_home_keeps_the_old_agent(tmp_path, with_graph):
    """An install from scratch over an existing home: the old agent's memory is still there and
    nothing was replaced without a recorded yes.

    Run: the installer end to end (harness fakes for claude/pip/network/marketplace, the REAL
    bootstrap.py), non-interactive, no --yes. Cell 1 has a graph (the installer skips bootstrap
    today, so this cell may already pass). Cell 2 moves knowledge.db away: memory without a graph,
    which the installer must refuse.
    """
    sb = _installer_with_real_bootstrap(tmp_path)
    sentinel_files = _existing_agent_home(sb, with_graph)
    codex_home = sb.home / ".codex"
    before = _file_map(sb.engram, sb.claude_home, codex_home)
    assert any(k.endswith("warm-briefing.md") for k in before), "the before-map misses the planted files"
    r = sb.run()

    if with_graph:
        assert r.returncode == 0, "an existing agent with a graph must install cleanly:\n" + r.all
        assert _node_count(sb.engram / "knowledge.db") == 5, "the node count changed"
        con = sqlite3.connect(str(sb.engram / "knowledge.db"))
        assert con.execute("SELECT COUNT(*) FROM nodes WHERE id='SENTINEL-node'").fetchone()[0] == 1
        con.close()
    else:
        assert r.returncode != 0, "memory without a graph must be refused by the installer:\n" + r.all
        assert not (sb.engram / "knowledge.db").exists(), "a graph was seeded over the old agent's memory"
    assert not (sb.engram / "first-run-pending").exists(), "the first-run marker was armed over an existing agent"
    for path, data in sentinel_files.items():
        assert path.exists() and path.read_bytes() == data, f"{path.name} was replaced"

    after = _file_map(sb.engram, sb.claude_home, codex_home)
    cfg_after = sb.engram / "config.json"
    cfg_before = sentinel_files[cfg_after]
    tier_changed = (not cfg_after.exists()) or \
        json.loads(cfg_after.read_bytes()).get("install_tier") != json.loads(cfg_before).get("install_tier")
    replaced = sorted(
        rel for rel, digest in before.items()
        if after.get(rel) != digest and not _allowed_change(rel, tier_changed)
    )
    assert not replaced, f"files present before the run were replaced or deleted: {replaced}"


# --------------------------------------------------------------------- docs gate (heuristic)

_FENCE_RE = re.compile(r"^\s*(?:>\s*)*(```|~~~)")
_CMD_SPLIT_RE = re.compile(r"&&|\|\||;|\|")
_PROTECTED_NAME_RE = re.compile(
    r"(?:^|/)(?:knowledge\.db(?:-wal|-shm)?|warm-briefing\.md|CLAUDE\.md|AGENTS\.md|config\.toml)$"
)
_PROTECTED_DIR_RE = re.compile(r"(?:^|/)(?:history|diary)(?:/\*?)?$")
_PROTECTED_HOME_RE = re.compile(
    r"^(?:~|\$HOME|\$\{HOME\})/\.engram/?\*?$|^\$ENGRAM_HOME/?\*?$|^\$\{ENGRAM_HOME(?::-[^}]*)?\}/?\*?$"
)
_BACKUP_WORD_RE = re.compile(r"(?<![A-Za-z0-9-])backup(?![A-Za-z0-9/-])", re.IGNORECASE)
_NEGATED_DELETE_RE = re.compile(r"\b(?:do not|don't|never|must not|avoid)\b(?:(?!\.\s)[^;:])*\bdelet(?:e|ing)\b", re.IGNORECASE)
_PROSE_BACKUP_RE = re.compile(r"\bback(?:ed|ing)?[ -]?up\b", re.IGNORECASE)


def _protected_key(token: str) -> tuple[str, ...] | None:
    """Substrings that identify the protected path a token names, or None if it is not protected."""
    if _PROTECTED_HOME_RE.match(token):
        return (".engram", "ENGRAM_HOME")
    m = _PROTECTED_NAME_RE.search(token)
    if m:
        name = m.group(0).lstrip("/")
        return ("knowledge.db",) if name.startswith("knowledge.db") else (name,)
    m = _PROTECTED_DIR_RE.search(token)
    if m:
        return (m.group(0).strip("/*").lstrip("/"),)
    return None


def _command_tokens(seg: str) -> list[str]:
    lex = shlex.shlex(seg, posix=True)
    lex.whitespace_split = True
    lex.commenters = "#"
    try:
        return list(lex)
    except ValueError:
        return seg.split()


def _code_segments(line: str, in_fence: bool) -> list[str]:
    """Shell-command segments of one doc line: the whole line in a fence, backtick spans otherwise."""
    pieces = [line] if in_fence else re.findall(r"`([^`]+)`", line)
    segs: list[str] = []
    for piece in pieces:
        for seg in _CMD_SPLIT_RE.split(piece):
            seg = seg.strip()
            if seg and not seg.startswith("#"):
                segs.append(seg)
    return segs


def _is_backup_evidence(seg: str, key: tuple[str, ...]) -> bool:
    toks = _command_tokens(seg)
    if not toks:
        return False
    if toks[0] in ("cp", "mv", "tar", "rsync") and any(k in t for t in toks[1:] for k in key):
        return True
    # "the literal word backup in a command": needs arguments, so a bare path span is not a command
    return len(toks) > 1 and bool(_BACKUP_WORD_RE.search(seg))


def _doc_hits(text: str) -> list[tuple[int, str]]:
    lines = text.split("\n")
    in_fence_at: list[bool] = []
    fenced = False
    for ln in lines:
        if _FENCE_RE.match(ln):
            in_fence_at.append(True)
            fenced = not fenced
        else:
            in_fence_at.append(fenced)
    segs_by_line = [_code_segments(ln, in_fence_at[i]) for i, ln in enumerate(lines)]

    def window_has_evidence(i: int, key: tuple[str, ...], skip: str | None, prose_ok: bool) -> bool:
        for j in range(max(0, i - 6), i + 1):
            for seg in segs_by_line[j]:
                if j == i and seg == skip:
                    continue
                if _is_backup_evidence(seg, key):
                    return True
            if prose_ok and _PROSE_BACKUP_RE.search(lines[j]):
                return True
        return False

    hits: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        flagged = False
        for seg in segs_by_line[i]:
            toks = _command_tokens(seg)
            if not toks or toks[0] not in ("rm", "cp"):
                continue
            args = [t for t in toks[1:] if not (t.startswith("-") and len(t) > 1)]
            targets = args if toks[0] == "rm" else args[-1:]
            keys = [k for k in (_protected_key(t) for t in targets) if k]
            if keys and not window_has_evidence(i, keys[0], seg, prose_ok=False):
                hits.append((i + 1, line.strip()))
                flagged = True
                break
        if flagged:
            continue
        words = re.sub(r"[^A-Za-z0-9_.\-]+", " ", line).split()
        db_at = [k for k, w in enumerate(words) if "knowledge.db" in w.lower()]
        del_at = [k for k, w in enumerate(words) if w.lower() in ("delete", "deleting")]
        if any(abs(a - b) <= 5 for a in db_at for b in del_at) and not _NEGATED_DELETE_RE.search(line):
            if not window_has_evidence(i, ("knowledge.db",), None, prose_ok=True):
                hits.append((i + 1, line.strip()))
    return hits


def _uninstall_header() -> str:
    out = []
    for ln in UNINSTALL_SH.read_text().split("\n")[1:]:
        if not ln.startswith("#"):
            break
        out.append(ln[1:])
    return "\n".join(out)


_PLANTED_DOC = """\
Intro text.

```bash
# take a copy first
cp -a ~/.engram ~/.engram-copy
rm -rf ~/.engram
```

{filler}

```bash
rm -rf ~/.engram   # HIT
rm ~/.claude/CLAUDE.md   # HIT
```

{filler}

Reset: `rm knowledge.db` and start again (HIT). Read `ls knowledge.db` first.

{filler}

Fresh start means deleting `knowledge.db` outright (HIT).
Never delete `knowledge.db` to refresh anything; that is a warning, not an instruction.
Run `rm -f "${{ENGRAM_HOME:-$HOME/.engram}}"/loop-mode.json` to clear the marker.
""".format(filler="\n".join(["Filler line."] * 8))


def _assert_doc_scanner_controls() -> None:
    lines = _PLANTED_DOC.split("\n")
    expected = [i + 1 for i, ln in enumerate(lines) if "HIT" in ln]
    hits = [ln for ln, _ in _doc_hits(_PLANTED_DOC)]
    assert len(expected) == 4 and hits == expected, \
        f"the docs scanner is broken; planted hits at lines {expected}, scanner found {hits}"


def test_docs_never_tell_a_bare_delete():
    """HEURISTIC gate over agent-facing docs: a delete or overwrite of a protected path needs a backup.

    Scans README-AGENT.md, README.md, USER_GUIDE.md, every skills/**/SKILL.md, the templates and
    the uninstall script's header comment. A hit is a shell `rm`, or a `cp` whose destination is a
    protected path (~/.engram as a whole, knowledge.db and its -wal/-shm, warm-briefing.md,
    CLAUDE.md, AGENTS.md, config.toml, history/, diary/), written in a fenced block or inline
    code, with no backup command (cp/mv/tar/rsync of the same path, or the word "backup" in a
    command) in the 6 preceding lines. Prose "delete"/"deleting" within 5 words of knowledge.db is
    also a hit, unless a backup command or the words "back up" appear in the same 7-line window, or
    the same clause is a warning ("do not / never ... delete").
    Heuristic: it reads lines, not meaning, so a flagged line may need rewording rather than
    a new command.
    """
    _assert_doc_scanner_controls()
    docs = [REPO_ROOT / "README-AGENT.md", REPO_ROOT / "README.md", REPO_ROOT / "USER_GUIDE.md"]
    docs += sorted((SRC / "skills").rglob("SKILL.md"))
    docs += sorted(p for p in (SRC / "templates").rglob("*") if p.is_file())
    found: list[str] = []
    for doc in docs:
        try:
            text = doc.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        rel = doc.relative_to(REPO_ROOT).as_posix()
        found += [f"{rel}:{ln}: {line[:110]}" for ln, line in _doc_hits(text)]
    found += [f"src/engram/tools/uninstall-local-marketplace.sh (header):{ln}: {line[:110]}"
              for ln, line in _doc_hits(_uninstall_header())]
    assert not found, (
        "docs that tell the reader to delete or overwrite a protected path with no backup step "
        "in the 6 lines before:\n  " + "\n  ".join(found)
    )
