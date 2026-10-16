"""Contract-first tests for #3005 — preserve-deadline re-arm for the loop heartbeat.

The defect: `engram-loop` re-armed `ScheduleWakeup` at the FULL cadence on
every wake.  Because `ScheduleWakeup` *replaces* the pending wake, a Monitor
expiry notice (arriving faster than the declared cadence) pushed the due
moment out forever and the heartbeat never fired.  The fix is to re-arm for
the *remaining* time to the original due moment when the wake is not the
heartbeat.

These tests are written against the contract table in the #3005 spec, BEFORE
the implementation, so an implementation that quietly collapses a row is
caught rather than reviewed.

  Unit — compute_rearm (one parametrized table, all four fields per cell):
    R1  heartbeat                          -> cadence, now+cadence, write, "heartbeat"
    R2  non-heartbeat, due in the future   -> max(60, due-now), UNCHANGED, no write, "preserved"
    R3  non-heartbeat, due already past    -> 60 (clamp), unchanged, no write, "preserved"
    R4  non-heartbeat, key absent          -> cadence, now+cadence, write, "migrated_absent"
    R5  non-heartbeat, key corrupt         -> cadence, now+cadence, write, "migrated_invalid"
    R6  cadence_seconds missing/non-int/<=0 -> default_cadence in every row
    R7  marker None / not a mapping        -> absent cadence + absent key (R4)
    R8  naive `now`                        -> ValueError (fail loud)

  The success rows and the OTHER-outcome rows live in the SAME table: the
  "preserved" cells (R2/R3) are the ones that must go red under a
  mutate-toward-pre-fix control, while the heartbeat and migrated cells in
  the same table must stay green.  A control in a separate, skippable test
  cannot demonstrate that.

  Boundary cells: due-now of exactly 60, 61 and 59 (clamp is max(60, ...)).
  Byte-identity: a preserved `next_due_at` is returned as the SAME string.

  CLI — the single writer of `next_due_at`:
    * stdout is the bare integer (composes with `delay=$(...)`).
    * a migration prints exactly one line to STDERR and nothing to stdout.
    * a preserved call leaves the marker file byte-identical (sha256 before/after).
    * a heartbeat call adds exactly one key (`next_due_at`) and changes no other.
    * absent marker / corrupt JSON -> exit 2, message names the path.
    * the legacy stdin-wrapping mode is unchanged when --rearm-delay is absent.

  Skill docs — the arm instruction, not just the helper:
    * EVERY `*.md` under the swept roots (`src/engram/skills/` and
      `deploy/autostart/`) is checked for the retired full-cadence arm SHAPES
      (see FULL_CADENCE_ARM_PHRASES).  The helper is inert while a doc still
      tells the agent to arm at a bare full-cadence number.  Round 3 widened
      the roots from skills-only to include `deploy/autostart/`: the boot
      charges are what arm the heartbeat at session start, so the very files
      this PR edits were, until then, outside the guard.
    * The sweep is SHAPE-based, and a NEW wording of a full-cadence arm can
      still evade it.  That is the honest bound, not a defect to hide — and
      WIDENING THE ROOTS DOES NOT TIGHTEN IT: a broader root set finds more
      instances of the KNOWN shapes; it does not make the shape list
      exhaustive.  Round 2's list missed the `rewritten wholesale` prose and
      the surviving `re-arm (1800s` arm shape, both added here after round
      2's review found them.  The STRUCTURAL protection is that the arm
      instruction now lives in exactly ONE place — engram-loop Step 2 item 3c,
      the `--rearm-delay` helper — and every other mention defers to it.  Do
      NOT read a green sweep as "no full-cadence arm instruction survives
      anywhere"; read it as "none of the KNOWN shapes does, in the roots
      walked".
    * The bound is WIDER than shape alone: the sweep walks `*.md` under those
      roots, so every NON-`.md` carrier is outside it ENTIRELY — and one such
      carrier was real: the Stop-hook nudge in
      `engram-loop-arm-stop-hook.py` (a `.py`; fold 3 / #3005).  That carrier
      gets a targeted per-carrier pin instead (the §3 cell below), NOT a
      `.py`-wide phrase sweep.
    * Positive cells in the SAME parametrized table assert the replacement is
      present, so the sweep cannot be satisfied by deleting the skill.
"""

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

LP_PATH = (
    Path(__file__).parent.parent / "src" / "engram" / "tools" / "loop_prompt.py"
)
_spec = importlib.util.spec_from_file_location("loop_prompt_rearm_test", LP_PATH)
lp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lp)

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# compute_rearm — the contract table, one row per cell
# ---------------------------------------------------------------------------

# (id, marker, is_heartbeat, default_cadence, expected(delay, next_due_at, write, reason))
CASES = [
    # -- R1 heartbeat -------------------------------------------------------
    (
        "R1_heartbeat_full_cadence",
        {"cadence_seconds": 1800},
        True,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "heartbeat"),
    ),
    (
        "R1b_heartbeat_ignores_existing_due",
        {"cadence_seconds": 1800, "next_due_at": "2026-10-12T12:05:00Z"},
        True,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "heartbeat"),
    ),
    (
        "R1c_heartbeat_default_cadence_when_absent",
        {},
        True,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "heartbeat"),
    ),
    # -- R2 non-heartbeat, due in the FUTURE (preserved) --------------------
    (
        "R2_future_preserved",
        {"cadence_seconds": 1800, "next_due_at": "2026-10-12T12:10:00Z"},
        False,
        1800,
        (600, "2026-10-12T12:10:00Z", False, "preserved"),
    ),
    (
        "R2b_future_under_clamp",
        {"cadence_seconds": 1800, "next_due_at": "2026-10-12T12:00:45Z"},
        False,
        1800,
        (60, "2026-10-12T12:00:45Z", False, "preserved"),
    ),
    (
        "R2c_boundary_exactly_60",
        {"cadence_seconds": 1800, "next_due_at": "2026-10-12T12:01:00Z"},
        False,
        1800,
        (60, "2026-10-12T12:01:00Z", False, "preserved"),
    ),
    (
        "R2d_boundary_exactly_61",
        {"cadence_seconds": 1800, "next_due_at": "2026-10-12T12:01:01Z"},
        False,
        1800,
        (61, "2026-10-12T12:01:01Z", False, "preserved"),
    ),
    (
        "R2e_boundary_exactly_59_clamps_to_60",
        {"cadence_seconds": 1800, "next_due_at": "2026-10-12T12:00:59Z"},
        False,
        1800,
        (60, "2026-10-12T12:00:59Z", False, "preserved"),
    ),
    (
        "R2f_preserved_ignores_cadence",
        {"cadence_seconds": 900, "next_due_at": "2026-10-12T13:00:00Z"},
        False,
        1800,
        (3600, "2026-10-12T13:00:00Z", False, "preserved"),
    ),
    # -- R3 non-heartbeat, due already PAST (clamp, preserved) --------------
    (
        "R3_past_clamps_to_60",
        {"cadence_seconds": 1800, "next_due_at": "2026-10-12T11:59:00Z"},
        False,
        1800,
        (60, "2026-10-12T11:59:00Z", False, "preserved"),
    ),
    (
        "R3b_past_long_ago_clamps_to_60",
        {"cadence_seconds": 1800, "next_due_at": "2026-10-12T11:00:00Z"},
        False,
        1800,
        (60, "2026-10-12T11:00:00Z", False, "preserved"),
    ),
    # -- R4 non-heartbeat, key ABSENT ---------------------------------------
    (
        "R4_absent_key_uses_marker_cadence",
        {"cadence_seconds": 900},
        False,
        1800,
        (900, "2026-10-12T12:15:00Z", True, "migrated_absent"),
    ),
    (
        "R4b_absent_key_uses_default_cadence",
        {},
        False,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "migrated_absent"),
    ),
    # -- R5 non-heartbeat, key present but CORRUPT --------------------------
    (
        "R5_unparseable_string",
        {"cadence_seconds": 1800, "next_due_at": "not-a-timestamp"},
        False,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "migrated_invalid"),
    ),
    (
        "R5b_naive_datetime_string",
        {"cadence_seconds": 1800, "next_due_at": "2026-10-12T12:10:00"},
        False,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "migrated_invalid"),
    ),
    (
        "R5c_empty_string",
        {"cadence_seconds": 1800, "next_due_at": ""},
        False,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "migrated_invalid"),
    ),
    (
        "R5d_non_str_int",
        {"cadence_seconds": 1800, "next_due_at": 12345},
        False,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "migrated_invalid"),
    ),
    (
        "R5e_null_value",
        {"cadence_seconds": 1800, "next_due_at": None},
        False,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "migrated_invalid"),
    ),
    (
        "R5f_corrupt_uses_marker_cadence",
        {"cadence_seconds": 900, "next_due_at": "garbage"},
        False,
        1800,
        (900, "2026-10-12T12:15:00Z", True, "migrated_invalid"),
    ),
    # -- R6 cadence_seconds missing / non-int / <= 0 -> default_cadence -----
    (
        "R6a_cadence_missing",
        {"kind": "heartbeat"},
        False,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "migrated_absent"),
    ),
    (
        "R6b_cadence_non_int_str",
        {"cadence_seconds": "1800"},
        False,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "migrated_absent"),
    ),
    (
        "R6c_cadence_zero",
        {"cadence_seconds": 0},
        False,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "migrated_absent"),
    ),
    (
        "R6d_cadence_negative",
        {"cadence_seconds": -5},
        False,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "migrated_absent"),
    ),
    (
        "R6e_cadence_float",
        {"cadence_seconds": 1800.0},
        False,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "migrated_absent"),
    ),
    (
        "R6f_cadence_none",
        {"cadence_seconds": None},
        False,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "migrated_absent"),
    ),
    (
        "R6g_non_int_cadence_falls_back_to_custom_default",
        {"cadence_seconds": "oops"},
        False,
        600,
        (600, "2026-10-12T12:10:00Z", True, "migrated_absent"),
    ),
    (
        "R6h_heartbeat_uses_custom_default",
        {},
        True,
        600,
        (600, "2026-10-12T12:10:00Z", True, "heartbeat"),
    ),
    (
        "R6i_heartbeat_zero_cadence_uses_custom_default",
        {"cadence_seconds": 0},
        True,
        600,
        (600, "2026-10-12T12:10:00Z", True, "heartbeat"),
    ),
    (
        "R6j_absent_key_uses_custom_default",
        {},
        False,
        600,
        (600, "2026-10-12T12:10:00Z", True, "migrated_absent"),
    ),
    # -- R7 marker None / not a mapping -------------------------------------
    (
        "R7a_marker_none_non_heartbeat",
        None,
        False,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "migrated_absent"),
    ),
    (
        "R7b_marker_list_non_heartbeat",
        ["not", "a", "mapping"],
        False,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "migrated_absent"),
    ),
    (
        "R7c_marker_string_non_heartbeat",
        "just a string",
        False,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "migrated_absent"),
    ),
    (
        "R7d_marker_none_heartbeat",
        None,
        True,
        1800,
        (1800, "2026-10-12T12:30:00Z", True, "heartbeat"),
    ),
]


@pytest.mark.parametrize(
    "case_id,marker,is_heartbeat,default_cadence,expected",
    CASES,
    ids=[c[0] for c in CASES],
)
def test_compute_rearm_contract_cell(
    case_id, marker, is_heartbeat, default_cadence, expected
):
    """Every contract cell asserts ALL FOUR fields, not just the delay."""
    exp_delay, exp_due, exp_write, exp_reason = expected
    decision = lp.compute_rearm(
        marker,
        now=NOW,
        is_heartbeat=is_heartbeat,
        default_cadence=default_cadence,
    )
    assert isinstance(decision, lp.RearmDecision)
    assert decision.delay_seconds == exp_delay, f"{case_id}: delay_seconds"
    assert decision.next_due_at == exp_due, f"{case_id}: next_due_at"
    assert decision.write_next_due_at is exp_write, f"{case_id}: write_next_due_at"
    assert decision.reason == exp_reason, f"{case_id}: reason"


def test_r2_byte_identity_of_preserved_value():
    """R2: the preserved `next_due_at` is returned as the SAME string.

    A re-format (parse -> strftime) is a silent rewrite, so the input here is
    a valid, aware ISO-8601 instant written with an explicit `+00:00` offset
    rather than the canonical `Z` tail: a re-format to the canonical shape
    would change the bytes and must fail this test.  (Canonical-`Z` inputs
    cannot discriminate, because a re-format of a canonical value is a no-op.)
    """
    raw = "2026-10-12T12:10:00+00:00"
    decision = lp.compute_rearm(
        {"cadence_seconds": 1800, "next_due_at": raw},
        now=NOW,
        is_heartbeat=False,
    )
    assert decision.reason == "preserved"
    assert decision.next_due_at == raw
    assert decision.next_due_at is raw or decision.next_due_at == raw
    assert decision.write_next_due_at is False


def test_r4_and_r5_are_distinguishable():
    """An absent key (pre-upgrade marker) and a corrupt key (real error) must
    not collapse to the same reason — R4 writes the field once, R5 flags a
    corruption.  If they collapse, a corrupt value is indistinguishble from a
    pre-upgrade marker and the migration notice never fires."""
    absent = lp.compute_rearm({}, now=NOW, is_heartbeat=False)
    corrupt = lp.compute_rearm(
        {"next_due_at": "garbage"}, now=NOW, is_heartbeat=False
    )
    assert absent.reason == "migrated_absent"
    assert corrupt.reason == "migrated_invalid"
    assert absent.reason != corrupt.reason


def test_r8_naive_now_raises():
    """R8: a naive `now` raises ValueError — fail loud, never guess a zone."""
    naive = datetime(2026, 10, 12, 12, 0, 0)
    with pytest.raises(ValueError):
        lp.compute_rearm({}, now=naive, is_heartbeat=False)


def test_r8_naive_now_raises_even_for_heartbeat():
    """R8 holds regardless of wake kind — the guard is not downstream of the
    heartbeat branch."""
    naive = datetime(2026, 10, 12, 12, 0, 0)
    with pytest.raises(ValueError):
        lp.compute_rearm({"cadence_seconds": 1800}, now=naive, is_heartbeat=True)


def test_compute_rearm_does_not_read_the_clock():
    """The function must not call datetime.now()/utcnow() internally — that is
    what makes the cells testable.  Passing two different `now` values for the
    same marker must yield two different due moments."""
    later = NOW + timedelta(seconds=120)
    a = lp.compute_rearm({}, now=NOW, is_heartbeat=True, default_cadence=1800)
    b = lp.compute_rearm({}, now=later, is_heartbeat=True, default_cadence=1800)
    assert a.next_due_at == "2026-10-12T12:30:00Z"
    assert b.next_due_at == "2026-10-12T12:32:00Z"


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _run(*args, stdin=None):
    return subprocess.run(
        [sys.executable, str(LP_PATH), *args],
        capture_output=True,
        text=True,
        input=stdin,
    )


def _write_marker(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# CLI — delay on stdout
# ---------------------------------------------------------------------------

def test_cli_stdout_is_bare_int(tmp_path):
    """stdout is the integer delay and NOTHING else (so `delay=$(...)` composes)."""
    marker = tmp_path / "loop-mode.json"
    _write_marker(
        marker,
        {"cadence_seconds": 1800, "next_due_at": "2026-10-12T12:10:00Z"},
    )
    # Use a real-time-ish marker so the preserved branch is deterministic:
    # the due moment is far in the future, so delay == due - now is huge but
    # still a bare integer.  Assert the shape, then the value via the unit API.
    res = _run("--rearm-delay", "--wake-kind", "other", "--marker", str(marker))
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() != ""
    assert re.fullmatch(r"-?\d+", res.stdout.strip()), repr(res.stdout)
    assert res.stdout.count("\n") <= 1


def test_cli_preserved_stdout_int_and_no_stderr(tmp_path):
    """A preserved call prints the remaining seconds and is silent on stderr."""
    due = datetime.now(timezone.utc) + timedelta(seconds=600)
    marker = tmp_path / "loop-mode.json"
    _write_marker(
        marker,
        {
            "cadence_seconds": 1800,
            "next_due_at": due.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    res = _run("--rearm-delay", "--wake-kind", "other", "--marker", str(marker))
    assert res.returncode == 0, res.stderr
    assert res.stderr == ""
    delay = int(res.stdout.strip())
    # ~600s remaining, allowing a few seconds of drift across the subprocess.
    assert 580 <= delay <= 600, delay


def test_cli_heartbeat_prints_cadence(tmp_path):
    """A heartbeat call prints the cadence."""
    marker = tmp_path / "loop-mode.json"
    _write_marker(marker, {"cadence_seconds": 1800})
    res = _run("--rearm-delay", "--wake-kind", "heartbeat", "--marker", str(marker))
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "1800"


# ---------------------------------------------------------------------------
# CLI — the writer
# ---------------------------------------------------------------------------

def test_cli_preserved_leaves_marker_byte_identical(tmp_path):
    """A preserved call does NOT write the marker: sha256 before == after."""
    due = datetime.now(timezone.utc) + timedelta(seconds=600)
    marker = tmp_path / "loop-mode.json"
    _write_marker(
        marker,
        {
            "cadence_seconds": 1800,
            "next_due_at": due.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    before = _sha(marker)
    res = _run("--rearm-delay", "--wake-kind", "other", "--marker", str(marker))
    assert res.returncode == 0, res.stderr
    assert _sha(marker) == before


def test_cli_heartbeat_writes_exactly_one_new_key(tmp_path):
    """A heartbeat call adds exactly one key (`next_due_at`) and changes no
    other key's value."""
    original = {
        "FIRST_ACTION": "read me",
        "activated": "2026-10-12T12:00:00Z",
        "cadence_seconds": 1800,
        "kind": "heartbeat",
    }
    marker = tmp_path / "loop-mode.json"
    _write_marker(marker, original)

    res = _run("--rearm-delay", "--wake-kind", "heartbeat", "--marker", str(marker))
    assert res.returncode == 0, res.stderr

    after = json.loads(marker.read_text(encoding="utf-8"))
    assert set(after) - set(original) == {"next_due_at"}
    assert "next_due_at" not in original
    for key, value in original.items():
        assert after[key] == value, key
    # the written value is a canonical Z timestamp
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", after["next_due_at"]
    ), after["next_due_at"]


def test_cli_migration_writes_next_due_at_once(tmp_path):
    """An absent key is the migration path: it IS written (write-once), so the
    next non-heartbeat wake preserves rather than migrating again."""
    original = {"cadence_seconds": 900}
    marker = tmp_path / "loop-mode.json"
    _write_marker(marker, original)

    res = _run("--rearm-delay", "--wake-kind", "other", "--marker", str(marker))
    assert res.returncode == 0, res.stderr
    after = json.loads(marker.read_text(encoding="utf-8"))
    assert set(after) - set(original) == {"next_due_at"}


# ---------------------------------------------------------------------------
# CLI — the migration notice goes to stderr, never stdout
# ---------------------------------------------------------------------------

def test_cli_migration_notice_on_stderr_not_stdout(tmp_path):
    marker = tmp_path / "loop-mode.json"
    _write_marker(marker, {"cadence_seconds": 1800})
    res = _run("--rearm-delay", "--wake-kind", "other", "--marker", str(marker))
    assert res.returncode == 0, res.stderr
    assert re.fullmatch(r"-?\d+", res.stdout.strip()), repr(res.stdout)
    assert res.stderr.strip() != ""
    assert res.stderr.count("\n") <= 1, res.stderr


def test_cli_invalid_notice_on_stderr_not_stdout(tmp_path):
    marker = tmp_path / "loop-mode.json"
    _write_marker(marker, {"cadence_seconds": 1800, "next_due_at": "garbage"})
    res = _run("--rearm-delay", "--wake-kind", "other", "--marker", str(marker))
    assert res.returncode == 0, res.stderr
    assert re.fullmatch(r"-?\d+", res.stdout.strip()), repr(res.stdout)
    assert res.stderr.strip() != ""


def test_cli_preserved_is_silent_on_stderr(tmp_path):
    due = datetime.now(timezone.utc) + timedelta(seconds=600)
    marker = tmp_path / "loop-mode.json"
    _write_marker(
        marker,
        {"cadence_seconds": 1800, "next_due_at": due.strftime("%Y-%m-%dT%H:%M:%SZ")},
    )
    res = _run("--rearm-delay", "--wake-kind", "other", "--marker", str(marker))
    assert res.returncode == 0
    assert res.stderr == ""


# ---------------------------------------------------------------------------
# CLI — hard errors never guess
# ---------------------------------------------------------------------------

def test_cli_absent_marker_exits_2_naming_path(tmp_path):
    """Exit 2 with OUR message (not argparse's "unrecognized arguments", which
    would also exit 2 and also echo the path — a false green).  The phrase
    assertion is what makes this discriminating."""
    missing = tmp_path / "does-not-exist.json"
    res = _run("--rearm-delay", "--wake-kind", "other", "--marker", str(missing))
    assert res.returncode == 2, (res.returncode, res.stdout, res.stderr)
    assert str(missing) in res.stderr
    assert "marker file not found" in res.stderr
    assert res.stdout.strip() == ""


def test_cli_corrupt_json_exits_2(tmp_path):
    """Exit 2 with OUR message for a present-but-unparseable file — never a
    guessed cadence, and distinguishable from argparse rejecting the flag."""
    marker = tmp_path / "loop-mode.json"
    marker.write_text("{ not valid json", encoding="utf-8")
    res = _run("--rearm-delay", "--wake-kind", "other", "--marker", str(marker))
    assert res.returncode == 2, (res.returncode, res.stdout, res.stderr)
    assert str(marker) in res.stderr
    assert "not valid JSON" in res.stderr
    assert res.stdout.strip() == ""


# ---------------------------------------------------------------------------
# CLI — the legacy stdin-wrapping mode is unchanged
# ---------------------------------------------------------------------------

def test_cli_stdin_mode_unchanged_without_rearm_flag():
    """When --rearm-delay is NOT given, the module still wraps stdin."""
    res = _run("--kind", "working", stdin="body\n")
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("<loop-wake>")
    assert '"kind":"working"' in res.stdout


# ---------------------------------------------------------------------------
# §1 (fold 3) — a PRESENT-but-invalid cadence_seconds must be LOUD, never a
# silent fallback to the default (Sol's colleague blocker)
# ---------------------------------------------------------------------------
#
# The guard in `_resolve_cadence` already REJECTED every non-positive-int
# cadence, but it did so SILENTLY — the same silent-fallback shape this PR
# removes, surviving on the INPUT side.  The contract:
#
#   * cadence_seconds PRESENT and not a positive int (str / float / bool /
#     None / 0 / negative) -> one line on STDERR naming the FIELD, the
#     offending VALUE and its TYPE, and the helper arms the default (1800).
#     Exit code stays 0 — a loop must never fail to arm.
#   * cadence_seconds ABSENT -> silent default (the legitimate pre-upgrade
#     state; the `next_due_at` migration already speaks for it).
#   * cadence_seconds a positive int -> silent, unchanged.
#   * a `bool` is rejected even though `True` is an `int` subclass (§4).
#
# The cadence notice carries its OWN reason token (`invalid_cadence`),
# distinct from the `next_due_at` reasons (`migrated_absent` /
# `migrated_invalid`) — they describe a different field.

# (id, marker json literal, expected value repr, expected type name)
_INVALID_CADENCE_VALUES = [
    ("string", '"3300"', "'3300'", "str"),
    ("float", "3300.5", "3300.5", "float"),
    ("bool_true", "true", "True", "bool"),
    ("zero", "0", "0", "int"),
    ("negative", "-5", "-5", "int"),
    ("null", "null", "None", "NoneType"),
]


def _write_cadence_marker(tmp_path, json_literal) -> Path:
    marker = tmp_path / "loop-mode.json"
    marker.write_text(
        '{"cadence_seconds": ' + json_literal + "}\n", encoding="utf-8"
    )
    return marker


@pytest.mark.parametrize(
    "case_id,json_literal,expected_repr,expected_type",
    _INVALID_CADENCE_VALUES,
    ids=[c[0] for c in _INVALID_CADENCE_VALUES],
)
def test_cli_bad_cadence_is_loud_and_arms_default(
    case_id, json_literal, expected_repr, expected_type, tmp_path
):
    """A PRESENT-but-invalid cadence prints a loud stderr line naming the
    field, the value, and the type — and still arms the default with rc 0.

    Reverting to the silent fallback (guard kept, notice removed) reddens
    this cell: stderr becomes empty.

    NOTE the test name deliberately avoids the literal reason token
    (`invalid_cadence`): pytest's `tmp_path` is derived from the test
    function NAME, and the notice embeds the marker PATH — so a cell named
    for the token would contain that token in its own stderr via the path and
    pass whether or not the notice fired (a self-confirming control, caught
    while writing these).  The token assertion below keys on the labelled
    `loop_prompt: invalid_cadence:` prefix, which a tmp path cannot contain."""
    marker = _write_cadence_marker(tmp_path, json_literal)
    res = _run("--rearm-delay", "--wake-kind", "heartbeat", "--marker", str(marker))
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "1800", res.stdout           # default armed
    assert res.stderr.strip() != "", (
        f"{case_id}: a present-but-invalid cadence was SILENT on stderr"
    )
    assert "loop_prompt: invalid_cadence:" in res.stderr, res.stderr  # own token
    assert f"cadence_seconds is {expected_repr}" in res.stderr, res.stderr
    assert expected_type in res.stderr, res.stderr


def test_cli_valid_cadence_stays_silent(tmp_path):
    """A positive int cadence is unchanged: armed as-is, no notice."""
    marker = _write_cadence_marker(tmp_path, "3300")
    res = _run("--rearm-delay", "--wake-kind", "heartbeat", "--marker", str(marker))
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "3300"
    assert res.stderr == ""


def test_cli_absent_cadence_stays_silent(tmp_path):
    """An ABSENT cadence is the legitimate pre-upgrade state — silent default,
    no notice (the fold's recorded decision)."""
    marker = tmp_path / "loop-mode.json"
    _write_marker(marker, {"kind": "heartbeat"})   # no cadence_seconds
    res = _run("--rearm-delay", "--wake-kind", "heartbeat", "--marker", str(marker))
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "1800"
    assert res.stderr == ""


def test_cli_bad_cadence_notice_fires_alongside_the_migration_notice(tmp_path):
    """The cadence notice is about a DIFFERENT field than the migration
    notice, so an invalid cadence on a pre-upgrade marker prints BOTH — under
    distinct reason tokens.  (Name avoids the token literal, same
    self-confirming-path trap as the cell above.)"""
    marker = _write_cadence_marker(tmp_path, '"3300"')   # no next_due_at
    res = _run("--rearm-delay", "--wake-kind", "other", "--marker", str(marker))
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "1800"
    assert "loop_prompt: invalid_cadence:" in res.stderr, res.stderr
    assert "loop_prompt: migrated_absent:" in res.stderr, res.stderr


# ---------------------------------------------------------------------------
# Skill docs — the arm INSTRUCTION must route through the helper
# ---------------------------------------------------------------------------
#
# Round 1 landed `loop_prompt.py --rearm-delay`, but the OLD full-cadence arm
# instruction survives in the two channels `engram-loop/SKILL.md` itself ranks
# as primary (the Step 2 stub) and backstop (the Step 1 `FIRST_ACTION` field) —
# and in the sibling school-day skill's own copies.  A helper nothing reads is
# inert: an agent following the surviving text arms at the full cadence and
# never consults it.  The sweep below walks EVERY skill doc and fails on the
# phrase, while the positive cells (SAME parametrized table) keep the sweep
# from being satisfied by deleting the skill.

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "src" / "engram" / "skills"

# Every root the arm-instruction sweep walks.  A LIST OF SURFACES, not a proof
# of coverage: the arm instruction is written in the skills AND in the boot
# charges (which arm the heartbeat at session start), so a sweep over the
# skills alone leaves the very files #3005 round 3 edits unguarded.  Widening
# this set does not tighten the shape bound — see the module docstring for the
# residual.
SWEEP_ROOTS = (
    SKILLS_ROOT,
    REPO_ROOT / "deploy" / "autostart",
)

# The full-cadence arm SHAPES `--rearm-delay` replaced.  None may appear in
# ANY `*.md` under SWEEP_ROOTS (`src/engram/skills/` + `deploy/autostart/`).
#
# This is a SHAPE list, not the class.  Round 2 shipped only the first two
# entries (written from the carriers then known) and so missed the
# `rewritten wholesale` prose (which named the very `cat > …loop-mode.json`
# command the helper's `jq` patch forbids) and the surviving bare-cadence arm
# at engram-loop `:452`; round 3 adds both.  A future reword can still evade
# it — see the module docstring for the residual and for the STRUCTURAL
# protection (the arm instruction lives in one place, Step 2 item 3c).
FULL_CADENCE_ARM_PHRASES = (
    "ScheduleWakeup at cadence_seconds",
    "at this loop's cadence",
    "rewritten wholesale",
    "re-arm `ScheduleWakeup` (1800s",
)


def _sweep_offenders(roots) -> list:
    """Return ``path: phrase`` offenders for FULL_CADENCE_ARM_PHRASES under *roots*.

    The roots are an ARGUMENT rather than a read of SWEEP_ROOTS, so a test can
    point the sweep at a temp tree and prove it reports what it is handed —
    making the guard's reach a checkable property instead of a claim.
    """
    offenders = []
    for root in roots:
        root = Path(root)
        for path in sorted(root.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            for phrase in FULL_CADENCE_ARM_PHRASES:
                if phrase in text:
                    try:
                        label = str(path.relative_to(REPO_ROOT))
                    except ValueError:
                        label = str(path)
                    offenders.append(f"{label}: {phrase!r}")
    return offenders


_ENGRAM_LOOP_SKILL = "claude/engram-loop/SKILL.md"
_SCHOOL_DAY_SKILL = "claude/engram-school-day/SKILL.md"

# (id, kind, skill_relpath_or_None, needle_or_None) — one row per cell.
SKILL_DOC_CASES = [
    # Sweep: no skill doc may carry a full-cadence arm instruction.
    ("sweep_no_full_cadence_arm_instruction", "sweep", None, None),
    # Positive cells: the fix (and its wake-kind classification) is actually
    # present, so the sweep is not satisfiable by gutting the skill.
    #
    # Each wake-kind needle is a CLAUSE of the classification sentence
    # (engram-loop Step 2 item 3c), NOT the bare token: `heartbeat`/`other`
    # occur 23/13 times in the doc, so a bare-token needle would survive a
    # mutation that deleted the classification entirely.  Keying on the
    # clause makes that deletion redden the cell.  (The helper's CLI form
    # `--wake-kind heartbeat|other` is NOT used here: it lives only in the
    # Step 1 `FIRST_ACTION` field, so a cell keyed on it would stay green
    # under this mutation — it would encode a *mention*, not the class.)
    (
        "engram_loop_routes_through_rearm_delay",
        "contains",
        _ENGRAM_LOOP_SKILL,
        "--rearm-delay",
    ),
    (
        "engram_loop_states_wake_kind_heartbeat",
        "contains",
        _ENGRAM_LOOP_SKILL,
        "is the **`heartbeat`**",
    ),
    (
        "engram_loop_states_wake_kind_other",
        "contains",
        _ENGRAM_LOOP_SKILL,
        "is `other`.**",
    ),
    (
        "engram_school_day_routes_through_rearm_delay",
        "contains",
        _SCHOOL_DAY_SKILL,
        "--rearm-delay",
    ),
]


@pytest.mark.parametrize(
    "case_id,kind,skill_relpath,needle",
    SKILL_DOC_CASES,
    ids=[c[0] for c in SKILL_DOC_CASES],
)
def test_skill_docs_route_the_arm_through_the_helper(
    case_id, kind, skill_relpath, needle
):
    """The sweep cell fails naming the offending path; the positive cells in
    this same table fail if the skill (or the fix) is deleted.

    HONEST BOUND: the sweep walks `*.md` under SWEEP_ROOTS only, so its reach
    is narrower than the class in two ways — the shape list can be evaded by a
    NEW wording (module docstring), and every NON-`.md` carrier is outside it
    ENTIRELY.  One such carrier was real: the Stop-hook nudge in
    `engram-loop-arm-stop-hook.py` (a `.py`), pinned separately by the §3 cell
    below — NOT by a `.py`-wide sweep.  A green sweep means "none of the KNOWN
    shapes, in the roots walked" and nothing wider."""
    if kind == "sweep":
        offenders = _sweep_offenders(SWEEP_ROOTS)
        assert not offenders, (
            "full-cadence arm instruction survives — every ScheduleWakeup "
            "arm must route through `loop_prompt.py --rearm-delay` "
            "(engram-loop Step 2 item 3c).  Roots walked: "
            + ", ".join(str(r.relative_to(REPO_ROOT)) for r in SWEEP_ROOTS)
            + ".  Offending path:phrase pairs:\n"
            + "\n".join(offenders)
        )
    else:
        path = SKILLS_ROOT / skill_relpath
        assert path.exists(), f"skill doc missing: {skill_relpath}"
        text = path.read_text(encoding="utf-8")
        assert needle in text, (
            f"{skill_relpath} is missing required text {needle!r} — the "
            "helper-routing fix (or its wake-kind classification) went away"
        )


# ---------------------------------------------------------------------------
# Guard reach — the sweep must walk the surfaces the arm instruction is
# actually written on, not just the skills (#3005 round 3)
# ---------------------------------------------------------------------------
#
# Round 2's sweep walked `src/engram/skills/` only.  The boot charges — which
# are what arm the heartbeat loop at session start, and are the very files
# this PR edits — therefore sat outside the guard: a full-cadence arm sentence
# reintroduced there would not have failed anything.  Widening SWEEP_ROOTS is
# the fix; these two cells make the reach checkable rather than asserted in
# prose, and NEITHER of them reads as exhaustiveness (the shape bound above is
# untouched by widening a root set).


def test_sweep_walks_the_boot_charges_not_only_the_skills():
    """`deploy/autostart/` is in the swept roots.

    Goes red if the roots ever narrow back to skills-only — the state this
    round fixed.  It asserts the CONFIGURED reach; the cell below asserts the
    sweep actually honours a root it is handed.
    """
    assert REPO_ROOT / "deploy" / "autostart" in SWEEP_ROOTS


def test_sweep_reports_a_banned_phrase_under_a_deploy_root(tmp_path):
    """A banned full-cadence arm phrase under a given root is reported.

    Points `_sweep_offenders` at a temp tree (a root that is NOT the skills
    dir) and asserts the phrase is found — so the reach above is a working
    sweep over whatever roots it is given, not a constant that happens to be
    present in a tuple.
    """
    (tmp_path / "boot-charge-fake.md").write_text(
        "and then re-arm `ScheduleWakeup` (1800s and stop\n", encoding="utf-8"
    )
    offenders = _sweep_offenders([tmp_path])
    assert offenders, (
        "a banned full-cadence arm phrase under a given root was not "
        "reported — the sweep is not reading the roots it is handed"
    )


# ---------------------------------------------------------------------------
# §3 (fold 3) — the Stop-hook nudge is a NON-`.md` carrier the sweep cannot
# see.  This is a targeted per-carrier pin, NOT a `.py`-wide phrase sweep.
# ---------------------------------------------------------------------------
#
# The arm-instruction sweep above walks `*.md` under two roots only.  The Stop
# hook's nudge (`engram-loop-arm-stop-hook.py` `_NUDGE_TEXT`) tells an agent to
# arm at the end of an ordinary turn — and a nudge-driven arm is a
# non-heartbeat (`other`) wake, so a bare arm there resets the deadline to the
# full cadence: the #3005 push, delivered by the safety net itself.  This cell
# reddens if the nudge stops routing its delay through the helper.

_HOOK_PATH = (
    REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-loop-arm-stop-hook.py"
)


def test_stop_hook_nudge_routes_the_arm_through_the_helper_delay():
    """The nudge must name the helper's `--rearm-delay --wake-kind other`, not
    a bare full-cadence arm.  A nudge-driven arm is an `other` wake — the
    agent is arming at the end of an ordinary turn, not on a heartbeat wake."""
    spec = importlib.util.spec_from_file_location(
        "loop_arm_hook_carrier_test", _HOOK_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = mod._NUDGE_TEXT
    assert "--rearm-delay" in text, (
        "the Stop-hook nudge no longer routes the arm through the helper's "
        f"delay — a bare arm on an `other` wake is the #3005 push. text={text!r}"
    )
    assert "--wake-kind other" in text, (
        f"the nudge must name the `other` wake kind. text={text!r}"
    )
    assert "at this loop's cadence" not in text, (
        f"the nudge still carries a bare full-cadence arm. text={text!r}"
    )


# ---------------------------------------------------------------------------
# §4 (fold 3) — two pins for guards a mutant survived (Sol's controls did not
# redden anything).  Each cell must go RED when its guard changes.
# ---------------------------------------------------------------------------


def test_bool_cadence_is_rejected_not_read_as_one():
    """`True`/`False` are `int` SUBCLASSES in Python, so the `isinstance(int)`
    check alone would accept `True` as the cadence `1`.  The guard's
    `not isinstance(cadence, bool)` clause must reject it, so a bool cadence
    falls back to the default — NOT to 1.

    Deleting that clause makes `_resolve_cadence` return `True` (=1) and
    reddens this cell."""
    for value in (True, False):
        heartbeat = lp.compute_rearm(
            {"cadence_seconds": value}, now=NOW, is_heartbeat=True
        )
        assert heartbeat.delay_seconds == 1800, (
            f"cadence_seconds={value!r} (a bool) must fall back to the "
            f"default 1800, not be read as the int {int(value)}"
        )
        # And on the migration path the default is armed too.
        migrated = lp.compute_rearm(
            {"cadence_seconds": value}, now=NOW, is_heartbeat=False
        )
        assert migrated.delay_seconds == 1800, migrated.delay_seconds


def test_cli_wake_kind_defaults_to_other_preserve(tmp_path):
    """With NO `--wake-kind`, the CLI must default to `other` (PRESERVE).

    From the code path, `other` is the safe default: a forgotten flag then
    costs a PRESERVED deadline (harmless — the loop continues toward its
    original due moment), whereas a `heartbeat` default would RESET the
    deadline to the full cadence on every flag-less re-arm — the #3005 push
    reintroduced.  Changing the default reddens this cell (delay becomes 1800
    and the marker is rewritten)."""
    due = datetime.now(timezone.utc) + timedelta(seconds=600)
    marker = tmp_path / "loop-mode.json"
    _write_marker(
        marker,
        {"cadence_seconds": 1800, "next_due_at": due.strftime("%Y-%m-%dT%H:%M:%SZ")},
    )
    before = _sha(marker)
    res = _run("--rearm-delay", "--marker", str(marker))   # NO --wake-kind
    assert res.returncode == 0, res.stderr
    delay = int(res.stdout.strip())
    assert 580 <= delay <= 600, (
        f"default must be `other` (remaining delay ~600s), got {delay}"
    )
    assert _sha(marker) == before, (
        "default must be `other` (PRESERVE) — the marker was rewritten, so "
        "the default behaved as `heartbeat`"
    )


# ---------------------------------------------------------------------------
# §3 (fold 4) — the PRINTED command must RUN as printed (Sol's round-2 blocker)
# ---------------------------------------------------------------------------
#
# Two carriers print the helper command an agent is told to run: the Stop
# hook's `_NUDGE_TEXT`, and engram-loop Step 2 item 3c's fenced line.  Both
# printed a form resolving in NO cwd.  The hook said a bare relative
# `tools/loop_prompt.py` — copied from the DEPLOYED plugin layout, where the
# file sits at `<plugin_root>/tools/`, while in the source tree it is
# `src/engram/tools/`.  Item 3c said `"$CLAUDE_PLUGIN_ROOT/tools/loop_prompt.py"`,
# and `CLAUDE_PLUGIN_ROOT` is UNSET in an agent's Bash env (measured: absent
# from `env` and from a login shell), so it expands to `/tools/loop_prompt.py`.
# An agent that follows either verbatim gets "No such file or directory" and
# arms by hand at its habit — silently, which is the defect.
#
# These cells EXTRACT the command from the artifact (never retyping it) and
# assert the PROPERTY, not the spelling: the path must not be bare-relative,
# and must be either an ABSOLUTE path that exists on disk or the
# `${CLAUDE_PLUGIN_ROOT:-…}` default form.
#
# They deliberately do NOT EXECUTE the printed command.  The DEPLOYED plugin's
# `loop_prompt.py` predates this PR and rejects `--rearm-delay` (measured:
# `unrecognized arguments: --rearm-delay --wake-kind other`), so an executing
# pin would fail for a reason unrelated to this defect; execution only becomes
# meaningful once this PR ships and the plugin is rebuilt.

_LOOP_PROMPT_BACKTICK_RE = re.compile(r"`([^`\n]*loop_prompt\.py[^`\n]*)`")
_LOOP_PROMPT_FENCED_LINE_RE = re.compile(
    r"^[ \t]*python3\b[^\n]*loop_prompt\.py[^\n]*--rearm-delay[^\n]*$", re.M
)
_DEFAULT_FORM_MARKER = "${CLAUDE_PLUGIN_ROOT:-"


def _clean_path_token(token: str) -> str:
    """Strip the quoting around a path token, including a JSON-escaped quote.

    Two kinds of junk attach to a path token in these carriers: the two JSON
    carriers (engram-loop's `FIRST_ACTION` heredoc and school-day's `printf`
    heredoc) hold the path inside a JSON string, so the shell-literal form
    there is `\\"…\\"` — the JSON escape for the same double-quoted form the
    bash carriers use — and the path is frequently glued to an opening paren,
    a backtick or a quote with no space between (`(\\"…`, `` (`"… ``).
    """
    token = token.replace('\\"', '"')
    return token.strip("(`'\"\\").strip("`'\"\\),")


def _extract_printed_path(command: str) -> str:
    """The `loop_prompt.py` path argument of a printed helper command."""
    for token in command.split():
        path = _clean_path_token(token)
        if path.endswith("loop_prompt.py"):
            return path
    raise AssertionError(
        f"the printed command does not name loop_prompt.py: {command!r}"
    )


def _assert_printed_command_runs_as_printed(command: str, carrier: str) -> str:
    """Assert the printed command's path RESOLVES, and return that path.

    Two properties, keyed on the spelling only as far as the shell cares:
    the path is not bare-relative, and it is either an absolute path that
    exists on disk or the `${CLAUDE_PLUGIN_ROOT:-…}` default form.
    """
    path = _extract_printed_path(command)
    _assert_path_resolves(path, carrier, command)
    return path


def _assert_path_resolves(path: str, carrier: str, command: str) -> None:
    """The property: an absolute path that exists, or the default sh form."""
    if path.startswith("/"):
        assert Path(path).is_file(), (
            f"{carrier}: the printed command names the ABSOLUTE path {path!r}, "
            "which does not exist on disk — a printed command must run as "
            f"printed. command={command!r}"
        )
        return
    assert _DEFAULT_FORM_MARKER in path, (
        f"{carrier}: the printed command's path {path!r} is BARE-RELATIVE (or a "
        "bare `$CLAUDE_PLUGIN_ROOT/…`, which is the same thing where that "
        "variable is unset): it resolves in no cwd — the helper is at "
        "`src/engram/tools/`, not `tools/`, and an unset CLAUDE_PLUGIN_ROOT "
        "expands to `/tools/`. It must be an absolute resolved path or carry "
        f"the ${{CLAUDE_PLUGIN_ROOT:-…}} default form. command={command!r}"
    )


def test_stop_hook_nudge_prints_a_command_that_resolves():
    """The nudge's printed command must name a path that RESOLVES.

    The hook ALREADY resolves the helper's real location at import time — it
    has to, to import `LOOP_WAKE_MARKER` and land it in `_LP_TOOLS_DIR` — so
    it must print the path it RESOLVED rather than a static guess (fold-4
    contract §1).  That is layout-agnostic: correct in the source checkout AND
    in the deployed plugin bundle, whereas a hardcoded guess is correct in at
    most one of them.

    Goes red if the nudge reverts to a bare `tools/loop_prompt.py`.
    """
    spec = importlib.util.spec_from_file_location(
        "loop_arm_hook_command_carrier_test", _HOOK_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    match = _LOOP_PROMPT_BACKTICK_RE.search(mod._NUDGE_TEXT)
    assert match, (
        "the nudge no longer prints a backticked helper command at all: "
        f"{mod._NUDGE_TEXT!r}"
    )
    path = _assert_printed_command_runs_as_printed(
        match.group(1), "engram-loop-arm-stop-hook.py _NUDGE_TEXT"
    )

    # Contract §1: when import-time resolution SUCCEEDED, the printed command
    # names the path it resolved — not a static guess.  (Skipped when
    # resolution failed, where contract §2's default form is the correct
    # output and `_assert_printed_command_runs_as_printed` has already
    # checked it.)
    if mod._LP_TOOLS_DIR is not None:
        resolved = str(mod._LP_TOOLS_DIR / "loop_prompt.py")
        assert path == resolved, (
            f"the hook resolved the helper to {resolved!r} but printed "
            f"{path!r} — print the path you resolved, not a guess"
        )


def test_engram_loop_step_2_item_3c_prints_a_command_that_resolves():
    """Step 2 item 3c's fenced command must name a path that RESOLVES.

    Extracted from the skill's fenced block, never retyped.  Goes red if it
    reverts to the bare `"$CLAUDE_PLUGIN_ROOT/…"` form, which expands to
    `/tools/loop_prompt.py` on a seat where the variable is unset (measured:
    absent in an agent's Bash env, login shell included).
    """
    text = (SKILLS_ROOT / _ENGRAM_LOOP_SKILL).read_text(encoding="utf-8")
    match = _LOOP_PROMPT_FENCED_LINE_RE.search(text)
    assert match, (
        "engram-loop Step 2 item 3c no longer prints a `python3 … "
        "loop_prompt.py --rearm-delay …` command line"
    )
    _assert_printed_command_runs_as_printed(
        match.group(0), "engram-loop/SKILL.md Step 2 item 3c"
    )


def test_stop_hook_command_falls_back_to_the_default_form(monkeypatch):
    """Contract §2: with import-time resolution FAILED (`_LP_TOOLS_DIR is
    None`), the printed command carries the `${CLAUDE_PLUGIN_ROOT:-…}` sh form
    — never a bare relative path (§1 item 3, "in either branch").

    The two carrier pins above exercise only the RESOLVED branch, because in a
    source checkout resolution always succeeds; this branch is reachable on a
    seat where it does not, where a malformed fallback would ship the very
    defect this fold fixes — a printed command that does not run.  Exercises
    the builder `_NUDGE_TEXT` interpolates.
    """
    spec = importlib.util.spec_from_file_location(
        "loop_arm_hook_fallback_carrier_test", _HOOK_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setattr(mod, "_LP_TOOLS_DIR", None)
    command = mod._loop_prompt_rearm_command()
    path = _assert_printed_command_runs_as_printed(
        command, "engram-loop-arm-stop-hook.py (resolution failed)"
    )
    assert not path.startswith("/"), (
        "with resolution failed the command must use the sh form, not an "
        f"absolute path; got {command!r}"
    )


# ---------------------------------------------------------------------------
# §7 (fold 4) — a PROPERTY sweep: every printed `--rearm-delay` command
# ---------------------------------------------------------------------------
#
# The cells above are keyed on sites that were NAMED, and a cell per named site
# cannot see a site nobody named.  That is not hypothetical: two rounds of this
# fix left the bare form on two of engram-loop's OWN new lines — `:323`, the
# marker heredoc's read-every-wake `FIRST_ACTION` field, and `:369` — and on a
# whole carrier (engram-school-day's own `FIRST_ACTION` heredoc, `:69`) while
# every per-site pin stayed green.  This cell keys on the TOKEN instead.
#
# `--rearm-delay` is the delay helper's signature, and keying on it (rather
# than on the path shape `tools/loop_prompt.py`) is what makes the sweep
# correct rather than noisy: the gate command (`engram-loop-gate.py`) and the
# marker helper (`format_loop_prompt`) share the PATH SHAPE but not this token,
# so engram-loop `:240`, `:241` and `:479` are skipped automatically and
# correctly — they are a gate command and the marker helper, neither of them
# the delay helper, and both out of class.  Do NOT "improve" this into a
# path-shaped sweep: it would then fail on those three legitimate lines.
#
# Scope is every `*.md` under the skills tree and `deploy/autostart/`, PLUS the
# hook — read from what it PRINTS, because the hook builds both the path and
# the flags at import time, so `--rearm-delay` is not a literal on its nudge
# line (it lives in the builder the nudge interpolates).

DELAY_HELPER_TOKEN = "--rearm-delay"
DELAY_SWEEP_ROOTS = (SKILLS_ROOT, REPO_ROOT / "deploy" / "autostart")

# The seven sites known at fold-4 time, each named by an anchor that survives
# line-number drift.  Named AND asserted, so the sweep cannot pass vacuously —
# an expected set that can go empty passes forever and reads identical to
# success.  This list is the anti-vacuity floor, not an allowlist: a BROKEN
# site reddens the property check whether or not it is registered here.
EXPECTED_DELAY_HELPER_SITES = (
    ("engram-loop-arm-stop-hook.py", "Arm now — ScheduleWakeup"),
    ("engram-loop/SKILL.md", '"FIRST_ACTION"'),
    ("engram-loop/SKILL.md", "creates it on the"),
    ("engram-loop/SKILL.md", "--rearm-delay --wake-kind <KIND>"),
    ("engram-school-day/SKILL.md", '"FIRST_ACTION"'),
    ("boot-charge-default-claude.md", "--rearm-delay --wake-kind heartbeat"),
    ("boot-charge-quiet-claude.md", "--rearm-delay --wake-kind heartbeat"),
)


def _path_before_delay_token(line: str) -> str:
    """The `loop_prompt.py` path that the line's `--rearm-delay` belongs to.

    The NEAREST path token preceding the flag, because a line can carry more
    than one `loop_prompt.py` mention: the boot charges also name the marker
    helper, which has no `--rearm-delay` and is deliberately out of class.
    """
    head = line[: line.index(DELAY_HELPER_TOKEN)]
    for token in reversed(head.split()):
        path = _clean_path_token(token)
        if path.endswith("loop_prompt.py"):
            return path
    raise AssertionError(
        f"a `{DELAY_HELPER_TOKEN}` with no loop_prompt.py path before it: "
        f"{line!r}"
    )


def _delay_helper_sites():
    """``(carrier, label, segment)`` for every printed `--rearm-delay` command."""
    sites = []
    for root in DELAY_SWEEP_ROOTS:
        for path in sorted(Path(root).rglob("*.md")):
            try:
                rel = str(path.relative_to(REPO_ROOT))
            except ValueError:
                rel = str(path)
            lines = path.read_text(encoding="utf-8").splitlines()
            for lineno, line in enumerate(lines, 1):
                if DELAY_HELPER_TOKEN in line:
                    sites.append((rel, f"{rel}:{lineno}", line))

    spec = importlib.util.spec_from_file_location(
        "loop_arm_hook_delay_sweep", _HOOK_PATH
    )
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    sites.append(
        (
            "engram-loop-arm-stop-hook.py",
            "engram-loop-arm-stop-hook.py:_NUDGE_TEXT (what it prints)",
            hook._NUDGE_TEXT,
        )
    )
    return sites


def test_every_printed_delay_helper_command_resolves():
    """Every printed delay-helper command in the swept carriers resolves.

    Extraction is per SITE, not per file: each `--rearm-delay` occurrence is
    paired with the nearest path token before it, so the boot charges' marker
    helper (a legitimate, out-of-class `loop_prompt.py` mention) is not dragged
    in beside the delay command that follows it.

    The property loop runs FIRST so a broken new site is reported as what it is
    — a non-resolving path, at a named line — rather than as a count mismatch;
    the count/expected checks then run as the anti-vacuity guard.
    """
    found = _delay_helper_sites()
    labels = [label for _, label, _ in found]

    for rel, label, segment in found:
        if rel.endswith(".md"):
            _assert_path_resolves(
                _path_before_delay_token(segment), label, segment
            )
        else:
            _assert_printed_command_runs_as_printed(segment, label)

    assert len(found) == len(EXPECTED_DELAY_HELPER_SITES), (
        f"the sweep found {len(found)} printed delay-helper site(s), expected "
        f"{len(EXPECTED_DELAY_HELPER_SITES)}.  Found: {labels}.  A NEW site is "
        "either a fresh defect to fold or a legitimate addition to register in "
        "EXPECTED_DELAY_HELPER_SITES — decide which; never widen the sweep "
        "blindly.  Expected: "
        + ", ".join(f"{c} ({a!r})" for c, a in EXPECTED_DELAY_HELPER_SITES)
    )
    for carrier, anchor in EXPECTED_DELAY_HELPER_SITES:
        assert any(
            carrier in rel and anchor in segment for rel, _l, segment in found
        ), (
            f"the sweep did not find the expected `{DELAY_HELPER_TOKEN}` site "
            f"in {carrier} carrying {anchor!r} — found: {labels}"
        )
