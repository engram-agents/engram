"""#3775 item 2 (part of #2652): the unsupervised launch's loud line names the owner's own refusal.

Contract-first: these assertions were written before the implementation. The design-seat ruling
on #3775 is: no in-hook retry; when the detached launch dies, read the owner's OWN refusal line
from the bytes THIS launch appended to engramd-owner.log and put it in the loud line; never infer
a reason from an exit code the hook cannot see; with nothing appended, keep today's wording.

  C1  a launch that dies after appending a refusal -> the loud line carries the owner's text
  C2  nothing appended / nothing matching `[engramd] ` (other than `listening on`) -> today's line
  C3  refusal lines left by an EARLIER launch are never reported (only bytes at/after the launch offset)
  C4  bash never opens the log (static scan); a non-regular log (`nolog`) -> no read, today's line
  C5  the read is bounded (byte cap, clipped output) and never blocks (FIFO swapped in after launch)
  C6  the wrapper must not claim a supervisor will retry on this unsupervised path, and that
      correction must survive report()'s 600-char middle clip: with a long quote plus a long
      ENGRAM_HOME, and on the no-GUI-domain path with several flags ON (a long fixed preamble)
  C7  the wrapper must not call whatever `[engramd] ` line it quotes a "refusal": the filter takes
      the last non-`listening on` owner line, which can be a shutdown notice

Harness: `tests/test_owner_launcher_3663.py`'s `Seat` (hermetic PATH, HOME in a tmp dir), reused.
The owner is the Seat's python stub; `seat.pre_script` is sourced by it and plays the owner's
dying words. Fixture paths are /Users/seat style.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_owner_launchd_3709 import DSeat  # noqa: E402  (the Darwin seat with a fake launchctl)
from test_owner_launcher_3663 import (  # noqa: E402  (tests/ is on sys.path, as every sibling assumes)
    FLAG_ON,
    Seat,
    _physical,
    context_lines,
)

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
HOOK = SRC / "hooks" / "claude" / "start-engramd-owner.sh"
HOOK_IO = SRC / "hooks" / "claude" / "_engramd_owner_hook_io.py"

# The real wording of the behind-schema refusal (engramd_owner.py, OwnerStoreBehindSchema).
BEHIND = ("no knowledge.db at /Users/seat/.engram/knowledge.db: the store is behind the schema this "
          "owner needs; run the migration (any ENGRAM session start does) and the supervisor will retry")
BEHIND_LINE = f"[engramd] {BEHIND}"
# A refusal without the owner's own supervisor clause, for C6's "the WRAPPER does not say it".
PLAIN = "store behind schema at /Users/seat/.engram/knowledge.db"

EXITED_LEAD = "ENGRAM owner could not be started: the process exited within a second."


def _exited_line(seat: Seat) -> str:
    """Today's wording, byte for byte (pinned in test_owner_serving_probe_3736.py too)."""
    h = _physical(seat.eh)
    return f"{EXITED_LEAD} ENGRAM_HOME={h}. Log: {h}/engramd-owner.log"


def _sh(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _dying_words(seat: Seat, *lines: str, code: int = 5) -> None:
    """The stub owner writes these to stderr (-> the log) and exits."""
    body = "".join(f"printf '%s\\n' {_sh(ln)} >&2\n" for ln in lines) + f"exit {code}\n"
    seat.pre_script.write_text(body, encoding="utf-8")


def _run(seat: Seat) -> str:
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    r = seat.run()
    assert r.returncode == 0, r.stderr[-300:]
    lines = context_lines(r)
    assert len(lines) == 1, r.stdout
    return lines[0]


def _helper_calls(seat: Seat) -> list[str]:
    return seat.helper_calls.read_text(encoding="utf-8").splitlines() if seat.helper_calls.exists() else []


def _refusal_since(home: Path, offset, timeout: float = 10.0) -> tuple[str, float]:
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, str(HOOK_IO), "refusal_since", str(home), str(offset)],
                       capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
                       cwd=str(home))
    assert r.returncode == 0, r.stderr[-300:]
    return r.stdout.strip(), time.monotonic() - t0


@pytest.fixture
def seat(tmp_path):
    return Seat(tmp_path)


# ---- C1 ----------------------------------------------------------------------------

def test_c1_the_loud_line_carries_the_owners_last_line(seat):
    _dying_words(seat, BEHIND_LINE)
    line = _run(seat)
    assert line.startswith("ENGRAM owner could not be started: "), line
    assert "exited within a second" in line, line
    # The correction now leads (C6), so a long quote can lose its TAIL to report()'s middle clip; the
    # full text is in the log (asserted below). The head of the owner's text is what the line must carry.
    assert BEHIND[:90] in line, line
    assert "[engramd] " not in line, "the owner's log prefix must be stripped"
    assert "Its last line: " in line and "engramd-owner.log" in line, line
    assert any(BEHIND in ln for ln in seat.log_lines() if "] [start-engramd-owner]" in ln), \
        "the full line must reach the log too"


def test_c1_the_owners_full_stop_is_not_doubled(seat):
    """The wrapper ends the quoted sentence itself, so the owner's own trailing full stop is dropped."""
    _dying_words(seat, "[engramd] refused.")
    line = _run(seat)
    assert "Its last line: refused. ENGRAM_HOME" in line, line
    assert "refused.." not in line, line


def test_c1_the_last_refusal_line_wins(seat):
    _dying_words(seat, "[engramd] first thing", "some traceback noise", "[engramd] the real reason")
    line = _run(seat)
    assert "the real reason" in line and "first thing" not in line, line


# ---- C2 ----------------------------------------------------------------------------

@pytest.mark.parametrize("words", [
    [],
    ["boom: not an engramd line"],
    ["[engramd] listening on /Users/seat/.engram/run/engramd.sock (pid 4242)"],
    ["  [engramd] indented, so not an owner line"],
    ["x [engramd] mid-line, so not an owner line"],
], ids=["silent", "other-text", "listening-on", "indented", "mid-line"])
def test_c2_nothing_to_quote_keeps_todays_wording(seat, words):
    _dying_words(seat, *words)
    assert _run(seat) == _exited_line(seat)


# ---- C3 ----------------------------------------------------------------------------

def test_c3_an_earlier_launchs_refusal_is_never_reported(seat):
    seat.log.write_text(f"[engramd] OLD refusal from an earlier launch: {PLAIN}\n", encoding="utf-8")
    _dying_words(seat)  # this launch dies appending nothing matching
    assert _run(seat) == _exited_line(seat)


def test_c3_this_launchs_refusal_wins_over_an_older_one(seat):
    seat.log.write_text("[engramd] OLD refusal from an earlier launch\n", encoding="utf-8")
    _dying_words(seat, "[engramd] NEW refusal from this launch")
    line = _run(seat)
    assert "NEW refusal from this launch" in line and "OLD refusal" not in line, line


def test_c3_helper_counts_only_bytes_at_or_after_the_offset(tmp_path):
    log = tmp_path / "engramd-owner.log"
    old = "[engramd] old reason\n"
    log.write_text(old + "[engramd] new reason\n", encoding="utf-8")
    assert _refusal_since(tmp_path, len(old.encode()))[0] == "new reason"
    assert _refusal_since(tmp_path, log.stat().st_size)[0] == ""
    assert _refusal_since(tmp_path, log.stat().st_size + 1000)[0] == ""
    assert _refusal_since(tmp_path, 0)[0] == "new reason"  # the LAST matching line
    for junk in ("-1", "abc", ""):
        assert _refusal_since(tmp_path, junk)[0] == "", junk


# ---- C4 ----------------------------------------------------------------------------

_ACCESS_CMDS = r"(?:cat|tail|head|grep|egrep|fgrep|sed|awk|tac|wc|stat|read|mapfile|readarray|less|more|od|xxd|strings|cut|sort)"


def test_c4_bash_never_opens_the_log():
    """Static scan: LOG_PATH / $ENGRAM_HOME/... appear in the hook only as words inside messages --
    never next to a redirection operator or a file-reading command (every open goes through the helper)."""
    offenders = []
    for n, raw in enumerate(HOOK.read_text(encoding="utf-8").splitlines(), 1):
        if raw.lstrip().startswith("#"):
            continue
        if not re.search(r'LOG_PATH|\$\{?ENGRAM_HOME\}?/', raw):
            continue
        if re.search(r'[<>]\s*"?\$\{?(?:LOG_PATH|ENGRAM_HOME)', raw) or \
           re.search(rf'\b{_ACCESS_CMDS}\b[^|;&]*"?\$\{{?(?:LOG_PATH|ENGRAM_HOME)', raw):
            offenders.append((n, raw.strip()))
    assert not offenders, offenders


def test_c4_non_regular_log_means_no_read_and_todays_wording(seat):
    os.mkfifo(seat.log)  # a reader-less FIFO: the launch's append-open fails -> `nolog`
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    _dying_words(seat, BEHIND_LINE)
    rc, out, err, _ = seat.run_timed(limit=15.0)
    assert rc == 0, err[-300:]
    lines = context_lines(subprocess.CompletedProcess([], rc, out, err))
    assert len(lines) == 1, out
    h = _physical(seat.eh)
    assert lines[0] == (f"{EXITED_LEAD} ENGRAM_HOME={h}. "
                        "The log is unavailable (not a regular file): owner output discarded"), lines[0]
    assert not any(" refusal_since " in c for c in _helper_calls(seat)), \
        "a nolog launch must not attempt the read"


# ---- C5 ----------------------------------------------------------------------------

def _cap() -> int:
    m = re.search(r"^REFUSAL_READ_CAP_BYTES\s*=\s*([0-9_ *]+)", HOOK_IO.read_text(encoding="utf-8"), re.M)
    assert m, "the read cap must be a named constant (REFUSAL_READ_CAP_BYTES) in the helper"
    return int(eval(m.group(1).strip(), {}, {}))  # a literal like `64 * 1024`


def test_c5_the_cap_is_a_named_constant_and_modest():
    assert 1024 <= _cap() <= 64 * 1024


def test_c5_a_huge_refusal_line_is_clipped(tmp_path):
    (tmp_path / "engramd-owner.log").write_text("[engramd] " + "x" * 1_000_000 + "\n", encoding="utf-8")
    out, _ = _refusal_since(tmp_path, 0)
    assert out.startswith("xxxx") and 0 < len(out) <= 500, len(out)


def test_c5_bytes_past_the_cap_are_not_read(tmp_path):
    cap = _cap()
    (tmp_path / "engramd-owner.log").write_text("n" * (cap + 10) + "\n[engramd] late\n", encoding="utf-8")
    assert _refusal_since(tmp_path, 0)[0] == "", "the read must stop at the cap"


def test_c5_control_bytes_and_bad_utf8_stay_one_clean_line(tmp_path):
    (tmp_path / "engramd-owner.log").write_bytes(b"[engramd] bad \xff\xfe byte\ttab\x1b[31mred\x00z\n")
    out, _ = _refusal_since(tmp_path, 0)
    assert out and "\n" not in out and "\x1b" not in out and "\x00" not in out and "\t" not in out, repr(out)


def test_c5_fifo_in_place_of_the_log_never_blocks(tmp_path):
    os.mkfifo(tmp_path / "engramd-owner.log")
    out, elapsed = _refusal_since(tmp_path, 0, timeout=8.0)
    assert out == "" and elapsed < 5.0, (out, elapsed)


def test_c5_missing_log_and_directory_log_are_silent(tmp_path):
    assert _refusal_since(tmp_path, 0)[0] == ""
    (tmp_path / "engramd-owner.log").mkdir()
    assert _refusal_since(tmp_path, 0)[0] == ""


def test_c5_fifo_swapped_in_after_launch_does_not_hang_the_hook(seat):
    """End to end: the owner appends its refusal, then the log is replaced by a FIFO before the hook reads."""
    swap = (f"rm -f '{seat.log}'; '{sys.executable}' -c "
            f"\"import os; os.mkfifo({str(seat.log)!r})\"\n")
    seat.pre_script.write_text(f"printf '%s\\n' {_sh(BEHIND_LINE)} >&2\n{swap}exit 5\n", encoding="utf-8")
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    rc, out, err, _ = seat.run_timed(limit=15.0)
    assert rc == 0, err[-300:]
    lines = context_lines(subprocess.CompletedProcess([], rc, out, err))
    assert len(lines) == 1 and lines[0] == _exited_line(seat), lines


# ---- C6 ----------------------------------------------------------------------------

def test_c6_the_wrapper_does_not_promise_a_retry(seat):
    _dying_words(seat, f"[engramd] {PLAIN}")
    line = _run(seat)
    assert PLAIN in line, line
    wrapper = line.replace(PLAIN, "")
    assert "supervisor will retry" not in wrapper.replace("No supervisor will retry", ""), wrapper
    assert "No supervisor will retry here" in line, line
    assert "new session" in line, line


LONG_REFUSAL = ("store behind schema at /Users/seat/.engram/knowledge.db " + "because-of-a-long-reason " * 20
                + "and the supervisor will retry")


def test_c6_the_correction_survives_the_clip_with_a_long_quote_and_a_long_home(tmp_path):
    """Varies BOTH the quote (LONG_REFUSAL, ~400 chars after the clip: this is what drives the line over
    the 600-char limit) and the home (~190 chars). The plain path puts ENGRAM_HOME after the quote, so
    the home alone cannot clip the correction; the long quote is what makes the cell discriminating."""
    deep = tmp_path / "/".join(["d" * 60] * 3)
    deep.mkdir(parents=True)
    seat = Seat(deep)
    assert len(str(_physical(seat.eh))) >= 190, str(seat.eh)
    _dying_words(seat, f"[engramd] {LONG_REFUSAL}")
    r = None
    seat.config(FLAG_ON)
    seat.systemctl("nobus")
    r = seat.run()
    assert r.returncode == 0, r.stderr[-300:]
    lines = context_lines(r)
    assert len(lines) == 1, r.stdout
    line = lines[0]
    assert "No supervisor will retry here" in line, line
    assert "new session" in line, line
    assert len(line) <= 700, len(line)  # within the clip (600) plus the clip marker
    assert "\n" not in line


# Short enough for the helper's 400-char quote clip to keep the owner's own closing clause.
SHORT_RETRY_REFUSAL = ("store behind schema at /Users/seat/.engram/knowledge.db; run the migration "
                       "and the supervisor will retry")
MANY_FLAGS = {k: True for k in ("session_start_reads_via_owner", "stop_reads_via_owner",
                                "surface_reads_via_owner", "user_identity_reads_via_owner")}


def test_c6_the_correction_survives_the_clip_on_the_no_gui_path_with_many_flags(tmp_path):
    """macOS with no launchd GUI domain: the `OWNER REFUSED` preamble plus the `Flags ON:` list is long,
    and with 3+ flags it used to push the correction (inside `Owner launch failed: ...`) into the clipped
    middle while the owner's own 'the supervisor will retry' survived in the tail."""
    dseat = DSeat(tmp_path / "seat")
    _dying_words(dseat, f"[engramd] {SHORT_RETRY_REFUSAL}")
    dseat.config(MANY_FLAGS)
    dseat.launchctl("no_gui_domain")
    rc, out, err, _ = dseat.run_timed(limit=20.0)
    assert rc == 0, err[-300:]
    lines = context_lines(subprocess.CompletedProcess([], rc, out, err))
    assert len(lines) == 1, out
    line = lines[0]
    assert line.startswith("OWNER REFUSED"), line
    assert "no supervisor will retry" in line.lower(), line
    # The wrapper's own later sentence also says it, so pin that the no-GUI HEAD carries it.
    assert "no supervisor will retry" in line[:80].lower(), line[:80]
    assert "supervisor will retry" in line, "the owner's own clause is kept: the hazard being guarded"
    assert len(line) <= 700, len(line)  # the clip really applied (600 plus the marker)


# ---- C7 ----------------------------------------------------------------------------

def test_c7_a_non_refusal_owner_line_is_not_called_a_refusal(seat):
    note = "substrate displaced detected at shutdown"
    _dying_words(seat, f"[engramd] {note}")
    line = _run(seat)
    assert note in line, line
    wording = line.split("ENGRAM_HOME=")[0]  # the tmp path after it carries the test's own name
    assert "refus" not in wording.lower(), wording
    assert "own words" not in wording, wording
    assert "Its last line: " + note in wording, wording
