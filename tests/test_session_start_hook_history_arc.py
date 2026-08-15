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
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"


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
    assert f"+{overflow} more chars" in text
    assert str(path) in text  # no sidecar -> points at the file's own path


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

    # Seed checkpoint_at via a first SessionStart (source=startup, so the
    # history-arc block itself doesn't fire this call, matching real usage).
    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            _run_hook_main(mod, {
                "session_id": session_id, "transcript_path": "/tmp/t.jsonl", "source": "startup",
            })
    with open(engram_home / "sessions" / f"{session_id}.json") as f:
        seeded = json.load(f)["checkpoint_at"]

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
