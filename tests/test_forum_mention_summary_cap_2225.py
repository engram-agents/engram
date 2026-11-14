"""#2225: the forum mention monitor's coalesced summary lists at most 10 threads, then ", +K more".

Drives the REAL flush_buffer python block, extracted from the shipped script (same
extraction style as test_forum_field_brakcet_4474.py), with one thread id per
buffered mention on stdin.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "src" / "engram" / "tools" / "forum-mention-monitor.sh"
EMPTY = "⟦ENGRAM⟧⟦/ENGRAM⟧"
HEAD = "\U0001f4e3 "


def _program() -> str:
    s = _SCRIPT.read_text()
    a = s.index("_FORUM_FIELD_PY='") + len("_FORUM_FIELD_PY='")
    field = s[a:s.index("\n'\n", a) + 1].replace("'\"'\"'", "'")
    f = s.index("flush_buffer() {")
    b = s.index("python3 -c \"$_FORUM_FIELD_PY\"'", f) + len("python3 -c \"$_FORUM_FIELD_PY\"'")
    flush = s[b:s.index("' < \"$BUF\"", b)]
    return field + flush


def _summary(thread_ids) -> str:
    r = subprocess.run([sys.executable, "-c", _program()], input="".join(f"{t}\n" for t in thread_ids),
                       capture_output=True, text=True, encoding="utf-8",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert out.endswith("\n")
    return out[:-1]


def _distinct(n, start=100):
    return list(range(start, start + n))


def test_exactly_ten_threads_all_listed_no_more():
    ids = _distinct(10)
    line = _summary(ids)
    exp = ", ".join(f"#{t}(×1)" for t in ids)
    assert line == f"{HEAD}10 new @-mentions across 10 threads: {exp} {EMPTY}"
    assert "more" not in line


def test_eleven_threads_ten_listed_plus_one_more():
    ids = _distinct(11)
    line = _summary(ids)
    exp = ", ".join(f"#{t}(×1)" for t in ids[:10])
    assert line == f"{HEAD}11 new @-mentions across 11 threads: {exp}, +1 more {EMPTY}"
    assert f"#{ids[10]}(" not in line


def test_twelve_threads_plus_two_more():
    line = _summary(_distinct(12))
    assert ", +2 more " in line


def test_119_threads_measured_case():
    ids = _distinct(119, start=1000)
    line = _summary(ids)
    assert line.startswith(f"{HEAD}119 new @-mentions across 119 threads: ")
    assert line.count("(×") == 10
    assert ", +109 more " in line
    assert len(line) < 250


def test_first_seen_order_kept():
    seq = [50, 7, 50, 90, 7, 3, 60, 61, 62, 63, 64, 65, 66]  # 11 distinct, 13 mentions
    line = _summary(seq)
    order = [int(m) for m in re.findall(r"#(\d+)\(", line)]
    assert order == [50, 7, 90, 3, 60, 61, 62, 63, 64, 65]
    assert ", +1 more " in line


def test_marker_is_last_and_more_precedes_it():
    line = _summary(_distinct(15))
    assert line.endswith(", +5 more " + EMPTY)
    assert line.count(EMPTY) == 1


def test_repeated_thread_keeps_its_count_and_header_counts_all_mentions():
    seq = [5, 5, 5, 6] + _distinct(12, start=200)  # 14 distinct, 16 mentions
    line = _summary(seq)
    assert line.startswith(f"{HEAD}16 new @-mentions across 14 threads: #5(×3), #6(×1), #200(×1)")
    assert ", +4 more " in line
