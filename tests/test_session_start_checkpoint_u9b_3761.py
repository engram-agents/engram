"""U9b (#3761, part of #2652): `session_checkpoint_block` fetches only what it renders.

Contract (the merge seat's ruling on #3761, plus the corrections from reading the code):

  C1  TWO statements replace the one: a COUNT(*) over `created_at >= ?` with the exclusion
      predicate in SQL, and a list query with the same predicate, `substr(trim(claim, WS), 1, 100)`
      in place of `claim`, ordered `created_at ASC, rowid ASC` and limited to
      CHECKPOINT_LIST_CAP. No full claim text crosses the owner socket or the direct connection.
  C2  OUTPUT BYTE-IDENTICAL to the old logic on every window: empty, all-excluded, under the
      cap, exactly the cap, cap + 1, over the cap, blank-text rows, recall_summary-only rows,
      claim-only rows, non-current rows, multibyte claims, whitespace edge cases, ties.
  C3  The exclusion and the trim are PYTHON `str.strip` semantics: `strip()` removes every
      character whose `isspace()` is true, SQLite's one-argument `trim` removes only U+0020, so the
      SQL embeds the explicit whitespace set. `substr` counts CHARACTERS as `[:100]` does, and
      the Python side must not strip or slice the already-trimmed, already-sliced text again.
  C4  Ties on `created_at` render in a deterministic (rowid) order, the same order the old
      full fetch produced.
  C5  The two statements are not atomic: total = max(count, len(list)); remaining =
      max(0, total - len(shown)) -- overlap, never a gap.
  C6  Both statements are registered owner templates (`engramd_read_templates.json`), the old
      one retired; every statement sent is a registered one, byte for byte.
  C7  BYTES ON THE WIRE: the owner's replies for this reader are bounded by
      CAP x 220 B + 1 KiB regardless of window size, MEASURED from the bytes the client reads.

The "reference" below is a copy of the pre-U9b function body (origin/dev 511195b3). The
differential cells compare the new function to it on the same store, flag OFF (direct
connection) and flag ON (fake owner, and the real owner where this host can run it).

Mutation targets (each run against the implementation, recorded in the PR):
  M1 drop the exclusion predicate from the COUNT only      -> header-count / `+N more` cells red
  M2 substr by BYTES (or a wrong length)                   -> the boundary cells red
  M3 drop the rowid tiebreak                               -> the tie cells red where SQLite's plan
                                                              makes the order observable
  M4 re-strip the already-trimmed text in Python           -> the post-slice-strip cell red
  M5 one-argument trim (space only)                        -> the whitespace cells red
"""
from __future__ import annotations

import json
import re
import socket
import sqlite3
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from _fake_owner import FakeOwner, registered_sql
from test_session_start_owner_read_3735 import (  # noqa: F401 -- fixtures and helpers reused
    CHECKPOINT_FROM,
    HOOK_REL,
    OWNER_KINDS,
    Fire,
    arm_failure,
    build_empty_store,
    home,
    load_ss,
    owner_of,
    set_flag,
    sql_set_of_function,
    stub_modules,
)

import engramd_client as ec  # noqa: E402
import engramd_read_registry as rr  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FN = "session_checkpoint_block"
WINDOW_FROM = CHECKPOINT_FROM  # 2026-10-12T00:00:00Z
CAP = 200

# ---------------------------------------------------------------------------------------
# The OLD logic, verbatim from origin/dev 511195b3 (the reference implementation).
# ---------------------------------------------------------------------------------------
OLD_SQL = ("SELECT id, type, claim, recall_summary, created_at, is_current, status FROM nodes "
           "WHERE created_at >= ? ORDER BY created_at ASC")


def old_block(mod, home: Path, injected_through: str) -> str:
    if not injected_through:
        return ""
    conn = sqlite3.connect(str(home / "knowledge.db"))
    try:
        rows = conn.execute(OLD_SQL, (injected_through,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    entries = []
    for row in rows:
        claim = (row[2] or "").strip()
        recall_summary = (row[3] or "").strip()
        if not (claim or recall_summary):
            continue
        text = recall_summary if recall_summary else claim[:100]
        entries.append((row[0], row[1], text, mod._liveness_tag_values(row[5], row[6])))
    if not entries:
        return ""
    total = len(entries)
    display_entries = entries[:mod.CHECKPOINT_LIST_CAP]
    remaining = total - len(display_entries)
    header = f"[Nodes created since checkpoint ({injected_through}) — {total} node(s)]:"
    block_lines = [header]
    for node_id, node_type, text, liveness in display_entries:
        block_lines.append(f"  - [{node_id}] ({node_type}){liveness} {text}")
    if remaining > 0:
        block_lines.append(f"  ... +{remaining} more")
    return "\n".join(block_lines)


# ---------------------------------------------------------------------------------------
# Store building
# ---------------------------------------------------------------------------------------
_BASE = datetime(2026, 10, 12, 1, 0, 0)
_BEFORE = "2026-10-11T23:59:59Z"  # strictly before the window


def ts(i: int) -> str:
    return (_BASE + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ins(conn, nid, claim, rs=None, created=None, current=1, status="active", typ="observation"):
    conn.execute(
        "INSERT INTO nodes (id, type, claim, recall_summary, created_at, is_current, status, metadata) "
        "VALUES (?,?,?,?,?,?,?,'{}')",
        (nid, typ, claim, rs, created, current, status))


def populate(conn, shape: str) -> None:
    """Three rows BEFORE the window in every shape (the filter is exercised), then the shape."""
    for k in range(3):
        ins(conn, f"pre{k}", f"before the window {k}", created=_BEFORE)
    n = lambda i, claim=None, rs=None, **kw: ins(  # noqa: E731
        conn, f"n{i:05d}", f"claim number {i}" if claim is None else claim, rs, ts(i), **kw)
    if shape == "empty":
        return
    if shape == "all_excluded":
        for i, (c, r) in enumerate([("", None), (None, None), (" \n", "  "), ("\u3000", "\t"), (None, "\u2003")]):
            n(i, c if c is not None else None, r)
            conn.execute("UPDATE nodes SET claim = ? WHERE id = ?", (c, f"n{i:05d}"))
        return
    if shape == "under_cap":
        for i in range(5):
            n(i)
    elif shape in ("exactly_cap", "cap_plus_1", "cap_plus_many"):
        for i in range({"exactly_cap": CAP, "cap_plus_1": CAP + 1, "cap_plus_many": 450}[shape]):
            n(i)
    elif shape == "over_cap_with_blanks":
        # every 7th row is blank and excluded: the COUNT must exclude them too
        for i in range(450):
            if i % 7 == 0:
                n(i, claim="", rs=None)
            else:
                n(i)
    elif shape == "many_summary_only":
        # 300 rows over the cap, 120 of them summary-only (claim NULL or blank, recall_summary
        # non-blank): every one is COUNTED. A COUNT whose predicate ignores the recall_summary arm
        # would drop all 120 from the header total and from the `+N more` tail.
        for i in range(300):
            if i % 5 in (0, 1):  # 120 of 300 are summary-only
                n(i, claim="", rs=f"summary only {i}")
                if i % 2:
                    conn.execute("UPDATE nodes SET claim = NULL WHERE id = ?", (f"n{i:05d}",))
            else:
                n(i)
    elif shape == "blanks_inside_first_cap":
        for i in range(260):
            if i % 5 == 0:
                n(i, claim=" \t ", rs=" ")
            else:
                n(i)
    elif shape == "rs_only":
        for i in range(6):
            n(i, claim="", rs=f"summary only {i}")
    elif shape == "claim_only":
        for i in range(6):
            n(i, rs=None)
    elif shape == "rs_blank_claim_present":
        for i in range(4):
            n(i, rs="   ")
    elif shape == "both_present_rs_wins":
        for i in range(4):
            n(i, rs=f"the summary {i}  ")
    elif shape == "non_current":
        n(0)
        n(1, current=0)
        n(2, current=0, status="retracted")
        n(3, current=0, status=" Retracted ")
        n(4, current=0, status=None)
        n(5, current=1, status="retracted")
    elif shape == "multibyte":
        n(0, claim="a" * 99 + "\U0001F600" + "tail")            # the 100th char is a 4-byte emoji
        n(1, claim="a" * 98 + "é" + "x" + "tail")               # 2-byte char at 99
        n(2, claim="汉" * 150)                                    # CJK, 3 bytes each
        n(3, claim="b" * 100)
        n(4, claim="b" * 101)
        n(5, claim="b" * 99)
        n(6, claim="汉" * 99 + "字" + "tail")
        n(7, claim="é" * 60)                                # combining marks: chars, not clusters
        n(8, claim="\U0001F600" * 150)
    elif shape == "whitespace_edges":
        n(0, claim="a" + " " * 150 + "b")                        # slice AFTER strip, no re-strip
        n(1, claim="a" + " " * 98 + "b" * 5)                    # 'a', 98 spaces, then b: cut lands after the first b
        n(2, claim="\n\n  claim two \u2003")
        n(3, claim="\x1cX\x1d")
        n(4, claim="\u200bzero width")                           # NOT whitespace to Python
        n(5, claim="\ufeffbom")                                  # NOT whitespace to Python
        n(6, claim="\u180e mongolian")                           # NOT whitespace since Unicode 6.3
        n(7, claim="  mixed \t inner   spaces  kept  ")
        n(8, claim="\x85\xa0 nel and nbsp \xa0\x85")
        n(9, claim="x", rs="\u2028")                              # whitespace-only summary, claim present
        n(10, claim="\u2029", rs="real summary")                 # whitespace-only claim, summary present
        n(11, claim=" " * 300 + "z")                             # 300 spaces then z: strips to "z"
        n(12, claim="\xa0" * 5 + "y" * 120)
    elif shape == "ties_small":
        for k, nid in enumerate(["zz", "mm", "aa", "qq", "bb", "yy", "cc", "ww"]):
            ins(conn, f"t_{nid}", f"tie {nid}", None, ts(0))
    elif shape == "ties_over_cap":
        # 250 rows share ONE created_at; ids descending so id order != insertion order
        for k in range(250):
            ins(conn, f"t{250 - k:04d}", f"tie row {k}", None, ts(0))
    elif shape == "ties_mixed":
        for k in range(230):
            ins(conn, f"u{(k * 37) % 1000:04d}_{k}", f"mixed tie {k}", None, ts(k // 23))
    elif shape == "late_rows_only_boundary":
        # a row exactly AT the checkpoint instant is in the window (>=)
        ins(conn, "at_boundary", "at the boundary", None, WINDOW_FROM)
        n(1)
    else:  # pragma: no cover
        raise AssertionError(shape)


SHAPES = [
    "empty", "all_excluded", "under_cap", "exactly_cap", "cap_plus_1", "cap_plus_many",
    "over_cap_with_blanks", "many_summary_only", "blanks_inside_first_cap", "rs_only", "claim_only",
    "rs_blank_claim_present", "both_present_rs_wins", "non_current", "multibyte",
    "whitespace_edges", "ties_small", "ties_over_cap", "ties_mixed", "late_rows_only_boundary",
]


def build(home: Path, shape: str):
    db = build_empty_store(home)
    conn = sqlite3.connect(str(db))
    populate(conn, shape)
    conn.commit()
    conn.close()
    return db


def new_direct(mod, home) -> str:
    mod._OWNER_RUN = None
    return mod.session_checkpoint_block(str(home), WINDOW_FROM)


def new_owner(mod, home, kind) -> str:
    with owner_of(kind, home):
        with Fire(mod):
            return mod.session_checkpoint_block(str(home), WINDOW_FROM)


# =======================================================================================
# C2 / C3 / C4 -- the differential cells: new == old, byte for byte
# =======================================================================================

@pytest.mark.parametrize("shape", SHAPES)
def test_golden_direct_path_equals_the_old_logic(shape, home, monkeypatch):
    """Flag OFF (direct read-only connection): the new function renders the same string as the
    old full-fetch logic on the same store, on every window shape.
    Mutations M1/M2/M3/M4/M5 -> the shape that exposes each is red (see the module docstring)."""
    build(home, shape)
    mod = load_ss(home, monkeypatch)
    expected = old_block(mod, home, WINDOW_FROM)
    assert new_direct(mod, home) == expected


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
@pytest.mark.parametrize("shape", SHAPES)
def test_golden_owner_path_equals_the_old_logic(shape, owner_kind, home, monkeypatch):
    """Flag ON (the owner shim): the same string again, through the fake owner and, where this
    host can run it, the real one."""
    build(home, shape)
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    expected = old_block(mod, home, WINDOW_FROM)
    assert new_owner(mod, home, owner_kind) == expected


def test_golden_cells_are_not_vacuous(home, monkeypatch):
    """Anti-vacuity: the reference itself shows the shapes the contract names (a differential
    against an empty string proves nothing)."""
    mod = load_ss(home, monkeypatch)
    seen = {}
    for shape in SHAPES:
        sub = home / shape
        sub.mkdir()
        build(sub, shape)
        seen[shape] = old_block(mod, sub, WINDOW_FROM)
    assert seen["empty"] == "" and seen["all_excluded"] == ""
    assert "— 5 node(s)" in seen["under_cap"] and "more" not in seen["under_cap"]
    assert "— 200 node(s)" in seen["exactly_cap"] and "more" not in seen["exactly_cap"]
    assert seen["cap_plus_1"].endswith("... +1 more") and "— 201 node(s)" in seen["cap_plus_1"]
    assert seen["cap_plus_many"].endswith("... +250 more")
    # 450 rows, indexes 0,7,...,448 blank = 65 blank -> 385 counted, 185 elided
    assert "— 385 node(s)" in seen["over_cap_with_blanks"] and seen["over_cap_with_blanks"].endswith("... +185 more")
    # 260 rows, every 5th blank (52) -> 208 counted, 8 elided
    assert "— 208 node(s)" in seen["blanks_inside_first_cap"] and seen["blanks_inside_first_cap"].endswith("... +8 more")
    # 300 rows, 120 summary-only (claim blank/NULL) and all counted: 300 node(s), 100 elided
    assert "— 300 node(s)" in seen["many_summary_only"] and seen["many_summary_only"].endswith("... +100 more")
    assert "summary only 0" in seen["many_summary_only"]
    assert "summary only 3" in seen["rs_only"] and "claim number 3" in seen["claim_only"]
    assert "claim number 2" in seen["rs_blank_claim_present"]  # blank summary falls back to the claim
    assert "[superseded]" in seen["non_current"] and "[retracted]" in seen["non_current"]
    assert "at the boundary" in seen["late_rows_only_boundary"]
    assert "before the window" not in "".join(seen.values())
    # the post-slice-strip case: 'a' + 99 spaces, NOT re-stripped to 'a'
    assert "(observation) a" + " " * 99 + "\n" in seen["whitespace_edges"] + "\n"
    # the 100th char is the emoji: the whole 99-ASCII + emoji prefix survives
    assert "a" * 99 + "\U0001F600" in seen["multibyte"] and "tail" not in seen["multibyte"].split("\n")[1]


# ---- explicit expectations, independent of the reference ------------------------------

def test_the_100th_character_boundary_is_pinned_by_characters_not_bytes(home, monkeypatch):
    """C3: a claim of 99 ASCII + a 4-byte emoji + 'tail' renders the emoji and stops (100
    characters); 100/101/99-char ASCII claims render 100/100/99 characters; CJK renders 100
    characters. Mutation M2 (substr by bytes, or length 99/101) -> red."""
    build(home, "multibyte")
    mod = load_ss(home, monkeypatch)
    out = new_direct(mod, home)
    lines = {ln.split("]", 1)[0].lstrip(" -["): ln for ln in out.split("\n")[1:]}
    text = lambda i: lines[f"n{i:05d}"].split(") ", 1)[1]  # noqa: E731
    assert text(0) == "a" * 99 + "\U0001F600"
    assert text(1) == "a" * 98 + "é" + "x"
    assert text(2) == "汉" * 100
    assert text(3) == "b" * 100 and text(4) == "b" * 100 and text(5) == "b" * 99
    assert text(6) == "汉" * 99 + "字"
    assert text(7) == "é" * 50
    assert text(8) == "\U0001F600" * 100


def test_the_text_is_cut_after_the_strip_and_never_stripped_again(home, monkeypatch):
    """C3/b: `claim.strip()[:100]` -- trim, then slice, and NOTHING after. A claim of 'a' + 150
    spaces + 'b' renders as 'a' + 99 spaces (trailing blanks kept); a Python-side re-strip of the
    sliced text would render 'a'. Mutation M4 -> red."""
    build(home, "whitespace_edges")
    mod = load_ss(home, monkeypatch)
    out = new_direct(mod, home)
    line = [ln for ln in out.split("\n") if "[n00000]" in ln][0]
    assert line == "  - [n00000] (observation) " + "a" + " " * 99
    # 300 spaces then 'z' strips to 'z'
    assert [ln for ln in out.split("\n") if "[n00011]" in ln][0] == "  - [n00011] (observation) z"
    # whitespace-only summary with a real claim renders the claim, and vice versa
    assert [ln for ln in out.split("\n") if "[n00009]" in ln][0].endswith(" x")
    assert [ln for ln in out.split("\n") if "[n00010]" in ln][0].endswith(" real summary")
    # zero-width space / BOM / U+180E are NOT whitespace to Python: kept, never trimmed
    assert "\u200bzero width" in out and "\ufeffbom" in out and "\u180e mongolian" in out


def test_ties_render_in_insertion_order_and_the_cap_cuts_the_same_rows(home, monkeypatch):
    """C4: 250 rows sharing one created_at: the first 200 by rowid are shown, in rowid order, the
    same set and order the old full fetch produced. (Discriminating power against the dropped
    tiebreak depends on SQLite's plan -- reported honestly in the PR.)"""
    build(home, "ties_over_cap")
    mod = load_ss(home, monkeypatch)
    out = new_direct(mod, home)
    ids = re.findall(r"^  - \[(\w+)\]", out, re.M)
    assert ids == [f"t{250 - k:04d}" for k in range(CAP)]
    assert out.endswith("... +50 more")


# =======================================================================================
# C3 -- the whitespace set: SQL trim(x, WS) == Python str.strip for EVERY character
# =======================================================================================

def _templates():
    """(count_sql, list_sql) of the committed registry for this site."""
    sqls = sql_set_of_function(FN)
    assert len(sqls) == 2, sorted(sqls)
    count = [s for s in sqls if s.startswith("SELECT COUNT(*) FROM nodes")]
    lst = [s for s in sqls if " LIMIT " in s]
    assert len(count) == 1 and len(lst) == 1, sorted(sqls)
    return count[0], lst[0]


def _all_chars():
    return [chr(i) for i in range(0x110000) if not (0xD800 <= i <= 0xDFFF)]


def test_the_sql_trim_equals_python_strip_for_every_character_and_mixed_strings():
    """C3/a: execute the REAL template SQL (read out of the committed registry) against an
    in-memory sqlite3 table. For EVERY non-surrogate code point c: the row with claim = c is
    counted iff `c.strip()` is non-empty, and the list query returns exactly `c.strip()[:100]`;
    plus leading/trailing runs, interior whitespace kept, and mixed strings. The one-argument
    `trim` (space only) fails this on e.g. '\\n' and U+3000. Mutation M5 -> red."""
    count_sql, list_sql = _templates()
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT, recall_summary TEXT, "
                 "created_at TEXT, is_current INTEGER, status TEXT)")
    # NOT sampled: a claim holding an embedded NUL (U+0000). SQLite's text functions stop at the
    # first NUL, so trim/substr see "a" in "a<NUL>b" where Python sees three characters. Claims
    # are not expected to carry NULs, but no writer rejects one today (write-path issue #3765): the design seat
    # accepted the divergence for U9b, and it is pinned by the characterization cells at the end of this file.
    samples = [c for c in _all_chars() if c != "\x00"]
    ws = [c for c in samples if c.isspace()]
    assert len(ws) == 29  # the set Python's isspace() knows (Unicode 15+), pinned: a change shows here
    for c in ws:
        samples += [c * 3, "x" + c, c + "x", c + "x" + c, "a" + c + "b", c * 2 + "mid" + c * 4 + "end" + c * 2]
    samples += [" \t\n\r\x0b\x0c\x1c\x1d\x1e\x1f\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
                "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000 core \u3000 inside \n",
                "\u200b\u200bx\u200b", "\ufeff", "\u180e", ""]
    conn.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
        [(f"r{i:07d}", "observation", s, None, "2026-10-12T01:00:00Z", 1, "active")
         for i, s in enumerate(samples)])
    counted = conn.execute(count_sql, ("2026-10-12T00:00:00Z",)).fetchone()[0]
    assert counted == sum(1 for s in samples if s.strip()), "COUNT predicate != Python (claim.strip() or '')"
    # the list statement is LIMITed to the cap; run the same text WITHOUT the LIMIT so every sample
    # is checked, not only the first CAP (the LIMIT itself is pinned elsewhere)
    assert list_sql.endswith(f" LIMIT {CAP}")
    rows = conn.execute(list_sql[: -len(f" LIMIT {CAP}")], ("2026-10-12T00:00:00Z",)).fetchall()
    by_id = {r[0]: r[2] for r in rows}
    expected = {f"r{i:07d}": s.strip()[:100] for i, s in enumerate(samples) if s.strip()}
    assert set(by_id) == set(expected)
    bad = [(k, by_id[k], v) for k, v in expected.items() if by_id[k] != v]
    assert not bad, bad[:5]


def test_the_whitespace_set_embedded_in_the_sql_is_exactly_the_isspace_set():
    """C3/a: the characters inside the trim() argument are EXACTLY {c : c.isspace()} -- no
    more (it would over-trim), no fewer (it would under-trim)."""
    for sql in _templates():
        sets = re.findall(r"trim\(coalesce\((?:claim|recall_summary), ''\), '([^']*)'\)", sql)
        assert sets, sql
        for s in sets:
            assert sorted(s) == sorted(c for c in _all_chars() if c.isspace()), repr(s)


# =======================================================================================
# C5 -- non-atomic statements: total = max(count, len(list))
# =======================================================================================

class _SkewConn:
    """Wraps a real sqlite3 connection; the COUNT statement answers `count_override`."""

    def __init__(self, real, count_override):
        self.real, self.override = real, count_override

    def execute(self, sql, params=()):
        cur = self.real.execute(sql, params)
        if sql.startswith("SELECT COUNT(*)"):
            return types.SimpleNamespace(fetchone=lambda: (self.override,), fetchall=lambda: [(self.override,)])
        return cur

    def close(self):
        self.real.close()


@pytest.mark.parametrize("override,header,tail", [
    (3, "— 5 node(s)", None),        # count LOWER than the list (rows landed between): never a gap
    (5, "— 5 node(s)", None),
    (9, "— 9 node(s)", "  ... +4 more"),   # count HIGHER (rows landed after the list): overlap, +N more
])
def test_total_is_the_larger_of_count_and_list_and_remaining_never_negative(override, header, tail, home, monkeypatch):
    build(home, "under_cap")
    mod = load_ss(home, monkeypatch)
    real = mod.connect_regular_ro
    monkeypatch.setattr(mod, "connect_regular_ro", lambda *a, **k: _SkewConn(real(*a, **k), override))
    out = new_direct(mod, home)
    assert header in out.split("\n")[0]
    assert (out.split("\n")[-1] == tail) if tail else ("more" not in out)
    assert len(out.split("\n")) == 1 + 5 + (1 if tail else 0)


# =======================================================================================
# C1 / C6 -- the statements sent are exactly the two registered templates
# =======================================================================================

def test_two_templates_are_registered_for_the_site_and_the_old_one_is_retired():
    sqls = sql_set_of_function(FN)
    assert len(sqls) == 2
    assert OLD_SQL not in registered_sql(), "the old full-fetch template must be retired"
    count_sql, list_sql = _templates()
    assert "created_at >= ?" in count_sql and "ORDER BY" not in count_sql
    assert list_sql.rstrip().endswith("ORDER BY created_at ASC, rowid ASC LIMIT %d" % CAP), list_sql
    # the exclusion predicate is in BOTH statements, identical
    pred = re.search(r"AND \((.*)\)", count_sql, re.S).group(1)
    assert pred in list_sql
    # no bare `claim` column is selected, only the trimmed substring
    select_list = list_sql.split(" FROM nodes", 1)[0]
    assert re.search(r"[ ,]claim[ ,]", select_list.replace("substr(", "")) is None or "substr(" in select_list
    assert "substr(trim(coalesce(claim, ''), '" in select_list


def test_the_limit_literal_equals_the_hook_cap(home, monkeypatch):
    """The template text is FIXED (the owner admits exact bytes), so the cap is a literal in it;
    this pins it to CHECKPOINT_LIST_CAP so raising one without the other turns red."""
    mod = load_ss(home, monkeypatch)
    assert mod.CHECKPOINT_LIST_CAP == CAP
    assert _templates()[1].endswith(f"LIMIT {CAP}")


def test_both_templates_pass_the_registry_classification_against_a_real_schema(home):
    """The committed registry entries survive `load_registry` (re-classified against the live
    schema, key == sha256(sql)); a refused template would be dropped silently by the owner."""
    db = build_empty_store(home)
    conn = sqlite3.connect(str(db))
    reg = rr.load_registry(REPO_ROOT / "src" / "engram" / "engramd_read_templates.json", conn)
    count_sql, list_sql = _templates()
    assert reg.check(count_sql, [WINDOW_FROM]) is None
    assert reg.check(list_sql, [WINDOW_FROM]) is None
    assert count_sql not in reg.refused and list_sql not in reg.refused
    assert reg.check(OLD_SQL, [WINDOW_FROM]) is not None
    conn.close()


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_flag_on_sends_exactly_the_count_then_the_list_and_nothing_else(owner_kind, home, monkeypatch):
    build(home, "over_cap_with_blanks")
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    seen = []
    real = ec.read

    def spy(method, params, **k):
        seen.append((method, params.get("sql"), params.get("args")))
        return real(method, params, **k)
    monkeypatch.setattr(ec, "read", spy)
    out = new_owner(mod, home, owner_kind)
    count_sql, list_sql = _templates()
    assert [(m, s) for m, s, _ in seen] == [("read", count_sql), ("read", list_sql)]
    assert all(list(a) == [WINDOW_FROM] for _, _, a in seen)
    assert out.startswith("[Nodes created since checkpoint")


def test_flag_off_the_direct_connection_runs_exactly_the_two_statements(home, monkeypatch):
    build(home, "over_cap_with_blanks")
    mod = load_ss(home, monkeypatch)
    statements = []
    real = mod.connect_regular_ro

    def traced(*a, **k):
        c = real(*a, **k)
        c.set_trace_callback(statements.append)
        return c
    monkeypatch.setattr(mod, "connect_regular_ro", traced)
    new_direct(mod, home)
    count_sql, list_sql = _templates()
    sel = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert sel == [count_sql.replace("?", f"'{WINDOW_FROM}'"), list_sql.replace("?", f"'{WINDOW_FROM}'")]


# =======================================================================================
# C7 -- bytes on the wire
# =======================================================================================

class _CountingSocketModule:
    """Stands in for `engramd_client.socket`: every byte the client RECEIVES is counted."""

    def __init__(self, counter):
        ns = {k: getattr(socket, k) for k in dir(socket) if not k.startswith("__")}
        outer = counter

        class Sock(socket.socket):
            def recv(self, n, *a):
                data = super().recv(n, *a)
                outer.append(len(data))
                return data
        ns["socket"] = Sock
        self.__dict__.update(ns)


PER_ROW_BOUND = 220      # id<=16 + type<=16 + 100 claim chars + null summary + 32 created_at + flags + JSON punctuation
ENVELOPE_BOUND = 1024    # the COUNT reply plus both status envelopes
WIRE_ROWS = 3200
CLAIM_LEN = 700


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_bytes_on_the_wire_are_bounded_by_the_cap_not_by_the_window(owner_kind, home, monkeypatch):
    """A long-gap window (3,200 rows, ~700-byte claims, ~2.2 MB of claim text): the bytes the client
    READS from the owner for this reader (both statements) are <= CAP x 220 B + 1 KiB = 45,024
    (220 B/row = id + type + a <=100-char claim + created_at + flags + JSON punctuation, summary
    NULL). MEASURED by counting recv() bytes on the client socket. The OLD query's reply on the
    same data, encoded the way the owner encodes rows, is shown to be > 2 MB, so the cell
    discriminates the old shape from the new one. No claim crossing the socket exceeds 100 chars."""
    db = build_empty_store(home)
    conn = sqlite3.connect(str(db))
    filler = "x" * (CLAIM_LEN - 16)
    conn.executemany(
        "INSERT INTO nodes (id, type, claim, recall_summary, created_at, is_current, status, metadata) "
        "VALUES (?,?,?,?,?,1,'active','{}')",
        [(f"w{i:05d}", "observation", f"claim {i:05d} " + filler, None, ts(i)) for i in range(WIRE_ROWS)])
    conn.commit()
    old_rows = conn.execute(OLD_SQL, (WINDOW_FROM,)).fetchall()
    conn.close()
    old_reply = len(json.dumps({"status": "ok", "result": [list(r) for r in old_rows]}).encode("utf-8"))
    assert old_reply > 2_000_000, old_reply

    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    received = []
    monkeypatch.setattr(ec, "socket", _CountingSocketModule(received))
    longest = []
    real = ec.read

    def spy(method, params, **k):
        res = real(method, params, **k)
        rows = getattr(res, "result", None)
        if isinstance(rows, list):
            longest.extend(len(r[2]) for r in rows if len(r) > 2 and isinstance(r[2], str))
        return res
    monkeypatch.setattr(ec, "read", spy)

    out = new_owner(mod, home, owner_kind)
    total = sum(received)
    assert "— %d node(s)" % WIRE_ROWS in out and out.endswith("... +%d more" % (WIRE_ROWS - CAP))
    assert len(received) >= 2 and total > 0, "vacuous: nothing measured"
    assert total <= CAP * PER_ROW_BOUND + ENVELOPE_BOUND, (total, CAP * PER_ROW_BOUND + ENVELOPE_BOUND)
    assert total * 20 < old_reply, (total, old_reply)
    assert longest and max(longest) <= 100, max(longest or [0])


# =======================================================================================
# failure behaviour is unchanged: never raises, returns "", records one owner failure
# =======================================================================================

@pytest.mark.parametrize("which", ["count", "list"])
@pytest.mark.parametrize("failure", [("unavailable", "timeout"), ("rejected", None)])
def test_a_failing_statement_degrades_to_empty_with_one_recorded_failure(which, failure, home, monkeypatch):
    build(home, "under_cap")
    set_flag(home, True)
    mod = load_ss(home, monkeypatch)
    count_sql, list_sql = _templates()
    arm_failure(mod, monkeypatch, failure, {count_sql if which == "count" else list_sql}, home)
    with owner_of("fake", home):
        with Fire(mod) as run:
            out = mod.session_checkpoint_block(str(home), WINDOW_FROM)
        assert out == ""
        assert run["failure"] is not None and run["failure"]["site"] == FN


# =======================================================================================
# Characterization: embedded NUL in a claim (a KNOWN divergence, pinned as current behaviour)
# =======================================================================================

def _nul_store(home: Path, normal: int, nuls: list[str]) -> None:
    """`normal` ordinary claims n00000.., then the NUL claims after them by created_at."""
    db = build_empty_store(home)
    conn = sqlite3.connect(str(db))
    for i in range(normal):
        ins(conn, f"n{i:05d}", f"claim number {i}", None, ts(i))
    for k, claim in enumerate(nuls):
        ins(conn, f"x{k:05d}", claim, None, ts(normal + k))
    conn.commit()
    conn.close()


def test_embedded_nul_claim_characterization_old_and_new_differ_under_the_cap(home, monkeypatch):
    """CHARACTERIZATION, not a claim of equality. Known divergence: SQLite text functions stop at
    the first NUL, Python does not. The design seat decides accept-vs-fix. If this cell goes red the
    behaviour changed, and the PR body / ledger statement about NUL must change with it.

    Window A: 3 normal claims, then three NUL claims "\x00a", "a\x00b", "\x00" (distinct
    created_at, after the normal ones). Flag OFF, same store, real old reference vs real new function."""
    _nul_store(home, 3, ["\x00a", "a\x00b", "\x00"])
    mod = load_ss(home, monkeypatch)
    old_expected = "\n".join([
        "[Nodes created since checkpoint (2026-10-12T00:00:00Z) \u2014 6 node(s)]:",
        "  - [n00000] (observation) claim number 0",
        "  - [n00001] (observation) claim number 1",
        "  - [n00002] (observation) claim number 2",
        "  - [x00000] (observation) \x00a",
        "  - [x00001] (observation) a\x00b",
        "  - [x00002] (observation) \x00",
    ])
    new_expected = "\n".join([
        "[Nodes created since checkpoint (2026-10-12T00:00:00Z) \u2014 6 node(s)]:",
        "  - [n00000] (observation) claim number 0",
        "  - [n00001] (observation) claim number 1",
        "  - [n00002] (observation) claim number 2",
        "  - [x00001] (observation) a",
        "  ... +2 more",
    ])
    assert old_block(mod, home, WINDOW_FROM) == old_expected
    assert new_direct(mod, home) == new_expected


def test_embedded_nul_claim_characterization_nul_rows_past_the_cap_are_invisible(home, monkeypatch):
    """CHARACTERIZATION. Known divergence (see the sibling cell): the design seat decides
    accept-vs-fix; if this cell goes red the behaviour changed and the PR body / ledger statement
    about NUL must change with it.

    Window B: 205 normal claims, then 3 NUL claims AFTER entry 200 by created_at. Old == new."""
    _nul_store(home, 205, ["\x00a", "a\x00b", "\x00"])
    mod = load_ss(home, monkeypatch)
    assert new_direct(mod, home) == old_block(mod, home, WINDOW_FROM)
