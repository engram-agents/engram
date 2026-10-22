"""Tests for the post-compact history-arc injection block (#2026 WS2) in the
SessionStart hook.

Under test: `history_arc_block(engram_home)` — a pure-read render of the most
recent history file (newest-by-filename resolution, per Ariadne's WS2
correction recorded in that function's docstring and in the PR body) for
injection on source=="compact"/"resume", plus main()'s wiring of it into the
else: branch and the dropped false-claim instruction line.

Mirrors the loading/harness conventions of test_session_start_hook_checkpoint.py
(#2026 WS1's sibling test file, added the same day by #2034): fresh module
import per test via importlib, in-memory/tmp-path fixture files, main() driven
via stdin+stdout capture.
"""

import importlib.util
import io
import json
import os
import re
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_hook_module(engram_home: Path):
    """Load the session-start hook with ENGRAM_HOME pointing at engram_home."""
    os.environ["ENGRAM_HOME"] = str(engram_home)
    os.environ.pop("ENGRAM_RUNTIME_DIR", None)
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    sys.modules["context_tracker"] = stub_ct

    for mod_name in list(sys.modules.keys()):
        if mod_name in ("engram-session-start-hook", "engram_session_start_hook"):
            del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location("engram_session_start_hook", _HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # engram-alpha#127: pin detect_harness() to "claude-code" (measured
    # budget=9,500) -- this dev/source checkout has no baked platform.json
    # (see _harness.py's own docstring), so the hook's own detect_harness()
    # would otherwise return None and get_harness_budgets(None) would fall
    # through to UNMEASURED_HARNESS_FLOOR (4,000), which is smaller than
    # this file's fixtures were written against. Same discipline as
    # tests/test_lesson_tripwire_injection_bounds.py's own
    # _load_harness_budgets() docstring.
    module.detect_harness = lambda: "claude-code"
    # engram-alpha#127: stub out the tool-capability-index block so these
    # tests are not incidentally coupled to the REAL checked-in
    # tool_capability_index.txt artifact's size (measured 11,049 bytes on
    # this checkout -- alone bigger than the hook's own 9,500-char TOTAL
    # budget, which would starve every lower-priority block sorted after
    # it, including the calibration block this file's own ordering tests
    # check against).
    module.tool_capability_index_block = lambda: ""
    return module


def _run_hook_main(module, hook_input: dict) -> dict:
    stdin_data = json.dumps(hook_input)
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with redirect_stdout(buf):
            module.main()
    return json.loads(buf.getvalue().strip())


def _mock_no_engram_client():
    return patch.dict("sys.modules", {"engram_client": None})


@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram"
    home.mkdir()
    (home / "sessions").mkdir()
    return home


def _write_history_file(history_dir: Path, name: str, content: str, mtime: float | None = None) -> Path:
    history_dir.mkdir(parents=True, exist_ok=True)
    path = history_dir / name
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------------------
# 1. startup behaviour byte-identical to before this change: no
#    history_arc_block() call, no change to that branch's output.
# ---------------------------------------------------------------------------

def test_startup_path_unaffected_by_history_arc(engram_home):
    history_dir = engram_home / "history"
    _write_history_file(history_dir, "2026-08-15.md", "Some arc content that should NOT appear on startup.")

    mod = _load_hook_module(engram_home)
    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            result = _run_hook_main(mod, {
                "session_id": "sess-startup-1",
                "transcript_path": "/tmp/t.jsonl",
                "source": "startup",
            })
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "Most recent arc —" not in ctx, "history_arc_block must not fire on source=='startup'"
    assert "Some arc content that should NOT appear on startup." not in ctx
    # The startup branch's own reading-list instruction is still present, unchanged.
    assert "follow your Session Start Reading list" in ctx


# ---------------------------------------------------------------------------
# 2. compact injects the newest-by-filename file's content, NOT whichever
#    file happens to have the latest mtime — the test that actually
#    exercises Ariadne's WS2 correction (newest-by-filename resolution,
#    not mtime). mtime must NOT be used for selection anymore: a
#    dream-cycle/backfill process touching an OLDER file's mtime is a real,
#    currently-occurring pattern, not a hypothetical.
# ---------------------------------------------------------------------------

def test_compact_injects_newest_by_filename_not_mtime_newest(engram_home):
    history_dir = engram_home / "history"
    # Written in order: 2026-08-14.md (older by filename) then
    # 2026-08-15.md (newer by filename).
    older_path = _write_history_file(
        history_dir, "2026-08-14.md",
        "OLDER-BY-FILENAME CONTENT — must not be selected despite its later mtime.",
    )
    newer_path = _write_history_file(
        history_dir, "2026-08-15.md",
        "NEWEST-BY-FILENAME CONTENT — this is the one that must be selected.",
    )
    # Re-touch the OLDER-by-filename file's mtime so it is LATER than the
    # newer-by-filename file's mtime — simulates Ariadne's counter-scenario
    # (a dream-cycle backfill touching an older history file after today's
    # own file was already written), the case mtime-based selection gets
    # wrong.
    newer_mtime = os.path.getmtime(newer_path)
    os.utime(older_path, (newer_mtime + 100, newer_mtime + 100))
    assert os.path.getmtime(older_path) > os.path.getmtime(newer_path)

    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))
    assert "NEWEST-BY-FILENAME CONTENT" in text, text
    assert "OLDER-BY-FILENAME CONTENT" not in text, text


def test_compact_wires_history_arc_into_additional_context(engram_home):
    history_dir = engram_home / "history"
    _write_history_file(history_dir, "2026-08-15.md", "Today's real content for compact injection.")

    mod = _load_hook_module(engram_home)
    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            result = _run_hook_main(mod, {
                "session_id": "sess-compact-1",
                "transcript_path": "/tmp/t.jsonl",
                "source": "compact",
            })
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "Today's real content for compact injection." in ctx


# ---------------------------------------------------------------------------
# 3. The injected block carries the "already lived, not a to-do list"
#    framing AND a boundary timestamp.
# ---------------------------------------------------------------------------

def test_block_carries_framing_and_boundary_timestamp(engram_home):
    history_dir = engram_home / "history"
    _write_history_file(history_dir, "2026-08-15.md", "Some content.")

    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))
    # Names the resolved file rather than claiming "today" (Ariadne's WS2
    # correction) — the framing clause is unchanged and load-bearing.
    assert "Most recent arc — 2026-08-15.md, last entry " in text
    assert "Already lived, not a to-do list" in text
    assert "today" not in text.lower()
    # Boundary timestamp is ISO-8601 UTC — cheap sanity check on shape.
    assert "T" in text and ("+00:00" in text or "Z" in text)


# ---------------------------------------------------------------------------
# 4. Empty (but existing) history file -> explicit "No history entries"
#    line naming the file, not an empty/absent block.
# ---------------------------------------------------------------------------

def test_empty_existing_file_renders_explicit_no_entries_line(engram_home):
    history_dir = engram_home / "history"
    _write_history_file(history_dir, "2026-08-15.md", "   \n  \n")  # whitespace-only

    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))
    assert text != ""
    assert "No history entries" in text
    assert "2026-08-15.md" in text
    # Still carries a boundary timestamp — the file DOES exist and has an mtime.
    assert "(as of " in text


# ---------------------------------------------------------------------------
# 5. No history files at all (fresh HISTORY_DIR, or HISTORY_DIR itself
#    missing) -> same explicit no-entries rendering, not "" and not an
#    exception. No file to name -> no filename, no timestamp.
# ---------------------------------------------------------------------------

def test_no_history_dir_at_all_renders_explicit_no_entries(engram_home):
    # HISTORY_DIR never created.
    assert not (engram_home / "history").exists()
    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))
    assert text != ""
    assert "No history entries — no history files exist yet." in text


def test_empty_history_dir_renders_explicit_no_entries(engram_home):
    history_dir = engram_home / "history"
    history_dir.mkdir()  # exists, but no files inside
    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))
    assert text != ""
    assert "No history entries — no history files exist yet." in text


def test_non_matching_files_only_renders_explicit_no_entries(engram_home):
    """A dream/ subdirectory entry and a -details.md sidecar with no matching
    base file must not be picked up (no recursion, filename pattern only)."""
    history_dir = engram_home / "history"
    history_dir.mkdir()
    (history_dir / "dream").mkdir()
    (history_dir / "dream" / "2026-08-15.md").write_text("Dream content — must not be picked up.")
    (history_dir / "2026-08-15-details.md").write_text("Details sidecar with no base file.")
    (history_dir / "notes.txt").write_text("Not a dated history file.")

    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))
    assert "No history entries — no history files exist yet." in text
    assert "Dream content" not in text
    assert "Details sidecar" not in text


# ---------------------------------------------------------------------------
# 6. Genuinely unreadable -> "", no exception. Distinct from #5.
# ---------------------------------------------------------------------------

def test_broken_symlink_history_dir_returns_empty_string(engram_home):
    real_target = engram_home / "nonexistent-target"
    history_dir = engram_home / "history"
    os.symlink(str(real_target), str(history_dir))  # dangling symlink
    assert not history_dir.exists()  # confirms it's genuinely broken
    assert os.path.islink(str(history_dir))

    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))
    assert text == "", f"broken-symlink HISTORY_DIR must be a genuine read error, got: {text!r}"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permission checks")
def test_unreadable_file_returns_empty_string(engram_home):
    history_dir = engram_home / "history"
    path = _write_history_file(history_dir, "2026-08-15.md", "Secret content nobody should read.")
    os.chmod(path, 0o000)
    try:
        mod = _load_hook_module(engram_home)
        text = mod.history_arc_block(str(engram_home))
        assert text == "", f"permission-denied file must be a genuine read error, got: {text!r}"
    finally:
        os.chmod(path, 0o644)  # restore so tmp_path cleanup can remove it


# ---------------------------------------------------------------------------
# 7. The emitted instruction line no longer contains the measured-false
#    claim.
# ---------------------------------------------------------------------------

def test_instruction_line_drops_measured_false_claim(engram_home):
    mod = _load_hook_module(engram_home)
    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            result = _run_hook_main(mod, {
                "session_id": "sess-compact-2",
                "transcript_path": "/tmp/t.jsonl",
                "source": "compact",
            })
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "the compaction summary carries work-thread continuity" not in ctx
    # And it doesn't just drop the claim silently -- it says what the
    # summary IS good for, without telling the reader to ignore it.
    assert "table-of-contents index" in ctx or "table of contents" in ctx.lower()


# ---------------------------------------------------------------------------
# 8. Cap enforcement: exceeding HISTORY_ARC_CHAR_CAP renders the explicit
#    elision line, not silent truncation.
# ---------------------------------------------------------------------------

def test_cap_enforcement_renders_explicit_elision_line(engram_home):
    history_dir = engram_home / "history"
    mod_probe = _load_hook_module(engram_home)
    cap = mod_probe.HISTORY_ARC_CHAR_CAP
    overflow = 500
    big_content = "X" * (cap + overflow)
    path = _write_history_file(history_dir, "2026-08-15.md", big_content)

    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))
    assert ("X" * cap) in text
    assert ("X" * (cap + 1)) not in text, "content must be capped, not silently included in full"
    assert f"+{overflow} earlier chars elided" in text
    assert str(path) in text  # no sidecar -> points at the file's own path
    # Elision line precedes the kept content, not follows it (gh#2052).
    assert text.index("earlier chars elided") < text.index("X" * cap)


# ---------------------------------------------------------------------------
# 9. Sidecar pointer: a <date>.md + <date>-details.md pair renders a pointer
#    to the details file in the elision line (builder's choice — see PR body
#    for why "always note it" was not picked).
# ---------------------------------------------------------------------------

def test_sidecar_pointer_used_in_elision_line_when_capped(engram_home):
    history_dir = engram_home / "history"
    mod_probe = _load_hook_module(engram_home)
    cap = mod_probe.HISTORY_ARC_CHAR_CAP
    big_content = "Y" * (cap + 200)
    base_path = _write_history_file(history_dir, "2026-08-15.md", big_content)
    details_path = history_dir / "2026-08-15-details.md"
    details_path.write_text("Full details live here.")

    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))
    assert str(details_path) in text, text[-300:]
    assert str(base_path) not in text, "when a sidecar exists, the elision line should point at IT, not the base file"


# ---------------------------------------------------------------------------
# 10. Ordering: history-arc block appears before the #1819 divider's guarded
#     section, adjacent to (not interleaved with) WS1's checkpoint block.
# ---------------------------------------------------------------------------

_CANNED_CONF_ALL = {
    "by_type": {
        "observation_factual": {"n": 5, "mean": 0.72, "p25": 0.65, "p50": 0.72, "p75": 0.80},
    },
}
_CANNED_CONF_7D = {"by_type": {}}


def _make_mock_engram_client():
    mock_client = MagicMock()

    def _call_side_effect(tool_name, arguments=None):
        if tool_name != "engram_stats":
            return {}
        try:
            params = json.loads((arguments or {}).get("payload_json", "{}"))
        except (json.JSONDecodeError, AttributeError):
            params = {}
        if params.get("mode") == "7-turn":
            return {"confidence": _CANNED_CONF_7D}
        return {"confidence": _CANNED_CONF_ALL, "memory": {"current_turn": 3, "tier_sizes": {}}}

    mock_client.call.side_effect = _call_side_effect
    return mock_client


def test_history_arc_precedes_calibration_and_is_adjacent_to_checkpoint(engram_home):
    import sqlite3

    history_dir = engram_home / "history"
    _write_history_file(history_dir, "2026-08-15.md", "Ordering-test arc content.")

    session_id = "sess-order-history"
    mod = _load_hook_module(engram_home)

    # Seed injected_through via a first SessionStart (source=startup, so the
    # history-arc block itself doesn't fire this call, matching real usage).
    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            _run_hook_main(mod, {
                "session_id": session_id, "transcript_path": "/tmp/t.jsonl", "source": "startup",
            })
    with open(engram_home / "sessions" / f"{session_id}.json") as f:
        seeded = json.load(f)["injected_through"]

    # A node in-window so the checkpoint block actually renders.
    db_path = engram_home / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT, "
        "recall_summary TEXT, created_at TEXT, is_current INTEGER, status TEXT)"
    )
    conn.execute(
        "INSERT INTO nodes (id, type, claim, created_at, is_current, status) "
        "VALUES (?, ?, ?, ?, 1, 'active')",
        ("ob_0099", "observation_factual", "Ordering-test node.", seeded),
    )
    conn.execute("PRAGMA user_version = 1")  # #3741: a real store carries a schema version; the flag-OFF calibration block now runs only on one
    conn.commit()
    conn.close()

    mock_client_class = MagicMock(return_value=_make_mock_engram_client())
    with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=mock_client_class)}):
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            result = _run_hook_main(mod, {
                "session_id": session_id, "transcript_path": "/tmp/t.jsonl", "source": "compact",
            })

    ctx = result["hookSpecificOutput"]["additionalContext"]
    history_idx = ctx.find("Most recent arc —")
    checkpoint_idx = ctx.find("Nodes created since checkpoint")
    calib_idx = ctx.find("ENGRAM Calibration")

    assert history_idx != -1, f"history-arc block missing entirely: {ctx!r}"
    assert checkpoint_idx != -1, f"checkpoint block missing entirely: {ctx!r}"
    assert calib_idx != -1, f"calibration block missing entirely: {ctx!r}"

    assert history_idx < calib_idx, "history-arc block must render before the #1819-guarded calibration block"
    assert checkpoint_idx < calib_idx, "checkpoint block must render before the #1819-guarded calibration block"

    # Adjacent, not interleaved: nothing else appended between the two
    # blocks' own text (the history block's own content sits directly
    # before the checkpoint header in `lines`).
    between = ctx[history_idx:checkpoint_idx]
    assert between.count("\n\n") <= 2, f"unexpected content between the two adjacent blocks: {between!r}"


# ---------------------------------------------------------------------------
# 11. gh#2052 regression: history_arc_block() was tail-slicing wrong — it
#     did `stripped[:CAP]` (a HEAD slice) on files that are chronological
#     ASCENDING (oldest entry first, newest last), so an over-cap file kept
#     the OLDEST content and dropped the NEWEST -- while the block's own
#     header claims "Most recent arc". These tests exercise the fix: a TAIL
#     slice, cut on a clean line boundary, with the elision line ABOVE the
#     kept content and an accurate, start-naming char count.
# ---------------------------------------------------------------------------

def _split_block(text: str):
    """Split a history_arc_block() over-cap render into its three guaranteed
    single-line-header/single-line-elision/multi-line-kept parts. Relies on
    the function's own contract: `f"{header}\\n{elision}\\n{kept}"`, where
    header and elision are always exactly one line each."""
    header_line, elision_line, kept_content = text.split("\n", 2)
    return header_line, elision_line, kept_content


def test_direction_keeps_newest_drops_oldest(engram_home):
    """The regression test for gh#2052: an over-cap file's NEWEST entries
    must survive and its OLDEST entries must be dropped -- never the
    reverse. Asserts on specific marker strings, not on lengths."""
    history_dir = engram_home / "history"
    mod_probe = _load_hook_module(engram_home)
    cap = mod_probe.HISTORY_ARC_CHAR_CAP

    filler = "x" * 60
    n_lines = (cap // (len(filler) + 20)) + 40  # comfortably over the cap
    lines = [f"- entry {i:04d} {filler}" for i in range(n_lines)]
    lines[0] = f"- OLDEST-ENTRY-MARKER {filler}"
    lines[-1] = f"- NEWEST-ENTRY-MARKER {filler}"
    big_content = "\n".join(lines)
    assert len(big_content.strip()) > cap, "fixture must actually exceed the cap"

    _write_history_file(history_dir, "2026-08-15.md", big_content)
    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))

    assert "NEWEST-ENTRY-MARKER" in text, "the newest entry must survive the cap (gh#2052)"
    assert "OLDEST-ENTRY-MARKER" not in text, "the oldest entry must be dropped, not the newest (gh#2052)"


def test_elision_line_precedes_content_and_names_the_start(engram_home):
    """The elision line renders ABOVE the kept content (not below), and its
    wording names what was elided FROM THE START -- not a generic
    "more chars" phrase that reads as trimming the end (gh#2052)."""
    history_dir = engram_home / "history"
    mod_probe = _load_hook_module(engram_home)
    cap = mod_probe.HISTORY_ARC_CHAR_CAP

    filler = "x" * 60
    n_lines = (cap // (len(filler) + 20)) + 40
    lines = [f"- entry {i:04d} {filler}" for i in range(n_lines)]
    big_content = "\n".join(lines)
    path = _write_history_file(history_dir, "2026-08-15.md", big_content)

    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))

    _header_line, elision_line, kept_content = _split_block(text)
    assert "earlier chars elided" in elision_line, elision_line
    assert str(path) in elision_line
    # The old, misleading wording must be gone entirely.
    assert "more chars" not in text
    # Elision line is structurally BEFORE the kept content (it is the
    # middle of the three split() parts, and kept_content carries none of
    # the elision wording).
    assert "elided" not in kept_content


def test_boundary_cleanliness_advances_to_next_complete_line(engram_home):
    """A raw tail slice can land mid-line; the kept content must always
    open on a COMPLETE line, never a fragment. Construct a fixture where a
    naive `stripped[-cap:]` slice demonstrably lands inside one very long
    line, and assert the returned text carries no fragment of it (gh#2052)."""
    history_dir = engram_home / "history"
    mod_probe = _load_hook_module(engram_home)
    cap = mod_probe.HISTORY_ARC_CHAR_CAP

    marker = "STRADDLE-MID-LINE-MARKER-"
    straddle = marker + ("s" * cap)  # one single line, longer than the cap alone
    rest = "\n- NEWEST-KEPT-LINE-MARKER\n"
    big_content = straddle + rest
    stripped = big_content.strip()
    assert len(stripped) > cap

    # Confirm the fixture actually exercises the mid-line case: a raw
    # `stripped[-cap:]` slice must land inside `straddle`, not at a
    # newline -- otherwise this test would not be testing what it claims.
    start_idx = len(stripped) - cap
    assert stripped[start_idx - 1] != "\n", "fixture must land mid-line for this test to be meaningful"
    assert marker not in stripped[start_idx:], "fixture must cut past the marker, inside the filler run"

    _write_history_file(history_dir, "2026-08-15.md", big_content)
    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))

    assert "NEWEST-KEPT-LINE-MARKER" in text
    assert marker not in text, "the straddled line must be dropped whole, not partially kept"
    assert "sss" not in text, "a raw mid-line cut would leak straddle filler ('s' runs) into the kept content"

    _header_line, _elision_line, kept_content = _split_block(text)
    assert kept_content == "- NEWEST-KEPT-LINE-MARKER", repr(kept_content)


def test_cut_chars_accurate_after_boundary_adjustment(engram_home):
    """cut_chars must equal len(stripped) - len(kept) for what is ACTUALLY
    returned -- i.e. computed AFTER the line-boundary adjustment, not the
    pre-adjustment `len(stripped) - HISTORY_ARC_CHAR_CAP` figure (gh#2052)."""
    history_dir = engram_home / "history"
    mod_probe = _load_hook_module(engram_home)
    cap = mod_probe.HISTORY_ARC_CHAR_CAP

    filler = "x" * 60
    n_lines = (cap // (len(filler) + 20)) + 40
    lines = [f"- entry {i:04d} {filler}" for i in range(n_lines)]
    big_content = "\n".join(lines)
    stripped = big_content.strip()
    assert len(stripped) > cap

    _write_history_file(history_dir, "2026-08-15.md", big_content)
    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))

    _header_line, elision_line, kept_content = _split_block(text)
    m = re.search(r"\+(\d+) earlier chars elided", elision_line)
    assert m, elision_line
    reported_cut = int(m.group(1))

    assert reported_cut == len(stripped) - len(kept_content), (
        reported_cut, len(stripped), len(kept_content)
    )
    # And the pre-adjustment figure (what the OLD/buggy report would have
    # said) must NOT be what's reported, since the boundary snap changes
    # the kept length by a nonzero amount for this fixture.
    naive_cut_chars = len(stripped) - cap
    assert kept_content != stripped[-cap:], "fixture must actually exercise a mid-line boundary snap"
    assert reported_cut != naive_cut_chars or len(kept_content) == cap


def test_undercap_path_unchanged_no_elision_line(engram_home):
    """Under the cap: full content, byte-identical to today, no elision
    line at all (unchanged behaviour -- gh#2052 only touches the over-cap
    branch)."""
    history_dir = engram_home / "history"
    content = "- a small entry\n- another small entry\n"
    _write_history_file(history_dir, "2026-08-15.md", content)

    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))

    assert "elided" not in text
    assert "more chars" not in text
    assert content.strip() in text
    _header_line, rest = text.split("\n", 1)
    assert rest == content.strip()


def test_kept_content_never_exceeds_the_cap(engram_home):
    """The kept content length must never exceed HISTORY_ARC_CHAR_CAP -- the
    line-boundary snap can only shrink it, never grow it past the cap
    (gh#2052)."""
    history_dir = engram_home / "history"
    mod_probe = _load_hook_module(engram_home)
    cap = mod_probe.HISTORY_ARC_CHAR_CAP

    filler = "x" * 60
    n_lines = (cap // (len(filler) + 20)) + 40
    lines = [f"- entry {i:04d} {filler}" for i in range(n_lines)]
    big_content = "\n".join(lines)
    _write_history_file(history_dir, "2026-08-15.md", big_content)

    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))

    _header_line, _elision_line, kept_content = _split_block(text)
    assert len(kept_content) <= cap, len(kept_content)


def test_no_newline_in_capped_tail_falls_back_to_raw_tail_not_empty(engram_home):
    """Degenerate case: the final HISTORY_ARC_CHAR_CAP characters of the file
    contain no newline at all (e.g. one pathologically long single line --
    a pasted blob, a very long one-line entry). There is no clean line
    boundary to advance to. Round-2 review finding (gh#2052): falling back
    to an empty kept region here is strictly worse than the original bug --
    it silently renders a real file as content-free behind an
    ordinary-looking elision line. The fix must fall back to the RAW tail
    (a possibly-ragged first line) rather than dropping everything."""
    history_dir = engram_home / "history"
    mod_probe = _load_hook_module(engram_home)
    cap = mod_probe.HISTORY_ARC_CHAR_CAP

    # One single line with no newline anywhere in it, long enough on its
    # own to exceed the cap -- so the entire capped tail region is inside
    # this one line, with no '\n' for the boundary-snap logic to find.
    single_line = "NO-NEWLINE-AT-ALL-" + ("z" * (cap + 500))
    _write_history_file(history_dir, "2026-08-15.md", single_line)
    stripped = single_line.strip()
    assert len(stripped) > cap
    # Sanity: confirm the fixture actually has no newline in the capped
    # tail region, so this test exercises the fallback and not the normal
    # line-boundary-snap path.
    start_idx = len(stripped) - cap
    assert "\n" not in stripped[start_idx:], "fixture must contain no newline in the capped tail"

    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))

    _header_line, _elision_line, kept_content = _split_block(text)
    assert kept_content != "", "no-newline-in-tail must fall back to the raw tail, not return empty content"
    assert len(kept_content) <= cap, len(kept_content)
    # The newest text (the file's tail end) must be present.
    assert kept_content.endswith("z" * 100), kept_content[-120:]


# ---------------------------------------------------------------------------
# 10. gh#2056: the near-empty boundary snap (a newline landing exactly one
#     char before the capped tail's own end) is a verified-correct edge, but
#     nothing previously pinned it DISTINCTLY from the no-newline-at-all
#     fallback above. Both leave "kept" far short of the cap, so a refactor
#     that accidentally merged the two branches (e.g. routing the
#     newline-found case through the no-newline fallback, or vice versa)
#     would pass every pre-existing test here -- none of them assert on the
#     EXACT kept length for either branch. These two tests close that gap:
#     one pins the near-empty snap's kept length at exactly 1 (this is what
#     "a newline one char before EOF" means -- only the char after the
#     newline survives), the other pins the no-newline fallback's kept
#     length at exactly the full cap (the raw, ragged tail is kept in full,
#     since there's no boundary to advance to). The two expected lengths (1
#     vs. cap) cannot both be produced by a merged/collapsed code path, so
#     if the branches are ever accidentally unified these tests fail with
#     different, path-identifying assertion errors rather than both silently
#     passing or both silently failing the same way.
# ---------------------------------------------------------------------------

def test_boundary_snap_one_char_before_eof_keeps_a_single_char(engram_home):
    """A newline landing exactly one character before the end of the capped
    tail region must snap the kept content down to that trailing single
    character -- the verified-correct near-empty edge cited in gh#2056.
    Distinct from the no-newline fallback (next test): here a newline WAS
    found in the tail, just pathologically close to the end, so the normal
    line-boundary-snap branch runs (not the fallback branch)."""
    history_dir = engram_home / "history"
    mod_probe = _load_hook_module(engram_home)
    cap = mod_probe.HISTORY_ARC_CHAR_CAP

    # Prefix keeps the mid-line boundary condition true (the char just
    # before start_idx must not be '\n', per the guard the function checks)
    # and is dropped by the cap regardless of its content.
    prefix = "P" * 500
    # Tail region (last `cap` chars of the stripped content): cap-2 filler
    # chars, then a newline one char before the tail's own end, then a
    # single trailing char -- so only that last char can survive the snap.
    tail = ("Q" * (cap - 2)) + "\n" + "Z"
    content = prefix + tail
    stripped = content.strip()
    assert len(stripped) > cap, "fixture must actually exceed the cap"

    start_idx = len(stripped) - cap
    assert stripped[start_idx - 1] != "\n", "fixture must land mid-line for the snap branch to run"
    assert "\n" in stripped[start_idx:], "fixture must contain a newline in the capped tail (not the fallback case)"

    _write_history_file(history_dir, "2026-08-15.md", content)
    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))

    _header_line, elision_line, kept_content = _split_block(text)
    # Verified by direct execution (see handoff transcript): kept == "Z",
    # cut_chars == len(stripped) - 1 == 32499 for cap == 32000.
    assert kept_content == "Z", repr(kept_content)
    assert len(kept_content) == 1, len(kept_content)
    m = re.search(r"\+(\d+) earlier chars elided", elision_line)
    assert m, elision_line
    assert int(m.group(1)) == len(stripped) - 1, (m.group(1), len(stripped))


def test_no_newline_fallback_keeps_full_cap_distinct_from_near_empty_snap(engram_home):
    """Companion to the near-empty-snap test above (gh#2056): the true
    no-newline-at-all fallback keeps the FULL cap's worth of raw, ragged
    tail -- not a single character. Pinning both exact kept-lengths (1 vs.
    `cap`) in the same file means the two branches cannot be silently
    collapsed into each other without one of these two tests failing."""
    history_dir = engram_home / "history"
    mod_probe = _load_hook_module(engram_home)
    cap = mod_probe.HISTORY_ARC_CHAR_CAP

    single_line = "NO-NEWLINE-AT-ALL-" + ("z" * (cap + 500))
    stripped = single_line.strip()
    assert len(stripped) > cap
    start_idx = len(stripped) - cap
    assert "\n" not in stripped[start_idx:], "fixture must contain no newline in the capped tail"

    _write_history_file(history_dir, "2026-08-15.md", single_line)
    mod = _load_hook_module(engram_home)
    text = mod.history_arc_block(str(engram_home))

    _header_line, elision_line, kept_content = _split_block(text)
    # Verified by direct execution (see handoff transcript): kept length ==
    # the full cap (32000), not 1 -- the raw tail is kept whole because
    # there is no newline to advance past.
    assert len(kept_content) == cap, len(kept_content)
    m = re.search(r"\+(\d+) earlier chars elided", elision_line)
    assert m, elision_line
    assert int(m.group(1)) == len(stripped) - cap, (m.group(1), len(stripped), cap)
    # Sanity: the two branches produce provably different kept lengths from
    # comparably-sized over-cap fixtures (1 vs. cap), which is the whole
    # point of pairing these tests (gh#2056).
    assert cap != 1
