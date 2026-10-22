"""#3882 (U19 of #2652), cell C6: the completeness test for the exclusive-maintenance class.

Every shipped tool that opens the live store directly must be exactly one of:
  (a) on the owner path          (ledger state `behind-flag` / `owner` / `flag-on`),
  (b) `direct-by-design: pre-owner`,
  (c) `direct-by-design: exclusive-maintenance` AND take the store-maintenance lock in its own source.
A tool that is in none of the three fails here. `direct` (not yet migrated) is a fourth state only for the
tools named in KNOWN_DIRECT, each with the reason; the allowlist is itself checked, so it cannot go stale.

The instrument (`unguarded_direct_openers`) is a pure function of (tools dir, ledger text), so the cells
below can PLANT a tool that opens the store directly without the lock and watch it go red.
Written before the implementation.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
TOOLS = SRC / "tools"
LEDGER = REPO / "docs" / "2652-migration-ledger.md"

_spec = importlib.util.spec_from_file_location("check_2652_ledger_c6", REPO / "tools" / "check_2652_ledger.py")
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)

SIX = (
    "ack_recompute_sweep.py",
    "cites_step3.py",
    "migrate_person_nodes.py",
    "migrate_prediction_pairing_edges.py",
    "migrate_stranded_supported_by.py",
    "surgical.py",
)
# The one entry point name every lock-taking tool calls (engram_core.acquire_store_maint_lock, or its
# print-and-return wrapper).
LOCK_CALLS = ("acquire_store_maint_lock", "acquire_store_maint_lock_or_report")
# Spellings that open the live store directly: a raw connect, or engram_core's `_get_db()` (which the
# ledger's connect-grep cannot see).
OPENER_ATTRS = ("connect", "_get_db")

# Tools whose live-store rows are still plain `direct` today, with the reason they are allowed to be.
KNOWN_DIRECT = {
    "anchoring_report.py": "read-only report; its own unit moves it onto the owner path",
}


def _calls(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                yield f.attr, f.value, n
            elif isinstance(f, ast.Name):
                yield f.id, None, n


def _sqlite_bindings(tree):
    r"""(module names bound to sqlite3, names bound to sqlite3.connect) anywhere in `tree`: `import sqlite3
    [as s]`, `from sqlite3 import connect [as c]`, and an assignment `X = <either spelling of connect>`
    (followed to a fixpoint, so `Y = X` is seen too). The ledger checker's `sqlite3\.connect\(` regex sees
    none of the aliased spellings; this instrument does."""
    mods, fns = {"sqlite3"}, set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.asname or a.name for a in n.names if a.name == "sqlite3")
        elif isinstance(n, ast.ImportFrom) and n.module == "sqlite3":
            fns.update(a.asname or a.name for a in n.names if a.name == "connect")

    def is_connect_ref(v):
        return (isinstance(v, ast.Attribute) and v.attr == "connect"
                and isinstance(v.value, ast.Name) and v.value.id in mods) or (
            isinstance(v, ast.Name) and v.id in fns)

    changed = True
    while changed:
        changed = False
        for n in ast.walk(tree):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, ast.AnnAssign) else []
            value = getattr(n, "value", None)
            if value is not None and is_connect_ref(value):
                for t in targets:
                    if isinstance(t, ast.Name) and t.id not in fns:
                        fns.add(t.id)
                        changed = True
    return mods, fns


def open_calls(tree, bindings=None):
    """Every Call node in `tree` that opens the store directly: a sqlite3 connect under any spelling in
    `bindings` (default: those bound in `tree` itself), or engram_core's `_get_db()`."""
    mods, fns = bindings if bindings is not None else _sqlite_bindings(tree)
    for name, owner, call in _calls(tree):
        if name == "connect" and isinstance(owner, ast.Name) and owner.id in mods:
            yield call
        elif owner is None and name in fns:
            yield call
        elif name == "_get_db":
            yield call


def opens_store_directly(tree) -> bool:
    return any(True for _ in open_calls(tree))


def takes_the_lock(tree) -> bool:
    return any(name in LOCK_CALLS for name, _, _ in _calls(tree))


def _ledger_by_file(text):
    """{file relative to tools/: [(state cell, target cell)]} for every Section-2 row naming a tools/ file."""
    states, _ = chk.row_column(text, "state", "states")
    targets, _ = chk.row_column(text, "target", "targets", report_width=False)
    firsts = dict(chk.section2_rows(text) or [])
    tgt = dict(targets)
    out = {}
    for num, state in states:
        cite = chk.CITE.match(firsts.get(num, ""))
        if not cite:
            continue
        path = cite.group(1)
        if not path.startswith("tools/"):
            continue
        out.setdefault(path[len("tools/"):], []).append((state, tgt.get(num, "")))
    return out


def _live(target_cell: str) -> bool:
    return chk.target_category(target_cell) in ("live store", "parameter")


def unguarded_direct_openers(tools_dir: Path, ledger_text: str, known_direct=KNOWN_DIRECT) -> list:
    """Problem lines; empty when every tool that opens the live store directly is accounted for."""
    by_file = _ledger_by_file(ledger_text)
    problems = []
    seen_direct = set()
    for path in sorted(Path(tools_dir).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rows = by_file.get(path.name, [])
        live_states = [s for s, t in rows if _live(t)]
        if not opens_store_directly(tree):
            continue
        locks = takes_the_lock(tree)
        if not rows:
            if not locks:
                problems.append(f"{path.name}: opens the store directly, is not in the ledger and takes no "
                                f"store-maintenance lock")
            continue
        if not live_states:
            continue  # every row is another database or prose: not the live store
        bad = False
        for s in live_states:
            head = s.split(":")[0].strip()
            phase = s.split(":", 1)[1].strip() if ":" in s else ""
            if head in ("behind-flag", "owner", "flag-on", "retired"):
                continue
            if head == "direct-by-design" and phase == "pre-owner":
                continue
            if head == "direct-by-design" and phase == "exclusive-maintenance":
                if not locks:
                    problems.append(f"{path.name}: ledger says exclusive-maintenance but the tool takes no "
                                    f"store-maintenance lock")
                    bad = True
                continue
            if head == "direct":
                seen_direct.add(path.name)
                if path.name not in known_direct:
                    problems.append(f"{path.name}: state `direct` and not on the known-direct list: move it "
                                    f"onto the owner path or make it exclusive-maintenance with the lock")
                    bad = True
                continue
            problems.append(f"{path.name}: unclassified state {s!r}")
            bad = True
        if bad:
            continue
    for name in sorted(set(known_direct) - seen_direct):
        problems.append(f"{name}: on the known-direct list but no longer `direct` in the ledger: drop it")
    return problems


# ---------------------------------------------------------------------------
# the instrument on the real tree
# ---------------------------------------------------------------------------

def test_real_tools_tree_every_direct_opener_is_accounted_for():
    problems = unguarded_direct_openers(TOOLS, LEDGER.read_text(encoding="utf-8"))
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("name", SIX)
def test_the_six_tools_take_the_lock_and_open_nothing_at_import_time(name):
    tree = ast.parse((TOOLS / name).read_text(encoding="utf-8"))
    assert takes_the_lock(tree), f"{name} takes no store-maintenance lock"
    # nothing at module level (outside every def) may open the store: the lock is taken in main, so an
    # import-time open would sit BEFORE it
    top = ast.Module(
        body=[n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))],
        type_ignores=[],
    )
    assert not opens_store_directly(top), f"{name} opens the store at import time"


def test_the_six_ledger_rows_are_exclusive_maintenance_and_name_the_lock():
    text = LEDGER.read_text(encoding="utf-8")
    by_file = _ledger_by_file(text)
    for name in SIX:
        live = [s for s, t in by_file[name] if _live(t)]
        assert live, name
        for s in live:
            assert s == "direct-by-design: exclusive-maintenance", (name, s)
    # the ledger names the lock and the unit
    assert ".maint.lockdir" in text and "#3882" in text


# ---------------------------------------------------------------------------
# C6: the instrument reddens on a PLANTED direct opener without the lock
# ---------------------------------------------------------------------------

_HEAD = (
    "| # | site (function) | kind | target | access | component | state | A6 delta | macOS |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
)


def _ledger(rows):
    body = "".join(
        f"| {i} | `tools/{f}` `main()` `c = sqlite3.connect(` | call | {target} | rw | tool (CLI) | {state} | — | n/a |\n"
        for i, (f, target, state) in enumerate(rows, start=1)
    )
    return "# ledger\n\n## 2. Rows\n\n" + _HEAD + body + "\n## 3. Registry\n\nnothing\n"


OPENS = "import sqlite3\n\ndef main():\n    return sqlite3.connect('x.db')\n"
OPENS_GETDB = "import engram_core as core\n\ndef main():\n    return core._get_db()\n"
LOCKED = ("import sqlite3\nimport engram_core as core\n\ndef main():\n"
          "    fd = core.acquire_store_maint_lock('x.db', 't')\n    return sqlite3.connect('x.db')\n")
LOCKED_WRAPPED = ("import sqlite3\nimport engram_core as core\n\ndef main():\n"
                  "    fd = core.acquire_store_maint_lock_or_report('x.db', 't')\n    return sqlite3.connect('x.db')\n")


def _tools(tmp_path, files):
    d = tmp_path / "tools"
    d.mkdir()
    for name, src in files.items():
        (d / name).write_text(src)
    return d


EXCL = "direct-by-design: exclusive-maintenance"


def test_c6_a_planted_tool_that_opens_directly_without_the_lock_and_is_not_in_the_ledger_is_red(tmp_path):
    d = _tools(tmp_path, {"rogue.py": OPENS})
    problems = unguarded_direct_openers(d, _ledger([]), known_direct={})
    assert any("rogue.py" in p and "takes no" in p for p in problems), problems


def test_c6_a_planted_get_db_opener_without_the_lock_is_red_too(tmp_path):
    """The ledger's connect-grep cannot see `core._get_db()`; the instrument can."""
    d = _tools(tmp_path, {"rogue2.py": OPENS_GETDB})
    problems = unguarded_direct_openers(d, _ledger([]), known_direct={})
    assert any("rogue2.py" in p for p in problems), problems


def test_c6_the_same_tool_with_the_lock_is_green(tmp_path):
    d = _tools(tmp_path, {"good.py": LOCKED, "good2.py": LOCKED_WRAPPED})
    assert unguarded_direct_openers(d, _ledger([]), known_direct={}) == []


def test_c6_ledger_says_exclusive_maintenance_but_the_tool_takes_no_lock_is_red(tmp_path):
    d = _tools(tmp_path, {"liar.py": OPENS})
    problems = unguarded_direct_openers(d, _ledger([("liar.py", "live store", EXCL)]), known_direct={})
    assert any("liar.py" in p and "exclusive-maintenance" in p for p in problems), problems


def test_c6_exclusive_maintenance_with_the_lock_is_green(tmp_path):
    d = _tools(tmp_path, {"fine.py": LOCKED})
    assert unguarded_direct_openers(d, _ledger([("fine.py", "live store", EXCL)]), known_direct={}) == []


def test_c6_plain_direct_is_red_unless_on_the_known_direct_list_and_a_stale_entry_is_red(tmp_path):
    d = _tools(tmp_path, {"old.py": OPENS})
    led = _ledger([("old.py", "live store", "direct")])
    assert any("old.py" in p for p in unguarded_direct_openers(d, led, known_direct={}))
    assert unguarded_direct_openers(d, led, known_direct={"old.py": "why"}) == []
    moved = _ledger([("old.py", "live store", "behind-flag")])
    stale = unguarded_direct_openers(d, moved, known_direct={"old.py": "why"})
    assert any("old.py" in p and "no longer" in p for p in stale), stale


def test_c6_owner_path_pre_owner_and_other_db_tools_are_green(tmp_path):
    d = _tools(tmp_path, {"flagged.py": OPENS, "boot.py": OPENS, "pkg.py": OPENS})
    led = _ledger([
        ("flagged.py", "live store", "behind-flag"),
        ("boot.py", "live store", "direct-by-design: pre-owner"),
        ("pkg.py", "other DB: a package db", "out of scope"),
    ])
    assert unguarded_direct_openers(d, led, known_direct={}) == []


def test_c6_a_tool_that_never_opens_the_store_is_not_in_scope(tmp_path):
    d = _tools(tmp_path, {"calm.py": "def main():\n    return 1\n"})
    assert unguarded_direct_openers(d, _ledger([]), known_direct={}) == []


# ---------------------------------------------------------------------------
# round 3, T4: the opener detection also sees the aliased spellings of sqlite3.connect
# (the ledger checker's `sqlite3\.connect\(` regex shares this gap and is NOT changed in this PR)
# ---------------------------------------------------------------------------

_LOCK_LINE = "    fd = core.acquire_store_maint_lock('x.db', 't')\n"
ALIASED_OPENERS = {
    "from-import": ("from sqlite3 import connect\nimport engram_core as core\n\ndef main():\n{lock}"
                    "    return connect('x.db')\n"),
    "from-import-as": ("from sqlite3 import connect as c\nimport engram_core as core\n\ndef main():\n{lock}"
                       "    return c('x.db')\n"),
    "module-alias": ("import sqlite3 as s\nimport engram_core as core\n\ndef main():\n{lock}"
                    "    return s.connect('x.db')\n"),
    "assigned-alias": ("import sqlite3\nimport engram_core as core\nDB_CONNECT = sqlite3.connect\n\n"
                       "def main():\n{lock}    return DB_CONNECT('x.db')\n"),
}


@pytest.mark.parametrize("spelling", sorted(ALIASED_OPENERS))
def test_r3_a_planted_aliased_opener_without_the_lock_is_red(tmp_path, spelling):
    d = _tools(tmp_path, {"rogue3.py": ALIASED_OPENERS[spelling].format(lock="")})
    problems = unguarded_direct_openers(d, _ledger([]), known_direct={})
    assert any("rogue3.py" in p and "takes no" in p for p in problems), (spelling, problems)


@pytest.mark.parametrize("spelling", sorted(ALIASED_OPENERS))
def test_r3_the_same_aliased_opener_with_the_lock_is_green(tmp_path, spelling):
    d = _tools(tmp_path, {"fine3.py": ALIASED_OPENERS[spelling].format(lock=_LOCK_LINE)})
    assert unguarded_direct_openers(d, _ledger([]), known_direct={}) == []


# ---------------------------------------------------------------------------
# round 3, T5: the lock is taken BEFORE the first store open, not merely somewhere in the source
# (`takes_the_lock` is presence-only). The runtime cell test_c1_each_tool_already_holds_the_lock_at_the_
# moment_of_its_first_store_open in test_store_maint_lock_3882.py does pin the order, but it needs a seat
# the owner supports and spawns real tools; this is the static, platform-independent counterpart.
# ---------------------------------------------------------------------------

ENTRY_NAMES = ("main", "_main")


def _opening_functions(tree, bindings):
    """Names of module-level functions that open the store directly, or call one that does (to a fixpoint)."""
    top = {f.name: f for f in tree.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
    opening = {n for n, f in top.items() if any(True for _ in open_calls(f, bindings))}
    changed = True
    while changed:
        changed = False
        for n, f in top.items():
            if n not in opening and any(name in opening for name, _, _ in _calls(f)):
                opening.add(n)
                changed = True
    return opening


def lock_precedes_first_open(tree):
    """(ok, detail): inside the tool's entry function (`main` / `_main`), the first store-maintenance lock
    call comes at an earlier source line than the first call that opens the store -- directly, or through a
    module-level helper that does. A tool with no lock call, or with no resolvable open, is not ok."""
    top = {f.name: f for f in tree.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
    # cites_step3 has a thin `main` over `_main`: the entry is the one that holds the lock call
    entries = [top[n] for n in ENTRY_NAMES if n in top]
    entry = next((e for e in entries if any(nm in LOCK_CALLS for nm, _, _ in _calls(e))),
                 entries[0] if entries else None)
    if entry is None:
        return False, "no main()/_main()"
    bindings = _sqlite_bindings(tree)
    opening = _opening_functions(tree, bindings)
    lock_lines = [c.lineno for name, _, c in _calls(entry) if name in LOCK_CALLS]
    open_lines = [c.lineno for c in open_calls(entry, bindings)]
    open_lines += [c.lineno for name, owner, c in _calls(entry) if owner is None and name in opening]
    if not lock_lines:
        return False, "the entry function takes no lock"
    if not open_lines:
        return False, "no store open found in the entry function (instrument blind)"
    ok = min(lock_lines) < min(open_lines)
    return ok, f"first lock line {min(lock_lines)}, first open line {min(open_lines)}"


@pytest.mark.parametrize("name", SIX)
def test_r3_each_of_the_six_tools_takes_the_lock_before_its_first_store_open(name):
    tree = ast.parse((TOOLS / name).read_text(encoding="utf-8"))
    ok, detail = lock_precedes_first_open(tree)
    assert ok, f"{name}: {detail}"


_BEFORE = ("import sqlite3\nimport engram_core as core\n\ndef main():\n"
           "    fd = core.acquire_store_maint_lock('x.db', 't')\n    return sqlite3.connect('x.db')\n")
_AFTER = ("import sqlite3\nimport engram_core as core\n\ndef main():\n"
          "    c = sqlite3.connect('x.db')\n    fd = core.acquire_store_maint_lock('x.db', 't')\n    return c\n")
_HELPER_AFTER = ("import sqlite3\nimport engram_core as core\n\ndef _db():\n    return sqlite3.connect('x.db')\n\n"
                 "def main():\n    c = _db()\n    fd = core.acquire_store_maint_lock_or_report('x.db', 't')\n    return c\n")
_HELPER_BEFORE = ("import sqlite3\nimport engram_core as core\n\ndef _db():\n    return sqlite3.connect('x.db')\n\n"
                  "def main():\n    fd = core.acquire_store_maint_lock_or_report('x.db', 't')\n    return _db()\n")


def test_r3_the_order_instrument_is_red_when_the_lock_comes_after_the_open_and_green_before():
    assert lock_precedes_first_open(ast.parse(_BEFORE))[0] is True
    assert lock_precedes_first_open(ast.parse(_AFTER))[0] is False
    assert lock_precedes_first_open(ast.parse(_HELPER_BEFORE))[0] is True
    assert lock_precedes_first_open(ast.parse(_HELPER_AFTER))[0] is False
    # presence-only would have passed the wrong-order tools: that is what this cell exists to refuse
    assert takes_the_lock(ast.parse(_AFTER)) and takes_the_lock(ast.parse(_HELPER_AFTER))


def test_r3_a_tool_with_no_lock_or_no_resolvable_open_is_not_ok():
    assert lock_precedes_first_open(ast.parse("import sqlite3\ndef main():\n    return sqlite3.connect('x')\n"))[0] is False
    assert lock_precedes_first_open(ast.parse(
        "import engram_core as core\ndef main():\n    return core.acquire_store_maint_lock('x', 't')\n"))[0] is False


def test_r3_a_connect_that_is_not_sqlite3s_is_not_an_opener(tmp_path):
    src = "from socket import connect\n\ndef main():\n    return connect(('h', 1))\n"
    d = _tools(tmp_path, {"net.py": src})
    assert unguarded_direct_openers(d, _ledger([]), known_direct={}) == []
