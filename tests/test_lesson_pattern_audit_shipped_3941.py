"""engram-alpha#3941 -- the lesson-pattern audit ships, and `--write-ranks` is safe to run from an install.

The tool used to run from a checkout only: it found the tripwire hook through a fixed path from its own
location in the source layout, it kept its default db and output path off ENGRAM_HOME, and a run with no
transcripts exited 2 (or, worse for a recurring caller, could have written empty ranks).  Contract cells:

  (a) flattened layout   a temp tree shaped like the installed plugin (tool at <root>/tools/, hook at
                         <root>/hooks/<name>, NO src/) imports and runs, with CLAUDE_PLUGIN_ROOT unset
                         (lookup anchored at the tool's tree) and set (plugin-root lookup).
  (b) zero transcripts   `--write-ranks` exits 0, says so, and leaves an existing rank file byte-identical;
                         with no file it creates none.  An empty rank file would read as "every lesson is
                         rare", a false tie-break.
  (e) interrupted write  a failure between the temp write and the replace leaves the old file intact and no
                         half file (no temp file) behind.
  tier                   essential, out of excluded[], and a test-map entry.

Every cell names its mutation in the docstring.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
TOOL_SRC = SRC_ENGRAM / "tools" / "lesson-pattern-audit.py"
TOOL_LINK = REPO_ROOT / "tools" / "lesson-pattern-audit.py"
HOOK_NAME = "engram-lesson-tripwire-hook.py"
TIERS = REPO_ROOT / "src" / "build" / "packaging" / "tiers.json"
TEST_MAP = SRC_ENGRAM / "packaging" / "test-map.json"
TOOL_PATH_KEY = "tools/lesson-pattern-audit.py"

_SCHEMA = """CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT NOT NULL, is_current INTEGER NOT NULL DEFAULT 1,
 metadata TEXT NOT NULL DEFAULT '{}', claim TEXT NOT NULL DEFAULT '')"""


def _db(path: Path) -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute(_SCHEMA)
    conn.execute("INSERT INTO nodes (id, type, is_current, metadata, claim) VALUES (?, 'lesson', 1, ?, 'c')",
                 ("lsx-001", json.dumps({"situation_pattern": "gh pr create"})))
    conn.commit()
    conn.close()
    return path


def _transcript(path: Path) -> Path:
    lines = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": c}}]}}
        for c in ("gh pr create a", "ls")]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return path


def _env(tmp: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_HOME", "PYTHONPATH", "ENGRAM_RUNTIME_DIR")}
    env["HOME"] = str(tmp)
    env.update(extra)
    return env


def _run(tool: Path, args, env, cwd) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(tool), *args], capture_output=True, text=True, env=env,
                          cwd=str(cwd), timeout=120)


@pytest.fixture
def tmp():
    d = Path(tempfile.mkdtemp(prefix="lpa3941-")).resolve()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _flat_tree(root: Path, with_hook: bool = True) -> Path:
    """The installed plugin layout: tools/ and a FLAT hooks/ (the build drops the claude/ level). No src/.
    with_hook=False recreates an Essential-only install: the tripwire hook is `convenience`, so its file is
    simply not there (the rest of hooks/ is shipped, as it would be)."""
    (root / "tools").mkdir(parents=True)
    (root / "hooks").mkdir()
    for name in ("lesson-pattern-audit.py", "_owner_read.py", "__init__.py"):
        shutil.copyfile(SRC_ENGRAM / "tools" / name, root / "tools" / name)
    for f in (SRC_ENGRAM / "hooks" / "claude").glob("*.py"):
        if f.name == HOOK_NAME and not with_hook:
            continue
        shutil.copyfile(f, root / "hooks" / f.name)
    assert (root / "hooks" / HOOK_NAME).exists() == with_hook
    assert not (root / "src").exists()
    return root / "tools" / "lesson-pattern-audit.py"


# ---- (a) the flattened layout --------------------------------------------------------------------

@pytest.mark.parametrize("plugin_root_set", [False, True], ids=["root-unset-tool-tree", "root-set"])
def test_a_the_tool_runs_in_the_flattened_plugin_layout(tmp, plugin_root_set):
    """Mutation: the hook path back to a fixed parents[N] from the tool's own file -> the import fails in this
    layout (the hook is at <root>/hooks/<name>, not at the source layout's depth) -> red on both variants."""
    root = tmp / "plugin"
    tool = _flat_tree(root)
    db = _db(tmp / "knowledge.db")
    transcript = _transcript(tmp / "t.jsonl")
    out = tmp / "ranks.json"
    extra = {"CLAUDE_PLUGIN_ROOT": str(root)} if plugin_root_set else {}
    done = _run(tool, ["--db", str(db), "--transcript-glob", str(transcript), "--write-ranks", str(out)],
                _env(tmp, **extra), cwd=tmp)
    assert done.returncode == 0, done.stderr
    assert json.loads(out.read_text())["ranks"] == {"lsx-001": 500.0}


def _load_tool(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("plugin_root_set", [False, True], ids=["root-unset", "root-set-without-hook"])
def test_a_the_hook_is_found_by_file_name_in_the_source_layout_too(tmp, monkeypatch, plugin_root_set):
    """The source layout (hooks/claude/<name>) resolves through the tool-tree lookup, from the repo-root
    symlink.  CLAUDE_PLUGIN_ROOT, when set to a root with no hook, falls through to the tool's own tree.
    Mutation: drop the hooks/claude/ probe -> _HOOK_PATH None -> red on both variants."""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    if plugin_root_set:
        empty = tmp / "empty-root"
        empty.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(empty))
    mod = _load_tool(TOOL_LINK, "lpa_src_layout")
    assert Path(mod._HOOK_PATH).name == HOOK_NAME and Path(mod._HOOK_PATH).is_file()
    assert Path(mod._HOOK_PATH).resolve().parent == (SRC_ENGRAM / "hooks" / "claude").resolve()


@pytest.mark.parametrize("plugin_root_set", [False, True], ids=["root-unset", "root-set-without-hook"])
def test_a_the_flattened_hook_is_found_by_import(tmp, monkeypatch, plugin_root_set):
    """The flattened layout's <root>/hooks/<name> is _HOOK_PATH, by import.  Mutation: drop the tool-tree
    hooks/ probe -> None -> red."""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    root = tmp / "plugin"
    tool = _flat_tree(root)
    if plugin_root_set:
        empty = tmp / "empty-root"
        empty.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(empty))
    mod = _load_tool(tool, "lpa_flat_import")
    assert Path(mod._HOOK_PATH) == (root / "hooks" / HOOK_NAME).resolve()


def test_a_a_hook_under_plugin_root_wins_when_set(tmp, monkeypatch):
    """A hook under $CLAUDE_PLUGIN_ROOT/hooks is chosen over the one in the tool's own tree.  Mutation: drop
    the $CLAUDE_PLUGIN_ROOT probe -> the tool-tree hook is chosen -> red."""
    tool = _flat_tree(tmp / "plugin")
    other = tmp / "other-root"
    (other / "hooks").mkdir(parents=True)
    shutil.copyfile(SRC_ENGRAM / "hooks" / "claude" / HOOK_NAME, other / "hooks" / HOOK_NAME)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(other))
    mod = _load_tool(tool, "lpa_env_wins")
    assert Path(mod._HOOK_PATH) == other / "hooks" / HOOK_NAME


# A hook in a directory ABOVE the tool's tree is not the tool's: an Essential-only install under
# ~/.engram/marketplace/plugins/engram/ must not measure a stale ~/.engram/hooks/ left by an old install.
_ANCESTOR_SPOTS = ["../hooks", "../hooks/claude", "../../hooks", "../../hooks/claude"]


@pytest.mark.parametrize("spot", _ANCESTOR_SPOTS)
def test_a_a_hook_planted_in_an_ancestor_of_the_plugin_root_is_not_used(tmp, monkeypatch, spot):
    """Layout outer/mid/plugin/tools, NO hook in the plugin tree, a hook planted above it.  _HOOK_PATH is None
    by import and `--write-ranks` (real subprocess) exits 0 with the no-hook line and writes nothing.
    Mutation: restore the walk over every parent of the tool file -> the planted hook is used -> red."""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    root = tmp / "outer" / "mid" / "plugin"
    tool = _flat_tree(root, with_hook=False)
    planted = (root / spot).resolve() / HOOK_NAME
    planted.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SRC_ENGRAM / "hooks" / "claude" / HOOK_NAME, planted)
    assert tmp in planted.parents and root.resolve() not in planted.parents
    mod = _load_tool(tool, "lpa_ancestor_import")
    assert mod._HOOK_PATH is None
    home = tmp / "home"
    home.mkdir()
    done = _run(tool, ["--db", str(_db(tmp / "knowledge.db")), "--transcript-glob",
                       str(_transcript(tmp / "t.jsonl")), "--write-ranks"],
                _env(tmp, ENGRAM_HOME=str(home)), cwd=tmp)
    assert done.returncode == 0, done.stderr
    assert "no tripwire hook installed; rank file left as is" in done.stdout
    assert list(home.iterdir()) == []


# ---- B1: the hook is `convenience`, the tool `essential` ------------------------------------------

NO_HOOK_LINE = "no tripwire hook installed; rank file left as is"


def _no_hook_setup(tmp: Path):
    root = tmp / "plugin"
    tool = _flat_tree(root, with_hook=False)
    home = tmp / "home"
    home.mkdir()
    return tool, home, _db(tmp / "knowledge.db"), _transcript(tmp / "t.jsonl")


def test_b1_a_no_hook_file_write_ranks_exits_0_says_so_and_leaves_the_file(tmp):
    """Essential-only layout for real (hook file absent, CLAUDE_PLUGIN_ROOT unset, no mock).  Mutation: the
    hook loader raises again on a missing hook -> exit 1 at import -> red."""
    tool, home, db, transcript = _no_hook_setup(tmp)
    existing = home / "lesson-fire-ranks.json"
    existing.write_bytes(b'{"ranks": {"lsx-001": 12.5}}\n')
    before = existing.read_bytes()
    done = _run(tool, ["--db", str(db), "--transcript-glob", str(transcript), "--write-ranks"],
                _env(tmp, ENGRAM_HOME=str(home)), cwd=tmp)
    assert done.returncode == 0, done.stderr
    assert NO_HOOK_LINE in done.stdout
    assert "Traceback" not in done.stderr
    assert existing.read_bytes() == before
    assert sorted(p.name for p in home.iterdir()) == ["lesson-fire-ranks.json"]


def test_b1_a_no_hook_file_and_no_rank_file_creates_none(tmp):
    tool, home, db, transcript = _no_hook_setup(tmp)
    done = _run(tool, ["--db", str(db), "--transcript-glob", str(transcript), "--write-ranks"],
                _env(tmp, ENGRAM_HOME=str(home)), cwd=tmp)
    assert done.returncode == 0, done.stderr
    assert NO_HOOK_LINE in done.stdout
    assert list(home.iterdir()) == []


@pytest.mark.parametrize("flag", [[], ["--compare-views"]], ids=["audit", "compare-views"])
def test_b1_b_no_hook_file_audit_modes_fail_with_one_line_not_a_traceback(tmp, flag):
    """These modes need the hook's matcher to do their job: a loud exit 1 and a clear message.  Mutation: treat
    the missing hook as 'nothing to do' (exit 0) in these modes -> red."""
    tool, home, db, transcript = _no_hook_setup(tmp)
    done = _run(tool, ["--db", str(db), "--transcript-glob", str(transcript), *flag],
                _env(tmp, ENGRAM_HOME=str(home)), cwd=tmp)
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "Traceback" not in done.stderr
    lines = [ln for ln in done.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1 and HOOK_NAME in lines[0], lines


def _broken_hook_setup(tmp: Path, kind: str):
    root = tmp / "plugin"
    tool = _flat_tree(root)
    real = (root / "hooks" / HOOK_NAME).read_text()
    broken = {"syntax": real + "\n)))\n",
              "raises": "raise RuntimeError('boom-hook-import')\n" + real,
              "exits0": "import sys\nsys.exit(0)\n" + real}[kind]
    (root / "hooks" / HOOK_NAME).write_text(broken)
    home = tmp / "home"
    home.mkdir()
    return tool, home, _db(tmp / "knowledge.db"), _transcript(tmp / "t.jsonl")


@pytest.mark.parametrize("kind", ["syntax", "raises", "exits0"])
def test_b1_e_a_hook_that_exists_but_does_not_import_stays_loud(tmp, kind):
    """The hook FILE is there, so 'not installed' does not apply: a syntax error, a failing top-level
    statement or the hook's own guard exiting 0 must end non-zero with the error on stderr, or a broken
    convenience install would read as a quiet success every night.  Mutation: decide 'hook not installed' by
    catching ImportError (or any load error) instead of testing for the file -> exit 0 -> red."""
    tool, home, db, transcript = _broken_hook_setup(tmp, kind)
    existing = home / "lesson-fire-ranks.json"
    existing.write_bytes(b'{"ranks": {}}\n')
    done = _run(tool, ["--db", str(db), "--transcript-glob", str(transcript), "--write-ranks"],
                _env(tmp, ENGRAM_HOME=str(home)), cwd=tmp)
    assert done.returncode != 0, (done.stdout, done.stderr)
    assert NO_HOOK_LINE not in done.stdout
    assert HOOK_NAME in done.stderr
    if kind == "raises":
        assert "boom-hook-import" in done.stderr
    assert existing.read_bytes() == b'{"ranks": {}}\n'


# ---- B2: a relative ENGRAM_HOME is a failure, not a success ----------------------------------------

@pytest.mark.parametrize("flag", [["--write-ranks"], [], ["--compare-views"]], ids=["write-ranks", "audit", "compare"])
def test_b2_a_relative_engram_home_exits_1_with_one_stderr_line(tmp, flag):
    """The hook's top-level guard exits 0 on a relative ENGRAM_HOME; the tool validates the home itself before
    any hook load.  Mutation: drop the validation in main() -> exit 0 and nothing written -> red."""
    db = _db(tmp / "knowledge.db")
    transcript = _transcript(tmp / "t.jsonl")
    done = _run(TOOL_LINK, ["--db", str(db), "--transcript-glob", str(transcript), *flag],
                _env(tmp, ENGRAM_HOME="rel"), cwd=tmp)
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "Traceback" not in done.stderr
    lines = [ln for ln in done.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1 and "ENGRAM_HOME" in lines[0], lines
    assert list(tmp.rglob("lesson-fire-ranks.json")) == []


# ---- tier closure ------------------------------------------------------------------------------------

# The one optional (fallible-load) dependency this essential tool may have on a higher tier: the tripwire hook,
# found by file and treated as 'nothing to rank' when the file is absent.  Everything else it loads must be
# essential.  A new higher-tier dependency fails the cell below until it is made fallible and pinned here.
OPTIONAL_HIGHER_TIER_LOADS = {HOOK_NAME}


def _tool_file_references() -> set:
    """Basenames of the shipped files the tool imports (`from tools import x` / `from tools.x import`) or
    loads by file name (a string constant that is a bare *.py name)."""
    import ast
    import re
    tree = ast.parse(TOOL_SRC.read_text(encoding="utf-8"))
    refs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == "tools":
                refs.update(f"{a.name}.py" for a in node.names)
            elif node.module.startswith("tools."):
                refs.add(node.module.split(".", 1)[1].replace(".", "/") + ".py")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and re.fullmatch(r"[A-Za-z0-9_.-]+\.py", node.value):
            refs.add(node.value)
    return refs


def test_tier_closure_the_essential_tool_loads_nothing_higher_unless_pinned_optional():
    """Mutation: drop the pinned-list check, and give the tool a second `convenience` dependency -> red."""
    manifest = json.loads(TIERS.read_text())
    tier_of = {}
    for m in manifest["mechanisms"]:
        for p in (m["path"], m.get("dest", m["path"])):
            tier_of.setdefault(os.path.basename(p), set()).add(m["tier"])
    refs = _tool_file_references()
    assert HOOK_NAME in refs and "_owner_read.py" in refs, refs   # the scan sees the real references
    higher = {name: sorted(tiers) for name in sorted(refs)
              for tiers in [tier_of.get(name)] if tiers is not None and tiers != {"essential"}}
    assert set(higher) == OPTIONAL_HIGHER_TIER_LOADS, (
        f"an essential tool may reference exactly {sorted(OPTIONAL_HIGHER_TIER_LOADS)} above its tier, "
        f"found {higher}")


# ---- (b) zero transcripts ------------------------------------------------------------------------

def _zero_run(tmp: Path, home: Path):
    db = _db(tmp / "knowledge.db")
    return _run(TOOL_LINK, ["--db", str(db), "--transcript-glob", str(tmp / "none" / "*.jsonl"),
                            "--write-ranks"], _env(tmp, ENGRAM_HOME=str(home)), cwd=tmp)


def test_b_zero_transcripts_leave_an_existing_rank_file_byte_identical(tmp):
    """Mutation: write the (empty) ranks anyway on an empty inventory -> the file changes -> red."""
    home = tmp / "home"
    home.mkdir()
    existing = home / "lesson-fire-ranks.json"
    existing.write_bytes(b'{"ranks": {"lsx-001": 12.5}, "generated_at": "2026-10-01T00:00:00+00:00"}\n')
    before = existing.read_bytes()
    done = _zero_run(tmp, home)
    assert done.returncode == 0, done.stderr
    assert "no transcripts found" in (done.stdout + done.stderr)
    assert "rank file left as is" in done.stdout
    assert existing.read_bytes() == before
    assert sorted(p.name for p in home.iterdir()) == ["lesson-fire-ranks.json"], "a temp file was left behind"


def test_b_zero_transcripts_and_no_file_creates_none(tmp):
    """Mutation: as above -> a rank file appears where there was none -> red."""
    home = tmp / "home"
    home.mkdir()
    done = _zero_run(tmp, home)
    assert done.returncode == 0, done.stderr
    assert "no transcripts found" in done.stdout
    assert list(home.iterdir()) == []


def test_b_transcripts_that_hold_no_tool_calls_also_leave_the_file_as_is(tmp):
    """Files exist but yield no target: still not a reason to write 'everything is rare'."""
    home = tmp / "home"
    home.mkdir()
    existing = home / "lesson-fire-ranks.json"
    existing.write_bytes(b'{"ranks": {}}\n')
    db = _db(tmp / "knowledge.db")
    (tmp / "t.jsonl").write_text("not json\n")
    done = _run(TOOL_LINK, ["--db", str(db), "--transcript-glob", str(tmp / "t.jsonl"), "--write-ranks"],
                _env(tmp, ENGRAM_HOME=str(home)), cwd=tmp)
    assert done.returncode == 0, done.stderr
    assert "rank file left as is" in done.stdout
    assert existing.read_bytes() == b'{"ranks": {}}\n'


# ---- defaults follow ENGRAM_HOME -----------------------------------------------------------------

def test_the_default_db_and_the_default_output_follow_engram_home(tmp):
    """Mutation: the default db back to ~/.engram/knowledge.db -> the tool reads (or fails on) HOME's store,
    not this ENGRAM_HOME's -> red."""
    home = tmp / "elsewhere"
    home.mkdir()
    _db(home / "knowledge.db")
    transcript = _transcript(tmp / "t.jsonl")
    done = _run(TOOL_LINK, ["--transcript-glob", str(transcript), "--write-ranks"],
                _env(tmp, ENGRAM_HOME=str(home)), cwd=tmp)
    assert done.returncode == 0, done.stderr
    assert json.loads((home / "lesson-fire-ranks.json").read_text())["ranks"] == {"lsx-001": 500.0}


# ---- (e) an interrupted write --------------------------------------------------------------------

def _load():
    spec = importlib.util.spec_from_file_location("lpa_3941_e", TOOL_LINK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_e_an_interrupted_write_leaves_the_old_file_and_no_half_file(tmp, monkeypatch):
    """Mutation: write straight to the final path (no temp file + os.replace) -> the old bytes are gone or
    half-written when the failure lands -> red."""
    mod = _load()
    path = tmp / "lesson-fire-ranks.json"
    path.write_bytes(b'{"ranks": {"lsx-001": 1.0}}\n')
    before = path.read_bytes()

    def boom(*a, **k):
        raise OSError("interrupted between the temp write and the replace")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        mod._write_whole_file_atomic(str(path), '{"ranks": {"lsx-001": 99.0}}\n')
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp.iterdir()) == ["lesson-fire-ranks.json"]


def test_e_a_keyboard_interrupt_cleans_up_too(tmp, monkeypatch):
    mod = _load()
    path = tmp / "lesson-fire-ranks.json"
    path.write_bytes(b"old\n")

    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        mod._write_whole_file_atomic(str(path), "new\n")
    monkeypatch.undo()
    assert path.read_bytes() == b"old\n"
    assert sorted(p.name for p in tmp.iterdir()) == ["lesson-fire-ranks.json"]


# ---- the move, the tier and the test map ---------------------------------------------------------

def test_the_tool_lives_in_src_with_a_repo_root_symlink():
    assert TOOL_SRC.is_file() and not TOOL_SRC.is_symlink()
    assert TOOL_LINK.is_symlink()
    assert os.readlink(TOOL_LINK) == "../src/engram/tools/lesson-pattern-audit.py"
    assert TOOL_LINK.resolve() == TOOL_SRC.resolve()


def test_the_tool_is_tiered_essential_and_no_longer_excluded():
    """Mutation: drop the tier entry, or leave the excluded[] entry in place -> red here (and the manifest
    gate's tag-once check goes red on the overlap)."""
    manifest = json.loads(TIERS.read_text())
    tiered = [m for m in manifest["mechanisms"] if m["path"] == TOOL_PATH_KEY]
    assert [m["tier"] for m in tiered] == ["essential"]
    assert TOOL_PATH_KEY not in {e["path"] for e in manifest.get("excluded", [])}


def test_the_test_map_names_the_tests():
    entries = [e for e in json.loads(TEST_MAP.read_text())["mappings"]
               if e.get("path_prefix_or_glob") == TOOL_PATH_KEY]
    assert len(entries) == 1
    globs = set(entries[0]["test_globs"])
    assert {"tests/test_lesson_pattern_audit.py", "tests/test_lesson_pattern_audit_shipped_3941.py"} <= globs
