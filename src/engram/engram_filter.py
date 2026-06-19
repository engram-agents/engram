"""Structured-filter parser for engram_list.

Parses a recursive condition tree (per issue #81 design) into a SQL fragment
+ parameter list ready to splice into the nodes-table query. Refuses
malformed input loudly rather than silently picking an interpretation.

Grammar (one recursive rule):

    Condition := Atomic | Compound
    Atomic    := {field: str, op: <atomic_op>, value: <val>}
    Compound  := {logic: "AND" | "OR" | "NOT", conditions: [Condition, ...]}

Top-level shorthand: the `filters` field accepts a bare list as implicit
AND. Nested lists must be wrapped explicitly with `logic`.

Optional atomic keys (silently dropped if misspelled — shape validator
does not enforce a closed key set on atomics):
  - case_sensitive (bool, default False) — applies to `contains`,
    `starts_with`, `ends_with`. Misspelling silently falls back to the
    case-insensitive default; double-check spelling when matching mixed-
    case values.

See alpha issue #81 for the full design rationale.
"""

from __future__ import annotations

from typing import Any


# Columns on the `nodes` table that are safe to filter against. Hardcoded
# to prevent injection via field names AND to give a meaningful "unknown
# field" error rather than a SQL error.
ALLOWED_NODE_COLUMNS: frozenset[str] = frozenset(
    {
        # Identity
        "id",
        "type",
        "claim",
        "created_at",
        # Evidence-specific
        "source_url",
        "source_title",
        "source_domain",
        "source_date",
        "source_accessed",
        "content_snippet",
        # Observation-specific
        "evidence_id",
        "quoted_text",
        "interpretation",
        "quote_type",
        # Prediction-specific
        "predicted_event",
        "resolution_timeframe",
        "status",
        "resolved_by",
        # Derivation-specific
        "logical_chain",
        # Versioning
        "confidence",
        "supersedes",
        "superseded_by",
        "is_current",
        # Memory
        "importance_base",
        "importance_score",
        "recall_turn",
        "recall_count",
        "recall_summary",
        "recall_keywords",
        "memory_status",
        "utility_score",
        # Source classification
        "source_type",
        # Feeling-report fields
        "reported_state",
        "trigger_text",
        "categorical_tag",
        "intensity_hint",
        "nudge_source",
        # Trust-tier (added in PR #413; closes #452)
        "trust_tier",
        "trust_signal_kind",
        "trust_signal_polarity",
        "trust_signal_weight",
        # Question-specific
        "question_category",
        "question_lacks",
    }
)


# Virtual fields translate to sub-queries against the `edges` table. They
# expose cross-table lookup ("find nodes that cite X" / "find nodes cited
# by X") without forcing callers to write raw JOIN syntax. Limited to the
# claim-citation relation family.
VIRTUAL_FIELDS: dict[str, dict[str, Any]] = {
    # Outgoing edges: this node CITES the value. Matches the
    # "find derivations that cite ob_NNNN" use case.
    "cites": {
        "direction": "outgoing",  # source = candidate, target = value
        "relations": ("cites", "supports", "derives_from"),
    },
    # Incoming edges: this node is CITED BY the value.
    "cited_by": {
        "direction": "incoming",  # target = candidate, source = value
        "relations": ("cites", "supports", "derives_from"),
    },
}


# Operators supported on regular (column-backed) atomic filters.
SCALAR_OPS: frozenset[str] = frozenset(
    {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "starts_with", "ends_with"}
)
LIST_OPS: frozenset[str] = frozenset({"in", "not_in"})
RANGE_OPS: frozenset[str] = frozenset({"between"})
NULL_OPS: frozenset[str] = frozenset({"is_null", "is_not_null"})

# Operators supported on virtual fields (edges-table lookup). Set-membership
# semantics — the candidate's edge-set either contains the value or doesn't.
VIRTUAL_OPS: frozenset[str] = frozenset({"eq", "ne", "in", "not_in"})

ALL_ATOMIC_OPS: frozenset[str] = SCALAR_OPS | LIST_OPS | RANGE_OPS | NULL_OPS

COMPOUND_LOGIC: frozenset[str] = frozenset({"AND", "OR", "NOT"})

MAX_DEPTH = 8


class FilterError(ValueError):
    """Raised when a filter dict is malformed. Message describes what's wrong."""


def _is_atomic(cond: dict) -> bool:
    return "field" in cond


def _is_compound(cond: dict) -> bool:
    return "logic" in cond


def _validate_shape(cond: Any, path: str = "filters") -> None:
    if not isinstance(cond, dict):
        raise FilterError(f"{path}: expected dict (atomic or compound), got {type(cond).__name__}")
    has_field = "field" in cond
    has_logic = "logic" in cond
    has_conditions = "conditions" in cond
    has_value = "value" in cond
    has_op = "op" in cond
    if has_field and has_logic:
        raise FilterError(
            f"{path}: dict has both 'field' (atomic) and 'logic' (compound) — pick one"
        )
    if has_field and has_conditions:
        raise FilterError(
            f"{path}: atomic (has 'field') must not have 'conditions' (that's compound-only)"
        )
    if has_logic and (has_op or has_value):
        raise FilterError(
            f"{path}: compound (has 'logic') must not have 'op' or 'value' (those are atomic-only)"
        )
    if not has_field and not has_logic:
        raise FilterError(
            f"{path}: dict missing 'field' (atomic) or 'logic' (compound) — provide one"
        )


def _parse_atomic(cond: dict, path: str) -> tuple[str, list]:
    field = cond.get("field")
    op = cond.get("op")
    if not isinstance(field, str) or not field:
        raise FilterError(f"{path}: 'field' must be a non-empty string")
    if not isinstance(op, str) or not op:
        raise FilterError(f"{path}: 'op' must be a non-empty string")

    # Virtual field path: cross-table edges lookup.
    if field in VIRTUAL_FIELDS:
        if op not in VIRTUAL_OPS:
            raise FilterError(
                f"{path}: virtual field '{field}' only supports ops "
                f"{sorted(VIRTUAL_OPS)}; got '{op}'"
            )
        return _build_virtual_clause(field, op, cond, path)

    if field not in ALLOWED_NODE_COLUMNS:
        valid = sorted(ALLOWED_NODE_COLUMNS | set(VIRTUAL_FIELDS.keys()))
        raise FilterError(f"{path}: unknown field '{field}'. Valid fields: {valid}")

    if op not in ALL_ATOMIC_OPS:
        raise FilterError(
            f"{path}: unknown op '{op}'. Valid ops: {sorted(ALL_ATOMIC_OPS)}"
        )

    return _build_scalar_clause(field, op, cond, path)


def _like_escape(value: str) -> str:
    r"""Escape SQL LIKE wildcards in a user-supplied value.

    SQLite LIKE treats `%` and `_` as wildcards. Without escaping, a query
    like `starts_with "ob_"` would match `obX0001` (the `_` matches any
    single character) — disastrous for ENGRAM's primary use case of
    filtering by node-ID prefix where IDs like `ob_`, `dv_`, `ls_` all
    contain a literal underscore.

    We use `\` as the LIKE escape character (paired with `ESCAPE '\\'` in
    the SQL fragment). Replace order matters: escape the escape character
    first, then the wildcards, so we don't double-escape.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_scalar_clause(field: str, op: str, cond: dict, path: str) -> tuple[str, list]:
    has_value = "value" in cond
    if op in NULL_OPS:
        if has_value:
            raise FilterError(f"{path}: op '{op}' must not have a 'value'")
        sql = f"\"{field}\" IS NULL" if op == "is_null" else f"\"{field}\" IS NOT NULL"
        return sql, []
    if not has_value:
        raise FilterError(f"{path}: op '{op}' requires a 'value'")

    value = cond["value"]

    if op in SCALAR_OPS:
        if op == "eq":
            return f"\"{field}\" = ?", [value]
        if op == "ne":
            # SQLite's `IS NOT` operator handles NULL natively: `NULL IS NOT 'x'`
            # evaluates TRUE because NULL is distinct from 'x'. So per design
            # Item 5, a NULL row matches `field ne X` without needing the
            # explicit `OR field IS NULL` we previously had.
            return f"\"{field}\" IS NOT ?", [value]
        if op == "gt":
            return f"\"{field}\" > ?", [value]
        if op == "gte":
            return f"\"{field}\" >= ?", [value]
        if op == "lt":
            return f"\"{field}\" < ?", [value]
        if op == "lte":
            return f"\"{field}\" <= ?", [value]
        # contains / starts_with / ends_with: LIKE with wildcard escaping per
        # `_like_escape` to protect against the SQLite `%` and `_` wildcards.
        # ESCAPE '\\' tells SQLite that `\` is the escape character.
        if op == "contains":
            case_sensitive = bool(cond.get("case_sensitive", False))
            escaped = _like_escape(str(value))
            if case_sensitive:
                return f"\"{field}\" LIKE ? ESCAPE '\\'", [f"%{escaped}%"]
            return f"LOWER(\"{field}\") LIKE LOWER(?) ESCAPE '\\'", [f"%{escaped}%"]
        if op == "starts_with":
            case_sensitive = bool(cond.get("case_sensitive", False))
            escaped = _like_escape(str(value))
            if case_sensitive:
                return f"\"{field}\" LIKE ? ESCAPE '\\'", [f"{escaped}%"]
            return f"LOWER(\"{field}\") LIKE LOWER(?) ESCAPE '\\'", [f"{escaped}%"]
        if op == "ends_with":
            case_sensitive = bool(cond.get("case_sensitive", False))
            escaped = _like_escape(str(value))
            if case_sensitive:
                return f"\"{field}\" LIKE ? ESCAPE '\\'", [f"%{escaped}"]
            return f"LOWER(\"{field}\") LIKE LOWER(?) ESCAPE '\\'", [f"%{escaped}"]

    if op in LIST_OPS:
        if not isinstance(value, list):
            raise FilterError(f"{path}: op '{op}' requires a list value, got {type(value).__name__}")
        if len(value) == 0:
            raise FilterError(f"{path}: op '{op}' requires a non-empty list")
        placeholders = ",".join("?" * len(value))
        if op == "in":
            return f"\"{field}\" IN ({placeholders})", list(value)
        # not_in: include NULLs for the same reason as `ne`
        return f"(\"{field}\" NOT IN ({placeholders}) OR \"{field}\" IS NULL)", list(value)

    if op in RANGE_OPS:  # between
        if not isinstance(value, list) or len(value) != 2:
            raise FilterError(
                f"{path}: op 'between' requires a 2-element list [low, high], got {value!r}"
            )
        return f"\"{field}\" BETWEEN ? AND ?", list(value)

    raise FilterError(f"{path}: unreachable — op '{op}' not handled")


def _build_virtual_clause(field: str, op: str, cond: dict, path: str) -> tuple[str, list]:
    spec = VIRTUAL_FIELDS[field]
    direction = spec["direction"]
    relations = spec["relations"]
    rel_placeholders = ",".join("?" * len(relations))
    if direction == "outgoing":
        sub_select_col = "source_id"  # candidate is the edge source
        match_col = "target_id"  # the value we're matching against
    else:  # incoming
        sub_select_col = "target_id"
        match_col = "source_id"

    if op in ("eq", "ne"):
        if "value" not in cond:
            raise FilterError(f"{path}: virtual field '{field}' op '{op}' requires a 'value'")
        value = cond["value"]
        if not isinstance(value, str):
            raise FilterError(f"{path}: virtual field op '{op}' value must be a node id (string)")
        sql = (
            f"id IN (SELECT {sub_select_col} FROM edges "
            f"WHERE {match_col} = ? AND relation IN ({rel_placeholders}))"
        )
        params = [value] + list(relations)
        if op == "ne":
            sql = f"NOT ({sql})"
        return sql, params

    if op in ("in", "not_in"):
        if "value" not in cond:
            raise FilterError(f"{path}: virtual field '{field}' op '{op}' requires a 'value'")
        value = cond["value"]
        if not isinstance(value, list) or len(value) == 0:
            raise FilterError(
                f"{path}: virtual field op '{op}' requires a non-empty list of node ids"
            )
        val_placeholders = ",".join("?" * len(value))
        sql = (
            f"id IN (SELECT {sub_select_col} FROM edges "
            f"WHERE {match_col} IN ({val_placeholders}) AND relation IN ({rel_placeholders}))"
        )
        params = list(value) + list(relations)
        if op == "not_in":
            sql = f"NOT ({sql})"
        return sql, params

    raise FilterError(f"{path}: unreachable — virtual op '{op}' not handled")


def _parse_compound(cond: dict, path: str, depth: int) -> tuple[str, list]:
    logic = cond.get("logic")
    if not isinstance(logic, str) or logic not in COMPOUND_LOGIC:
        raise FilterError(
            f"{path}: 'logic' must be one of {sorted(COMPOUND_LOGIC)}, got {logic!r}"
        )
    conditions = cond.get("conditions")
    if not isinstance(conditions, list):
        raise FilterError(f"{path}: compound requires a 'conditions' list")
    if logic == "NOT":
        if len(conditions) != 1:
            raise FilterError(
                f"{path}: 'NOT' requires exactly 1 child condition, got {len(conditions)}"
            )
    else:  # AND, OR
        if len(conditions) == 0:
            raise FilterError(
                f"{path}: '{logic}' requires at least one child condition (empty list is ambiguous)"
            )

    parts: list[str] = []
    params: list = []
    for i, child in enumerate(conditions):
        child_path = f"{path}.conditions[{i}]"
        frag, child_params = _parse_condition(child, child_path, depth + 1)
        parts.append(frag)
        params.extend(child_params)

    if logic == "NOT":
        return f"NOT ({parts[0]})", params
    if len(parts) == 1:
        # Length-1 AND/OR is semantically the single condition; flatten.
        return parts[0], params
    joiner = " AND " if logic == "AND" else " OR "
    return "(" + joiner.join(parts) + ")", params


def _parse_condition(cond: Any, path: str, depth: int) -> tuple[str, list]:
    if depth > MAX_DEPTH:
        raise FilterError(
            f"{path}: condition nesting exceeds max depth {MAX_DEPTH} (probable runaway)"
        )
    _validate_shape(cond, path)
    if _is_atomic(cond):
        return _parse_atomic(cond, path)
    return _parse_compound(cond, path, depth)


def parse_filters(filters: Any) -> tuple[str, list]:
    """Parse a filter spec into (sql_fragment, params).

    Accepts:
      - A dict (atomic or compound condition)
      - A list (top-level implicit AND shorthand; only at top level)
      - None or [] or {} → raises (use None/omitted at the call site to mean "no filter")

    Returns the SQL fragment as a parenthesized expression suitable for
    inserting into a WHERE clause, plus the parameter list in order.

    Raises FilterError on any malformed input.
    """
    if filters is None:
        raise FilterError("filters: cannot be None (omit at call site instead)")
    if isinstance(filters, list):
        # Top-level shorthand: list = implicit AND
        if len(filters) == 0:
            raise FilterError("filters: top-level list shorthand must not be empty")
        wrapped = {"logic": "AND", "conditions": filters}
        return _parse_condition(wrapped, "filters", 0)
    if isinstance(filters, dict):
        return _parse_condition(filters, "filters", 0)
    raise FilterError(
        f"filters: must be a dict or list (top-level shorthand), got {type(filters).__name__}"
    )


def contains_field(filters: Any, field_name: str) -> bool:
    """Recursively check if any atomic condition uses `field_name`.

    Used by engram_list's conflict-detection: if a caller sets the legacy
    single-field kwarg (node_type / status) AND the same field appears in
    the structured filter, refuse. A recursive walk is more precise than
    a textual `field_name in json` scan (which can false-positive on values
    that happen to equal the field name).
    """
    if isinstance(filters, list):
        return any(contains_field(c, field_name) for c in filters)
    if isinstance(filters, dict):
        if filters.get("field") == field_name:
            return True
        conditions = filters.get("conditions")
        if isinstance(conditions, list):
            return any(contains_field(c, field_name) for c in conditions)
    return False


def validate_fields(fields: Any) -> list[str]:
    """Validate a field-projection list. Returns the list unchanged or raises."""
    if fields is None:
        return []
    if not isinstance(fields, list):
        raise FilterError(f"fields: must be a list of column names, got {type(fields).__name__}")
    if len(fields) == 0:
        raise FilterError("fields: list must not be empty (omit instead to return all columns)")
    valid = ALLOWED_NODE_COLUMNS
    for f in fields:
        if not isinstance(f, str):
            raise FilterError(f"fields: entries must be strings, got {f!r}")
        if f not in valid:
            raise FilterError(f"fields: unknown field '{f}'. Valid fields: {sorted(valid)}")
    return list(fields)
