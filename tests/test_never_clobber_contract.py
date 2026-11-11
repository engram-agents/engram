"""Contract tests: setup never silently overwrites or deletes a user's file.

Written BEFORE the implementation, each cell failing for the contract reason it names until the
contract is implemented. Related guards exist (#4440 for CLAUDE.md, #4452 for AGENTS.md); this
file pins the wider rule.

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
      0 nodes AND any of warm-briefing.md, seed-manifest.json, a non-empty history/ or diary/;
      a folder holding only dotfiles such as .DS_Store is empty for this purpose),
      bootstrap never seeds over it. Graph present: bootstrap writes nothing (the installer
      already skips bootstrap). Memory without a graph: bootstrap REFUSES even with --force,
      writes nothing, creates no knowledge.db and no first-run-pending, and the message names
      what it found and, if `<ENGRAM_HOME>/db-backup/` holds any `*.db`, the newest one.
  C6. The installer with no tty and no --yes never replaces an existing differing CLAUDE.md:
      DEFAULT_BACKUP_AND_REPLACE_CLAUDE_MD is 0.
  C7. Backup fidelity. A backup promises the file's BYTES, its permission MODE and its
      modification time. It does NOT promise extended attributes or ACLs (the copy is
      shutil.copyfileobj + shutil.copystat, which drops them on macOS). Backups accumulate: every
      replace adds one, and nothing deletes them automatically; the user-facing text says so.
  C8. Obstructed first-backup name. If `<name>.pre-engram` resolves to the same file as the
      identity file itself (a hard link, or a symlink to it), replacing the file would overwrite
      that backup too, so the run REFUSES (even with --force), writes nothing, and the one
      refusal names the remedy: move that backup name aside. A dangling link or a directory at
      that name is a different case: it is left alone, the stamped backup is written, and the
      run proceeds on consent.
  C9. Consent is compared on resolved paths: the parent directory is resolved and the name is
      kept as written (a symlink AT the path is not followed into its target). The refusal
      prints that one canonical form. A second spelling of the same path (a symlinked folder,
      /var vs /private/var, a relative path) counts as the same path.

MERGE class (a JSON file where ENGRAM owns a few keys and the user owns everything else):
  M1. The owned keys are ONE closed, named list per file, in engram_file_guard.OWNED_KEYS:
      settings.json -> {cleanupPeriodDays}; config.json -> {install_tier, forum.url (the key
      `url` inside the object `forum`)}. A key not on the list is never ENGRAM's to change.
  M2. A merge keeps every other key, in value and in order. The tests compare the non-owned part
      BYTE FOR BYTE on its re-serialization with the file's own indent (the merge re-serializes
      the whole file, so the raw file bytes differ by the owned entry) and also key by key; each
      test says which.
  M3. A file that does not parse (or is not a JSON object) is NOT a merge candidate: nothing
      repairs a broken user file. The merge refuses and the file is left untouched.
  M4. A stamped backup is taken before a merge that changes the file. A merge that leaves the
      file byte-identical writes nothing and backs up nothing.
  The three config.json writers (install.sh step 3, install-local-marketplace.sh's python block,
  set_forum_url.py) are still their own writers; the static gate lists them as "slice 2: MERGE
  backup".

Two MECHANICAL gates, so a write site added later is caught without anyone remembering:
  * test_differential_gate_no_consent builds its fixture FROM WHAT THE CODE WRITES: it runs
    bootstrap into a fresh home, records every regular file that run created, and plants a
    unique sentinel at each of those paths in a second home. A future write site shows up in the
    first run, so it is covered automatically.
  * test_static_write_sites_are_reviewed parses bootstrap.py, install.sh (the shell AND the
    Python heredoc blocks PY_* inside it) and set_forum_url.py, and lists every call that can
    overwrite, truncate, rename or delete a file. Each must sit inside a reviewed allowlist entry
    (with a one-line reason) or the test fails and names it. An entry covers ONE occurrence: a
    second identical call in the same function is a new site.
A heuristic third gate, test_docs_never_tell_a_bare_delete, scans the agent-facing docs.

All runs use tmp homes through env; the real ~/.claude, ~/.codex and ~/.engram are never read or
written. Every "this content is gone / kept" assertion uses a unique SENTINEL string, and every
search for a sentinel is positive-controlled (the same search is run on a planted copy first),
because a broken search reads as "lost".
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
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

USER_AUTHORED = _sentinel("warm-briefing-user-authored")


def _memory_without_graph(tmp_path: Path) -> Path:
    wb = tmp_path / ".engram" / "warm-briefing.md"
    wb.parent.mkdir(parents=True, exist_ok=True)
    wb.write_bytes(USER_AUTHORED)
    return wb


def _assert_nothing_written_by_refusal(tmp_path: Path, r: subprocess.CompletedProcess, wb: Path) -> None:
    assert r.returncode != 0, "bootstrap must refuse to seed over an existing agent's memory:\n" + _text(r)
    assert wb.read_bytes() == USER_AUTHORED, "warm-briefing.md was overwritten"
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

    The first-ever backup (.pre-engram) is kept as it is; the current file gets its own stamped
    backup before it is replaced, so the CURRENT sentinel survives.
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
    this is not an existing-agent home. A CLAUDE.md that equals the render is recognised as
    identical without any seed manifest.
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
    """C2: ONE refusal names EVERY differing protected path."""
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
_PY_OS = {"replace", "rename", "remove", "unlink", "link", "symlink", "truncate", "removedirs", "rmdir",
          "system", "popen"}
# Names that are write calls when imported straight from os / shutil (`from os import replace`),
# or fetched by name (`getattr(x, "write_text")`). open/fdopen are write calls only with a write
# mode, which a bare name cannot show, so importing them by name is treated as a write spelling too.
_PY_FROM_OS_NAMES = _PY_OS | {"open", "fdopen"}
_PY_GETATTR_NAMES = (_PY_WRITE_METHODS_ANY_RECEIVER | _PY_SHUTIL | _PY_OS | {"unlink", "open", "fdopen"})
_WRITE_MODE_RE = re.compile(r"[wax+]")
_OS_OPEN_WRITE_RE = re.compile(r"O_(WRONLY|RDWR|CREAT|TRUNC|APPEND)")

# Reviewed write sites that the contract KEEPS. Anything else in bootstrap.py is a violation:
# it must go through the consent/backup helper (src/engram/engram_file_guard.py) instead; the
# helper's own writes are exempt by file. `call=None` allows the whole function.
BOOTSTRAP_ALLOWLIST = [
    {"func": "main", "call": "engram_tools_link.unlink()",
     "reason": "removes a symlink at that name (guarded by is_symlink()); only the link, never its target"},
    {"func": "main", "call": "engram_tools_link.rename(backup)",
     "reason": "moves an existing scatter-install dir aside to tools.scatter-backup, nothing deleted"},
    {"func": "main", "call": "engram_tools_link.symlink_to(plugin_tools)",
     "reason": "creates the plugin tools link after the old entry was removed or moved aside"},
    {"func": "main", "call": "engram_hooks_link.unlink()",
     "reason": "removes a symlink at that name (guarded by is_symlink()); only the link, never its target"},
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

    def is_import_spelling(node: ast.AST) -> bool:
        """The import forms that hide a write call from the call-spelling checks below: an alias
        of os / shutil (`import os as o`), a write function pulled in by name (`from os import
        replace`, `from shutil import move`), or anything from subprocess."""
        if isinstance(node, ast.Import):
            return any(
                (a.name in ("os", "shutil") and a.asname not in (None, a.name)) or a.name.split(".")[0] == "subprocess"
                for a in node.names
            )
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            mod = (node.module or "").split(".")[0]
            if mod == "subprocess":
                return True
            wanted = _PY_FROM_OS_NAMES if mod == "os" else _PY_SHUTIL if mod == "shutil" else None
            return wanted is not None and any(a.name == "*" or a.name in wanted for a in node.names)
        return False

    def is_write(call: ast.Call) -> bool:
        f = call.func
        if isinstance(f, ast.Name):
            if f.id == "getattr":  # getattr(x, "write_text")(...) hides the method name from the checks below
                return (len(call.args) >= 2 and isinstance(call.args[1], ast.Constant)
                        and call.args[1].value in _PY_GETATTR_NAMES)
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
            if (isinstance(child, ast.Call) and is_write(child)) or is_import_spelling(child):
                text = ast.unparse(child)
                hits.append((child.lineno, name, text if len(text) <= 90 else text[:87] + "..."))
            walk(child, name)

    walk(tree, "<module>")
    return hits


def _py_violations(source: str, allowlist: list[dict]) -> list[tuple[int, str, str]]:
    """Write sites not covered by the allowlist. Each entry covers ONE occurrence of its
    (func, call): a second identical call in an allowlisted function is a new site and fails."""
    budget: Counter = Counter((a["func"], a["call"]) for a in allowlist)
    out = []
    for line, func, text in _py_write_sites(source):
        if budget[(func, text)] > 0:
            budget[(func, text)] -= 1
        else:
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
    os.symlink(p, q)
    os.truncate(p, 0)
    os.rmdir(p)
    os.removedirs(p)
    os.link(p, q)
    os.open(p, os.O_WRONLY | os.O_CREAT)
    os.fdopen(3, "w")
    os.system("rm " + p)
    os.popen("rm " + p)
    getattr(p, "write_text")("x")
def quiet(p, s):
    open(p)
    open(p, "rb")
    s.replace("a", "b")
    d = dict()
    d.copy()
    p.read_text()
    os.open(p, os.O_RDONLY)
    os.fdopen(3, "r")
    getattr(p, "name")
'''

# One planted positive per import spelling that hides a write call from the call checks.
_PLANTED_IMPORT_POSITIVE = [
    "import os as o",
    "import shutil as sh",
    "from os import replace",
    "from os import path, rename",
    "from shutil import move",
    "from shutil import *",
    "import subprocess",
    "from subprocess import run",
]
_PLANTED_IMPORT_NEGATIVE = [
    "import os",
    "import shutil",
    "import os, shutil",
    "from os import path",
    "from os import environ, getcwd",
    "from shutil import which",
    "from pathlib import Path",
    "import os as os",
]
_PLANTED_IMPORTS = "\n".join(_PLANTED_IMPORT_POSITIVE + _PLANTED_IMPORT_NEGATIVE) + "\n"


_PLANTED_TWICE = """
def allowlisted(p):
    p.unlink()
    p.unlink()
"""


def _assert_python_scanner_controls() -> None:
    hits = _py_write_sites(_PLANTED_PY)
    assert len(hits) == 28, f"the AST scanner is broken: found {len(hits)} of 28 planted write sites: {hits}"
    assert all(func == "planted" for _l, func, _t in hits), "the scanner flagged a read-only call in quiet()"
    # One planted positive per new spelling, found by the SAME scanner (named, so a miss says which).
    texts = {t for _l, _f, t in hits}
    for spelled in ("os.symlink(p, q)", "os.truncate(p, 0)", "os.rmdir(p)", "os.removedirs(p)", "os.link(p, q)",
                    "os.open(p, os.O_WRONLY | os.O_CREAT)", "os.fdopen(3, 'w')", "os.system('rm ' + p)",
                    "os.popen('rm ' + p)", "getattr(p, 'write_text')"):
        assert spelled in texts, f"the AST scanner missed the planted spelling {spelled!r}: {sorted(texts)}"
    imports = [t for _l, _f, t in _py_write_sites(_PLANTED_IMPORTS)]
    want = [ast.unparse(ast.parse(s)) for s in _PLANTED_IMPORT_POSITIVE]
    assert imports == want, (
        f"the import-spelling check is broken: found {imports}; "
        f"missed {[w for w in want if w not in imports]}, false hits {[i for i in imports if i not in want]}"
    )
    # Positive control for the per-(func, call) count: one entry, two identical calls -> exactly one violation.
    one_entry = [{"func": "allowlisted", "call": "p.unlink()", "reason": "control"}]
    left = _py_violations(_PLANTED_TWICE, one_entry)
    assert len(left) == 1 and left[0][1:] == ("allowlisted", "p.unlink()"), \
        f"a second identical call in an allowlisted function was not flagged: {left}"
    assert _py_violations(_PLANTED_TWICE, one_entry * 2) == [], "two entries must cover two identical calls"


_PROTECTED_VAR_RE = re.compile(
    r"\$\{?(?:CLAUDE_MD_BACKUP|CLAUDE_MD|DB_PATH|CONFIG_PATH)(?![A-Za-z0-9_])"
    r"|\$\{?ENGRAM_HOME\}?/(?!(?:marketplace|venv|install-logs|\.deployed-version)(?:/|$|[^A-Za-z0-9_.-]))[^\s\"']+"
    r"|\$\{?(?:CLAUDE_HOME|CODEX_HOME)\}?/[^\s\"']+"
)

# Reviewed shell lines that touch files but not a user's protected file. (They do not expand a
# protected variable, so the scanner would not flag them anyway; the entries record the review.)
# Shell write sites to a protected variable that are NOT yet moved into the helper. Each reason
# names the slice that moves it; the entry is removed in that slice.
INSTALL_SH_PENDING = [
    ('cp -p "$CLAUDE_MD" "$CLAUDE_MD_BACKUP"',
     "slice 2: install.sh step 6 keeps its own `cp -p` backup beside the file until the installer "
     "hands the backup to the helper; bootstrap backs up per write in the meantime"),
]

INSTALL_SH_ALLOWLIST = [
    ('rm -rf "$WORK_DIR"', "the installer's own scratch directory"),
    ('cp -R "$WORK_DIR/." "$LOG_SAVE_DIR/"', "saves the failed run's log into $ENGRAM_HOME/install-logs (ENGRAM-owned)"),
]

MARKETPLACE_SH = SRC / "tools" / "install-local-marketplace.sh"
SET_FORUM_URL_PY = SRC / "tools" / "set_forum_url.py"

# Python heredoc blocks inside install.sh (the PY_* variables). Entries are func="<BLOCK>:<function>";
# each covers ONE occurrence.
INSTALL_SH_PY_ALLOWLIST = [
    {"func": "PY_CONFIG:<module>", "call": "open(path, 'w', encoding='utf-8')",
     "reason": "slice 2: MERGE backup. Step 3 writes install_tier into config.json in place; the MERGE "
               "class backs the file up first once the installer hands this write to the guard"},
]
# The config.json block of install-local-marketplace.sh (selected by the text `install_tier`).
MARKETPLACE_CONFIG_ALLOWLIST = [
    {"func": "<module>", "call": "open(path, 'w')",
     "reason": "slice 2: MERGE backup. Persists install_tier into config.json; same hand-over to the guard"},
]
# src/engram/tools/set_forum_url.py: the one shared forum.url writer (atomic temp + replace).
SET_FORUM_URL_ALLOWLIST = [
    {"func": "_atomic_write", "call": "os.fdopen(fd, 'w')",
     "reason": "slice 2: MERGE backup. Writes forum.url into config.json via an atomic temp file"},
    {"func": "_atomic_write", "call": "os.replace(tmp, path)",
     "reason": "slice 2: MERGE backup. Publishes that temp file over config.json"},
    {"func": "_atomic_write", "call": "os.unlink(tmp)",
     "reason": "slice 2: MERGE backup. Removes the writer's own temp file on failure"},
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


_SHELL_CMD_RE = re.compile(r"(?<![\w./$-])(cp|mv|rm|tee|sed|ln|install|python3?)(?=\s)")
# `>` / `>>` / `&>` / `2>` (but not `>&2`, `->`, `=>`, `>=`, `>(`); the target is read from the original line.
_REDIRECT_RE = re.compile(r"(?<![<>=\-])&?>>?\|?(?![>&(=])")
_REDIRECT_TARGET_RE = re.compile(r"""(?:"[^"]*"|'[^']*'|[^\s;&|()"'])+""")
_PY_INLINE_MODE_RES = (
    re.compile(r"""\bopen\([^,()]*,\s*\\?['"]([A-Za-z+]+)\\?['"]"""),
    re.compile(r"""\bmode\s*=\s*\\?['"]([A-Za-z+]+)\\?['"]"""),
)
_PY_INLINE_WRITE_RE = re.compile(r"\.write_(?:text|bytes)\(")


def _inline_python_writes(seg: str) -> bool:
    """True if a `python -c "..."` segment opens a file for writing or calls write_text/write_bytes."""
    if _PY_INLINE_WRITE_RE.search(seg):
        return True
    return any(re.search(r"[wax+]", mode) for rx in _PY_INLINE_MODE_RES for mode in rx.findall(seg))


def _sh_write_sites(text: str) -> list[tuple[int, str]]:
    """(line, text) for every shell line that can write, replace or delete a protected path:
    cp/mv/rm/tee/sed -i/ln -f/install whose target expands a protected variable, a `>` or `>>`
    redirect to one, and `python -c` whose inline code opens a file for writing."""
    masked, open_quote = _mask_shell(text)
    assert open_quote is None, "the shell scanner lost track of quotes; its result would be meaningless"
    hits = []
    for lineno, (orig, mline) in enumerate(zip(text.split("\n"), masked.split("\n")), 1):
        for m in _REDIRECT_RE.finditer(mline):
            tm = _REDIRECT_TARGET_RE.match(orig[m.end():].lstrip())
            if tm and _PROTECTED_VAR_RE.search(tm.group(0)):
                hits.append((lineno, orig.strip()))
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
            opts = [t for t in tokens[1:] if t.startswith("-") and len(t) > 1]
            args = [t for t in tokens[1:] if not (t.startswith("-") and len(t) > 1)]
            cmd = tokens[0]
            if cmd in ("python", "python3"):
                if "-c" in opts and _inline_python_writes(seg):
                    hits.append((lineno, seg))
                continue
            if cmd == "cp" or cmd == "install":
                targets = args[-1:]
            elif cmd == "ln":  # only a forced link replaces an existing name
                forced = any(re.match(r"^-[A-Za-z]*f", o) or o == "--force" for o in opts)
                targets = args[-1:] if forced else []
            elif cmd == "sed":  # only in-place editing writes the file (GNU `-i`, BSD `-i ''`, `-i.bak`)
                in_place = any(re.match(r"^-[A-Za-z]*i", o) or o.startswith("--in-place") for o in opts)
                targets = args if in_place else []
            else:  # mv removes its source; rm removes everything it is given; tee writes every file it is given
                targets = args
            if any(_PROTECTED_VAR_RE.search(t) for t in targets):
                hits.append((lineno, seg))
    return sorted(set(hits))


_PLANTED_SH_POSITIVE = [
    'cp -p "$CLAUDE_MD" "$CLAUDE_MD_BACKUP"',
    'x=1; mv "$CONFIG_PATH" /tmp/x',
    'test -e a && rm -f "$DB_PATH"',
    'if rm "$ENGRAM_HOME/warm-briefing.md"; then :; fi',
    'echo x > "$CLAUDE_MD"',
    'echo x >> $CLAUDE_HOME/notes.md',
    'printf y 2> "$CONFIG_PATH"',
    'echo hi | tee "$CLAUDE_MD"',
    'tee -a "$ENGRAM_HOME/warm-briefing.md" < /dev/null',
    "sed -i 's/a/b/' \"$CLAUDE_MD\"",
    "sed -i '' 's/a/b/' \"$CLAUDE_MD\"",
    "sed -i.bak 's/a/b/' \"$DB_PATH\"",
    'ln -sf /tmp/x "$CLAUDE_MD"',
    'install -m 600 /tmp/x "$CLAUDE_MD"',
    "python3 -c \"open(p, 'w').write('x')\"",
]
_PLANTED_SH_NEGATIVE = [
    'cp "$CLAUDE_MD" /tmp/copy-only-reads-the-protected-file',
    'rm -rf "$ENGRAM_HOME/venv"',
    'fail "remove the link (rm \'$CLAUDE_MD\') then re-run"   # quoted: message text, not a command',
    '# rm "$CLAUDE_MD"  <- a comment',
    'cat "$CLAUDE_MD" > /tmp/read-only-copy',
    'echo hi > /dev/null 2>&1',
    'grep x "$CLAUDE_MD" | tee /tmp/log',
    'sed -n 1p "$CLAUDE_MD"',
    'ln -s "$CLAUDE_MD" /tmp/link',
    'install -m 600 "$CLAUDE_MD" /tmp/copy',
    "python3 -c \"print(open(p).read(), open(q, 'r').read(), open('/a/x/ax'))\"",
    '[ "$a" -gt 1 ] && echo "x -> y"',
]
_PLANTED_SH = "#!/usr/bin/env bash\n" + "\n".join(_PLANTED_SH_POSITIVE + _PLANTED_SH_NEGATIVE) + "\n"

_PLANTED_SH_PY = """#!/usr/bin/env bash
read -r -d '' PY_READS <<'PY' || true
import sys
print(open(sys.argv[1]).read())
PY
read -r -d '' PY_PLANTED <<'PY' || true
import sys
open(sys.argv[1], "w").write("x")
PY
"""


def _assert_shell_scanner_controls() -> None:
    hits = _sh_write_sites(_PLANTED_SH)
    want = list(range(2, 2 + len(_PLANTED_SH_POSITIVE)))
    assert [ln for ln, _ in hits] == want, (
        f"the shell scanner is broken; planted-script hits: {hits}; "
        f"missed lines {sorted(set(want) - {ln for ln, _ in hits})}, "
        f"false hits {sorted({ln for ln, _ in hits} - set(want))}"
    )
    blocks = _heredoc_python_blocks(_PLANTED_SH_PY)
    assert [name for name, _l, _c in blocks] == ["PY_READS", "PY_PLANTED"], f"heredoc extractor broken: {blocks}"
    flagged = _heredoc_python_hits(_PLANTED_SH_PY)
    assert [(name, func) for _l, name, func, _t in flagged] == [("PY_PLANTED", "<module>")], \
        f"the heredoc python scan is broken: {flagged}"


def _heredoc_python_blocks(text: str) -> list[tuple[str, int, str]]:
    """(name, first body line, code) for every `<<'PY'` heredoc in a shell file. The name is the
    PY_* variable it is read into, else `line<N>`."""
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        if re.search(r"<<-?\s*'PY'", lines[i]):
            m = re.search(r"\b(PY_[A-Z_]+)\b", lines[i])
            name = m.group(1) if m else f"line{i + 1}"
            j = i + 1
            body = []
            while j < len(lines) and lines[j].strip() != "PY":
                body.append(lines[j])
                j += 1
            assert j < len(lines), f"unterminated PY heredoc starting at line {i + 1}"
            out.append((name, i + 2, "\n".join(body)))
            i = j
        i += 1
    return out


def _heredoc_python_hits(text: str, only_containing: "str | None" = None) -> list[tuple[int, str, str, str]]:
    """(file line, block name, enclosing function, call) for every write site in the Python heredoc
    blocks of a shell file. A block that does not parse fails the test (it cannot be judged)."""
    hits = []
    for name, start, code in _heredoc_python_blocks(text):
        if only_containing is not None and only_containing not in code:
            continue
        try:
            sites = _py_write_sites(code)
        except SyntaxError as e:
            pytest.fail(f"the Python heredoc block {name} (line {start}) does not parse, so it cannot be scanned: {e}")
        hits += [(start + ln - 1, name, func, call) for ln, func, call in sites]
    return hits


def _heredoc_violations(text: str, allowlist: list[dict], only_containing: "str | None" = None):
    """Heredoc write sites not covered by the allowlist (entries use func='<BLOCK>:<function>')."""
    sites = [(ln, f"{name}:{func}", call) for ln, name, func, call in _heredoc_python_hits(text, only_containing)]
    budget: Counter = Counter((a["func"], a["call"]) for a in allowlist)
    out = []
    for ln, func, call in sites:
        if budget[(func, call)] > 0:
            budget[(func, call)] -= 1
        else:
            out.append((ln, func, call))
    return out


def test_static_write_sites_are_reviewed():
    """Static gate: every file-writing call in bootstrap.py / install.sh is a reviewed, kept site.

    The allowlist holds only the sites the contract KEEPS; the helper that implements consent and
    backup lives in another module (src/engram/engram_file_guard.py), so none of its calls appear
    here. Sites not yet moved into it are listed with a reason that names the slice that moves them.
    Covered: bootstrap.py (AST), install.sh (shell scanner AND the AST scan of its Python heredoc
    blocks), set_forum_url.py (AST) and the config.json block of install-local-marketplace.sh.

    THE BOUND, stated plainly. This gate covers the call SPELLINGS it lists and no others:
      * method calls: write_text, write_bytes, symlink_to, unlink, Path.open / open with a write
        mode, Path.replace / Path.rename (one argument);
      * os.<fn> and shutil.<fn> calls for the functions in _PY_OS / _PY_SHUTIL (including
        os.system and os.popen), os.open with write flags, os.fdopen with a write mode;
      * import spellings that would hide those calls: `import os|shutil as X`, `from os import
        <write fn>`, `from shutil import <write fn>`, and any import of subprocess;
      * getattr(<x>, "<one of those function names>").
    A write reached some other way (importlib / __import__, exec or eval, a function stored in a
    variable, a C extension, a spelling not in the lists above) is NOT seen. The gate catches a
    later edit that uses a listed spelling; it does not prove the absence of every possible write.
    Each listed spelling has a planted positive in _PLANTED_PY / _PLANTED_IMPORT_POSITIVE that this
    same scanner must find (test body: _assert_python_scanner_controls).
    """
    _assert_python_scanner_controls()
    _assert_shell_scanner_controls()
    py_bad = _py_violations(BOOTSTRAP_PY.read_text(), BOOTSTRAP_ALLOWLIST)
    pending = {cmd for cmd, reason in INSTALL_SH_PENDING if "slice 2" in reason}
    install_text = INSTALL_SH.read_text()
    sh_bad = [(ln, text) for ln, text in _sh_write_sites(install_text) if text not in pending]
    assert len(_heredoc_python_blocks(install_text)) >= 3, "the heredoc extractor found no PY_* blocks in install.sh"
    heredoc_bad = _heredoc_violations(install_text, INSTALL_SH_PY_ALLOWLIST)
    problems = [f"src/engram/bootstrap.py:{ln}: {text}   (in {func})" for ln, func, text in py_bad]
    problems += [f"src/engram/tools/install.sh:{ln}: {text}" for ln, text in sh_bad]
    problems += [f"src/engram/tools/install.sh:{ln}: {text}   (Python block {func})" for ln, func, text in heredoc_bad]
    assert not problems, (
        "file-writing calls outside the reviewed allowlist (each must go through the consent and "
        "backup helper, or be reviewed and added to the allowlist with a reason):\n  " + "\n  ".join(problems)
    )


def test_config_json_writers_are_listed_for_slice_2():
    """The three config.json writers stay as they are in slice 1 and are listed with the reason
    'slice 2: MERGE backup'. Each entry must still match a real write site (a stale entry fails),
    and no other write site may appear in these writers."""
    for entry in INSTALL_SH_PY_ALLOWLIST + MARKETPLACE_CONFIG_ALLOWLIST + SET_FORUM_URL_ALLOWLIST:
        assert entry["reason"].startswith("slice 2: MERGE backup"), entry
    install_hits = _heredoc_python_hits(INSTALL_SH.read_text())
    assert any(n == "PY_CONFIG" for _l, n, _f, _c in install_hits), "install.sh step 3 (PY_CONFIG) write site moved or vanished"
    market_text = MARKETPLACE_SH.read_text()
    market_hits = _heredoc_python_hits(market_text, only_containing="install_tier")
    assert market_hits, "install-local-marketplace.sh's config.json block moved or vanished"
    assert Counter((f, c) for _l, _n, f, c in market_hits) == Counter((a["func"], a["call"]) for a in MARKETPLACE_CONFIG_ALLOWLIST), \
        f"install-local-marketplace.sh's config.json block does not match its allowlist: {market_hits}"
    forum_src = SET_FORUM_URL_PY.read_text()
    assert _py_violations(forum_src, SET_FORUM_URL_ALLOWLIST) == [], "set_forum_url.py grew a write site"
    assert len(_py_write_sites(forum_src)) == len(SET_FORUM_URL_ALLOWLIST), "a set_forum_url.py allowlist entry is stale"


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


def test_install_sh_consent_is_replace_of_exactly_claude_md_even_with_a_space_in_the_path(tmp_path):
    """The yes the installer asks for covers CLAUDE.md only: bootstrap gets `--replace <that
    path>` as ONE argument (a CLAUDE_HOME with a space in it must not split), never `--force`."""
    sb = Sandbox(tmp_path)
    spaced = sb.root / "my claude home"
    spaced.mkdir()
    (spaced / "CLAUDE.md").write_bytes(_sentinel("spaced-claude-md"))
    r = sb.run("--yes", env={"CLAUDE_HOME": str(spaced)})
    assert r.returncode == 0, r.all
    argv_lines = sb.calls_starting("argv-of-bootstrap ")
    assert len(argv_lines) == 1, sb.call_lines()
    argv = json.loads(argv_lines[0].split(" ", 1)[1])
    assert argv == ["--replace", str(spaced / "CLAUDE.md")], argv
    assert "--force" not in argv


def test_install_sh_passes_no_consent_flag_when_there_is_no_claude_md(tmp_path):
    """No existing CLAUDE.md -> nothing to consent to -> bootstrap gets no arguments (the empty
    array must expand to nothing under `set -u`)."""
    sb = Sandbox(tmp_path)
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    argv_lines = sb.calls_starting("argv-of-bootstrap ")
    assert [json.loads(l.split(" ", 1)[1]) for l in argv_lines] == [[]], sb.call_lines()


def test_install_sh_with_real_bootstrap_replaces_a_spaced_claude_md_after_backup(tmp_path):
    """End to end with the REAL bootstrap.py: the one `--replace` path is understood, the file
    is backed up and replaced."""
    sb = _installer_with_real_bootstrap(tmp_path)
    spaced = sb.root / "my claude home"
    spaced.mkdir()
    mine = _sentinel("spaced-claude-md-real")
    (spaced / "CLAUDE.md").write_bytes(mine)
    r = sb.run("--yes", env={"CLAUDE_HOME": str(spaced)})
    assert r.returncode == 0, r.all
    assert (spaced / "CLAUDE.md").read_bytes() != mine
    assert (spaced / "CLAUDE.md.pre-engram").read_bytes() == mine
    stamped = list(spaced.glob("CLAUDE.md.engram-backup.*"))
    assert len(stamped) == 1 and stamped[0].read_bytes() == mine


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
        sb.engram / "warm-briefing.md": _sentinel("warm-briefing-user-authored"),
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
    then). Cell 2 moves knowledge.db away: memory without a graph, which the installer must
    refuse.
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


_SUDO_OPTS_WITH_VALUE = {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "-r", "-t"}


def _strip_sudo(toks: list[str]) -> list[str]:
    """`sudo [options] rm ...` is judged as `rm ...` (sudo changes who deletes, not what is deleted)."""
    if not toks or toks[0] != "sudo":
        return toks
    i = 1
    while i < len(toks) and toks[i].startswith("-") and len(toks[i]) > 1:
        i += 2 if toks[i] in _SUDO_OPTS_WITH_VALUE else 1
    return toks[i:]


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
            toks = _strip_sudo(_command_tokens(seg))
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
sudo rm -rf ~/.engram   # HIT
sudo -u root rm ~/.claude/CLAUDE.md   # HIT
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
    assert len(expected) == 6 and hits == expected, \
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


# ----------------------------------------------- C5: newest graph backup is the newest by mtime

def test_newest_db_backup_is_by_mtime_not_by_name(tmp_path):
    """Name order and mtime order disagree: the refusal names the file modified last."""
    wb = _memory_without_graph(tmp_path)
    by_name = tmp_path / ".engram" / "db-backup" / "knowledge-20261105.db"   # later name, OLDER mtime
    by_mtime = tmp_path / ".engram" / "db-backup" / "knowledge-20261101.db"  # earlier name, NEWER mtime
    by_name.parent.mkdir(parents=True)
    by_name.write_bytes(b"later name, older file")
    by_mtime.write_bytes(b"earlier name, newer file")
    os.utime(by_name, (1_000_000_000, 1_000_000_000))
    os.utime(by_mtime, (1_100_000_000, 1_100_000_000))
    r = _run(tmp_path)
    _assert_nothing_written_by_refusal(tmp_path, r, wb)
    msg = _text(r)
    assert "knowledge-20261101.db" in msg, "the refusal must name the mtime-newest backup:\n" + msg
    assert "knowledge-20261105.db" not in msg, "only the mtime-newest backup should be named:\n" + msg


def test_newest_db_backup_ties_go_to_the_later_name(tmp_path):
    wb = _memory_without_graph(tmp_path)
    a = tmp_path / ".engram" / "db-backup" / "knowledge-20261101.db"
    b = tmp_path / ".engram" / "db-backup" / "knowledge-20261105.db"
    a.parent.mkdir(parents=True)
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    os.utime(a, (1_000_000_000, 1_000_000_000))
    os.utime(b, (1_000_000_000, 1_000_000_000))
    msg = _text(_run(tmp_path))
    assert "knowledge-20261105.db" in msg and "knowledge-20261101.db" not in msg, msg
    assert wb.read_bytes() == USER_AUTHORED


# ------------------------------------------------ move-aside message: the reinstall sentence

MOVE_ASIDE_SENTENCE = "The installed plugin and its Python environment move too, so ENGRAM must be reinstalled afterwards."


def test_memory_refusal_gives_the_move_aside_command_and_the_reinstall_sentence(tmp_path):
    _memory_without_graph(tmp_path)
    msg = _text(_run(tmp_path))
    assert "mv " in msg and ".moved-aside-" in msg, msg
    assert MOVE_ASIDE_SENTENCE in msg, "the move-aside message must say the plugin and venv move too:\n" + msg


@pytest.mark.parametrize("extra_env", [None, {"FORCE": "1"}])
def test_graph_present_refusal_gives_the_reinstall_sentence(tmp_path, extra_env):
    first = _run_env(tmp_path)
    assert first.returncode == 0, _text(first)
    again = _run_env(tmp_path, extra_env=extra_env)
    msg = _text(again)
    assert again.returncode != 0 and ".moved-aside-" in msg, msg
    assert MOVE_ASIDE_SENTENCE in msg, msg
    assert "delete" not in msg.lower(), "no message of ours says delete:\n" + msg


# ------------------------------------------------------------------ MERGE class (M1-M4)

_fg_spec = importlib.util.spec_from_file_location("engram_file_guard_contract", SRC / "engram_file_guard.py")
fg = importlib.util.module_from_spec(_fg_spec)
_fg_spec.loader.exec_module(fg)


def test_merge_owned_keys_are_one_closed_named_list():
    """M1: the owned keys are exactly this list. Changing it is a contract change, not a refactor."""
    assert fg.OWNED_KEYS == {
        "settings.json": ("cleanupPeriodDays",),
        "config.json": ("install_tier", "forum.url"),
    }


def _nested(doc: dict, dotted: str):
    for part in dotted.split("."):
        doc = doc[part]
    return doc


def _strip_dotted(doc: dict, dotted: str) -> dict:
    out = json.loads(json.dumps(doc))
    parts = dotted.split(".")
    node = out
    for part in parts[:-1]:
        node = node.get(part, {})
    node.pop(parts[-1], None)
    return out


_USER_DOCS = {
    "settings.json": {"model": "sonnet", "permissions": {"allow": ["Bash(ls)", "Read"], "deny": []},
                      "env": {"NAME": "café"}, "theme": "dark"},
    "config.json": {"self_lineage": "anthropic:opus", "forum": {"theme": "x", "retries": 3},
                    "embedding": {"model": "m", "dims": 384}, "cadence": {"drowsiness_ceiling_tokens": 800000}},
}


@pytest.mark.parametrize("filename,key,value", [
    ("settings.json", "cleanupPeriodDays", 30),
    ("config.json", "install_tier", "dev"),
    ("config.json", "forum.url", "https://forum.example.test"),
])
def test_merge_one_cell_per_owned_key(tmp_path, filename, key, value):
    """M1-M4 for each owned key: the key is set, every other key keeps its value and order, a
    stamped backup of the original bytes is taken first, the file mode is kept, and a second
    identical merge writes nothing and backs up nothing.

    Comparison, stated: the NON-OWNED part is compared byte for byte on its re-serialization
    (json.dumps with the file's own 2-space indent) AND key by key (values and key order, at the
    top level and inside the objects that hold owned keys). The raw file bytes are not compared,
    because the merge re-serializes the whole file, so they differ by the owned entry.
    """
    before = _USER_DOCS[filename]
    path = tmp_path / filename
    original = (json.dumps(before, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(original)
    os.chmod(path, 0o640)

    assert fg.apply_merge(path, {key: value}) == "merged"

    after = json.loads(path.read_text(encoding="utf-8"))
    assert _nested(after, key) == value, "the owned key was not set"
    assert json.dumps(_strip_dotted(after, key), indent=2, ensure_ascii=False) == \
        json.dumps(_strip_dotted(before, key), indent=2, ensure_ascii=False), \
        "the non-owned part changed (byte-for-byte comparison of its re-serialization)"
    assert list(after) == list(before) + ([key] if "." not in key else []), "top-level key order changed"
    for top, sub in before.items():
        if isinstance(sub, dict):
            assert list(after[top])[: len(sub)] == list(sub), f"key order inside {top!r} changed"
            for k, v in sub.items():
                assert after[top][k] == v, f"{top}.{k} changed"
    assert stat_mode(path) == 0o640, "the file mode was not kept"
    backups = _backups_of(path)
    assert [b.read_bytes() for b in backups] == [original], "exactly one stamped backup of the original bytes"

    # M4: merging the same value again leaves the file byte-identical and makes no backup.
    sha, mtime = _sha(path), path.stat().st_mtime_ns
    assert fg.apply_merge(path, {key: value}) == "unchanged"
    assert (_sha(path), path.stat().st_mtime_ns) == (sha, mtime), "an unchanged merge wrote the file"
    assert _backups_of(path) == backups, "an unchanged merge made a backup"


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o7777


def test_merge_into_an_absent_file_creates_only_the_owned_key_mode_0600(tmp_path):
    path = tmp_path / "settings.json"
    assert fg.apply_merge(path, {"cleanupPeriodDays": 90}) == "created"
    assert json.loads(path.read_text()) == {"cleanupPeriodDays": 90}
    assert stat_mode(path) == 0o600
    assert _backups_of(path) == [], "there is nothing to back up when the file was absent"


@pytest.mark.parametrize("filename,existing,changes", [
    # a key that is not on the list is never ENGRAM's to change, even alongside an owned one
    ("settings.json", {"model": "sonnet"}, {"cleanupPeriodDays": 30, "model": "other"}),
    ("settings.json", {"model": "sonnet"}, {"model": "other"}),
    ("config.json", {"forum": {"theme": "x"}}, {"forum.url": "https://x.test", "forum.theme": "y"}),
    ("config.json", {"self_lineage": "anthropic:opus"}, {"self_lineage": "other"}),
    # a file with no owned keys is never merged into
    ("other.json", {"a": 1}, {"a": 2}),
    # an owned nested key whose parent the user made a non-object: setting it would change that value
    ("config.json", {"forum": "a string the user wrote"}, {"forum.url": "https://x.test"}),
])
def test_merge_that_would_change_an_unowned_key_is_refused_and_the_file_untouched(tmp_path, filename, existing, changes):
    """NEGATIVE cell for M1/M2: refuse with MergeRefused; the file, its mtime and its directory are unchanged."""
    path = tmp_path / filename
    path.write_bytes((json.dumps(existing, indent=2) + "\n").encode())
    state = (_sha(path), path.stat().st_mtime_ns, sorted(x.name for x in tmp_path.iterdir()))
    with pytest.raises(fg.MergeRefused):
        fg.apply_merge(path, changes)
    assert (_sha(path), path.stat().st_mtime_ns, sorted(x.name for x in tmp_path.iterdir())) == state


@pytest.mark.parametrize("content", [b"{not json", b"", b"[1, 2]", b"42", b"null", b"\xff\xfe\x00broken"])
@pytest.mark.parametrize("filename,key,value", [
    ("settings.json", "cleanupPeriodDays", 30),
    ("config.json", "install_tier", "dev"),
    ("config.json", "forum.url", "https://x.test"),
])
def test_merge_never_repairs_a_file_that_does_not_parse(tmp_path, filename, key, value, content):
    """M3: a file that does not parse (or is not a JSON object) is not a merge candidate: refused,
    byte-identical afterwards, no backup, no temp file."""
    path = tmp_path / filename
    path.write_bytes(content)
    state = (_sha(path), path.stat().st_mtime_ns)
    with pytest.raises(fg.MergeRefused):
        fg.apply_merge(path, {key: value})
    assert (_sha(path), path.stat().st_mtime_ns) == state
    assert sorted(x.name for x in tmp_path.iterdir()) == [filename]


def test_bootstrap_retention_flag_takes_a_stamped_backup_and_keeps_the_other_keys(tmp_path):
    """cleanupPeriodDays is the one owned key bootstrap touches: --retention-days goes through the merge."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    user = {"model": "sonnet", "hooks": {"Stop": []}, "theme": "dark"}
    original = (json.dumps(user, indent=2) + "\n").encode()
    settings.write_bytes(original)
    r = _run(tmp_path, args=["--retention-days", "90"])
    assert r.returncode == 0, _text(r)
    after = json.loads(settings.read_text())
    assert after == {**user, "cleanupPeriodDays": 90} and list(after) == [*user, "cleanupPeriodDays"]
    assert [b.read_bytes() for b in _backups_of(settings)] == [original]


def test_bootstrap_refusal_for_a_broken_settings_file_offers_repair_or_move_aside_never_remove(tmp_path):
    """M3 through bootstrap: refused before anything is created; the message says repair or move
    aside (no message of ours tells the user to remove a file)."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b"{not json")
    r = _run(tmp_path, args=["--retention-days", "90"])
    msg = _text(r)
    assert r.returncode != 0 and "REFUSED" in msg, msg
    assert settings.read_bytes() == b"{not json"
    assert "move it aside" in msg and "remove" not in msg.lower() and "delete" not in msg.lower(), msg
    assert not _db(tmp_path).exists()


# ------------------------------------- the helpers the static gate exempts by file: behaviour pins
#
# The static gate exempts engram_file_guard.py by file, because the guard IS the helper. So that
# exemption cannot silently widen, (1) the guard's own write sites are listed below per
# (function, call), and (2) the two helpers that carry the guarantee get a behaviour pin here.
# A behaviour pin (not a hash of the function source) because a hash fails on every harmless edit
# and says nothing about what the function does; a pin fails exactly when the guarantee breaks.

GUARD_ALLOWLIST = [
    {"func": "copy_exclusive", "call": "os.fdopen(fd, 'wb')", "reason": "writes the private temp it created (mkstemp)"},
    {"func": "copy_exclusive", "call": "os.link(temp, dst)", "reason": "publishes exclusively: FileExistsError for anything at dst"},
    {"func": "copy_exclusive", "call": "os.replace(temp, dst)", "reason": "only on a filesystem without hard links, and only after dst was found absent"},
    {"func": "copy_exclusive", "call": "os.unlink(temp)", "reason": "removes the temp this call created"},
    {"func": "atomic_write_text", "call": "os.fdopen(fd, 'w', encoding='utf-8')",
     "reason": "writes the unique exclusive temp tempfile.mkstemp created beside the target"},
    {"func": "atomic_write_text", "call": "os.replace(tmp, path)", "reason": "atomic publish: never writes through the target"},
    {"func": "atomic_write_text", "call": "os.unlink(tmp)", "reason": "removes the temp on failure"},
    {"func": "apply", "call": "os.unlink(e.path)", "reason": "a delete request: only after a stamped backup, only on consent"},
]


def test_guard_write_sites_are_exactly_the_reviewed_ones():
    assert _py_violations((SRC / "engram_file_guard.py").read_text(), GUARD_ALLOWLIST) == []
    assert len(_py_write_sites((SRC / "engram_file_guard.py").read_text())) == len(GUARD_ALLOWLIST), \
        "a guard allowlist entry is stale (it matches no write site)"


def test_copy_exclusive_still_refuses_anything_at_dst(tmp_path):
    """Behaviour pin: exclusive create. A regular file, a symlink and a dangling symlink at dst
    all raise FileExistsError and are left exactly as they were."""
    src = tmp_path / "src.txt"
    src.write_bytes(b"source bytes\n")
    existing = tmp_path / "existing"
    existing.write_bytes(b"already here\n")
    real = tmp_path / "real"
    real.write_bytes(b"link target\n")
    link = tmp_path / "link"
    link.symlink_to(real)
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "nowhere")
    for dst in (existing, link, dangling):
        with pytest.raises(FileExistsError):
            fg.copy_exclusive(src, dst)
    assert existing.read_bytes() == b"already here\n" and real.read_bytes() == b"link target\n"
    assert link.is_symlink() and dangling.is_symlink() and not (tmp_path / "nowhere").exists()
    assert sorted(x.name for x in tmp_path.iterdir()) == ["dangling", "existing", "link", "real", "src.txt"], \
        "a temp file was left behind"
    fresh = tmp_path / "fresh"
    fg.copy_exclusive(src, fresh)
    assert fresh.read_bytes() == b"source bytes\n"


def test_atomic_write_text_replaces_the_file_and_never_writes_through_a_hard_link(tmp_path):
    """Behaviour pin: the target is replaced (os.replace), not truncated in place, so another hard
    link to the old file keeps the old bytes; the requested mode is applied; no temp is left."""
    target = tmp_path / "t.txt"
    target.write_bytes(b"old bytes\n")
    other_link = tmp_path / "other-link.txt"
    os.link(target, other_link)
    fg.atomic_write_text(target, "new bytes\n", 0o640)
    assert target.read_bytes() == b"new bytes\n" and stat_mode(target) == 0o640
    assert other_link.read_bytes() == b"old bytes\n", "the write went through the hard link"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["other-link.txt", "t.txt"], "a temp file was left behind"
