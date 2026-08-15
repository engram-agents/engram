"""Tests for engram_nap appending a milestone line to today's history file
(engram-alpha#2026 WS3).

Coverage (per the WS3 build spec):
  1. Calling nap (mode="nap") with a real message appends exactly one new
     bullet line (`- <message>`) to today's ~/.engram/history/YYYY-MM-DD.md
     under the test's isolated ENGRAM_HOME/DATA_DIR.
  2. If the file doesn't exist yet, it's created with the `# YYYY-MM-DD`
     header, then the bullet. (Covered by the same test as #1 — a fresh
     TEST_DIR has no history file until the first nap call.)
  3. If the file already has content (a hand-written entry already present),
     the new bullet is appended AFTER it, existing content byte-preserved
     (not clobbered, not reordered).
  4. Calling advance_turn (the sleep/turn-advancing mode) does NOT touch the
     history file at all — asserted via file non-existence after a
     sleep-mode call on a fresh TEST_DIR.
  5. Fail-soft: if the history file's directory is made unwritable,
     engram_nap itself still returns its normal success result — the
     checkpoint must not fail because the history write failed.
  6. Atomicity: simulate a failure mid-write (os.replace raising) and assert
     (a) no .tmp file is left behind, (b) the original file's content is
     unchanged/intact.
  7. Two consecutive nap calls in the same test (same day) append TWO
     separate bullets, in order, neither overwriting the other.

No existing test file covered `_checkpoint_internal`/`engram_nap`'s history-
write side effect before this PR (searched tests/ for `_nap_impl` and
`_checkpoint_internal`; the two payload_json tests found —
test_nap_payload.py, test_advance_turn_payload.py — cover payload validation
and the checkpoint's own return-key shape only, not the history file).
"""

import atexit
import datetime
import json
import os
import shutil
import sys
import tempfile
import zoneinfo
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_nap_history_write_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _today_str():
    # Must match _append_nap_to_history's own day-boundary exactly
    # (America/New_York, not UTC -- see that function's docstring clause
    # (e) for why UTC silently picks the wrong file on ~76% of evenings).
    return datetime.datetime.now(
        zoneinfo.ZoneInfo("America/New_York")
    ).strftime("%Y-%m-%d")


def _history_path():
    import engram_core as core
    return core.DATA_DIR / "history" / f"{_today_str()}.md"


# ---------------------------------------------------------------------------
# 1 + 2. Fresh file: creates header, appends the one bullet
# ---------------------------------------------------------------------------

def test_nap_appends_bullet_creates_file_with_header():
    server = fresh_server()
    msg = "Test milestone: WS3 nap-history-write coverage (ob_9999)."
    r = json.loads(server._nap_impl(message=msg))
    assert "error" not in r, f"_nap_impl returned error: {r}"

    path = _history_path()
    assert path.exists(), f"history file was not created: {path}"
    content = path.read_text(encoding="utf-8")
    assert content.startswith(f"# {_today_str()}\n"), (
        f"missing/incorrect header: {content!r}"
    )
    assert f"- [auto-nap] {msg}\n" in content, f"bullet missing from content: {content!r}"
    bullet_lines = [l for l in content.splitlines() if l.startswith("- ")]
    assert len(bullet_lines) == 1, f"expected exactly one bullet, got: {bullet_lines}"


# ---------------------------------------------------------------------------
# 3. Existing hand-written content preserved; new bullet appended AFTER it
# ---------------------------------------------------------------------------

def test_nap_appends_after_existing_handwritten_content():
    server = fresh_server()
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handwritten = f"# {_today_str()}\n\n- Hand-written milestone already here.\n"
    path.write_text(handwritten, encoding="utf-8")

    msg = "Nap-appended milestone landing after hand-written content."
    r = json.loads(server._nap_impl(message=msg))
    assert "error" not in r, f"_nap_impl returned error: {r}"

    content = path.read_text(encoding="utf-8")
    assert content.startswith(handwritten.rstrip("\n")), (
        f"existing hand-written content was clobbered or reordered: {content!r}"
    )
    assert "Hand-written milestone already here." in content
    assert content.index("Hand-written milestone") < content.index(msg), (
        f"new bullet was not appended AFTER existing content: {content!r}"
    )


# ---------------------------------------------------------------------------
# 4. advance_turn (sleep/turn-advancing mode) does not touch the history file
# ---------------------------------------------------------------------------

def test_advance_turn_does_not_touch_history_file():
    server = fresh_server()
    path = _history_path()
    assert not path.exists(), "history file unexpectedly pre-exists on a fresh TEST_DIR"

    r = json.loads(server._advance_turn_impl(
        message="Sleep-mode checkpoint; must not write the history file."
    ))
    assert "error" not in r, f"_advance_turn_impl returned error: {r}"
    assert r.get("turn_advanced") is True, f"advance_turn did not advance the turn: {r}"
    assert not path.exists(), (
        f"advance_turn (sleep/turn-advancing mode) created the history file, "
        f"which is out of scope for WS3: {path}"
    )


# ---------------------------------------------------------------------------
# 5. Fail-soft: unwritable history directory does not fail engram_nap
# ---------------------------------------------------------------------------

def test_nap_fail_soft_when_history_dir_unwritable():
    server = fresh_server()
    import engram_core as core
    history_dir = core.DATA_DIR / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(history_dir, 0o500)  # read + execute only, no write
    try:
        r = json.loads(server._nap_impl(
            message="Fail-soft check: history dir made unwritable."
        ))
    finally:
        os.chmod(history_dir, 0o700)  # restore so TEST_DIR cleanup can proceed

    assert "error" not in r, (
        f"_nap_impl failed when the history dir was unwritable: {r}"
    )
    assert r.get("status") == "checkpoint_saved", (
        f"nap checkpoint did not complete normally despite fail-soft contract: {r}"
    )


# ---------------------------------------------------------------------------
# 6. Atomicity: a failure mid-write leaves no .tmp file and preserves
#    the original content untouched.
# ---------------------------------------------------------------------------

def test_history_write_atomic_on_mid_write_failure():
    server = fresh_server()
    import engram_core as core
    history_dir = core.DATA_DIR / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    today = _today_str()
    path = history_dir / f"{today}.md"
    original = f"# {today}\n\n- Pre-existing content that must survive.\n"
    path.write_text(original, encoding="utf-8")

    # engram_snapshot_worker.write_durable_snapshot ALSO calls os.replace
    # within the same _nap_impl call (reviewer-fairy round caught the
    # original comment here claiming otherwise -- it was wrong). Scope the
    # failure to only the history file's own .tmp path so this test
    # exercises _append_nap_to_history specifically, not the snapshot
    # worker's independent (and separately-tested) failure handling.
    real_replace = os.replace

    def _replace_or_fail(src, dst, *a, **kw):
        if str(dst).endswith(".md"):
            raise OSError("simulated mid-write failure")
        return real_replace(src, dst, *a, **kw)

    with mock.patch("os.replace", side_effect=_replace_or_fail):
        r = json.loads(server._nap_impl(
            message="This bullet must not land; os.replace fails mid-write."
        ))

    assert "error" not in r, f"_nap_impl failed when os.replace raised: {r}"

    tmp_path = str(path) + ".tmp"
    assert not os.path.exists(tmp_path), (
        f"leftover .tmp file after a failed mid-write: {tmp_path}"
    )
    content = path.read_text(encoding="utf-8")
    assert content == original, (
        f"original content was not preserved after a failed mid-write: {content!r}"
    )
    hw = r.get("history_write")
    assert hw is not None and hw.get("written") is False, (
        f"a failed write must be observable via result['history_write'], not silent: {r}"
    )
    assert hw.get("reason"), f"a failed write must carry a non-empty reason: {hw}"


# ---------------------------------------------------------------------------
# 8. Observability: a successful write reports written=True, reason=None
# ---------------------------------------------------------------------------

def test_history_write_success_is_observable_in_result():
    server = fresh_server()
    r = json.loads(server._nap_impl(message="Observability check: a normal nap."))
    assert "error" not in r, f"_nap_impl returned error: {r}"
    hw = r.get("history_write")
    assert hw == {"written": True, "reason": None}, (
        f"a successful write must report written=True, reason=None: {hw}"
    )


# ---------------------------------------------------------------------------
# 9. Newline handling: an embedded \n in message doesn't break the
#    one-bullet-per-nap invariant
# ---------------------------------------------------------------------------

def test_message_with_embedded_newline_stays_one_bullet():
    server = fresh_server()
    msg = "First line of a multi-line message.\nSecond line, should not break the bullet."
    r = json.loads(server._nap_impl(message=msg))
    assert "error" not in r, f"_nap_impl failed on a multi-line message: {r}"

    content = _history_path().read_text(encoding="utf-8")
    bullet_lines = [l for l in content.splitlines() if l.startswith("- ")]
    assert len(bullet_lines) == 1, (
        f"a multi-line message must still produce exactly one bullet, got: {bullet_lines}"
    )
    assert bullet_lines[0] == "- [auto-nap] First line of a multi-line message. Second line, should not break the bullet.", (
        f"embedded newline should collapse to a single space, got: {bullet_lines[0]!r}"
    )


# ---------------------------------------------------------------------------
# 7. Two consecutive nap calls append two separate bullets, in order
# ---------------------------------------------------------------------------

def test_two_consecutive_naps_append_two_separate_bullets():
    server = fresh_server()
    msg1 = "First nap milestone of this burst."
    msg2 = "Second nap milestone of this burst."

    r1 = json.loads(server._nap_impl(message=msg1))
    assert "error" not in r1, f"first _nap_impl call returned error: {r1}"
    r2 = json.loads(server._nap_impl(message=msg2))
    assert "error" not in r2, f"second _nap_impl call returned error: {r2}"

    path = _history_path()
    content = path.read_text(encoding="utf-8")
    bullet_lines = [l for l in content.splitlines() if l.startswith("- ")]
    assert bullet_lines == [f"- [auto-nap] {msg1}", f"- [auto-nap] {msg2}"], (
        f"expected two ordered, non-overwritten bullets, got: {bullet_lines}"
    )
