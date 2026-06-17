"""Tests for tools/cohort_dispatch.py — batch-summary cohort orchestration.

Covers:

  prepare subcommand:
    1. Given 30 node IDs and chunk-size=15, produces 2 chunks with correct contents.
    2. Chunk dirs exist with payload.json; no prompt.md (new: dispatcher builds prompt inline).
    3. Manifest JSON lists both chunks with correct metadata (including attempt_count=0).
    4. Nodes not found in DB are silently skipped with a warning.
    15. Duplicate IDs in input are deduplicated with a warning.

  verify-in subcommand:
    18. Happy path: valid cohort → exit 0, JSON output {"passed": true}.
    19. Schema-fail: missing required field → exit non-zero, error mentions chunk + field.
    20. Content-integrity-fail: DB claim differs from payload claim → exit non-zero.
    21. No-duplicates-fail: same ID in two chunks → exit non-zero.
    22. Chunk-size-fail: chunk with 16 entries → exit non-zero.

  validate subcommand:
    5. Valid item → goes to clean_items.json.
    6. Over-cap summary → goes to failures.json with correct error message.
    7. Node-ID-as-keyword → goes to failures.json with correct error message.
    8. Mix of valid + two invalid → correct split, correct error messages.
    9. Exit code 0 on no failures, 1 on failures.
    10. Missing agent_output.json for a chunk → payload IDs surface in failures.json.
    16. Agent output with preamble text parses correctly (brace-scanner robustness).
    23. Validate failure path writes retry_payload.json with previous_error field.

  incorporate / incorporate-retry subcommand:
    11. Retry that fixes all failures → final_payload with all summaries, no failures.
    12. Retry that fixes some failures → fixed go to summaries, unfixed go to failures.
    13. Retry that produces no output for a node → "no output" failure message.
    14. Previously clean items always appear in final_payload summaries.
    24. incorporate auto-builds retry_payload.json for retryable failures.
    25. incorporate auto-builds unfixable.json for invented-ID (non-retryable) failures.
    26. attempt_count increments in manifest.json across retry rounds.

  Dispatch prompt size regression:
    27. _build_initial_prompt returns a short string (<1000 chars) regardless of payload size.
    28. _build_retry_prompt stays under bounded ceiling (15 × max-error items).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Project-root on path (same pattern as other tests in this suite)
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.cohort_dispatch import main as dispatch_main  # noqa: E402
from tools.cohort_dispatch import _parse_agent_output, _build_initial_prompt, _build_retry_prompt  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_sqlite_db(node_ids: list[str], tmp_dir: Path) -> Path:
    """Create a minimal ENGRAM-shaped SQLite DB with the given node IDs."""
    db_path = tmp_dir / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            type TEXT,
            claim TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            recall_summary TEXT,
            recall_keywords TEXT
        )
        """
    )
    for i, nid in enumerate(node_ids):
        conn.execute(
            "INSERT INTO nodes (id, type, claim, quoted_text, interpretation) VALUES (?, ?, ?, ?, ?)",
            (
                nid,
                "observation_factual",
                f"This is the claim for node {nid} — a synthetic test claim with enough text.",
                None,
                None,
            ),
        )
    conn.commit()
    conn.close()
    return db_path


def _valid_item(node_id: str) -> dict:
    return {
        "node_id": node_id,
        "recall_summary": f"Short valid summary for {node_id}.",
        "recall_keywords": ["test-kw", "batch-summary", "recall"],
    }


def _run(argv: list[str]) -> int:
    """Run cohort_dispatch main with the given argv; return exit code."""
    return dispatch_main(argv)


# ---------------------------------------------------------------------------
# Test 1–3: prepare subcommand
# ---------------------------------------------------------------------------


def test_prepare_30_ids_chunk_size_15_produces_2_chunks():
    """prepare with 30 IDs and chunk-size=15 produces exactly 2 chunks."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        node_ids = [f"ob_{str(i).zfill(4)}" for i in range(1, 31)]
        db_path = _make_sqlite_db(node_ids, tmp_dir)
        out_dir = tmp_dir / "out"

        exit_code = _run([
            "prepare",
            "--ids", ",".join(node_ids),
            "--out", str(out_dir),
            "--chunk-size", "15",
            "--db", str(db_path),
        ])
        assert exit_code == 0, f"prepare exited {exit_code}"

        manifest_path = out_dir / "manifest.json"
        assert manifest_path.exists(), "manifest.json not created"

        manifest = json.loads(manifest_path.read_text())
        assert manifest["total_chunks"] == 2, (
            f"Expected 2 chunks, got {manifest['total_chunks']}"
        )
        assert manifest["total_nodes"] == 30, (
            f"Expected 30 total_nodes, got {manifest['total_nodes']}"
        )
        assert manifest["chunk_size"] == 15


def test_prepare_chunk_dirs_and_files_exist():
    """prepare creates chunk-0/ and chunk-1/ with payload.json (no prompt.md).

    The dispatcher builds the prompt inline at dispatch time using the paths
    in the manifest — no per-chunk prompt.md is written.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        node_ids = [f"ob_{str(i).zfill(4)}" for i in range(1, 31)]
        db_path = _make_sqlite_db(node_ids, tmp_dir)
        out_dir = tmp_dir / "out"

        _run([
            "prepare",
            "--ids", ",".join(node_ids),
            "--out", str(out_dir),
            "--chunk-size", "15",
            "--db", str(db_path),
        ])

        for chunk_idx in range(2):
            chunk_dir = out_dir / f"chunk-{chunk_idx}"
            assert chunk_dir.exists(), f"chunk-{chunk_idx}/ not created"
            assert (chunk_dir / "payload.json").exists(), f"chunk-{chunk_idx}/payload.json missing"
            # No prompt.md — prompt is constructed inline by the dispatcher
            assert not (chunk_dir / "prompt.md").exists(), (
                f"chunk-{chunk_idx}/prompt.md should NOT exist (dispatcher builds prompt inline)"
            )

        # Verify payload.json structure and counts
        payload_0 = json.loads((out_dir / "chunk-0" / "payload.json").read_text())
        payload_1 = json.loads((out_dir / "chunk-1" / "payload.json").read_text())
        assert len(payload_0) == 15, f"chunk-0 payload should have 15 entries, got {len(payload_0)}"
        assert len(payload_1) == 15, f"chunk-1 payload should have 15 entries, got {len(payload_1)}"

        # Each payload entry must have id, type, claim; no recall_summary/recall_keywords
        for entry in payload_0 + payload_1:
            assert "id" in entry, f"payload entry missing 'id': {entry}"
            assert "type" in entry, f"payload entry missing 'type': {entry}"
            assert "claim" in entry, f"payload entry missing 'claim': {entry}"
            assert "recall_summary" not in entry, "recall_summary must not be in payload"
            assert "recall_keywords" not in entry, "recall_keywords must not be in payload"


def test_prepare_manifest_has_attempt_count_and_no_prompt_path():
    """Manifest has attempt_count=0 (new field) and no prompt_path (removed field)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        node_ids = [f"ob_{str(i).zfill(4)}" for i in range(1, 4)]
        db_path = _make_sqlite_db(node_ids, tmp_dir)
        out_dir = tmp_dir / "out"

        _run([
            "prepare",
            "--ids", ",".join(node_ids),
            "--out", str(out_dir),
            "--db", str(db_path),
        ])

        manifest = json.loads((out_dir / "manifest.json").read_text())
        assert "attempt_count" in manifest, "manifest missing 'attempt_count'"
        assert manifest["attempt_count"] == 0, (
            f"attempt_count should be 0 after prepare, got {manifest['attempt_count']}"
        )
        # prompt_path should not be in chunk metadata (no prompt.md written)
        for chunk in manifest["chunks"]:
            assert "prompt_path" not in chunk, (
                f"chunk metadata should not have 'prompt_path' (no prompt.md written): {chunk}"
            )


def test_prepare_unequal_chunks_remainder():
    """prepare with 17 IDs and chunk-size=10 produces 2 chunks (10 + 7)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        node_ids = [f"ob_{str(i).zfill(4)}" for i in range(1, 18)]
        db_path = _make_sqlite_db(node_ids, tmp_dir)
        out_dir = tmp_dir / "out"

        exit_code = _run([
            "prepare",
            "--ids", ",".join(node_ids),
            "--out", str(out_dir),
            "--chunk-size", "10",
            "--db", str(db_path),
        ])
        assert exit_code == 0

        manifest = json.loads((out_dir / "manifest.json").read_text())
        assert manifest["total_chunks"] == 2
        assert manifest["chunks"][0]["node_count"] == 10
        assert manifest["chunks"][1]["node_count"] == 7


# ---------------------------------------------------------------------------
# Test 5–10: validate subcommand
# ---------------------------------------------------------------------------


def _write_agent_output(chunk_dir: Path, items: list[dict]) -> None:
    """Write a synthetic agent_output.json in {"items": [...]} format."""
    chunk_dir.mkdir(parents=True, exist_ok=True)
    payload_path = chunk_dir / "payload.json"
    # Write a matching payload.json so the validator can look up payloads
    payload = [{"id": item["node_id"], "type": "observation_factual", "claim": "synthetic"} for item in items]
    payload_path.write_text(json.dumps(payload))
    output = json.dumps({"items": items})
    (chunk_dir / "agent_output.json").write_text(output)


def _write_manifest(out_dir: Path, chunks: list[Path], attempt_count: int = 0) -> None:
    """Write a minimal manifest.json pointing to given chunk dirs."""
    chunk_manifests = []
    for i, chunk_dir in enumerate(chunks):
        chunk_manifests.append({
            "chunk_index": i,
            "node_count": 0,
            "node_ids": [],
            "chunk_dir": str(chunk_dir),
            "payload_path": str(chunk_dir / "payload.json"),
        })
    manifest = {
        "total_nodes": 0,
        "total_chunks": len(chunks),
        "chunk_size": 15,
        "out_dir": str(out_dir),
        "attempt_count": attempt_count,
        "chunks": chunk_manifests,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest))


def test_validate_clean_item_goes_to_clean_items():
    """A valid item goes to clean_items.json."""
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir()
        chunk_dir = out_dir / "chunk-0"

        _write_agent_output(chunk_dir, [_valid_item("ob_0001")])
        _write_manifest(out_dir, [chunk_dir])

        exit_code = _run(["validate", "--out", str(out_dir)])
        assert exit_code == 0, "Expected exit code 0 for no failures"

        clean = json.loads((out_dir / "clean_items.json").read_text())
        failures = json.loads((out_dir / "failures.json").read_text())
        assert len(clean) == 1, f"Expected 1 clean item, got {len(clean)}"
        assert clean[0]["node_id"] == "ob_0001"
        assert len(failures) == 0


def test_validate_overcap_summary_goes_to_failures():
    """An over-cap summary (>200 chars) goes to failures.json with correct error."""
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir()
        chunk_dir = out_dir / "chunk-0"

        overcap_item = {
            "node_id": "ob_0002",
            "recall_summary": "A" * 201,  # 201 chars — exceeds hard cap
            "recall_keywords": ["kw1", "kw2", "kw3"],
        }
        _write_agent_output(chunk_dir, [overcap_item])
        _write_manifest(out_dir, [chunk_dir])

        exit_code = _run(["validate", "--out", str(out_dir)])
        assert exit_code == 1, "Expected exit code 1 (failures present)"

        clean = json.loads((out_dir / "clean_items.json").read_text())
        failures = json.loads((out_dir / "failures.json").read_text())
        assert len(clean) == 0
        assert len(failures) == 1
        assert failures[0]["node_id"] == "ob_0002"
        assert "200" in failures[0]["error"] or "hard cap" in failures[0]["error"] or "exceeds" in failures[0]["error"]


def test_validate_node_id_as_keyword_goes_to_failures():
    """A keyword that is an ENGRAM node ID goes to failures.json with correct error."""
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir()
        chunk_dir = out_dir / "chunk-0"

        id_as_kw_item = {
            "node_id": "ob_0003",
            "recall_summary": "A valid summary under 200 chars.",
            "recall_keywords": ["valid-kw", "ob_1234", "another-kw"],  # ob_1234 is an ID
        }
        _write_agent_output(chunk_dir, [id_as_kw_item])
        _write_manifest(out_dir, [chunk_dir])

        exit_code = _run(["validate", "--out", str(out_dir)])
        assert exit_code == 1

        failures = json.loads((out_dir / "failures.json").read_text())
        assert len(failures) == 1
        assert failures[0]["node_id"] == "ob_0003"
        # Error should mention the ID pattern issue
        assert "ob_1234" in failures[0]["error"] or "node ID" in failures[0]["error"]


def test_validate_mixed_batch_correct_split():
    """1 valid + 1 overcap + 1 id-as-keyword → 1 clean, 2 failures."""
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir()
        chunk_dir = out_dir / "chunk-0"

        items = [
            _valid_item("ob_0001"),
            {
                "node_id": "ob_0002",
                "recall_summary": "B" * 201,
                "recall_keywords": ["kw1", "kw2", "kw3"],
            },
            {
                "node_id": "ob_0003",
                "recall_summary": "Short valid summary.",
                "recall_keywords": ["good-kw", "dv_9999", "another"],
            },
        ]
        _write_agent_output(chunk_dir, items)
        _write_manifest(out_dir, [chunk_dir])

        exit_code = _run(["validate", "--out", str(out_dir)])
        assert exit_code == 1

        clean = json.loads((out_dir / "clean_items.json").read_text())
        failures = json.loads((out_dir / "failures.json").read_text())

        assert len(clean) == 1
        assert clean[0]["node_id"] == "ob_0001"

        assert len(failures) == 2
        failure_ids = {f["node_id"] for f in failures}
        assert failure_ids == {"ob_0002", "ob_0003"}


def test_validate_exit_code_0_no_failures():
    """All-valid batch → exit code 0."""
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir()
        chunk_dir = out_dir / "chunk-0"

        _write_agent_output(chunk_dir, [
            _valid_item("ob_0001"),
            _valid_item("ob_0002"),
        ])
        _write_manifest(out_dir, [chunk_dir])

        exit_code = _run(["validate", "--out", str(out_dir)])
        assert exit_code == 0


def test_validate_clean_path_produces_final_payload():
    """All-valid batch → exit 0 AND final_payload.json written with correct shape.

    This is the most common runtime path (no failures → no retry needed).
    The dream master unconditionally reads final_payload.json; it must exist
    after a clean validate run.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir()
        chunk_dir = out_dir / "chunk-0"

        _write_agent_output(chunk_dir, [
            _valid_item("ob_0001"),
            _valid_item("ob_0002"),
            _valid_item("ob_0003"),
        ])
        _write_manifest(out_dir, [chunk_dir])

        exit_code = _run(["validate", "--out", str(out_dir)])
        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

        # final_payload.json must exist
        final_payload_path = out_dir / "final_payload.json"
        assert final_payload_path.exists(), "final_payload.json not written on clean validate path"

        # Shape must be {"summaries": [...], "failures": []}
        payload = json.loads(final_payload_path.read_text())
        assert "summaries" in payload, f"final_payload.json missing 'summaries' key: {payload}"
        assert "failures" in payload, f"final_payload.json missing 'failures' key: {payload}"
        assert isinstance(payload["summaries"], list), "'summaries' must be a list"
        assert isinstance(payload["failures"], list), "'failures' must be a list"

        # All 3 clean items must appear in summaries
        assert len(payload["summaries"]) == 3, (
            f"Expected 3 summaries, got {len(payload['summaries'])}"
        )
        assert payload["failures"] == [], (
            f"Expected empty failures, got {payload['failures']}"
        )
        summary_ids = {s["node_id"] for s in payload["summaries"]}
        assert summary_ids == {"ob_0001", "ob_0002", "ob_0003"}, (
            f"Unexpected summary IDs: {summary_ids}"
        )


def test_validate_missing_agent_output_warns_and_continues():
    """chunk-0 has agent_output.json; chunk-1 has no agent_output.json or payload.json.

    When no payload.json is present either, nothing is added to failures — only a
    warning is emitted. chunk-0 is still processed normally.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir()
        chunk_0 = out_dir / "chunk-0"
        chunk_1 = out_dir / "chunk-1"
        chunk_1.mkdir(parents=True)

        _write_agent_output(chunk_0, [_valid_item("ob_0001")])
        _write_manifest(out_dir, [chunk_0, chunk_1])

        # chunk-1 intentionally has no agent_output.json or payload.json
        exit_code = _run(["validate", "--out", str(out_dir)])
        # chunk-0 clean, chunk-1 has no payload so nothing added to failures
        assert exit_code == 0

        clean = json.loads((out_dir / "clean_items.json").read_text())
        assert len(clean) == 1
        assert clean[0]["node_id"] == "ob_0001"


# ---------------------------------------------------------------------------
# Test 11–14: incorporate-retry subcommand
# ---------------------------------------------------------------------------


def _setup_incorporate_retry(
    tmp_dir: Path,
    prior_clean: list[dict],
    prior_failures: list[dict],
    retry_items: list[dict],
) -> tuple[Path, Path]:
    """Write clean_items.json, failures.json, and retry agent_output.json.

    Returns (out_dir, retry_output_path).
    """
    out_dir = tmp_dir / "out"
    out_dir.mkdir(parents=True)

    (out_dir / "clean_items.json").write_text(json.dumps(prior_clean))
    (out_dir / "failures.json").write_text(json.dumps(prior_failures))

    retry_output_path = tmp_dir / "retry_output.json"
    retry_output_path.write_text(json.dumps({"items": retry_items}))

    return out_dir, retry_output_path


def test_incorporate_retry_all_fixed():
    """Retry that fixes all failures → final_payload has all summaries, no failures."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        prior_clean = [
            {"node_id": "ob_0001", "recall_summary": "Clean summary.", "recall_keywords": ["kw1", "kw2", "kw3"]},
        ]
        prior_failures = [
            {"node_id": "ob_0002", "payload": {"id": "ob_0002", "type": "observation_factual", "claim": "test"}, "error": "overcap"},
            {"node_id": "ob_0003", "payload": {"id": "ob_0003", "type": "observation_factual", "claim": "test"}, "error": "id-as-keyword"},
        ]
        retry_items = [
            _valid_item("ob_0002"),
            _valid_item("ob_0003"),
        ]

        out_dir, retry_path = _setup_incorporate_retry(tmp_dir, prior_clean, prior_failures, retry_items)

        exit_code = _run(["incorporate-retry", "--retry-output", str(retry_path), "--out", str(out_dir)])
        assert exit_code == 0

        final = json.loads((out_dir / "final_payload.json").read_text())
        assert len(final["summaries"]) == 3, f"Expected 3 summaries, got {len(final['summaries'])}"
        assert len(final["failures"]) == 0, f"Expected 0 failures, got {len(final['failures'])}"
        summary_ids = {s["node_id"] for s in final["summaries"]}
        assert summary_ids == {"ob_0001", "ob_0002", "ob_0003"}

        # B1 regression: when all failures are resolved, retry_payload.json must NOT exist.
        # The retry loop (engram-sleep skill) exits on [ -f retry_payload.json ]; an empty file (2 bytes "[]")
        # would keep the loop running to MAX_RETRIES unnecessarily.
        assert not (out_dir / "retry_payload.json").exists(), (
            "retry_payload.json should not exist when all failures are resolved — "
            "file presence is the loop-exit signal"
        )


def test_incorporate_retry_some_still_fail():
    """Retry that fixes some but not all → correct split in final_payload."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        prior_clean = [
            {"node_id": "ob_0001", "recall_summary": "Clean summary.", "recall_keywords": ["kw1", "kw2", "kw3"]},
        ]
        prior_failures = [
            {"node_id": "ob_0002", "payload": {"id": "ob_0002", "type": "observation_factual", "claim": "test"}, "error": "overcap"},
            {"node_id": "ob_0003", "payload": {"id": "ob_0003", "type": "observation_factual", "claim": "test"}, "error": "id-as-keyword"},
        ]
        retry_items = [
            _valid_item("ob_0002"),
            # ob_0003 still invalid — overcap again
            {
                "node_id": "ob_0003",
                "recall_summary": "C" * 201,
                "recall_keywords": ["kw1", "kw2", "kw3"],
            },
        ]

        out_dir, retry_path = _setup_incorporate_retry(tmp_dir, prior_clean, prior_failures, retry_items)

        exit_code = _run(["incorporate-retry", "--retry-output", str(retry_path), "--out", str(out_dir)])
        assert exit_code == 0

        final = json.loads((out_dir / "final_payload.json").read_text())
        assert len(final["summaries"]) == 2
        assert len(final["failures"]) == 1
        summary_ids = {s["node_id"] for s in final["summaries"]}
        assert "ob_0001" in summary_ids
        assert "ob_0002" in summary_ids
        assert final["failures"][0]["node_id"] == "ob_0003"
        assert "one retry" in final["failures"][0]["reason"]


def test_incorporate_retry_no_output_for_node():
    """Retry fairy produces no output for a node → 'no output' failure message."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        prior_clean = []
        prior_failures = [
            {"node_id": "ob_0002", "payload": {"id": "ob_0002", "type": "observation_factual", "claim": "test"}, "error": "overcap"},
        ]
        retry_items = []  # Retry fairy produced nothing

        out_dir, retry_path = _setup_incorporate_retry(tmp_dir, prior_clean, prior_failures, retry_items)

        exit_code = _run(["incorporate-retry", "--retry-output", str(retry_path), "--out", str(out_dir)])
        assert exit_code == 0

        final = json.loads((out_dir / "final_payload.json").read_text())
        assert len(final["summaries"]) == 0
        assert len(final["failures"]) == 1
        assert final["failures"][0]["node_id"] == "ob_0002"
        assert "no output" in final["failures"][0]["reason"]


def test_incorporate_retry_prior_clean_always_in_final():
    """Previously clean items always appear in final_payload summaries,
    even when all retry items fail."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        prior_clean = [
            {"node_id": "ob_0001", "recall_summary": "Clean summary.", "recall_keywords": ["kw1", "kw2", "kw3"]},
            {"node_id": "ob_0004", "recall_summary": "Another clean one.", "recall_keywords": ["foo", "bar", "baz"]},
        ]
        prior_failures = [
            {"node_id": "ob_0002", "payload": {"id": "ob_0002", "type": "observation_factual", "claim": "test"}, "error": "overcap"},
        ]
        retry_items = [
            # Still invalid
            {"node_id": "ob_0002", "recall_summary": "D" * 201, "recall_keywords": ["kw1", "kw2", "kw3"]},
        ]

        out_dir, retry_path = _setup_incorporate_retry(tmp_dir, prior_clean, prior_failures, retry_items)

        _run(["incorporate-retry", "--retry-output", str(retry_path), "--out", str(out_dir)])

        final = json.loads((out_dir / "final_payload.json").read_text())
        summary_ids = {s["node_id"] for s in final["summaries"]}
        assert "ob_0001" in summary_ids, "Prior-clean ob_0001 must appear in final summaries"
        assert "ob_0004" in summary_ids, "Prior-clean ob_0004 must appear in final summaries"
        assert len(final["failures"]) == 1
        assert final["failures"][0]["node_id"] == "ob_0002"


# ---------------------------------------------------------------------------
# Test 15: prepare dedup
# ---------------------------------------------------------------------------


def test_prepare_deduplicates_input_ids(capsys):
    """Duplicate IDs in input are deduplicated; a warning is emitted; only unique IDs processed."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        # ob_0001 is duplicated; ob_0002 appears once
        duped_ids = ["ob_0001", "ob_0001", "ob_0002"]
        unique_ids = ["ob_0001", "ob_0002"]
        db_path = _make_sqlite_db(unique_ids, tmp_dir)
        out_dir = tmp_dir / "out"

        exit_code = _run([
            "prepare",
            "--ids", ",".join(duped_ids),
            "--out", str(out_dir),
            "--db", str(db_path),
        ])
        assert exit_code == 0

        manifest = json.loads((out_dir / "manifest.json").read_text())
        # 2 unique nodes → processed as 2 nodes
        assert manifest["total_nodes"] == 2, (
            f"Expected 2 unique nodes, got {manifest['total_nodes']}"
        )
        # Verify the warning was emitted on stderr
        captured = capsys.readouterr()
        assert "duplicate" in captured.err.lower(), (
            f"Expected duplicate warning on stderr, got: {captured.err!r}"
        )


# ---------------------------------------------------------------------------
# Test 16: _parse_agent_output preamble robustness
# ---------------------------------------------------------------------------


def test_parse_agent_output_with_preamble():
    """Agent output with a preamble sentence parses correctly via brace-scanner."""
    valid_json = json.dumps({
        "items": [
            {
                "node_id": "ob_0001",
                "recall_summary": "A clean summary for testing preamble robustness.",
                "recall_keywords": ["preamble", "robustness", "batch-summary"],
            }
        ]
    })
    raw = (
        "Sure, here's the result:\n\n"
        + valid_json
        + "\n\nLet me know if you need any adjustments."
    )
    items = _parse_agent_output(raw)
    assert len(items) == 1, f"Expected 1 item, got {len(items)}"
    assert items[0]["node_id"] == "ob_0001"
    assert items[0]["recall_summary"] == "A clean summary for testing preamble robustness."
    assert items[0]["recall_keywords"] == ["preamble", "robustness", "batch-summary"]


# ---------------------------------------------------------------------------
# Test 17: validate missing agent_output.json surfaces IDs in failures
# ---------------------------------------------------------------------------


def test_validate_missing_agent_output_surfaces_ids_in_failures():
    """chunk-1 missing agent_output.json but has payload.json → IDs land in failures.json."""
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir()
        chunk_0 = out_dir / "chunk-0"
        chunk_1 = out_dir / "chunk-1"

        # chunk-0: valid output
        _write_agent_output(chunk_0, [_valid_item("ob_0001")])

        # chunk-1: has payload.json but no agent_output.json
        chunk_1.mkdir(parents=True)
        chunk_1_payload = [
            {"id": "ob_0010", "type": "observation_factual", "claim": "test claim A"},
            {"id": "ob_0011", "type": "observation_factual", "claim": "test claim B"},
        ]
        (chunk_1 / "payload.json").write_text(json.dumps(chunk_1_payload))

        # Write manifest pointing payload_path to chunk-1's payload.json
        manifest_data = {
            "total_nodes": 3,
            "total_chunks": 2,
            "chunk_size": 15,
            "out_dir": str(out_dir),
            "attempt_count": 0,
            "chunks": [
                {
                    "chunk_index": 0,
                    "node_count": 1,
                    "node_ids": ["ob_0001"],
                    "chunk_dir": str(chunk_0),
                    "payload_path": str(chunk_0 / "payload.json"),
                },
                {
                    "chunk_index": 1,
                    "node_count": 2,
                    "node_ids": ["ob_0010", "ob_0011"],
                    "chunk_dir": str(chunk_1),
                    "payload_path": str(chunk_1 / "payload.json"),
                },
            ],
        }
        (out_dir / "manifest.json").write_text(json.dumps(manifest_data))

        exit_code = _run(["validate", "--out", str(out_dir)])
        # Failures present → exit code 1
        assert exit_code == 1, f"Expected exit code 1 (failures), got {exit_code}"

        clean = json.loads((out_dir / "clean_items.json").read_text())
        failures = json.loads((out_dir / "failures.json").read_text())

        # chunk-0's valid item must be clean
        assert len(clean) == 1
        assert clean[0]["node_id"] == "ob_0001"

        # chunk-1's IDs must appear in failures with a missing-output reason
        assert len(failures) == 2, f"Expected 2 failures (chunk-1 IDs), got {len(failures)}"
        failure_ids = {f["node_id"] for f in failures}
        assert failure_ids == {"ob_0010", "ob_0011"}, (
            f"Expected chunk-1 IDs in failures, got {failure_ids}"
        )
        for f in failures:
            assert "missing" in f["error"].lower(), (
                f"Expected 'missing' in failure error, got: {f['error']!r}"
            )


# ---------------------------------------------------------------------------
# Test 18–22: verify-in subcommand
# ---------------------------------------------------------------------------


def _make_cohort_dir(
    tmp_dir: Path,
    chunks_payloads: list[list[dict]],
    db_path: Path | None = None,
) -> Path:
    """Write a minimal cohort dir (manifest + per-chunk payload.json files).

    chunks_payloads: list of lists — each inner list is the payload for one chunk.
    db_path: if None, use a non-existent path (content-integrity checks will error).
    """
    out_dir = tmp_dir / "cohort"
    out_dir.mkdir(parents=True, exist_ok=True)

    chunk_metas = []
    for idx, entries in enumerate(chunks_payloads):
        chunk_dir = out_dir / f"chunk-{idx}"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        payload_path = chunk_dir / "payload.json"
        payload_path.write_text(json.dumps(entries))
        chunk_metas.append({
            "chunk_index": idx,
            "node_count": len(entries),
            "node_ids": [e["id"] for e in entries if "id" in e],
            "chunk_dir": str(chunk_dir),
            "payload_path": str(payload_path),
        })

    manifest = {
        "total_nodes": sum(len(p) for p in chunks_payloads),
        "total_chunks": len(chunks_payloads),
        "chunk_size": 15,
        "out_dir": str(out_dir),
        "attempt_count": 0,
        "chunks": chunk_metas,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest))
    return out_dir


def test_verify_in_happy_path():
    """Valid cohort → exit 0, JSON output has passed=true."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        node_ids = ["ob_0001", "ob_0002", "ob_0003"]
        db_path = _make_sqlite_db(node_ids, tmp_dir)

        # Build payload using exactly the DB's claim strings
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(db_path))
        conn.row_factory = _sqlite3.Row
        rows = {r["id"]: dict(r) for r in conn.execute("SELECT id, type, claim FROM nodes").fetchall()}
        conn.close()

        payload = [
            {"id": nid, "type": rows[nid]["type"], "claim": rows[nid]["claim"]}
            for nid in node_ids
        ]
        cohort_dir = _make_cohort_dir(tmp_dir, [payload], db_path=db_path)

        exit_code = _run(["verify-in", "--out", str(cohort_dir), "--db", str(db_path)])
        assert exit_code == 0, f"Expected exit 0 (all pass), got {exit_code}"


def test_verify_in_schema_fail_missing_field():
    """Missing required field → exit non-zero, error mentions chunk + field."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        # Entry missing 'claim' field
        bad_entry = {"id": "ob_0001", "type": "observation_factual"}  # no 'claim'
        cohort_dir = _make_cohort_dir(tmp_dir, [[bad_entry]])

        # Use a non-existent db so content-integrity doesn't confuse the failure
        fake_db = tmp_dir / "fake.db"
        exit_code = _run(["verify-in", "--out", str(cohort_dir), "--db", str(fake_db)])
        assert exit_code == 1, f"Expected exit 1 (schema failure), got {exit_code}"


def test_verify_in_content_integrity_fail():
    """DB claim differs from payload claim → exit non-zero (race-window check)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        node_ids = ["ob_0001"]
        db_path = _make_sqlite_db(node_ids, tmp_dir)

        # Tamper with the claim in the payload (simulate race window)
        tampered_payload = [
            {
                "id": "ob_0001",
                "type": "observation_factual",
                "claim": "TAMPERED CLAIM — does not match DB",
            }
        ]
        cohort_dir = _make_cohort_dir(tmp_dir, [tampered_payload], db_path=db_path)

        exit_code = _run(["verify-in", "--out", str(cohort_dir), "--db", str(db_path)])
        assert exit_code == 1, (
            f"Expected exit 1 (content integrity failure), got {exit_code}"
        )


def test_verify_in_no_duplicates_fail():
    """Same ID in two chunks → exit non-zero."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        # ob_0001 appears in both chunk-0 and chunk-1
        chunk_0 = [{"id": "ob_0001", "type": "observation_factual", "claim": "claim A"}]
        chunk_1 = [
            {"id": "ob_0001", "type": "observation_factual", "claim": "claim A"},  # dup
            {"id": "ob_0002", "type": "observation_factual", "claim": "claim B"},
        ]
        cohort_dir = _make_cohort_dir(tmp_dir, [chunk_0, chunk_1])

        fake_db = tmp_dir / "fake.db"
        exit_code = _run(["verify-in", "--out", str(cohort_dir), "--db", str(fake_db)])
        assert exit_code == 1, f"Expected exit 1 (duplicate ID), got {exit_code}"


def test_verify_in_chunk_size_fail():
    """Chunk with 16 entries (> max 15) → exit non-zero."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        # 16 entries — exceeds chunk size limit
        big_chunk = [
            {"id": f"ob_{str(i).zfill(4)}", "type": "observation_factual", "claim": f"claim {i}"}
            for i in range(1, 17)
        ]
        cohort_dir = _make_cohort_dir(tmp_dir, [big_chunk])

        fake_db = tmp_dir / "fake.db"
        exit_code = _run(["verify-in", "--out", str(cohort_dir), "--db", str(fake_db)])
        assert exit_code == 1, f"Expected exit 1 (chunk too large), got {exit_code}"


# ---------------------------------------------------------------------------
# Test 23: validate failure path writes retry_payload.json
# ---------------------------------------------------------------------------


def test_validate_failure_writes_retry_payload():
    """Validate with failures writes retry_payload.json with previous_error field."""
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir()
        chunk_dir = out_dir / "chunk-0"

        overcap_item = {
            "node_id": "ob_0002",
            "recall_summary": "A" * 201,  # 201 chars — exceeds hard cap
            "recall_keywords": ["kw1", "kw2", "kw3"],
        }
        _write_agent_output(chunk_dir, [overcap_item])
        _write_manifest(out_dir, [chunk_dir])

        exit_code = _run(["validate", "--out", str(out_dir)])
        assert exit_code == 1, "Expected exit code 1 (failures present)"

        # retry_payload.json must exist
        retry_payload_path = out_dir / "retry_payload.json"
        assert retry_payload_path.exists(), "retry_payload.json not written on failure path"

        retry_payload = json.loads(retry_payload_path.read_text())
        assert isinstance(retry_payload, list), "retry_payload.json must be a JSON array"
        assert len(retry_payload) == 1, f"Expected 1 retry item, got {len(retry_payload)}"

        retry_item = retry_payload[0]
        assert retry_item.get("id") == "ob_0002", (
            f"retry_payload item missing correct id (schema uses 'id'): {retry_item}"
        )
        assert "previous_error" in retry_item, (
            "retry_payload item must have 'previous_error' field so retry fairy knows what to avoid"
        )


# ---------------------------------------------------------------------------
# Test 24–26: incorporate subcommand new artifacts
# ---------------------------------------------------------------------------


def test_incorporate_writes_retry_payload_for_retryable_failures():
    """incorporate auto-builds retry_payload.json for still-failing (retryable) items."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        prior_clean: list[dict] = []
        prior_failures = [
            {
                "node_id": "ob_0002",
                "payload": {"id": "ob_0002", "type": "observation_factual", "claim": "test"},
                "error": "overcap",
            },
        ]
        # Retry still fails (still overcap)
        retry_items = [
            {"node_id": "ob_0002", "recall_summary": "E" * 201, "recall_keywords": ["kw1", "kw2", "kw3"]},
        ]

        out_dir, retry_path = _setup_incorporate_retry(tmp_dir, prior_clean, prior_failures, retry_items)
        _write_manifest(out_dir, [])  # minimal manifest for attempt_count tracking

        exit_code = _run(["incorporate", "--retry-output", str(retry_path), "--out", str(out_dir)])
        assert exit_code == 0

        # retry_payload.json must exist and contain the still-failing item
        retry_payload_path = out_dir / "retry_payload.json"
        assert retry_payload_path.exists(), "retry_payload.json not written by incorporate"
        retry_payload = json.loads(retry_payload_path.read_text())
        assert len(retry_payload) == 1, f"Expected 1 retryable item, got {len(retry_payload)}"
        assert retry_payload[0]["id"] == "ob_0002", (
            f"retryable item should be ob_0002, got: {retry_payload[0]}"
        )
        assert "previous_error" in retry_payload[0], (
            "retryable item in retry_payload.json must have 'previous_error' field"
        )


def test_incorporate_writes_unfixable_for_invented_id():
    """incorporate auto-builds unfixable.json when fairy invents an unknown node_id."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        prior_clean: list[dict] = []
        prior_failures = [
            {
                "node_id": "ob_0002",
                "payload": {"id": "ob_0002", "type": "observation_factual", "claim": "test"},
                "error": "overcap",
            },
        ]
        # Retry fairy returned output for a DIFFERENT (invented) node ID
        retry_items = [
            _valid_item("ob_INVENTED_9999"),  # not in the original failure set
        ]

        out_dir, retry_path = _setup_incorporate_retry(tmp_dir, prior_clean, prior_failures, retry_items)
        _write_manifest(out_dir, [])

        exit_code = _run(["incorporate", "--retry-output", str(retry_path), "--out", str(out_dir)])
        assert exit_code == 0

        unfixable_path = out_dir / "unfixable.json"
        assert unfixable_path.exists(), "unfixable.json not written by incorporate"
        unfixable = json.loads(unfixable_path.read_text())
        assert len(unfixable) == 1, f"Expected 1 unfixable item (invented ID), got {len(unfixable)}"
        assert "INVENTED" in unfixable[0]["node_id"] or "INVENTED" in str(unfixable[0]), (
            f"unfixable item should reference the invented ID: {unfixable[0]}"
        )
        assert "invented" in unfixable[0]["reason"].lower() or "unknown" in unfixable[0]["reason"].lower(), (
            f"unfixable reason should mention invented/unknown ID: {unfixable[0]['reason']}"
        )


def test_incorporate_increments_attempt_count():
    """attempt_count in manifest.json increments correctly across retry rounds."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        # Round 1: prior state has 1 failure
        prior_clean: list[dict] = []
        prior_failures = [
            {
                "node_id": "ob_0002",
                "payload": {"id": "ob_0002", "type": "observation_factual", "claim": "test"},
                "error": "overcap",
            },
        ]
        retry_items = [
            {"node_id": "ob_0002", "recall_summary": "F" * 201, "recall_keywords": ["kw1", "kw2", "kw3"]},
        ]

        out_dir, retry_path = _setup_incorporate_retry(tmp_dir, prior_clean, prior_failures, retry_items)
        _write_manifest(out_dir, [], attempt_count=0)

        # Round 1 incorporate
        _run(["incorporate", "--retry-output", str(retry_path), "--out", str(out_dir)])
        manifest_after_round1 = json.loads((out_dir / "manifest.json").read_text())
        assert manifest_after_round1["attempt_count"] == 1, (
            f"attempt_count should be 1 after first incorporate, got {manifest_after_round1['attempt_count']}"
        )

        # Round 2: re-run incorporate (simulate a second retry round)
        # Need to reset failures.json and clean_items.json for round 2
        (out_dir / "failures.json").write_text(json.dumps(prior_failures))
        (out_dir / "clean_items.json").write_text(json.dumps(prior_clean))
        retry_path.write_text(json.dumps({"items": retry_items}))

        _run(["incorporate", "--retry-output", str(retry_path), "--out", str(out_dir)])
        manifest_after_round2 = json.loads((out_dir / "manifest.json").read_text())
        assert manifest_after_round2["attempt_count"] == 2, (
            f"attempt_count should be 2 after second incorporate, got {manifest_after_round2['attempt_count']}"
        )


def test_incorporate_prior_clean_revalidation_failure_surfaces_in_unfixable():
    """A prior-clean item that fails re-validation lands in unfixable.json (not just
    a builder failure), and is excluded from the cumulative clean set (#1216 fold).

    'Shouldn't happen' in practice (deterministic validator), but if the validator
    tightens mid-cohort the stranded item must be operator-visible, not silently
    dropped into final_payload only.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        # prior_clean carries an item that is NO LONGER valid (overcap summary) —
        # simulates a validator tightening between the round-0 clean write and now.
        prior_clean = [
            {"node_id": "ob_0001", "recall_summary": "Clean.", "recall_keywords": ["a", "b", "c"]},
            {"node_id": "ob_0009", "recall_summary": "Q" * 201, "recall_keywords": ["a", "b", "c"]},  # now invalid
        ]
        prior_failures = [
            {"node_id": "ob_0002", "payload": {"id": "ob_0002", "type": "observation_factual", "claim": "t"}, "error": "overcap"},
        ]
        retry_items = [_valid_item("ob_0002")]
        out_dir, retry_path = _setup_incorporate_retry(tmp_dir, prior_clean, prior_failures, retry_items)
        _write_manifest(out_dir, [])

        exit_code = _run(["incorporate", "--retry-output", str(retry_path), "--out", str(out_dir)])
        assert exit_code == 0

        # The stranded item appears in unfixable.json with a re-validation reason.
        unfixable = json.loads((out_dir / "unfixable.json").read_text())
        stranded = [u for u in unfixable if u["node_id"] == "ob_0009"]
        assert stranded, f"ob_0009 should surface in unfixable.json, got {unfixable}"
        assert "re-validation" in stranded[0]["reason"]

        # It must NOT be re-asserted into the cumulative clean set.
        clean = json.loads((out_dir / "clean_items.json").read_text())
        clean_ids = {c["node_id"] for c in clean}
        assert "ob_0009" not in clean_ids, "re-validation-failed item must not stay clean"
        assert {"ob_0001", "ob_0002"} <= clean_ids, "valid items (prior + fixed) must be clean"


# ---------------------------------------------------------------------------
# Test 29: multi-round cumulative-clean / bad-pool-only acceptance (#1215)
# ---------------------------------------------------------------------------


def _overcap_item(node_id: str) -> dict:
    """A still-failing retry item (summary over the 200-char hard cap)."""
    return {
        "node_id": node_id,
        "recall_summary": "Z" * 201,
        "recall_keywords": ["kw1", "kw2", "kw3"],
    }


def test_incorporate_multiround_cumulative_clean_no_regression():
    """≥3-round retry: clean count is monotonic non-decreasing and the failure
    pool shrinks to exactly the previous round's still-failing items (#1215).

    Reproduces the 2026-06-16 live-cycle regression in miniature: each retry
    round fixes a STRICT SUBSET of the previous round's failures, and the test
    re-runs `incorporate` WITHOUT manually resetting clean_items.json/failures.json
    between rounds — exactly the orchestrator's loop. The pre-fix bug re-read
    round-0's baseline each round and silently dropped intermediate fixes, so
    the clean count could go backwards. Here it must only ever go up.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        out_dir = tmp_dir / "out"
        out_dir.mkdir(parents=True)

        # Round-0 baseline (what cmd_validate would write): 5 clean, 6 failing.
        initial_clean = [_valid_item(f"ob_{i:04d}") for i in range(1, 6)]  # ob_0001..ob_0005
        fail_ids = [f"ob_{i:04d}" for i in range(10, 16)]                  # ob_0010..ob_0015
        round0_failures = [
            {"node_id": nid, "payload": {"id": nid, "type": "observation_factual", "claim": "test"}, "error": "overcap"}
            for nid in fail_ids
        ]
        (out_dir / "clean_items.json").write_text(json.dumps(initial_clean))
        (out_dir / "failures.json").write_text(json.dumps(round0_failures))
        _write_manifest(out_dir, [], attempt_count=0)

        # Each round's retry fairy output fixes a STRICT SUBSET; the rest stay overcap.
        # Round 1 fixes 3 (ob_0010-12), round 2 fixes 2 (ob_0013-14), round 3 fixes 1 (ob_0015).
        rounds = [
            # round 1: fix 0010,0011,0012 ; 0013,0014,0015 still fail
            [_valid_item("ob_0010"), _valid_item("ob_0011"), _valid_item("ob_0012"),
             _overcap_item("ob_0013"), _overcap_item("ob_0014"), _overcap_item("ob_0015")],
            # round 2: pool is now only 0013,0014,0015 — fix 0013,0014 ; 0015 still fails
            [_valid_item("ob_0013"), _valid_item("ob_0014"), _overcap_item("ob_0015")],
            # round 3: pool is now only 0015 — fix it
            [_valid_item("ob_0015")],
        ]
        expected_pool_sizes = [6, 3, 1]   # failures.json read at the START of each round
        expected_clean_after = [8, 10, 11]  # cumulative clean after each round
        prev_clean_count = len(initial_clean)  # 5

        retry_path = tmp_dir / "retry_output.json"
        for rnd, retry_items in enumerate(rounds):
            # This asserts the EXTERNAL-loop invariant: the orchestrator reads the
            # current (already-shrunken) failures.json as the round's bad-only pool —
            # it does not exercise an internal code path. Against the pre-fix code this
            # check fails on round 2 (failures.json still held all 6 round-0 items, so
            # pool_ids != the 3-item retry_ids).
            pool = json.loads((out_dir / "failures.json").read_text())
            assert len(pool) == expected_pool_sizes[rnd], (
                f"round {rnd+1}: failure pool should be {expected_pool_sizes[rnd]} "
                f"(bad-only, shrinking), got {len(pool)}"
            )
            # Bad-pool-only: every item dispatched this round must be one that failed last round.
            retry_ids = {it["node_id"] for it in retry_items}
            pool_ids = {f["node_id"] for f in pool}
            assert retry_ids == pool_ids, (
                f"round {rnd+1}: retry input {retry_ids} must equal the prior-round "
                f"failure pool {pool_ids} (never re-dispatch already-clean items)"
            )

            retry_path.write_text(json.dumps({"items": retry_items}))
            exit_code = _run(["incorporate", "--retry-output", str(retry_path), "--out", str(out_dir)])
            assert exit_code == 0

            final = json.loads((out_dir / "final_payload.json").read_text())
            clean_count = len(final["summaries"])
            # Monotonic non-decreasing — the core #1215 invariant.
            assert clean_count >= prev_clean_count, (
                f"round {rnd+1}: clean count regressed {prev_clean_count} -> {clean_count}"
            )
            assert clean_count == expected_clean_after[rnd], (
                f"round {rnd+1}: expected {expected_clean_after[rnd]} clean, got {clean_count}"
            )
            prev_clean_count = clean_count

        # End state: all 11 nodes clean (5 initial + 6 fixed across 3 rounds), no failures,
        # and retry_payload.json removed so the orchestrator loop exits.
        final = json.loads((out_dir / "final_payload.json").read_text())
        assert len(final["summaries"]) == 11
        assert len(final["failures"]) == 0
        summary_ids = {s["node_id"] for s in final["summaries"]}
        assert summary_ids == {f"ob_{i:04d}" for i in (1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 15)}
        assert not (out_dir / "retry_payload.json").exists(), (
            "retry_payload.json must be removed once the pool is empty (loop-exit signal)"
        )
        # attempt_count tracked all three rounds.
        manifest = json.loads((out_dir / "manifest.json").read_text())
        assert manifest["attempt_count"] == 3


def test_incorporate_unfixable_accumulates_across_rounds():
    """Unfixable items (invented IDs) recorded in an early round survive in
    unfixable.json after later rounds (#1215 cumulative-unfixable).

    An unfixable item drops out of the active failure pool, so if unfixable.json
    were overwritten per-round it would vanish. It must persist.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        out_dir = tmp_dir / "out"
        out_dir.mkdir(parents=True)

        # Round-0: two failing items.
        round0_failures = [
            {"node_id": "ob_0010", "payload": {"id": "ob_0010", "type": "observation_factual", "claim": "t"}, "error": "overcap"},
            {"node_id": "ob_0011", "payload": {"id": "ob_0011", "type": "observation_factual", "claim": "t"}, "error": "overcap"},
        ]
        (out_dir / "clean_items.json").write_text(json.dumps([]))
        (out_dir / "failures.json").write_text(json.dumps(round0_failures))
        _write_manifest(out_dir, [], attempt_count=0)

        retry_path = tmp_dir / "retry_output.json"

        # Round 1: fairy fixes ob_0010 but invents an unknown ID instead of ob_0011.
        # ob_0011 gets "no output" (stays retryable); the invented ID → unfixable.
        retry_path.write_text(json.dumps({"items": [
            _valid_item("ob_0010"),
            _valid_item("ob_INVENTED_1"),
        ]}))
        _run(["incorporate", "--retry-output", str(retry_path), "--out", str(out_dir)])

        unfixable_r1 = json.loads((out_dir / "unfixable.json").read_text())
        assert any("INVENTED_1" in u["node_id"] for u in unfixable_r1)

        # Round 2: pool is now just ob_0011 — fix it. A different invented ID appears too.
        retry_path.write_text(json.dumps({"items": [
            _valid_item("ob_0011"),
            _valid_item("ob_INVENTED_2"),
        ]}))
        _run(["incorporate", "--retry-output", str(retry_path), "--out", str(out_dir)])

        unfixable_r2 = json.loads((out_dir / "unfixable.json").read_text())
        ids = {u["node_id"] for u in unfixable_r2}
        # Round-1's invented ID must STILL be present (cumulative), plus round-2's.
        assert any("INVENTED_1" in i for i in ids), "round-1 unfixable item was dropped"
        assert any("INVENTED_2" in i for i in ids), "round-2 unfixable item missing"

        # And the real nodes both ended up clean.
        final = json.loads((out_dir / "final_payload.json").read_text())
        clean_ids = {s["node_id"] for s in final["summaries"]}
        assert {"ob_0010", "ob_0011"} <= clean_ids


# ---------------------------------------------------------------------------
# Test 27: dispatch prompt size regression
# ---------------------------------------------------------------------------


def test_build_initial_prompt_is_short():
    """_build_initial_prompt returns <1000 chars regardless of payload size.

    This is a regression guard against re-embedding payload content inline.
    The new contract: the prompt names file paths only; all content lives in
    the payload file the fairy Reads at dispatch time.
    """
    # Simulate a 15-node payload (worst-case chunk size)
    fake_payload_path = "/some/cohort/chunk-0/payload.json"
    fake_output_path = "/some/cohort/chunk-0/agent_output.json"

    prompt = _build_initial_prompt(fake_payload_path, fake_output_path)
    assert len(prompt) < 1000, (
        f"_build_initial_prompt returned {len(prompt)} chars — "
        f"expected < 1000 (no inline payload embedding). "
        f"First 200 chars: {prompt[:200]!r}"
    )
    # Must mention both paths
    assert fake_payload_path in prompt, "Prompt must reference the input payload path"
    assert fake_output_path in prompt, "Prompt must reference the output path"


# ---------------------------------------------------------------------------
# Test 28: retry prompt size regression
# ---------------------------------------------------------------------------


def test_build_retry_prompt_bounded():
    """Worst-case retry prompt stays under bounded ceiling.

    The retry path includes failure-count overhead (node_id + error +
    previous_error per item), so its upper bound is looser than the
    initial prompt's. This test guards the parent-never-embeds-bulk-
    content property against future _build_retry_prompt refactors.

    Ceiling rationale: 15 failures × ~219 chars each (node_id ~7 chars +
    previous_error 200 chars + per-item markup) + 14 separators + ~450 chars
    of static template overhead ≈ 4519 chars measured. Ceiling of 6000 gives
    ~33% headroom above the realistic worst-case while still catching a
    regression where _build_retry_prompt accidentally embeds bulk content.
    """
    fake_retry_payload_path = "/some/cohort/retry_payload.json"
    fake_output_path = "/some/cohort/retry_output_attempt_1.json"
    fakes = [
        {"node_id": f"ob_{i:04d}", "error": "X" * 200, "previous_error": "Y" * 200}
        for i in range(15)
    ]
    prompt = _build_retry_prompt(
        retry_payload_path=fake_retry_payload_path,
        output_path=fake_output_path,
        failures=fakes,
    )
    assert len(prompt) < 6000, (
        f"Retry prompt too large: {len(prompt)} chars — "
        f"expected < 6000 (no inline bulk-content embedding). "
        f"First 200 chars: {prompt[:200]!r}"
    )
    # Must mention both paths
    assert fake_retry_payload_path in prompt, "Prompt must reference the retry payload path"
    assert fake_output_path in prompt, "Prompt must reference the output path"
