"""Behaviour contract for making the lesson-tripwire loaders' fail-soft `except`
visible (#3503, follow-up to #3485 / #3502). Cells K1-K6 of the spec.

Written BEFORE the fix (contract-first; project CLAUDE.md, "Contract-first
tests"). The defect: both loaders (`load_tripwires`, `load_prevention_lessons`)
end in `except Exception: return []`, so a caller cannot tell "no lessons" from
"the loader failed". The fix records the failure out of band (a module-level
list + one sanitized stderr line) and shows ONE bounded, rate-limited line in
the injected context.

Contract (K1-K7):
  K1 fail-soft unchanged: each loader returns [] and never raises.
  K2 record on failure: LOADER_FAILURES entry + exactly one stderr line
     `lesson-tripwire: <loader> failed: <ExcType>: <message>` (message
     sanitized, <= 200 chars; the whole line <= 300).
  K3 clean path (incl. an empty result) records nothing, writes nothing, and
     the injected output is byte-identical to the pre-change output.
  K4 one bounded visible line, at most once per 1800 s (stamp file); an
     unreadable / corrupt / unwritable stamp fails VISIBLE, never raises.
  K5 a failure of EITHER loader shows the line, with no hits gate: a
     prevention-only failure with nothing else to emit makes the line the whole
     block (the blocking deny gate is off for the run and the hook never blocks
     on an infrastructure failure), rendered by emit_context like the
     load_tripwires-failure case.
  K6 the whole block stays within the ceiling when the line is present.
  K7 the rate-limit stamp is written only AFTER the response carrying the line
     was printed: an exit between staging and printing (or a failed print)
     leaves no stamp, so the next failure shows the line again.

Cell design: every injection cell has a CONTROL on the same fixture without the
injected failure. K3's golden output was captured from the UNMODIFIED hook, so
that cell is green before and after by design.
"""
from __future__ import annotations

import importlib.util
import io
import json
import re
import sqlite3
import sys
import unittest.mock
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.append(_SRC_ENGRAM)

HOOK_PATH = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-lesson-tripwire-hook.py"

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def hook(tmp_path, monkeypatch):
    mod = _load("lesson_tripwire_hook_3503", HOOK_PATH)
    # Stamp file lives in the test's own dir (attribute may not exist before
    # the fix: raising=False keeps the pre-fix red about behaviour, not setup).
    monkeypatch.setattr(mod, "LOADER_NOTICE_STAMP_PATH", tmp_path / "stamp" / "notice.json", raising=False)
    mod._ORIG_LOADERS = (mod.load_tripwires, mod.load_prevention_lessons)
    return mod


GOOD_META = {
    "situation_pattern": "git push --force",
    "prevention_type": "shadow",
    "scaffolding_nudge": "do not force push",
    "tripwire_priority": "high",
}
LINE_RE = re.compile(r"^lesson-tripwire: (load_tripwires|load_prevention_lessons) failed: (\w+): (.*)$")
BOOM = "boom"


def _make_db(tmp_path, with_row=True, name="k.db"):
    db = tmp_path / name
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE nodes (id TEXT, type TEXT, is_current INTEGER, "
        "memory_status TEXT, claim TEXT, metadata TEXT, recall_summary TEXT)"  # recall_summary: alpha#3924, read by load_tripwires
    )
    if with_row:
        conn.execute(
            "INSERT INTO nodes (id, type, is_current, memory_status, claim, metadata) VALUES (?,?,?,?,?,?)",
            ("ls_good", "lesson", 1, "active", "good claim", json.dumps(GOOD_META)),
        )
    conn.commit()
    conn.close()
    return db


def _boom(*a, **k):
    raise RuntimeError(BOOM)


def _inject(kind, tmp_path, monkeypatch, hook):
    """Return the db path a loader should be pointed at to FAIL in manner `kind`."""
    if kind == "raises":
        db = _make_db(tmp_path)
        monkeypatch.setattr(hook.sqlite3, "connect", _boom)
        return db
    if kind == "not-a-db":
        db = tmp_path / "junk.db"
        db.write_bytes(b"this is definitely not a sqlite database\n" * 50)
        return db
    if kind == "no-nodes-table":
        db = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE other (x)")
        conn.commit()
        conn.close()
        return db
    if kind == "locked":
        db = _make_db(tmp_path)
        locker = sqlite3.connect(str(db), isolation_level=None)
        locker.execute("BEGIN EXCLUSIVE")
        real_connect = sqlite3.connect

        def quick(*a, **k):
            k["timeout"] = 0.05  # real lock, shortened wait: deterministic, no 2 s sleep
            return real_connect(*a, **k)

        monkeypatch.setattr(hook.sqlite3, "connect", quick)
        _LOCKERS.append(locker)
        return db
    raise AssertionError(kind)


_LOCKERS: list = []
INJECTIONS = ["raises", "not-a-db", "no-nodes-table", "locked"]
LOADERS = ["load_tripwires", "load_prevention_lessons"]


@pytest.fixture(autouse=True)
def _release_lock():
    yield
    while _LOCKERS:
        _LOCKERS.pop().close()


def _expected_control(loader):
    if loader == "load_tripwires":
        return [("git push --force", "do not force push", "ls_good", "high", "full", None, "good claim")]  # 5th: match_scope (#3920); 6th/7th: recall_summary, claim (#3924)
    return [{"id": "ls_good", "pattern": "git push --force",
             "prevention_type": "shadow", "nudge": "do not force push"}]


def _failures(hook):
    return list(getattr(hook, "LOADER_FAILURES"))


# --- K1: fail-soft unchanged ---------------------------------------------------

@pytest.mark.parametrize("loader", LOADERS)
def test_k1_control_clean_fixture_returns_rows(hook, tmp_path, loader):
    """CONTROL: the same fixture without an injected failure returns the row."""
    db = _make_db(tmp_path)
    assert getattr(hook, loader)(db) == _expected_control(loader)


@pytest.mark.parametrize("kind", INJECTIONS)
@pytest.mark.parametrize("loader", LOADERS)
def test_k1_injected_failure_returns_empty_and_never_raises(hook, tmp_path, monkeypatch, loader, kind):
    """Red mutation (f): catching only sqlite3.Error makes the `raises`
    (RuntimeError) cells escape."""
    db = _inject(kind, tmp_path, monkeypatch, hook)
    assert getattr(hook, loader)(db) == []


# --- K2: record on failure -----------------------------------------------------

@pytest.mark.parametrize("kind", INJECTIONS)
@pytest.mark.parametrize("loader", LOADERS)
def test_k2_failure_is_recorded_and_one_stderr_line_written(hook, tmp_path, monkeypatch, capsys, loader, kind):
    """Red mutation (a): removing the record/stderr write from one loader reddens
    that loader's cells only."""
    db = _inject(kind, tmp_path, monkeypatch, hook)
    capsys.readouterr()
    assert getattr(hook, loader)(db) == []
    err = capsys.readouterr().err
    lines = [ln for ln in err.split("\n") if ln]
    assert len(lines) == 1, err
    m = LINE_RE.match(lines[0])
    assert m and m.group(1) == loader, lines[0]
    rec = _failures(hook)
    assert len(rec) == 1
    assert rec[0]["loader"] == loader
    assert rec[0]["exc_type"] == m.group(2)
    assert isinstance(rec[0]["message"], str)
    if kind == "raises":
        assert m.group(2) == "RuntimeError" and m.group(3) == BOOM


@pytest.mark.parametrize("loader", LOADERS)
def test_k2_message_is_sanitized_and_bounded(hook, tmp_path, monkeypatch, capsys, loader):
    """A message with a newline, a control char and 500 characters is flattened
    to one line, cut to 200 characters; the whole line is <= 300."""
    db = _make_db(tmp_path)

    def evil(*a, **k):
        raise RuntimeError("a\nb\x07" + "x" * 500)

    monkeypatch.setattr(hook.sqlite3, "connect", evil)
    capsys.readouterr()
    assert getattr(hook, loader)(db) == []
    err = capsys.readouterr().err
    assert err.endswith("\n") and err.count("\n") == 1
    line = err[:-1]
    assert line == f"lesson-tripwire: {loader} failed: RuntimeError: " + ("a b " + "x" * 196)
    assert len(line) <= 300
    assert not re.search(r"[\x00-\x1f\x7f]", line)
    assert not re.search(r"[\x00-\x1f\x7f]", _failures(hook)[0]["message"])


def test_k2_whole_line_capped_at_300_even_with_long_type_name(hook, tmp_path, monkeypatch, capsys):
    db = _make_db(tmp_path)
    Long = type("E" * 400, (Exception,), {})

    def evil(*a, **k):
        raise Long("m" * 400)

    monkeypatch.setattr(hook.sqlite3, "connect", evil)
    capsys.readouterr()
    assert hook.load_tripwires(db) == []
    line = capsys.readouterr().err.rstrip("\n")
    assert line.startswith("lesson-tripwire: load_tripwires failed: ") and "\n" not in line
    assert len(line) <= 300


# --- K3: clean path writes nothing --------------------------------------------

@pytest.mark.parametrize("loader", LOADERS)
@pytest.mark.parametrize("with_row", [True, False], ids=["rows", "empty-result"])
def test_k3_clean_path_records_nothing_and_writes_nothing(hook, tmp_path, capsys, loader, with_row):
    """Red mutation (d): writing the line when the result is empty."""
    db = _make_db(tmp_path, with_row=with_row)
    capsys.readouterr()
    out = getattr(hook, loader)(db)
    assert (out == []) == (not with_row)
    cap = capsys.readouterr()
    assert cap.err == "" and cap.out == ""
    assert _failures(hook) == []


def test_k3_missing_db_file_is_not_a_failure(hook, tmp_path, capsys):
    """No database file at all is the pre-existing silent `return []` before the
    try; it is not a loader failure and stays silent."""
    capsys.readouterr()
    assert hook.load_tripwires(tmp_path / "nope.db") == []
    assert hook.load_prevention_lessons(tmp_path / "nope.db") == []
    assert capsys.readouterr().err == ""
    assert _failures(hook) == []


# --- main() driver -------------------------------------------------------------

HOOK_INPUT = {"tool_name": "Bash", "tool_input": {"command": "git push --force origin x"}}


def _run_main(hook, monkeypatch, capsys, *, trip_db=None, prev_db=None, recall="", tripwires=None,
              prevention=None, now=None, out=None):
    """Run main() with the real loaders pointed at the given dbs (or patched
    results). Returns (stdout, stderr)."""
    orig_lt, orig_lp = hook._ORIG_LOADERS  # captured once: re-wrapping a wrapper would break
    if trip_db is not None:
        monkeypatch.setattr(hook, "load_tripwires", lambda: orig_lt(trip_db))
    elif tripwires is not None:
        monkeypatch.setattr(hook, "load_tripwires", lambda: tripwires)
    if prev_db is not None:
        monkeypatch.setattr(hook, "load_prevention_lessons", lambda: orig_lp(prev_db))
    elif prevention is not None:
        monkeypatch.setattr(hook, "load_prevention_lessons", lambda: prevention)
    monkeypatch.setattr(hook, "check_in_turn_recall", lambda *a, **k: recall)
    monkeypatch.setattr(hook, "_ws4_config", lambda: {"active": False, "seed": ""})
    monkeypatch.setattr(hook, "_append_prevention_ledger", lambda *a, **k: None)
    if now is not None:
        monkeypatch.setattr(hook, "_loader_notice_now", lambda: now[0], raising=False)
    out = io.StringIO() if out is None else out
    capsys.readouterr()
    with unittest.mock.patch("sys.stdin", io.StringIO(json.dumps(HOOK_INPUT))), \
            unittest.mock.patch("sys.stdout", out):
        try:
            hook.main()
        except SystemExit:
            pass
    return (out.getvalue() if hasattr(out, "getvalue") else ""), capsys.readouterr().err


def _ctx(stdout):
    return json.loads(stdout)["hookSpecificOutput"]["additionalContext"] if stdout else ""


_TAIL = "tripwires or blocking lessons may be missing this prompt"
NOTICE_TRIP = f"[lesson-tripwire] load_tripwires failed (RuntimeError); {_TAIL}"
NOTICE_PREV = f"[lesson-tripwire] load_prevention_lessons failed (RuntimeError); {_TAIL}"
NOTICE_BOTH = f"[lesson-tripwire] load_tripwires, load_prevention_lessons failed (RuntimeError); {_TAIL}"


def _notice_lines(ctx):
    return [ln for ln in ctx.split("\n") if ln.startswith("[lesson-tripwire]") and " failed (" in ln]


# Golden output captured from the UNMODIFIED hook (origin/dev 51a06f38) for the
# clean fixture below: one matching tripwire, no prevention rows.
GOLDEN_CLEAN_STDOUT = '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "\\u25b8 1 shown \\u00b7 0 cut\\n[lesson-tripwire] Action-moment pattern matched \\u2014 remember:\\n  \\u2022 do not force push"}}\n'


def test_k3_clean_main_output_is_byte_identical_to_pre_change(hook, tmp_path, monkeypatch, capsys):
    """Golden captured on unmodified origin/dev. Red mutations (d)/(a-variants)
    that alter the clean-path output move this cell; it is green before the fix
    by design (the control for the whole contract)."""
    db = _make_db(tmp_path)
    out, err = _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db)
    assert out == GOLDEN_CLEAN_STDOUT
    assert err == ""
    assert not hook.LOADER_NOTICE_STAMP_PATH.exists()


def test_k3_clean_empty_result_main_emits_nothing(hook, tmp_path, monkeypatch, capsys):
    db = _make_db(tmp_path, with_row=False)
    out, err = _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db)
    assert out == "" and err == ""


# --- K4: one bounded visible line, rate limited -------------------------------

def _failing_main(hook, monkeypatch, capsys, tmp_path, clock, kind="raises", **kw):
    db = _make_db(tmp_path, name=f"f{clock[0]}.db")
    return _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=clock, **kw)


def test_k4_control_no_failure_no_line_even_when_block_emitted(hook, tmp_path, monkeypatch, capsys):
    db = _make_db(tmp_path)
    out, _ = _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=[1000.0])
    assert _notice_lines(_ctx(out)) == []


def test_k4_first_failure_shows_exactly_one_bounded_line(hook, tmp_path, monkeypatch, capsys):
    db = _make_db(tmp_path)
    monkeypatch.setattr(hook.sqlite3, "connect", _boom)
    out, err = _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=[1000.0])
    ctx = _ctx(out)
    assert _notice_lines(ctx) == [NOTICE_BOTH]
    assert len(NOTICE_BOTH) <= 200
    assert len([ln for ln in err.split("\n") if ln]) == 2  # one stderr line per failed loader


def test_k4_second_failure_within_window_hides_line_but_stderr_stays(hook, tmp_path, monkeypatch, capsys):
    """Red mutation (b): dropping the rate limit reddens this cell, not the first-failure cell."""
    db = _make_db(tmp_path)
    monkeypatch.setattr(hook.sqlite3, "connect", _boom)
    clock = [1000.0]
    out1, _ = _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=clock)
    assert _notice_lines(_ctx(out1)) == [NOTICE_BOTH]
    clock[0] = 1000.0 + 1799.0
    out2, err2 = _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=clock)
    assert _notice_lines(_ctx(out2)) == []
    assert len([ln for ln in err2.split("\n") if ln]) == 2


def test_k4_after_window_shows_again_without_sleeping(hook, tmp_path, monkeypatch, capsys):
    db = _make_db(tmp_path)
    monkeypatch.setattr(hook.sqlite3, "connect", _boom)
    clock = [1000.0]
    _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=clock)
    clock[0] = 1000.0 + 1800.0
    out, _ = _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=clock)
    assert _notice_lines(_ctx(out)) == [NOTICE_BOTH]
    # and the window restarts from the new showing
    clock[0] += 10
    out, _ = _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=clock)
    assert _notice_lines(_ctx(out)) == []


@pytest.mark.parametrize("junk", ["not json {{", "", "[]", '{"shown_at": "soon"}', '{"shown_at": null}'])
def test_k4_corrupt_stamp_file_still_shows_line(hook, tmp_path, monkeypatch, capsys, junk):
    """Red mutation (c): swallowing the stamp failure silently reddens this."""
    db = _make_db(tmp_path)
    monkeypatch.setattr(hook.sqlite3, "connect", _boom)
    hook.LOADER_NOTICE_STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
    hook.LOADER_NOTICE_STAMP_PATH.write_text(junk)
    out, _ = _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=[5000.0])
    assert _notice_lines(_ctx(out)) == [NOTICE_BOTH]


def test_k4_future_dated_stamp_fails_visible(hook, tmp_path, monkeypatch, capsys):
    db = _make_db(tmp_path)
    monkeypatch.setattr(hook.sqlite3, "connect", _boom)
    hook.LOADER_NOTICE_STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
    hook.LOADER_NOTICE_STAMP_PATH.write_text(json.dumps({"shown_at": 9e12}))
    out, _ = _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=[5000.0])
    assert _notice_lines(_ctx(out)) == [NOTICE_BOTH]


def test_k4_unwritable_runtime_dir_still_shows_line_and_does_not_raise(hook, tmp_path, monkeypatch, capsys):
    """Red mutation (c): the stamp WRITE failing must not hide the line."""
    db = _make_db(tmp_path)
    monkeypatch.setattr(hook.sqlite3, "connect", _boom)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, so nothing can be created beneath it")
    monkeypatch.setattr(hook, "LOADER_NOTICE_STAMP_PATH", blocker / "sub" / "notice.json")
    out, _ = _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=[5000.0])
    assert _notice_lines(_ctx(out)) == [NOTICE_BOTH]


def test_k4_tripwire_failure_alone_emits_line_with_no_hits(hook, tmp_path, monkeypatch, capsys):
    """The tripwire loader failing means there are no hits, so nothing else
    would be emitted: the line is the only content, routed through
    emit_context (trailer first)."""
    bad = tmp_path / "junk.db"
    bad.write_bytes(b"not a database" * 100)
    good = _make_db(tmp_path)
    out, err = _run_main(hook, monkeypatch, capsys, trip_db=bad, prev_db=good, now=[1000.0])
    ctx = _ctx(out)
    assert ctx.split("\n")[0].startswith("▸ ")
    lines = _notice_lines(ctx)
    assert len(lines) == 1
    assert lines[0].startswith("[lesson-tripwire] load_tripwires failed (DatabaseError); tripwires or blocking lessons")
    assert len(lines[0]) <= 200
    assert "load_tripwires failed: DatabaseError:" in err


# --- K5: where each loader's failure goes --------------------------------------

def test_k5_prevention_failure_alone_shows_the_line_as_the_whole_block(hook, tmp_path, monkeypatch, capsys):
    """Prevention loader fails, tripwire loader ok, no hit, no recall: the line
    IS the block (the blocking gate is off and nothing else would signal it),
    rendered through emit_context (trailer first, within the ceiling), the stamp
    is written after the print, and a second run inside the window is silent.
    Red mutation (b): putting the has_hits gate back emits nothing here; red
    mutation (d): a bare print loses the leading trailer."""
    good = _make_db(tmp_path, with_row=False)
    bad = tmp_path / "junk.db"
    bad.write_bytes(b"not a database" * 100)
    clock = [1000.0]
    out, err = _run_main(hook, monkeypatch, capsys, trip_db=good, prev_db=bad, now=clock)
    ctx = _ctx(out)
    expected = NOTICE_PREV.replace("RuntimeError", "DatabaseError")
    assert _notice_lines(ctx) == [expected]
    assert len(expected) <= 200
    assert ctx.split("\n")[0].startswith("\u25b8 ")  # emit_context trailer leads
    assert len(ctx) <= hook.TRIPWIRE_BLOCK_CHAR_CEILING
    assert "lesson-tripwire: load_prevention_lessons failed: DatabaseError:" in err
    assert [f["loader"] for f in _failures(hook)] == ["load_prevention_lessons"]
    assert hook.LOADER_NOTICE_STAMP_PATH.exists()  # shown => window consumed
    clock[0] += 10
    out2, err2 = _run_main(hook, monkeypatch, capsys, trip_db=good, prev_db=bad, now=clock)
    assert out2 == ""
    assert "load_prevention_lessons failed:" in err2  # stderr + record unchanged


def test_k5_prevention_failure_with_a_tripwire_block_shows_line_in_that_block(hook, tmp_path, monkeypatch, capsys):
    good = _make_db(tmp_path)
    bad = tmp_path / "junk.db"
    bad.write_bytes(b"not a database" * 100)
    out, err = _run_main(hook, monkeypatch, capsys, trip_db=good, prev_db=bad, now=[1000.0])
    ctx = _ctx(out)
    assert "do not force push" in ctx
    lines = _notice_lines(ctx)
    assert len(lines) == 1 and "load_prevention_lessons failed (DatabaseError)" in lines[0]
    assert "load_prevention_lessons failed:" in err


def test_k5_main_resets_the_record_each_run(hook, tmp_path, monkeypatch, capsys):
    bad = tmp_path / "junk.db"
    bad.write_bytes(b"not a database" * 100)
    good = _make_db(tmp_path, with_row=False)
    _run_main(hook, monkeypatch, capsys, trip_db=good, prev_db=bad, now=[1000.0])
    assert len(_failures(hook)) == 1
    _run_main(hook, monkeypatch, capsys, trip_db=good, prev_db=good, now=[1000.0])
    assert _failures(hook) == []


# --- K6: bounded -----------------------------------------------------------------

def _near_ceiling_tripwires(hook, monkeypatch, capsys, notice_prev_db, tmp_path):
    """Find a nudge length whose clean block lands within 25 chars of the ceiling."""
    clean = _make_db(tmp_path, with_row=False)
    ceiling = hook.TRIPWIRE_BLOCK_CHAR_CEILING
    for pad in range(40, 200):
        trips = [("git push --force", f"n{i:02d} " + "z" * pad, f"ls_{i:04d}", "normal") for i in range(40)]
        out, _ = _run_main(hook, monkeypatch, capsys, tripwires=trips, prev_db=clean, now=[1.0])
        n = len(_ctx(out))
        if ceiling - 25 <= n <= ceiling:
            return trips, n
    raise AssertionError("could not build a near-ceiling fixture")


def test_k6_near_ceiling_block_plus_line_stays_within_bound(hook, tmp_path, monkeypatch, capsys):
    """Red mutation (e): rendering the line outside emit_context (a bare
    append/print after the block) pushes the total past the ceiling."""
    # alpha#3924: 40 co-firing hits is above CLUSTER_THRESHOLD; this cell pins the ORIGINAL bounded
    # block (loader line + capped bullets under the 1800 ceiling), so the threshold is put out of reach.
    monkeypatch.setattr(hook, "CLUSTER_THRESHOLD", 10 ** 6)
    trips, base_len = _near_ceiling_tripwires(hook, monkeypatch, capsys, None, tmp_path)
    bad = tmp_path / "junk.db"
    bad.write_bytes(b"not a database" * 100)
    out, _ = _run_main(hook, monkeypatch, capsys, tripwires=trips, prev_db=bad, now=[1.0])
    ctx = _ctx(out)
    assert len(_notice_lines(ctx)) == 1
    assert len(ctx) <= hook.TRIPWIRE_BLOCK_CHAR_CEILING
    assert ctx.split("\n")[0].startswith("▸ ")  # trailer still leads


def test_k6_line_never_carries_raw_exception_text(hook, tmp_path, monkeypatch, capsys):
    db = _make_db(tmp_path)

    def evil(*a, **k):
        raise RuntimeError("line1\nSECRET-ish line2 " + "y" * 600)

    monkeypatch.setattr(hook.sqlite3, "connect", evil)
    out, err = _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=[1.0])
    ctx = _ctx(out)
    lines = _notice_lines(ctx)
    assert lines == [NOTICE_BOTH]
    assert "SECRET-ish" not in ctx and "line1" not in ctx
    assert all(len(ln) <= 300 for ln in err.split("\n"))


# --- K7: the stamp is written only after the print ----------------------------

def _stamp_spy(hook, monkeypatch, out):
    calls = []
    real = hook._mark_loader_notice_shown

    def spy(now):
        calls.append(out.getvalue())  # what stdout held when the stamp was written
        return real(now)

    monkeypatch.setattr(hook, "_mark_loader_notice_shown", spy)
    return calls


def test_k7_normal_run_writes_the_stamp_exactly_once_after_the_print(hook, tmp_path, monkeypatch, capsys):
    db = _make_db(tmp_path)
    monkeypatch.setattr(hook.sqlite3, "connect", _boom)
    out = io.StringIO()
    calls = _stamp_spy(hook, monkeypatch, out)
    _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=[1000.0], out=out)
    assert len(calls) == 1
    assert NOTICE_BOTH in _ctx(calls[0])  # the response was already printed
    assert hook.LOADER_NOTICE_STAMP_PATH.exists()


def test_k7_clean_run_never_calls_the_stamp_writer(hook, tmp_path, monkeypatch, capsys):
    db = _make_db(tmp_path)
    out = io.StringIO()
    calls = _stamp_spy(hook, monkeypatch, out)
    _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=[1000.0], out=out)
    assert calls == []


def test_k7_exit_between_staging_and_print_leaves_no_stamp_and_next_run_shows_line(hook, tmp_path, monkeypatch, capsys):
    """An unguarded exception after the line is staged (here: building the JSON
    response) is swallowed by the outer handler. Red mutation (a): stamping at
    staging time leaves a stamp here and the second run is silent."""
    db = _make_db(tmp_path)
    monkeypatch.setattr(hook.sqlite3, "connect", _boom)
    real_dumps = hook.json.dumps

    def dumps(obj, *a, **k):
        if isinstance(obj, dict) and "hookSpecificOutput" in obj:
            raise RuntimeError("serialisation exploded")
        return real_dumps(obj, *a, **k)

    clock = [1000.0]
    with unittest.mock.patch.object(hook.json, "dumps", dumps):
        out1, _ = _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=clock)
    assert out1 == ""
    assert not hook.LOADER_NOTICE_STAMP_PATH.exists()
    clock[0] += 10
    out2, _ = _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=clock)
    assert _notice_lines(_ctx(out2)) == [NOTICE_BOTH]
    assert hook.LOADER_NOTICE_STAMP_PATH.exists()


class _BrokenStdout(io.StringIO):
    def write(self, s):
        raise BrokenPipeError("stdout closed")


def test_k7_failed_print_leaves_no_stamp(hook, tmp_path, monkeypatch, capsys):
    """Red mutation (c), PLAUSIBLE-WRONG: stamping in a `finally` records the
    window as consumed although nothing was shown."""
    db = _make_db(tmp_path)
    monkeypatch.setattr(hook.sqlite3, "connect", _boom)
    clock = [1000.0]
    _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=clock, out=_BrokenStdout())
    assert not hook.LOADER_NOTICE_STAMP_PATH.exists()
    out2, _ = _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=clock)
    assert _notice_lines(_ctx(out2)) == [NOTICE_BOTH]


def test_k7_stamp_write_failure_after_print_never_raises(hook, tmp_path, monkeypatch, capsys):
    db = _make_db(tmp_path)
    monkeypatch.setattr(hook.sqlite3, "connect", _boom)
    blocker = tmp_path / "blocker"
    blocker.write_text("file")
    monkeypatch.setattr(hook, "LOADER_NOTICE_STAMP_PATH", blocker / "sub" / "n.json")
    out, _ = _run_main(hook, monkeypatch, capsys, trip_db=db, prev_db=db, now=[1.0])
    assert _notice_lines(_ctx(out)) == [NOTICE_BOTH]
