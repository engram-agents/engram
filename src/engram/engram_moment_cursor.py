"""The ONE shared compare-and-set advance of a person's moment-rotation cursor (#3856, U13 of #2652).

`metadata.special_moment_cursor` used to have two writers (the surface hook and `engram_query`), each a
read-modify-write of the WHOLE metadata dict. That loses a concurrent `pronouns` / `special_moments` edit (or
gets lost itself). This module replaces both copies with one function that advances the cursor in ONE SQL
statement and never rewrites any other key. Stdlib only; no dependency on engram_core, so the hook, the
server and the owner can all import it.

Exports:
  read_cursor(value)        THE Python cursor rule: an int that is not a bool and lies in [-(2**53), 2**53),
                            else 0 (floats, strings, None, bool, absent, anything wider all give 0).
  CURSOR_SQL_EXPR           the SQL expression for the STORED cursor under the SAME rule. Callers compose it
                            by string; the statement has one definition.
  advance_moment_cursor(conn, person_id, from_cursor)
                            -> {"applied": bool, "cursor": int | None, "reason": str | None}

Why the window is [-(2**53), 2**53) and not int64: `from_cursor` is validated to that JSON-safe window (the
owner op will refuse the same values earlier), so a stored value outside it could never be sent back as
`from` and the cursor would be stuck. Treating it as 0 on BOTH sides keeps the rotation alive. Measured on
SQLite 3.53.4: for a stored 2**63, `json_type` says 'integer' while `json_extract` returns the float
9.223372036854776e+18 and Python's `json.loads` gives the exact int; the range test below makes both sides
say 0.

What "the SQL predicate agrees with the Python rule" covers, and what it does not: it holds for EVERY VALUE
`json.dumps` can emit (pinned over the stored values absent, 0, 5, -1, 1.5, "3", true, false, null, 2**53 - 1,
the window edges and everything past int64). It does NOT hold for three classes of CORRUPT row, which the two
parsers read differently, and which are deliberately not reconciled (measured on SQLite 3.53.4): (a) DUPLICATE
keys: Python's `json.loads` keeps the LAST (`{"special_moment_cursor":1,"special_moment_cursor":2}` reads 2),
SQLite the FIRST (reads 1); `{"special_moment_cursor":"x","special_moment_cursor":7}` reads 7 in Python and 0 in
SQL; (b) an ESCAPED key spelling (`"special\\u005fmoment_cursor"` reads as the cursor key in Python, so it
overrides the plain one: 8 vs 4 in SQL); (c) `NaN` / `Infinity` in the blob: Python parses an object, SQLite's
`json_valid` says no, so the row answers `not_object`. In every class the callers send the Python-side value, the
statement compares against the SQL-side one, and the result fails in the SAFE direction: `applied: false`
(`stale`, or `not_object`), the row byte-unchanged, nothing raised, the moment repeats. A cell pins exactly that.

Compare-and-set, not increment: the caller renders the moment at index `from % len(moments)` from its own
read, then asks to advance FROM that value. If someone advanced first the statement matches no row and the
answer is `stale` with the CURRENT cursor. There is deliberately NO request_id and no idempotency key: a
replay is harmless by construction (a retried call finds from + 1 and reports `stale`), unlike `use_reward`,
whose bump is not idempotent.

This function does NOT commit. The caller owns the transaction (commit after, rollback on failure).

`reason` is a closed set: `stale` (current person row, object metadata, cursor differs), `not_person` (no
current person row with that id), `not_object` (metadata is NULL, not valid JSON, or not a JSON object).
"""
from __future__ import annotations

_LO = -(2 ** 53)
_HI = 2 ** 53          # exclusive

_PATH = "$.special_moment_cursor"

# The stored cursor under the same rule as read_cursor. json_type says 'integer' for some values json_extract
# then returns as a float (beyond int64), so the BETWEEN test is part of the rule, not decoration.
CURSOR_SQL_EXPR = (
    "CASE WHEN json_type(metadata, '$.special_moment_cursor') = 'integer'"
    " AND json_extract(metadata, '$.special_moment_cursor') BETWEEN %d AND %d"
    " THEN json_extract(metadata, '$.special_moment_cursor') ELSE 0 END" % (_LO, _HI - 1)
)

# json_type() of malformed text raises, so the object test is guarded by json_valid in a CASE.
_IS_OBJECT_SQL = "CASE WHEN json_valid(metadata) THEN json_type(metadata) END = 'object'"

# The stored cursor, SELF-GUARDED: an invalid blob yields NULL (never reaching json_type / json_extract, which raise
# `malformed JSON`) whatever order SQLite evaluates the ANDs in. The ONE definition the UPDATE splices in; a cell
# runs it alone and with the object test last.
CURSOR_GUARDED_SQL_EXPR = "CASE WHEN json_valid(metadata) THEN " + CURSOR_SQL_EXPR + " END"

_ADVANCE_SQL = (
    "UPDATE nodes SET metadata = json_set(metadata, '$.special_moment_cursor', :from + 1)"
    " WHERE id = :id AND type = 'person' AND is_current = 1"
    " AND " + _IS_OBJECT_SQL +
    # Self-guarded: whatever order SQLite evaluates the ANDs in, a malformed legacy row can never reach json_type /
    # json_extract (which raise `malformed JSON`); an invalid blob makes this NULL, i.e. no match.
    " AND " + CURSOR_GUARDED_SQL_EXPR + " = :from"
)


def read_cursor(value) -> int:
    """THE cursor rule: an int that is not a bool and lies in [-(2**53), 2**53), else 0."""
    if isinstance(value, int) and not isinstance(value, bool) and _LO <= value < _HI:
        return value
    return 0


def _check_from(from_cursor) -> int:
    if (not isinstance(from_cursor, int)) or isinstance(from_cursor, bool) or not (_LO <= from_cursor < _HI):
        raise ValueError("from_cursor must be an int (not bool) in [-(2**53), 2**53)")
    return from_cursor


def advance_moment_cursor(conn, person_id, from_cursor) -> dict:
    """Advance `person_id`'s cursor from `from_cursor` to `from_cursor + 1` in one statement. Never commits.

    Returns {"applied": True, "cursor": from + 1, "reason": None} on a match, else {"applied": False,
    "cursor": <current cursor under read_cursor> for `stale` / None otherwise, "reason": <closed set>}.
    Raises ValueError on a `from_cursor` that is not a real int in [-(2**53), 2**53).
    """
    frm = _check_from(from_cursor)
    cur = conn.execute(_ADVANCE_SQL, {"from": frm, "id": person_id})
    if cur.rowcount == 1:
        return {"applied": True, "cursor": frm + 1, "reason": None}
    # Classified in SQL (no Python parse of the metadata blob): the SAME predicates as the UPDATE.
    row = conn.execute(
        "SELECT CASE WHEN " + _IS_OBJECT_SQL + " THEN 1 ELSE 0 END,"
        " CASE WHEN " + _IS_OBJECT_SQL + " THEN " + CURSOR_SQL_EXPR + " END"
        " FROM nodes WHERE id = :id AND type = 'person' AND is_current = 1",
        {"id": person_id},
    ).fetchone()
    if row is None:
        return {"applied": False, "cursor": None, "reason": "not_person"}
    if not row[0]:
        return {"applied": False, "cursor": None, "reason": "not_object"}
    return {"applied": False, "cursor": int(row[1]), "reason": "stale"}
