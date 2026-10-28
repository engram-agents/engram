"""engram-alpha#2544 — tools/reconcile_lesson_demotions.py.

Coverage:
  1. A lesson still stored "blocking" with a hot lease (fresh override count
     >= threshold) is reported as a candidate.
  2. A lesson already stored "advisory" is NOT reported, even with a hot
     lease in the ledger (nothing to reconcile -- already done).
  3. A lesson whose lease has gone COLD (override count now below threshold,
     because the counted window has moved past the old overrides) is NOT
     reported -- the read-time hook itself would resume blocking on it, so
     a durable demotion would be stricter than reality.
  4. A lesson never mentioned in the ledger is NOT reported.
  5. The reused hook logic is genuinely reused, not reimplemented: patching
     the hook module's _demotion_config to a different threshold changes
     which candidates are found -- checked to be able to FAIL by asserting
     against the wrong (unpatched) threshold's expected set first.
  6-9. Loud-vs-silent (Borges's review of PR-2621): a missing knowledge.db
     or an unreadable-but-present ledger raises ReconcileError rather than
     returning an empty candidates list -- {"candidates": []} must mean
     "checked cleanly, found nothing", never "could not check". A missing
     LEDGER (as opposed to an unreadable one) is the normal healthy case
     and must NOT raise. The CLI surfaces this as a distinguishable
     {"error": ..., "candidates": null} shape with a non-zero exit code.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_reconcile_module():
    spec = importlib.util.spec_from_file_location(
        "reconcile_lesson_demotions", str(TOOLS_DIR / "reconcile_lesson_demotions.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def reconcile():
    return _load_reconcile_module()


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "knowledge.db"
    conn = sqlite3.connect(str(p))
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, is_current INTEGER, "
        "memory_status TEXT, metadata TEXT)"
    )
    conn.commit()
    conn.close()
    return p


def _insert_lesson(db_path, lesson_id, prevention_type, is_current=1, memory_status="active"):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, memory_status, metadata) "
        "VALUES (?, 'lesson', ?, ?, ?)",
        (lesson_id, is_current, memory_status, json.dumps({"prevention_type": prevention_type})),
    )
    conn.commit()
    conn.close()


def _write_ledger(ledger_path, entries):
    with open(ledger_path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _override_entry(ts, lesson_id):
    return {
        "ts": ts,
        "lesson_id": lesson_id,
        "mode": "blocking",
        "override_present": True,
    }


def _lease_entry(ts, lesson_id, override_count=2, threshold=2, window_days=7):
    return {
        "ts": ts,
        "lesson_id": lesson_id,
        "mode": "auto_demoted_lease",
        "override_count": override_count,
        "threshold": threshold,
        "window_days": window_days,
    }


def test_hot_lease_on_blocking_lesson_is_a_candidate(reconcile, db_path, tmp_path):
    now = time.time()
    _insert_lesson(db_path, "ls_0100", "blocking")
    ledger_path = tmp_path / "ledger.jsonl"
    _write_ledger(ledger_path, [
        _override_entry(now - 3600, "ls_0100"),
        _override_entry(now - 1800, "ls_0100"),
        _lease_entry(now - 1800, "ls_0100"),
    ])

    candidates = reconcile.find_candidates(db_path=db_path, ledger_path=ledger_path, now=now)
    ids = [c["lesson_id"] for c in candidates]
    assert "ls_0100" in ids
    entry = next(c for c in candidates if c["lesson_id"] == "ls_0100")
    assert entry["override_count"] == 2
    assert entry["threshold"] == 2


def test_already_advisory_lesson_is_not_a_candidate(reconcile, db_path, tmp_path):
    now = time.time()
    _insert_lesson(db_path, "ls_0101", "advisory")
    ledger_path = tmp_path / "ledger.jsonl"
    _write_ledger(ledger_path, [
        _override_entry(now - 3600, "ls_0101"),
        _override_entry(now - 1800, "ls_0101"),
        _lease_entry(now - 1800, "ls_0101"),
    ])

    candidates = reconcile.find_candidates(db_path=db_path, ledger_path=ledger_path, now=now)
    assert candidates == []


def test_cold_lease_is_not_a_candidate(reconcile, db_path, tmp_path):
    """Both overrides happened long ago -- outside the 7-day window as
    measured from `now`. The lease has gone cold: the read-time hook would
    NOT currently treat this lesson as demoted, so it must not be durably
    demoted either."""
    now = time.time()
    stale = now - (30 * 86400)  # 30 days ago, outside the default 7-day window
    _insert_lesson(db_path, "ls_0102", "blocking")
    ledger_path = tmp_path / "ledger.jsonl"
    _write_ledger(ledger_path, [
        _override_entry(stale - 3600, "ls_0102"),
        _override_entry(stale, "ls_0102"),
        _lease_entry(stale, "ls_0102"),
    ])

    candidates = reconcile.find_candidates(db_path=db_path, ledger_path=ledger_path, now=now)
    assert candidates == []


def test_lesson_absent_from_ledger_is_not_a_candidate(reconcile, db_path, tmp_path):
    now = time.time()
    _insert_lesson(db_path, "ls_0103", "blocking")
    ledger_path = tmp_path / "ledger.jsonl"
    _write_ledger(ledger_path, [])  # empty ledger

    candidates = reconcile.find_candidates(db_path=db_path, ledger_path=ledger_path, now=now)
    assert candidates == []


def test_inactive_lesson_is_not_a_candidate(reconcile, db_path, tmp_path):
    """A lesson with memory_status != 'active' (faded to tier-3, e.g.) must
    not be reported -- matches the hook's own two queries, both of which
    filter on memory_status = 'active' (Ariadne's review of PR-2621)."""
    now = time.time()
    _insert_lesson(db_path, "ls_0105", "blocking", memory_status="faded")
    ledger_path = tmp_path / "ledger.jsonl"
    _write_ledger(ledger_path, [
        _override_entry(now - 3600, "ls_0105"),
        _override_entry(now - 1800, "ls_0105"),
        _lease_entry(now - 1800, "ls_0105"),
    ])

    candidates = reconcile.find_candidates(db_path=db_path, ledger_path=ledger_path, now=now)
    assert candidates == []


def test_inactive_lesson_with_non_faded_status_is_also_not_a_candidate(reconcile, db_path, tmp_path):
    """Mutation-control gap Ariadne caught on PR-2621: the prior version of
    this suite only exercised memory_status='faded', which a `!= 'faded'`
    mutant of the SQL filter would also correctly reject -- discriminating
    nothing between `= 'active'` (the real code) and `!= 'faded'` (a
    non-equivalent mutant). A THIRD status value that both filters treat
    differently (active matches neither `= 'active'` under this status NOR
    excluded-by `!= 'faded'`... concretely: `!= 'faded'` would WRONGLY admit
    this row, `= 'active'` correctly excludes it) is what actually
    discriminates the two implementations."""
    now = time.time()
    _insert_lesson(db_path, "ls_0106", "blocking", memory_status="stale")
    ledger_path = tmp_path / "ledger.jsonl"
    _write_ledger(ledger_path, [
        _override_entry(now - 3600, "ls_0106"),
        _override_entry(now - 1800, "ls_0106"),
        _lease_entry(now - 1800, "ls_0106"),
    ])

    candidates = reconcile.find_candidates(db_path=db_path, ledger_path=ledger_path, now=now)
    assert candidates == []


def test_null_memory_status_lesson_is_not_a_candidate(reconcile, db_path, tmp_path):
    """engram-alpha#2647: a lesson row with memory_status IS NULL (e.g. an
    old/malformed node predating the field's introduction) must not be
    reported. Re-run of the reviewer-fairy's own mutant, independently
    re-verified rather than trusted from its unverified report: patching
    the live query to `memory_status = 'active' OR memory_status IS NULL`
    escapes ALL 16 tests in this suite as of this commit (re-measured
    directly, via a temporary mutation + full-suite run, not assumed from
    the fairy's original count) -- this is the fixture that closes that
    gap. SQL three-valued logic means `memory_status = 'active'` on a NULL
    column evaluates to NULL/false under the real code, so this row is
    correctly excluded there; it is only the OR-IS-NULL mutant that admits
    it, which is what makes this fixture targeted rather than redundant
    with test_inactive_lesson_with_non_faded_status_is_also_not_a_candidate
    (that one's 'stale' mutant is a non-NULL string and doesn't touch this
    code path at all)."""
    now = time.time()
    _insert_lesson(db_path, "ls_0107", "blocking", memory_status=None)
    ledger_path = tmp_path / "ledger.jsonl"
    _write_ledger(ledger_path, [
        _override_entry(now - 3600, "ls_0107"),
        _override_entry(now - 1800, "ls_0107"),
        _lease_entry(now - 1800, "ls_0107"),
    ])

    candidates = reconcile.find_candidates(db_path=db_path, ledger_path=ledger_path, now=now)
    assert candidates == []


def test_reuses_hook_demotion_config_not_reimplemented(reconcile, db_path, tmp_path, monkeypatch):
    """Sourcing proof, checked to be able to FAIL: patch the hook module's
    _demotion_config to require 5 overrides instead of the default 2. A
    lesson with only 2 overrides (which WOULD be a candidate under the
    default config) must then NOT be reported -- proving find_candidates
    reads the threshold from the hook module rather than a hardcoded 2."""
    now = time.time()
    _insert_lesson(db_path, "ls_0104", "blocking")
    ledger_path = tmp_path / "ledger.jsonl"
    _write_ledger(ledger_path, [
        _override_entry(now - 3600, "ls_0104"),
        _override_entry(now - 1800, "ls_0104"),
        _lease_entry(now - 1800, "ls_0104"),
    ])

    hook = reconcile._load_hook_module()
    # Sanity: under the real default config, this lesson IS a candidate.
    baseline = reconcile.find_candidates(
        db_path=db_path, ledger_path=ledger_path, now=now, hook_module=hook
    )
    assert any(c["lesson_id"] == "ls_0104" for c in baseline)

    monkeypatch.setattr(hook, "_demotion_config", lambda: {"overrides": 5, "window_days": 7})
    patched = reconcile.find_candidates(
        db_path=db_path, ledger_path=ledger_path, now=now, hook_module=hook
    )
    assert patched == []


# ---------------------------------------------------------------------------
# Hook-path resolution across build topologies (Ariadne's review of
# PR-2621): the source tree nests the hook under hooks/claude/, but the
# plugin build flattens hooks/claude/* -> hooks/* -- a fixed sub-path
# assuming source layout resolves to a path that does not exist in a
# deployed install, and _load_hook_module raised an uncaught
# FileNotFoundError on EVERY invocation there (not a ReconcileError, so
# main() did not catch it either). These tests recreate both topologies
# rather than trusting that the source-tree case (implicitly exercised by
# every test above, since _load_hook_module() with no override finds the
# real repo file) generalizes to the deployed one.
# ---------------------------------------------------------------------------


def test_resolve_hook_path_prefers_plugin_root(reconcile, tmp_path, monkeypatch):
    """A CLAUDE_PLUGIN_ROOT pointing at a deployed (flattened) layout --
    hooks/engram-lesson-tripwire-hook.py, no claude/ level -- must resolve
    to THAT file, not fall through to the real source tree."""
    plugin_root = tmp_path / "plugin"
    hook_dir = plugin_root / "hooks"
    hook_dir.mkdir(parents=True)
    deployed_hook = hook_dir / "engram-lesson-tripwire-hook.py"
    deployed_hook.write_text("# stub deployed hook\n")

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    resolved = reconcile._resolve_hook_path()
    assert resolved == deployed_hook


def test_resolve_hook_path_falls_back_to_walk_parents(reconcile, tmp_path, monkeypatch):
    """CLAUDE_PLUGIN_ROOT set but pointing at a root with NO hook present
    (e.g. a stale/wrong value) must fall through to the walk-parents search
    and still find the real source-tree hook -- proving the fallback path
    itself works, not just that CLAUDE_PLUGIN_ROOT works when it's right."""
    empty_root = tmp_path / "not-the-plugin"
    empty_root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(empty_root))

    resolved = reconcile._resolve_hook_path()
    assert resolved is not None
    assert resolved.name == "engram-lesson-tripwire-hook.py"
    assert resolved.exists()


def test_resolve_hook_path_returns_none_when_nothing_matches(reconcile, monkeypatch):
    """Checked to fail: if NEITHER CLAUDE_PLUGIN_ROOT nor any real parent
    directory can supply the hook, resolution returns None rather than
    raising or fabricating a path -- _load_hook_module is what turns that
    into a loud ReconcileError (next test)."""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(
        reconcile, "_HOOK_RELATIVE_PATHS", ("no/such/hook-file-anywhere.py",)
    )
    assert reconcile._resolve_hook_path() is None


@pytest.mark.skipif(
    os.geteuid() == 0 if hasattr(os, "geteuid") else False,
    reason="root bypasses file permission bits -- chmod 0o000 would not "
           "actually block traversal, making this test vacuously true in a "
           "root-run CI container rather than a real check",
)
def test_resolve_hook_path_skips_unreadable_candidate_root(reconcile, tmp_path, monkeypatch):
    """engram-alpha#2646: Path.exists() stats every path component from the
    root, so a candidate whose PARENT directory is unreadable (permission
    denied, not merely absent) raises OSError instead of returning False.
    Before the fix that escaped _resolve_hook_path() uncaught, crashing
    resolution at import time rather than treating that one candidate as
    'not found' and continuing to the next. Point CLAUDE_PLUGIN_ROOT at a
    directory chmod'd 0o000 (no execute/search permission) so stat'ing
    anything underneath it raises PermissionError, and confirm the walk
    survives it and still finds the real hook via the parents fallback."""
    blocked_root = tmp_path / "blocked"
    blocked_root.mkdir()
    blocked_root.chmod(0o000)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(blocked_root))
    try:
        resolved = reconcile._resolve_hook_path()
    finally:
        blocked_root.chmod(0o755)  # restore so tmp_path cleanup can remove it

    assert resolved is not None
    assert resolved.name == "engram-lesson-tripwire-hook.py"
    assert resolved.exists()


def test_load_hook_module_raises_loud_when_unresolvable(reconcile, monkeypatch):
    """The actual PR-2621 defect, reproduced directly: with HOOK_PATH
    unresolved, _load_hook_module must raise ReconcileError -- never let an
    uncaught FileNotFoundError (or any other bare exception) escape, which
    is what happened on every deployed-install invocation before this fix."""
    monkeypatch.setattr(reconcile, "HOOK_PATH", None)
    with pytest.raises(reconcile.ReconcileError):
        reconcile._load_hook_module()


# ---------------------------------------------------------------------------
# Loud-vs-silent (Borges's review of PR-2621): {"candidates": []} must mean
# "checked cleanly, found nothing" -- never "could not check". A genuine
# failure to check must be a different, distinguishable shape.
# ---------------------------------------------------------------------------


def test_missing_db_raises_not_silently_returns_empty(reconcile, tmp_path):
    """No knowledge.db at all is a real failure to check, not a clean
    zero-candidates night -- must raise, not return []."""
    missing_db = tmp_path / "does-not-exist.db"
    ledger_path = tmp_path / "ledger.jsonl"
    _write_ledger(ledger_path, [])

    with pytest.raises(reconcile.ReconcileError):
        reconcile.find_candidates(db_path=missing_db, ledger_path=ledger_path, now=time.time())


@pytest.mark.skipif(
    os.geteuid() == 0 if hasattr(os, "geteuid") else False,
    reason="root bypasses file permission bits -- chmod 0o000 would not "
           "actually block the read, making this test vacuously true in a "
           "root-run CI container rather than a real check",
)
def test_unreadable_ledger_raises_not_silently_returns_empty(reconcile, db_path, tmp_path):
    """A ledger that EXISTS but can't be read (permission denied) is a real
    failure -- must raise, not silently report zero candidates. A missing
    ledger (tested elsewhere) is the opposite case and must NOT raise."""
    ledger_path = tmp_path / "unreadable-ledger.jsonl"
    _write_ledger(ledger_path, [])
    ledger_path.chmod(0o000)
    try:
        with pytest.raises(reconcile.ReconcileError):
            reconcile.find_candidates(db_path=db_path, ledger_path=ledger_path, now=time.time())
    finally:
        ledger_path.chmod(0o644)  # restore so tmp_path cleanup can remove it


def test_missing_ledger_does_not_raise(reconcile, db_path, tmp_path):
    """The normal case -- no lesson has ever crossed the lease threshold,
    so the ledger file was never created. This must NOT be treated as an
    error; it is the common healthy state."""
    ledger_path = tmp_path / "never-created-ledger.jsonl"
    assert not ledger_path.exists()

    candidates = reconcile.find_candidates(
        db_path=db_path, ledger_path=ledger_path, now=time.time()
    )
    assert candidates == []


def test_cli_reports_error_shape_and_exits_nonzero_on_missing_db(tmp_path):
    """The CLI entrypoint surfaces a ReconcileError as {"error": ..., "candidates":
    null} on stdout and a non-zero exit code -- distinguishable by a caller
    parsing stdout from the healthy {"candidates": []} shape."""
    import subprocess

    missing_db = tmp_path / "does-not-exist.db"
    ledger_path = tmp_path / "ledger.jsonl"
    ledger_path.write_text("")

    result = subprocess.run(
        [
            sys.executable, str(TOOLS_DIR / "reconcile_lesson_demotions.py"),
            "--db", str(missing_db), "--ledger", str(ledger_path),
        ],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["candidates"] is None
    assert "error" in payload
