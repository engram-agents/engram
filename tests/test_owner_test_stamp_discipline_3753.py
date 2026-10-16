"""The shared default behind the owner's schema gate in tests, and the meta-test that keeps it honest (#3753).

Why this file exists: the gate refuses a store below `engram_core.LATEST_SCHEMA_VERSION` before
anything else in `start()`. A hand-listed sweep of fixtures only covers the files that existed
when it was written (a new owner test file landed after the first sweep and broke on Linux
seats the Mac never ran). So:

  1. tests/conftest.py `_owner_gate_sees_current_store` (autouse) stamps an existing, behind,
     hand-built store just before the REAL gate runs, for in-process starts. Cells 1-4 pin what
     it does and, as important, what it must NOT hide.
  2. Cell 5 is a static meta-test: every test module that constructs a real `EngramdOwner` (or
     calls the owner's `main()`) must say how its store reaches the latest version: an explicit
     stamp (`_owner_store`), a store built by the real migration, the opt-out marker, or an
     allowlist entry with a reason. A new owner test file that says none of these fails HERE, on
     every platform, instead of on a Linux seat.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_core  # noqa: E402
import engramd_owner as eo  # noqa: E402

LATEST = engram_core.LATEST_SCHEMA_VERSION


def _short_home(tmp_path_factory=None):
    import tempfile
    return Path(tempfile.mkdtemp(prefix="dsc-"))


@pytest.fixture
def home():
    import shutil
    d = _short_home()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _hand_built_behind_store(home: Path) -> Path:
    db = home / "knowledge.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT)")
    conn.commit()  # user_version stays 0
    conn.close()
    return db


def _start_stop(home: Path):
    o = eo.EngramdOwner(home, request_deadline=0.6)
    o.start(install_signal_handlers=False)
    o.stop()


# --- 1-4: what the default does and does not hide -----------------------------------------

@requires_linux_owner
def test_1_default_lets_a_hand_built_behind_store_start(home):
    db = _hand_built_behind_store(home)
    _start_stop(home)
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST
    finally:
        conn.close()


@requires_linux_owner
@pytest.mark.owner_schema_gate_real
def test_2_the_opt_out_marker_keeps_the_real_refusal(home):
    _hand_built_behind_store(home)
    with pytest.raises(eo.OwnerStoreBehindSchema):
        _start_stop(home)


@requires_linux_owner
def test_3_default_never_invents_a_store(home):
    """Absent store: still refused, still nothing created. Mutation: have the default seed a
    missing store -> this goes red (and the gate's own cells would too)."""
    with pytest.raises(eo.OwnerStoreBehindSchema):
        _start_stop(home)
    assert os.listdir(home) == []


@requires_linux_owner
def test_4_default_leaves_zero_byte_and_non_sqlite_files_refused(home):
    (home / "knowledge.db").write_bytes(b"")
    with pytest.raises(eo.OwnerStoreBehindSchema):
        _start_stop(home)
    (home / "knowledge.db").write_bytes(b"not sqlite" * 100)
    with pytest.raises(eo.OwnerStoreBehindSchema):
        _start_stop(home)


def test_4b_the_default_wraps_and_still_calls_the_real_gate(home):
    """The wrapper is not a no-op: a missing store reaches the real gate's own refusal text."""
    with pytest.raises(eo.OwnerStoreBehindSchema) as ei:
        eo._require_store_schema_current(home / "knowledge.db")
    assert "no knowledge.db" in str(ei.value)


# --- 5: the static meta-test --------------------------------------------------------------

_STARTS_REAL_OWNER = re.compile(r"EngramdOwner\(|\beo\.main\(|engramd_owner\.main\(")

# Any of these in the module source is evidence the module states how its store gets current.
_EVIDENCE = (
    "_owner_store",               # explicit stamp via the shared helper
    "owner_schema_gate_real",     # deliberately exercises the gate
    "_run_db_one_time_setup",     # store built by the real migration ladder (lands at latest)
    "make_test_store",            # tools/engramd_a6_latency: built by the real ladder
    "make_store(", "build_store(", "make_real_store(", "chain_store(",  # helpers that call the ladder
)

# Modules that construct an owner but never start one against a store (or whose store is built
# elsewhere), with the reason. Adding a line here is a statement a reviewer can read.
_ALLOWLIST = {
    "test_engramd_owner_arm_3708.py": "constructs owners to test peer-credential dispatch; never calls start()",
    "test_engramd_owner_schema_gate_3753.py": "the gate's own file; controls the store version itself",
    "test_owner_launchd_3709.py": "its only EngramdOwner( is inside a CHILD-process code string (K6 shared-predicate cell); the in-process owner is never started here",
}


def _lacks_evidence(src: str) -> bool:
    return not any(tok in src for tok in _EVIDENCE)


def _owner_test_modules():
    for p in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        if p.name == Path(__file__).name:
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        if _STARTS_REAL_OWNER.search(src):
            yield p, src


def test_5_every_module_that_starts_a_real_owner_states_how_its_store_gets_current():
    offenders = []
    for p, src in _owner_test_modules():
        if p.name in _ALLOWLIST:
            continue
        if _lacks_evidence(src):
            offenders.append(p.name)
    assert not offenders, (
        f"{offenders}: these test modules construct a real EngramdOwner (or call the owner's main()) "
        "but say nothing about the store's schema version. The owner refuses a store below "
        "engram_core.LATEST_SCHEMA_VERSION before its pidfile/socket/ACL checks. Do ONE of: stamp the "
        "fixture's store with tests/_owner_store.py (seed_current_store / stamp_current_schema), build it "
        "with engram_core._run_db_one_time_setup, mark the test @pytest.mark.owner_schema_gate_real, or "
        "add the module to _ALLOWLIST here with a reason. (An owner in a CHILD process, or on an absent "
        "store, is not covered by the conftest default and needs the explicit stamp.)"
    )


def test_5b_the_meta_test_discriminates():
    """Control: a module that builds an owner and says nothing IS flagged, one that stamps is not.
    (The scan is the same predicate test 5 applies, run on two synthetic sources.)"""
    silent = "import engramd_owner as eo\nowner = eo.EngramdOwner(home)\nowner.start()\n"
    stamped = "from _owner_store import seed_current_store\n" + silent
    assert _STARTS_REAL_OWNER.search(silent) and _lacks_evidence(silent)
    assert _STARTS_REAL_OWNER.search(stamped) and not _lacks_evidence(stamped)


def test_5c_allowlist_entries_are_live():
    """An allowlist line for a module that no longer matches is stale, not harmless."""
    live = {p.name for p, _ in _owner_test_modules()}
    stale = [n for n in _ALLOWLIST if n not in live]
    assert not stale, stale
