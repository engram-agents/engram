"""Tests for engram_filter (structured-filter parser for engram_list, issue #81).

Two test classes:

  * TestParser — unit tests against engram_filter.parse_filters directly.
    No DB, no server. Validates grammar, error paths, SQL fragment shape.
  * TestIntegration — engram_list invoked with filters_json against a seeded
    test DB. Validates end-to-end behavior including projection, limit
    semantics, and the cross-table virtual fields.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram_filter import (  # noqa: E402
    FilterError,
    parse_filters,
    validate_fields,
    ALLOWED_NODE_COLUMNS,
)

TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_filter")


# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------


class TestParserAtomic:
    def test_eq(self):
        sql, params = parse_filters({"field": "type", "op": "eq", "value": "observation_factual"})
        assert "= ?" in sql
        assert params == ["observation_factual"]

    def test_ne_includes_null(self):
        sql, params = parse_filters({"field": "status", "op": "ne", "value": "tainted"})
        # Per issue #81 Item 5: NULL row matches `ne X` because value IS not X.
        # SQLite's `IS NOT` handles NULL natively — no explicit `OR IS NULL` needed.
        assert "IS NOT" in sql
        assert params == ["tainted"]

    def test_ne_null_row_matches_empirically(self):
        # Regression: confirm that `field IS NOT 'x'` actually returns NULL rows
        # in SQLite, validating the dead-code simplification.
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.executemany("INSERT INTO t VALUES (?)", [("a",), ("b",), (None,)])
        rows = conn.execute("SELECT v FROM t WHERE v IS NOT 'a'").fetchall()
        assert (None,) in rows, "SQLite IS NOT did not include NULL row"
        assert ("b",) in rows
        assert ("a",) not in rows

    def test_gt_gte_lt_lte(self):
        for op, sql_op in [("gt", ">"), ("gte", ">="), ("lt", "<"), ("lte", "<=")]:
            sql, params = parse_filters({"field": "confidence", "op": op, "value": 0.5})
            assert sql_op in sql
            assert params == [0.5]

    def test_in_with_list(self):
        sql, params = parse_filters({
            "field": "type", "op": "in", "value": ["observation_factual", "derivation"]
        })
        assert "IN (?,?)" in sql
        assert params == ["observation_factual", "derivation"]

    def test_in_rejects_empty_list(self):
        with pytest.raises(FilterError, match="non-empty list"):
            parse_filters({"field": "type", "op": "in", "value": []})

    def test_in_rejects_scalar(self):
        with pytest.raises(FilterError, match="list value"):
            parse_filters({"field": "type", "op": "in", "value": "observation_factual"})

    def test_not_in_includes_null(self):
        sql, params = parse_filters({"field": "type", "op": "not_in", "value": ["x", "y"]})
        # Same NULL-handling logic as `ne` per design.
        assert "NOT IN" in sql
        assert "IS NULL" in sql

    def test_contains_case_insensitive_by_default(self):
        sql, params = parse_filters({"field": "claim", "op": "contains", "value": "hello"})
        assert "LOWER" in sql
        assert params == ["%hello%"]

    def test_contains_case_sensitive_opt_in(self):
        sql, params = parse_filters({
            "field": "claim", "op": "contains", "value": "Hello", "case_sensitive": True
        })
        assert "LOWER" not in sql
        assert params == ["%Hello%"]

    def test_starts_with_and_ends_with(self):
        # Note: underscore is a LIKE wildcard in SQLite, so the parser escapes
        # it to `\_` in the param value (paired with `ESCAPE '\'` in the SQL).
        sql_start, p_start = parse_filters({"field": "id", "op": "starts_with", "value": "ob_"})
        assert p_start == ["ob\\_%"]
        assert "ESCAPE" in sql_start
        sql_end, p_end = parse_filters({"field": "id", "op": "ends_with", "value": "_001"})
        assert p_end == ["%\\_001"]
        assert "ESCAPE" in sql_end

    def test_between_inclusive(self):
        sql, params = parse_filters({
            "field": "id", "op": "between", "value": ["ob_0043", "ob_0046"]
        })
        assert "BETWEEN ? AND ?" in sql
        assert params == ["ob_0043", "ob_0046"]

    def test_between_rejects_non_pair(self):
        with pytest.raises(FilterError, match="2-element list"):
            parse_filters({"field": "id", "op": "between", "value": ["ob_0001"]})
        with pytest.raises(FilterError, match="2-element list"):
            parse_filters({"field": "id", "op": "between", "value": "ob_0001"})

    def test_is_null_no_value(self):
        sql, params = parse_filters({"field": "evidence_id", "op": "is_null"})
        assert "IS NULL" in sql
        assert params == []

    def test_is_null_rejects_value(self):
        with pytest.raises(FilterError, match="must not have a 'value'"):
            parse_filters({"field": "evidence_id", "op": "is_null", "value": "x"})

    def test_is_not_null(self):
        sql, _ = parse_filters({"field": "evidence_id", "op": "is_not_null"})
        assert "IS NOT NULL" in sql

    def test_unknown_field_lists_valid(self):
        with pytest.raises(FilterError) as exc:
            parse_filters({"field": "nonexistent_column", "op": "eq", "value": "x"})
        assert "unknown field" in str(exc.value)
        assert "Valid fields" in str(exc.value)

    def test_unknown_op(self):
        with pytest.raises(FilterError, match="unknown op"):
            parse_filters({"field": "type", "op": "fuzzy_match", "value": "x"})

    def test_missing_value_required(self):
        with pytest.raises(FilterError, match="requires a 'value'"):
            parse_filters({"field": "type", "op": "eq"})


class TestParserCompound:
    def test_top_level_list_shorthand_is_and(self):
        sql, params = parse_filters([
            {"field": "type", "op": "eq", "value": "observation_factual"},
            {"field": "status", "op": "eq", "value": "active"},
        ])
        # length-2 AND wraps in parens with " AND " joiner
        assert " AND " in sql
        assert params == ["observation_factual", "active"]

    def test_explicit_and(self):
        sql, params = parse_filters({
            "logic": "AND",
            "conditions": [
                {"field": "type", "op": "eq", "value": "derivation"},
                {"field": "confidence", "op": "gt", "value": 0.5},
            ],
        })
        assert " AND " in sql
        assert params == ["derivation", 0.5]

    def test_explicit_or(self):
        sql, params = parse_filters({
            "logic": "OR",
            "conditions": [
                {"field": "type", "op": "eq", "value": "observation_factual"},
                {"field": "type", "op": "eq", "value": "derivation"},
            ],
        })
        assert " OR " in sql

    def test_not_arity_exactly_one(self):
        sql, _ = parse_filters({
            "logic": "NOT",
            "conditions": [{"field": "status", "op": "eq", "value": "tainted"}],
        })
        assert sql.startswith("NOT (")

    def test_not_rejects_arity_zero(self):
        with pytest.raises(FilterError, match="exactly 1 child"):
            parse_filters({"logic": "NOT", "conditions": []})

    def test_not_rejects_arity_two(self):
        with pytest.raises(FilterError, match="exactly 1 child"):
            parse_filters({
                "logic": "NOT",
                "conditions": [
                    {"field": "a", "op": "eq", "value": 1},
                    {"field": "b", "op": "eq", "value": 2},
                ],
            })

    def test_and_empty_conditions_rejected(self):
        with pytest.raises(FilterError, match="at least one child"):
            parse_filters({"logic": "AND", "conditions": []})

    def test_or_empty_conditions_rejected(self):
        with pytest.raises(FilterError, match="at least one child"):
            parse_filters({"logic": "OR", "conditions": []})

    def test_length_one_compound_flattens(self):
        # AND/OR with single child returns the child's SQL unwrapped.
        sql, _ = parse_filters({
            "logic": "AND",
            "conditions": [{"field": "type", "op": "eq", "value": "x"}],
        })
        # Single-child compound flattens to the atomic clause (no joiner)
        assert " AND " not in sql

    def test_top_level_empty_list_rejected(self):
        with pytest.raises(FilterError, match="must not be empty"):
            parse_filters([])

    def test_nested_recursion(self):
        # (A OR B) AND C
        sql, _ = parse_filters({
            "logic": "AND",
            "conditions": [
                {
                    "logic": "OR",
                    "conditions": [
                        {"field": "type", "op": "eq", "value": "observation_factual"},
                        {"field": "type", "op": "eq", "value": "derivation"},
                    ],
                },
                {"field": "status", "op": "eq", "value": "active"},
            ],
        })
        assert " OR " in sql
        assert " AND " in sql

    def test_max_depth_enforced(self):
        # Build a tree deeper than MAX_DEPTH (8).
        atom = {"field": "type", "op": "eq", "value": "x"}
        node = atom
        for _ in range(10):
            node = {"logic": "AND", "conditions": [node]}
        with pytest.raises(FilterError, match="max depth"):
            parse_filters(node)


class TestParserShape:
    def test_mixed_shape_atomic_with_conditions(self):
        with pytest.raises(FilterError, match="atomic .*must not have 'conditions'"):
            parse_filters({"field": "type", "op": "eq", "value": "x", "conditions": []})

    def test_mixed_shape_compound_with_value(self):
        with pytest.raises(FilterError, match="must not have 'op' or 'value'"):
            parse_filters({"logic": "AND", "conditions": [], "value": "x"})

    def test_dict_with_both_field_and_logic(self):
        with pytest.raises(FilterError, match="both 'field'.*and 'logic'"):
            parse_filters({"field": "type", "logic": "AND", "conditions": []})

    def test_dict_with_neither_field_nor_logic(self):
        with pytest.raises(FilterError, match="missing 'field'.*or 'logic'"):
            parse_filters({"conditions": []})

    def test_top_level_not_dict_or_list(self):
        with pytest.raises(FilterError, match="must be a dict or list"):
            parse_filters("not a dict or list")

    def test_none_rejected(self):
        with pytest.raises(FilterError, match="cannot be None"):
            parse_filters(None)


class TestParserVirtualFields:
    def test_cites_eq(self):
        sql, params = parse_filters({"field": "cites", "op": "eq", "value": "ob_0218"})
        assert "FROM edges" in sql
        assert "target_id = ?" in sql  # outgoing: candidate -> target
        assert params[0] == "ob_0218"

    def test_cited_by_eq(self):
        sql, params = parse_filters({"field": "cited_by", "op": "eq", "value": "dv_0218"})
        assert "FROM edges" in sql
        assert "source_id = ?" in sql  # incoming: source -> candidate
        assert params[0] == "dv_0218"

    def test_cites_ne_wraps_in_not(self):
        sql, _ = parse_filters({"field": "cites", "op": "ne", "value": "ob_0001"})
        assert sql.startswith("NOT (")

    def test_cites_in_list(self):
        sql, params = parse_filters({
            "field": "cites", "op": "in", "value": ["ob_0001", "ob_0002"]
        })
        assert "target_id IN" in sql
        assert "ob_0001" in params and "ob_0002" in params

    def test_virtual_field_rejects_unsupported_op(self):
        with pytest.raises(FilterError, match="only supports ops"):
            parse_filters({"field": "cites", "op": "contains", "value": "ob_0001"})

    def test_virtual_field_value_must_be_string(self):
        with pytest.raises(FilterError, match="must be a node id"):
            parse_filters({"field": "cites", "op": "eq", "value": 42})


class TestValidateFields:
    def test_valid_fields_pass_through(self):
        result = validate_fields(["id", "claim", "evidence_id"])
        assert result == ["id", "claim", "evidence_id"]

    def test_unknown_field_rejected(self):
        with pytest.raises(FilterError, match="unknown field"):
            validate_fields(["id", "nonexistent_column"])

    def test_non_list_rejected(self):
        with pytest.raises(FilterError, match="must be a list"):
            validate_fields("id,claim")

    def test_empty_list_rejected(self):
        with pytest.raises(FilterError, match="must not be empty"):
            validate_fields([])

    def test_non_string_entry_rejected(self):
        with pytest.raises(FilterError, match="entries must be strings"):
            validate_fields(["id", 42])

    def test_none_returns_empty(self):
        # None means "no projection, return default columns"
        assert validate_fields(None) == []


# ---------------------------------------------------------------------------
# Integration tests — engram_list with structured filters end-to-end
# ---------------------------------------------------------------------------


def fresh_server():
    """Reset state and return the server module rebound to the test dir."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path[0] != repo_root:
        sys.path.insert(0, repo_root)
    for mod in ("server", "engram_client", "engram_filter"):
        sys.modules.pop(mod, None)
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _add_observation(server, claim: str, url: str = "https://example.com/test") -> str:
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Test source",
        "quoted_text": claim,
        "interpretation": f"Interpretation of: {claim}",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    return r["observation_id"]


class TestEngramListStructured:
    def test_backward_compat_single_field(self):
        server = fresh_server()
        ob_id = _add_observation(server, "test claim one")
        result = json.loads(server.engram_list(node_type="observation_factual"))
        assert result["status"] == "ok"
        assert result["mode"] == "single_field"
        assert any(n["id"] == ob_id for n in result["nodes"])

    def test_structured_mode_flag(self):
        server = fresh_server()
        _add_observation(server, "test claim one")
        result = json.loads(server.engram_list(
            filters_json=json.dumps({"field": "type", "op": "eq", "value": "observation_factual"})
        ))
        assert result["status"] == "ok"
        assert result["mode"] == "structured"
        assert result["total_matched"] >= 1

    def test_filters_with_invalid_json_errors(self):
        server = fresh_server()
        result = json.loads(server.engram_list(filters_json="{not valid json"))
        assert result["status"] == "error"
        assert "invalid JSON" in result["error"]

    def test_filters_unknown_field_errors(self):
        server = fresh_server()
        result = json.loads(server.engram_list(
            filters_json=json.dumps({"field": "nonexistent", "op": "eq", "value": "x"})
        ))
        assert result["status"] == "error"
        assert "unknown field" in result["error"]

    def test_field_projection(self):
        server = fresh_server()
        _add_observation(server, "test for projection")
        result = json.loads(server.engram_list(
            filters_json=json.dumps({"field": "type", "op": "eq", "value": "observation_factual"}),
            fields_json=json.dumps(["id", "claim"]),
        ))
        assert result["status"] == "ok"
        for node in result["nodes"]:
            assert set(node.keys()) == {"id", "claim"}

    def test_limit_zero_returns_count_only(self):
        server = fresh_server()
        for i in range(3):
            _add_observation(server, f"claim {i}", url=f"https://example.com/test{i}")
        result = json.loads(server.engram_list(
            filters_json=json.dumps({"field": "type", "op": "eq", "value": "observation_factual"}),
            limit=0,
        ))
        assert result["status"] == "ok"
        assert result["total_matched"] >= 3
        assert result["shown"] == 0
        assert result["nodes"] == []

    def test_unlimited_returns_all(self):
        server = fresh_server()
        for i in range(5):
            _add_observation(server, f"claim {i}", url=f"https://example.com/test{i}")
        result = json.loads(server.engram_list(
            filters_json=json.dumps({"field": "type", "op": "eq", "value": "observation_factual"}),
            unlimited=True,
        ))
        assert result["status"] == "ok"
        assert result["shown"] == result["total_matched"]
        assert result["truncated"] == 0

    def test_negative_limit_errors(self):
        server = fresh_server()
        result = json.loads(server.engram_list(limit=-5))
        assert result["status"] == "error"

    def test_node_type_conflict_with_filters_type(self):
        server = fresh_server()
        _add_observation(server, "claim")
        result = json.loads(server.engram_list(
            node_type="observation_factual",
            filters_json=json.dumps({"field": "type", "op": "eq", "value": "derivation"}),
        ))
        assert result["status"] == "error"
        assert "conflict" in result["error"]

    def test_total_matched_alias_total_matching(self):
        # Per the backward-compat alias in the return shape.
        server = fresh_server()
        _add_observation(server, "claim for alias check")
        result = json.loads(server.engram_list(node_type="observation_factual"))
        assert result["total_matched"] == result["total_matching"]

    def test_recursive_and_or_compound(self):
        server = fresh_server()
        _add_observation(server, "alpha")
        _add_observation(server, "beta")
        # Find observations whose claim contains alpha OR beta.
        result = json.loads(server.engram_list(
            filters_json=json.dumps({
                "logic": "AND",
                "conditions": [
                    {"field": "type", "op": "eq", "value": "observation_factual"},
                    {
                        "logic": "OR",
                        "conditions": [
                            {"field": "claim", "op": "contains", "value": "alpha"},
                            {"field": "claim", "op": "contains", "value": "beta"},
                        ],
                    },
                ],
            })
        ))
        assert result["status"] == "ok"
        assert result["total_matched"] >= 2

    def test_id_range_between(self):
        server = fresh_server()
        ids = []
        for i in range(5):
            ids.append(_add_observation(server, f"claim {i}", url=f"https://example.com/r{i}"))
        # Between the first and third id.
        low, high = sorted(ids)[0], sorted(ids)[2]
        result = json.loads(server.engram_list(
            filters_json=json.dumps({
                "field": "id", "op": "between", "value": [low, high]
            })
        ))
        assert result["status"] == "ok"
        assert result["total_matched"] >= 3

    def test_starts_with_escapes_underscore_wildcard(self):
        # Regression: SQLite LIKE treats `_` as single-char wildcard.
        # Without escaping, `starts_with "ob_"` would also match nodes whose
        # id starts with `ob` followed by ANY character. ENGRAM IDs all have
        # literal underscores (ob_, dv_, ls_), so this is THE primary use case.
        # Direct SQL test: insert two rows, one matching, one wildcard-trap.
        server = fresh_server()
        from engram_filter import parse_filters
        sql, params = parse_filters({"field": "id", "op": "starts_with", "value": "ob_"})
        # The SQL should contain ESCAPE clause and the param should have
        # the underscore escaped.
        assert "ESCAPE" in sql, f"missing ESCAPE clause: {sql}"
        assert "\\_" in params[0], f"underscore not escaped in param: {params}"

        # End-to-end via the empty test DB: confirm no false-positive matches.
        conn = server._get_db()
        # Direct insert (test only — bypasses normal node creation pathways)
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, is_current) "
            "VALUES (?, ?, ?, ?, 1)",
            ("obX0001", "observation_factual", "wildcard-trap test row", "2026-05-11T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, is_current) "
            "VALUES (?, ?, ?, ?, 1)",
            ("ob_test001", "observation_factual", "real ob_ prefix test row", "2026-05-11T00:00:00Z"),
        )
        conn.commit()

        result = json.loads(server.engram_list(
            filters_json=json.dumps({"field": "id", "op": "starts_with", "value": "ob_"})
        ))
        ids_returned = {n["id"] for n in result["nodes"]}
        assert "ob_test001" in ids_returned, "expected literal ob_ match missing"
        assert "obX0001" not in ids_returned, (
            "wildcard underscore trap fired — LIKE escape is broken"
        )

    def test_contains_escapes_percent_wildcard(self):
        # Parallel regression for `%` wildcard in `contains`.
        server = fresh_server()
        conn = server._get_db()
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, is_current) "
            "VALUES (?, ?, ?, ?, 1)",
            ("ob_x001", "observation_factual", "100% certainty here", "2026-05-11T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, is_current) "
            "VALUES (?, ?, ?, ?, 1)",
            ("ob_x002", "observation_factual", "100 ANYTHING certainty", "2026-05-11T00:00:00Z"),
        )
        conn.commit()

        # contains "100%" should match the literal-percent row only, not the
        # "100 ANYTHING" wildcard-trap.
        result = json.loads(server.engram_list(
            filters_json=json.dumps({"field": "claim", "op": "contains", "value": "100%"})
        ))
        ids_returned = {n["id"] for n in result["nodes"]}
        assert "ob_x001" in ids_returned
        assert "ob_x002" not in ids_returned, (
            "`%` wildcard trap fired — LIKE escape is broken for contains"
        )

    def test_starts_with_handles_backslash_in_value(self):
        # Edge case: user value contains a literal backslash. After escaping,
        # the value should still be matchable.
        from engram_filter import _like_escape
        # \ → \\ (escape the escape char)
        assert _like_escape("a\\b") == "a\\\\b"
        # %, _, \ all get escaped
        assert _like_escape("ob_50%") == "ob\\_50\\%"


# ---------------------------------------------------------------------------
# include_superseded — text-layer leak audit capability (PR #131 round-2)
#
# Closes the parity gap surfaced when engram_audit_string was retired:
# audit_string had `include_superseded` to scan the historical layer
# for text-layer leaks (Mao-Cao prototype, ob_1492 / ls_0020). engram_list
# hardcoded is_current=1, so retirement would have lost the capability.
# These tests lock in the relaxed predicate.
# ---------------------------------------------------------------------------


class TestEngramListIncludeSuperseded:
    def _seed_current_and_superseded(self, server):
        """Insert one current and one superseded obs with the same target text."""
        conn = server._get_db()
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, is_current) "
            "VALUES (?, ?, ?, ?, 1)",
            ("ob_s001", "observation_factual",
             "Current claim mentioning Mao-Cao leak.",
             "2026-05-14T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, is_current) "
            "VALUES (?, ?, ?, ?, 0)",
            ("ob_s002", "observation_factual",
             "Superseded claim also mentioning Mao-Cao leak.",
             "2026-05-13T00:00:00Z"),
        )
        conn.commit()

    def test_default_excludes_superseded(self):
        server = fresh_server()
        self._seed_current_and_superseded(server)
        result = json.loads(server.engram_list(
            filters_json=json.dumps(
                {"field": "claim", "op": "contains", "value": "Mao-Cao"}
            )
        ))
        ids = {n["id"] for n in result["nodes"]}
        assert "ob_s001" in ids
        assert "ob_s002" not in ids, "superseded should be excluded by default"

    def test_include_superseded_true_scans_historical_layer(self):
        server = fresh_server()
        self._seed_current_and_superseded(server)
        result = json.loads(server.engram_list(
            filters_json=json.dumps(
                {"field": "claim", "op": "contains", "value": "Mao-Cao"}
            ),
            include_superseded=True,
        ))
        ids = {n["id"] for n in result["nodes"]}
        assert "ob_s001" in ids
        assert "ob_s002" in ids, (
            "include_superseded=True must surface superseded nodes "
            "(audit_string parity, retired in #123)"
        )

    def test_include_superseded_with_no_other_filters_does_not_blow_up_query(self):
        """Sentinel test: include_superseded=True with no other predicates
        previously would have produced `WHERE ` (malformed SQL). The 1=1
        sentinel preserves a valid query shape."""
        server = fresh_server()
        self._seed_current_and_superseded(server)
        result = json.loads(server.engram_list(include_superseded=True))
        assert result["status"] == "ok"
        ids = {n["id"] for n in result["nodes"]}
        assert "ob_s001" in ids
        assert "ob_s002" in ids

    def test_include_superseded_false_explicit_is_default(self):
        """Passing include_superseded=False explicitly matches the default."""
        server = fresh_server()
        self._seed_current_and_superseded(server)
        result = json.loads(server.engram_list(include_superseded=False))
        ids = {n["id"] for n in result["nodes"]}
        assert "ob_s001" in ids
        assert "ob_s002" not in ids
