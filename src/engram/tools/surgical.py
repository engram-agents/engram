#!/usr/bin/env python3
"""ENGRAM Surgical Tools — high-stakes data correction with full audit trail.

These tools fix data corruption caused by identified server bugs. They are
NOT for convenience edits, schema migrations, or routine operations.

Design principles:
  - Two-phase: preview (read-only) then execute (with confirmation token)
  - Git snapshot before every mutation
  - edit_history entry for every change
  - Bug reference (qu_ or ob_ node) required as justification
  - The operator and the patient are the same entity — maximum care

Usage (from Claude Code via the engram-surgical skill):
    python surgical.py preview <operation> <args...>
    python surgical.py execute <operation> <args...> --token <confirmation_token>

Operations:
    recompute_confidence <node_id> --reason <reason> --bug-ref <node_id>
        Recompute a node's confidence from its formula inputs and cascade downstream.
    metadata_patch <node_id> <json_patch> --reason <reason> --bug-ref <node_id>
    field_patch <node_id> <field_name> <new_value> --reason <reason> --bug-ref <node_id>
    edge_add <source_id> <target_id> <relation> --reason <reason> --bug-ref <node_id>
    edge_remove <source_id> <target_id> <relation> --reason <reason> --bug-ref <node_id>
"""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Import confidence constants from the shared module (single source of truth).
# Derive the repo root from this script's location (tools/ is one level down).
PROJECT_DIR = Path(__file__).resolve().parent.parent
# #2664: append, not insert(0) -- this only needs PROJECT_DIR to be
# SOMEWHERE on sys.path so the two bare imports below resolve (true both
# for standalone CLI use, where sys.path starts empty of it, and for
# tests/test_surgical_relation_sets.py's exec_module-based fresh reload,
# where tests/conftest.py has ALREADY put PROJECT_DIR/src/engram on
# sys.path -- just behind the repo root). insert(0) reordered PROJECT_DIR
# ahead of the repo root on every exec_module() call, permanently, for
# the rest of that pytest session -- the #2565 tools/-package collision
# shape, and the actual root cause the test file's own sibling fix
# (removing its now-redundant insert(0)) could not close on its own.
sys.path.append(str(PROJECT_DIR))
from engram_confidence import (
    CONFIDENCE_MAP, SOURCE_CLASS_CONFIDENCE_DISCOUNT,
    PREDICTIVE_CONFIDENCE_CAP, REASONING_CLASS, REASONING_DISCOUNT,
    ABDUCTIVE_CONFIDENCE_CAP,
)
from engram_core import (
    CASCADE_RELATIONS, CITATION_SUPPORT_RELATIONS, EDGE_CLASSIFICATIONS,
    _pending_markers_for_node,
)

DATA_DIR = Path(os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram"))
DB_PATH = DATA_DIR / "knowledge.db"
SNAPSHOT_PATH = DATA_DIR / "graph_snapshot.md"
GIT_EXE = "git"
GIT_TIMEOUT = 30


# ── Helpers ──────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git(*args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(
            [GIT_EXE, *args], cwd=DATA_DIR,
            capture_output=True, text=True, timeout=GIT_TIMEOUT, env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _get_current_turn() -> int:
    config_path = DATA_DIR / "config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
        return config.get("memory", {}).get("current_turn", 0)
    return 0


def _log_edit(conn: sqlite3.Connection, action: str, node_id: str,
              node_type: str, details: dict):
    """Append to edit_history. Surgical edits use 'surgical_*' action prefixes."""
    conn.execute(
        """INSERT INTO edit_history (timestamp, turn, action, node_id, node_type, details)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (_now(), _get_current_turn(), action, node_id, node_type,
         json.dumps(details)),
    )


def _generate_confirmation_token(operation: str, args: dict) -> str:
    """Generate a deterministic token from the operation details.

    The token ensures the execute phase matches the preview phase — you can't
    execute an operation you didn't preview.
    """
    payload = json.dumps({"op": operation, "args": args}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _git_snapshot_before(reason: str) -> dict:
    """Commit current state before surgical operation."""
    # Stage knowledge.db and snapshot
    files = []
    if DB_PATH.exists():
        files.append(str(DB_PATH))
    if SNAPSHOT_PATH.exists():
        files.append(str(SNAPSHOT_PATH))
    if not files:
        return {"committed": False, "reason": "no files to stage"}

    _git("add", "--", *files)
    msg = f"[pre-surgical] {reason}"
    result = _git("commit", "-m", msg, "--allow-empty")
    if result.returncode == 0:
        sha = _git("rev-parse", "HEAD").stdout.strip()
        return {"committed": True, "sha": sha, "message": msg}
    # No changes to commit is fine — means we're at a clean state
    sha = _git("rev-parse", "HEAD").stdout.strip()
    return {"committed": False, "sha": sha, "reason": "already at clean state"}


def _git_snapshot_after(operation: str, node_id: str) -> dict:
    """Commit state after surgical operation."""
    files = []
    if DB_PATH.exists():
        files.append(str(DB_PATH))
    if SNAPSHOT_PATH.exists():
        files.append(str(SNAPSHOT_PATH))
    if not files:
        return {"committed": False}

    _git("add", "--", *files)
    msg = f"[surgical] {operation} on {node_id}"
    result = _git("commit", "-m", msg)
    if result.returncode == 0:
        sha = _git("rev-parse", "HEAD").stdout.strip()
        return {"committed": True, "sha": sha, "message": msg}
    return {"committed": False, "reason": result.stderr.strip()}


def _fetch_node(conn: sqlite3.Connection, node_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        return None
    return dict(row)


# ── Invariant checks (Item B, #405 slice 1) ─────────────────────────────
#
# Preview-time (and execute-time re-check) refuse-loud gate. Surgical
# ops bypass every MCP-layer guardrail (DAG validity, cascade adjudication,
# supersede semantics) -- this is the mechanical half of hardening that:
# it does NOT gate on policy (no CLAUDE.md warning, no skill, no human
# greenlight beyond the existing token handshake, no maturity gating --
# those are #405's policy half and are explicitly out of scope here).
#
# Each finding is {"invariant": name, "severity": "refuse"|"warn", "detail": str}.
# "refuse" findings block token issuance at preview and block execution
# even given a stale-but-valid token (re-checked at execute time). A named
# "refuse" can be downgraded to "warn" via --override-invariant <name> --
# the default is refuse; the override must be named, never blanket.

def _check_invariants(conn: sqlite3.Connection, operation: str, op_args: dict,
                       overrides: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """Run the five preview-time invariant checks for `operation`.

    Returns (findings, applied_overrides) -- applied_overrides names only the
    invariants that were ACTUALLY downgraded from refuse to warn (i.e. an
    override name that matched no real finding has no effect and is not
    reported as applied).
    """
    override_set = set(overrides or [])
    applied_overrides: set[str] = set()
    findings: list[dict] = []

    def _add(invariant: str, severity: str, detail: str) -> None:
        if severity == "refuse" and invariant in override_set:
            applied_overrides.add(invariant)
            severity = "warn"
            detail = f"[overridden by --override-invariant {invariant}] {detail}"
        findings.append({"invariant": invariant, "severity": severity, "detail": detail})

    # Which node(s) does this operation touch?
    if operation in ("recompute_confidence", "metadata_patch", "field_patch"):
        touched = [op_args.get("node_id")]
    elif operation in ("edge_add", "edge_remove"):
        touched = [op_args.get("source_id"), op_args.get("target_id")]
    else:
        touched = []

    # 1. temporal_causal direction on edge_add -- read the classification
    #    table from engram_core, never hard-code the temporal_causal set.
    if operation == "edge_add":
        relation = op_args.get("relation")
        classification = EDGE_CLASSIFICATIONS.get(relation, {})
        if classification.get("temporal_causal"):
            source = _fetch_node(conn, op_args["source_id"])
            target = _fetch_node(conn, op_args["target_id"])
            s_created = source.get("created_at") if source else None
            t_created = target.get("created_at") if target else None
            if s_created and t_created:
                if s_created < t_created:
                    _add(
                        "temporal_causal_direction", "refuse",
                        f"source {op_args['source_id']} (created_at={s_created}) is OLDER "
                        f"than target {op_args['target_id']} (created_at={t_created}) for "
                        f"temporal_causal relation '{relation}' -- this anchors an older "
                        f"node as deriving from a newer one (DAG violation).",
                    )
                elif s_created == t_created:
                    _add(
                        "temporal_causal_direction", "warn",
                        f"source {op_args['source_id']} and target {op_args['target_id']} "
                        f"share the same created_at ({s_created}) for temporal_causal "
                        f"relation '{relation}'.",
                    )

    # 2. Pending cascade marker on any touched node -- surgery on a node
    #    with an unadjudicated cascade hides a real problem rather than
    #    fixing it (the issue's own framing).
    for nid in touched:
        if not nid:
            continue
        markers = _pending_markers_for_node(conn, nid)
        if markers:
            names = ", ".join(f"{m['root_op']}:{m['root_node']}" for m in markers)
            _add(
                "pending_cascade_marker", "refuse",
                f"node {nid} carries {len(markers)} pending cascade marker(s) ({names}) -- "
                f"adjudicate via the engram-resolve-cascade skill / engram_cascade_resolve "
                f"before surgery.",
            )

    # 3. Legacy marker fields (pre-#2411 field form) -- markers are rows
    #    now, adjudicated, not patched.
    if operation == "metadata_patch":
        patch = op_args.get("json_patch") or {}
        hit = sorted(k for k in ("tainted_by", "stale_by") if k in patch)
        if hit:
            _add(
                "legacy_marker_field", "refuse",
                f"metadata_patch would set/clear legacy marker field(s) {hit} -- these are "
                f"pre-#2411 fields; markers are rows now, adjudicated via "
                f"engram_cascade_resolve, not patched directly.",
            )
    if operation == "field_patch":
        field_name = op_args.get("field_name")
        if field_name in ("tainted_by", "stale_by"):
            _add(
                "legacy_marker_field", "refuse",
                f"field_patch targets legacy marker field '{field_name}' -- markers are "
                f"rows now, adjudicated via engram_cascade_resolve, not patched directly.",
            )

    # 4. Orphan/strand on edge_remove -- advisory only (health penalises
    #    orphans; it does not make them invalid).
    if operation == "edge_remove":
        source_id, target_id, relation = (
            op_args.get("source_id"), op_args.get("target_id"), op_args.get("relation"),
        )
        for nid in (source_id, target_id):
            if not nid:
                continue
            remaining = conn.execute(
                "SELECT COUNT(*) AS c FROM edges WHERE (source_id = ? OR target_id = ?) "
                "AND NOT (source_id = ? AND target_id = ? AND relation = ?)",
                (nid, nid, source_id, target_id, relation),
            ).fetchone()
            if remaining["c"] == 0:
                _add(
                    "orphan_strand", "warn",
                    f"removing this edge would leave node {nid} with zero remaining "
                    f"edges (orphaned).",
                )

    # 5. Non-current target -- advisory only (retracted/superseded rows are
    #    audit history, not invalid targets).
    for nid in touched:
        if not nid:
            continue
        node = _fetch_node(conn, nid)
        if node is not None and node.get("is_current") == 0:
            _add(
                "non_current_target", "warn",
                f"node {nid} has is_current=0 (retracted/superseded) -- this row is "
                f"audit history.",
            )

    return findings, sorted(applied_overrides)


def _invariant_refuse_response(operation: str, phase: str, findings: list[dict],
                               extra: dict) -> dict:
    """Build the refuse-loud response for preview or execute. No
    confirmation_token is ever included -- refusal means no token is issued
    (preview) and a stale-but-valid token from an earlier, clean preview is
    not honored (execute)."""
    result = {
        "operation": operation,
        "phase": phase,
        "error": "invariant_refused",
        "invariant_findings": findings,
        "instruction": (
            "Refused: one or more blocking invariant(s) failed. Review "
            "invariant_findings. To proceed anyway, re-run with "
            "--override-invariant <name> for each named invariant you have "
            "verified is a false positive -- never blanket-override."
        ),
    }
    result.update(extra)
    return result


def _run_invariant_gate(conn: sqlite3.Connection, operation: str, phase: str,
                        op_args: dict, overrides: list[str] | None,
                        extra: dict) -> tuple[list[dict], list[str], dict | None]:
    """Run `_check_invariants` and, if any refuse-severity finding survives
    overrides, build the refuse-loud response. Shared by all five surgical
    ops so the gate logic (and its position ahead of the token comparison)
    lives in exactly one place.

    Returns (findings, applied_overrides, refuse_response_or_None). Callers
    that get a non-None refuse_response must `conn.close()` and return it
    immediately -- this helper never closes the connection itself, since
    the caller still needs `conn` for `_fetch_node` / follow-on queries in
    the non-refuse case.
    """
    findings, applied_overrides = _check_invariants(conn, operation, op_args, overrides)
    refuse_response = None
    if any(f["severity"] == "refuse" for f in findings):
        refuse_response = _invariant_refuse_response(operation, phase, findings, extra)
    return findings, applied_overrides, refuse_response


# ── Operations ───────────────────────────────────────────────────────────


# ── Confidence computation (uses shared constants from engram_confidence.py) ──

def _compute_confidence_for_node(conn: sqlite3.Connection, node: dict) -> float | None:
    """Recompute confidence from formula inputs for a single node.

    Returns the computed confidence, or None for types that don't have confidence
    (definitions, questions, goals, etc.).
    """
    ntype = node["type"]

    # Types with no confidence
    if ntype in ("definition", "question", "goal", "goal_tension",
                 "feeling_report", "contradiction", "evidence", "person",
                 "lesson", "task"):
        return None

    # Axioms are definitionally 1.0
    if ntype == "axiom":
        return 1.0

    # Observations: confidence from quote_type + source_class
    if ntype in ("observation_factual", "observation_predictive"):
        qt = node.get("quote_type")
        if not qt or qt not in CONFIDENCE_MAP:
            return 0.5  # fallback
        meta = json.loads(node.get("metadata") or "{}")
        source_class = meta.get("source_class", "external")

        if source_class == "user_stated":
            conf = CONFIDENCE_MAP["official_statement"]
        else:
            conf = CONFIDENCE_MAP[qt]
            if source_class == "introspective":
                conf = conf * SOURCE_CLASS_CONFIDENCE_DISCOUNT["introspective"]

        if ntype == "observation_predictive":
            conf = min(conf, PREDICTIVE_CONFIDENCE_CAP)
        return round(conf, 3)

    # Derivations and theories: confidence from premises + reasoning_type
    if ntype in ("derivation", "theory"):
        # Formula-input premises: CASCADE_RELATIONS (derives_from +
        # acknowledged_derives_from), matching engram_core.py's own canonical
        # recompute (#2349 blocker 2) — deliberately NOT CITATION_SUPPORT_RELATIONS,
        # which also carries supported_by and answers a different question
        # ("which premises were affected"), not "what does the formula read".
        _rels = sorted(CASCADE_RELATIONS)
        premises = conn.execute(
            "SELECT target_id FROM edges WHERE source_id = ? AND relation IN "
            f"({','.join('?' for _ in _rels)})",
            (node["id"], *_rels),
        ).fetchall()
        if not premises:
            return 0.5  # no premises found

        confidences = []
        for row in premises:
            pnode = conn.execute(
                "SELECT confidence FROM nodes WHERE id = ?", (row["target_id"],)
            ).fetchone()
            if pnode and pnode["confidence"] is not None:
                confidences.append(pnode["confidence"])

        if not confidences:
            return 0.5

        # Get reasoning_type from logical_chain metadata or node metadata
        meta = json.loads(node.get("metadata") or "{}")
        reasoning_type = meta.get("reasoning_type")

        if reasoning_type and reasoning_type in REASONING_CLASS:
            rclass = REASONING_CLASS[reasoning_type]
            discount = REASONING_DISCOUNT[reasoning_type]

            if rclass == "inductive_corroboration":
                product = 1.0
                for c in confidences:
                    product *= (1.0 - c)
                conf = (1.0 - product) * discount
            elif rclass == "abductive":
                cap = ABDUCTIVE_CONFIDENCE_CAP.get(reasoning_type, 0.85)
                conf = min(min(confidences) * discount, cap)
            else:
                conf = min(confidences) * discount
        else:
            # Legacy fallback: chain mode
            conf = min(confidences) * 0.95

        # Theory discount
        if ntype == "theory":
            conf = conf * 0.90

        return round(conf, 3)

    # Conjectures: keep existing confidence (set at creation, not formula-derived)
    if ntype == "conjecture":
        return node.get("confidence", 0.40)

    return 0.5  # unknown type fallback


def _get_downstream_nodes(conn: sqlite3.Connection, node_id: str) -> list[str]:
    """Get all nodes that depend on this node (follow edges in reverse: target→source).

    Ranges over CITATION_SUPPORT_RELATIONS (derives_from, supported_by,
    acknowledged_derives_from) — the same one-hop premise relation set
    engram_core.py's own `_direct_premise_ids` uses, mirrored in the reverse
    edge direction. `cites` is deliberately excluded: it is not a premise
    relation (see CITATION_SUPPORT_RELATIONS' own comment in engram_core.py),
    and including it walked into nodes whose confidence formula never reads
    the changed node at all.
    """
    _rels = sorted(CITATION_SUPPORT_RELATIONS)
    rows = conn.execute(
        "SELECT source_id FROM edges WHERE target_id = ? AND relation IN "
        f"({','.join('?' for _ in _rels)})",
        (node_id, *_rels),
    ).fetchall()
    return [r["source_id"] for r in rows]


def recompute_confidence(phase: str, node_id: str,
                         reason: str, bug_ref: str, token: str = "",
                         overrides: list[str] | None = None) -> dict:
    """Recompute a node's confidence from its formula inputs and cascade downstream.

    Does NOT accept an arbitrary number — recalculates from quote_type (observations),
    reasoning_type + premise confidences (derivations), or definitional value (axioms).
    Then BFS-cascades to all downstream dependents.
    """
    conn = _get_db()
    node = _fetch_node(conn, node_id)
    if node is None:
        return {"error": f"Node {node_id} not found"}

    old_conf = node["confidence"]
    new_conf = _compute_confidence_for_node(conn, node)

    # BFS to find all downstream nodes that would be affected
    cascade_preview = []
    visited = set()
    queue = [node_id]
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        downstream = _get_downstream_nodes(conn, current)
        for ds_id in downstream:
            if ds_id not in visited:
                ds_node = _fetch_node(conn, ds_id)
                if ds_node:
                    ds_old = ds_node["confidence"]
                    ds_new = _compute_confidence_for_node(conn, ds_node)
                    if ds_old != ds_new:
                        cascade_preview.append({
                            "node_id": ds_id,
                            "type": ds_node["type"],
                            "claim": (ds_node.get("claim") or "")[:80],
                            "old_confidence": ds_old,
                            "new_confidence": ds_new,
                        })
                queue.append(ds_id)

    op_args = {"node_id": node_id, "reason": reason, "bug_ref": bug_ref}

    findings, applied_overrides, refuse = _run_invariant_gate(
        conn, "recompute_confidence", phase, op_args, overrides,
        {"node_id": node_id, "reason": reason, "bug_ref": bug_ref},
    )
    if refuse:
        conn.close()
        return refuse

    expected_token = _generate_confirmation_token("recompute_confidence", op_args)

    result = {
        "operation": "recompute_confidence",
        "node_id": node_id,
        "node_type": node["type"],
        "claim": (node.get("claim") or "")[:120],
        "old_confidence": old_conf,
        "recomputed_confidence": new_conf,
        "changed": old_conf != new_conf,
        "cascade_affected": len(cascade_preview),
        "cascade_preview": cascade_preview[:20],  # cap preview at 20
        "reason": reason,
        "bug_ref": bug_ref,
        "confirmation_token": expected_token,
        "invariant_findings": findings,
    }

    if phase == "preview":
        result["phase"] = "preview"
        if old_conf == new_conf and not cascade_preview:
            result["instruction"] = "No changes needed — confidence already matches formula."
        else:
            result["instruction"] = (
                f"Review the changes above. To execute, run with "
                f"--token {expected_token}"
            )
        conn.close()
        return result

    # Execute phase
    if token != expected_token:
        conn.close()
        return {"error": "Token mismatch", "expected": expected_token, "got": token}

    git_before = _git_snapshot_before(
        f"recompute_confidence on {node_id} (ref: {bug_ref})"
    )
    result["git_before"] = git_before

    # Update the root node
    updates_applied = []
    if old_conf != new_conf:
        old_history = node.get("confidence_history", "[]")
        try:
            history = json.loads(old_history) if old_history else []
        except (json.JSONDecodeError, TypeError):
            history = []
        history.append({
            "timestamp": _now(),
            "value": new_conf,
            "reason": f"[surgical recompute] {reason} (ref: {bug_ref})",
        })
        conn.execute(
            "UPDATE nodes SET confidence = ?, confidence_history = ? WHERE id = ?",
            (new_conf, json.dumps(history), node_id),
        )
        updates_applied.append({"node_id": node_id, "old": old_conf, "new": new_conf})

        _log_edit(conn, "surgical_recompute_confidence", node_id, node["type"], {
            "old_confidence": old_conf,
            "recomputed_confidence": new_conf,
            "reason": reason,
            "bug_ref": bug_ref,
            "invariant_overrides": applied_overrides,
        })

    # BFS cascade: recompute all downstream dependents
    visited_exec = {node_id}
    queue_exec = _get_downstream_nodes(conn, node_id)
    while queue_exec:
        ds_id = queue_exec.pop(0)
        if ds_id in visited_exec:
            continue
        visited_exec.add(ds_id)

        ds_node = _fetch_node(conn, ds_id)
        if not ds_node:
            continue

        ds_old = ds_node["confidence"]
        ds_new = _compute_confidence_for_node(conn, ds_node)

        if ds_old != ds_new and ds_new is not None:
            ds_history = ds_node.get("confidence_history", "[]")
            try:
                history = json.loads(ds_history) if ds_history else []
            except (json.JSONDecodeError, TypeError):
                history = []
            history.append({
                "timestamp": _now(),
                "value": ds_new,
                "reason": f"[cascade from {node_id}] {reason} (ref: {bug_ref})",
            })
            conn.execute(
                "UPDATE nodes SET confidence = ?, confidence_history = ? WHERE id = ?",
                (ds_new, json.dumps(history), ds_id),
            )
            updates_applied.append({"node_id": ds_id, "old": ds_old, "new": ds_new})

            _log_edit(conn, "surgical_cascade_recompute", ds_id, ds_node["type"], {
                "old_confidence": ds_old,
                "recomputed_confidence": ds_new,
                "cascade_from": node_id,
                "reason": reason,
                "bug_ref": bug_ref,
            })

        # Continue BFS from this node
        for further in _get_downstream_nodes(conn, ds_id):
            if further not in visited_exec:
                queue_exec.append(further)

    conn.commit()
    conn.close()

    git_after = _git_snapshot_after("recompute_confidence", node_id)
    result["phase"] = "executed"
    result["git_after"] = git_after
    result["updates_applied"] = updates_applied
    result["total_updated"] = len(updates_applied)
    return result


def metadata_patch(phase: str, node_id: str, json_patch: dict,
                   reason: str, bug_ref: str, token: str = "",
                   overrides: list[str] | None = None) -> dict:
    """Patch specific keys in a node's metadata JSON."""
    conn = _get_db()
    node = _fetch_node(conn, node_id)
    if node is None:
        return {"error": f"Node {node_id} not found"}

    old_meta = json.loads(node.get("metadata") or "{}")
    new_meta = {**old_meta, **json_patch}

    op_args = {"node_id": node_id, "json_patch": json_patch,
               "reason": reason, "bug_ref": bug_ref}

    findings, applied_overrides, refuse = _run_invariant_gate(
        conn, "metadata_patch", phase, op_args, overrides,
        {"node_id": node_id, "reason": reason, "bug_ref": bug_ref},
    )
    if refuse:
        conn.close()
        return refuse

    expected_token = _generate_confirmation_token("metadata_patch", op_args)

    result = {
        "operation": "metadata_patch",
        "node_id": node_id,
        "node_type": node["type"],
        "claim": (node.get("claim") or "")[:120],
        "old_metadata": old_meta,
        "new_metadata": new_meta,
        "patch_applied": json_patch,
        "reason": reason,
        "bug_ref": bug_ref,
        "confirmation_token": expected_token,
        "invariant_findings": findings,
    }

    if phase == "preview":
        result["phase"] = "preview"
        result["instruction"] = (
            f"Review the change above. To execute, run with "
            f"--token {expected_token}"
        )
        conn.close()
        return result

    if token != expected_token:
        conn.close()
        return {"error": "Token mismatch", "expected": expected_token, "got": token}

    git_before = _git_snapshot_before(
        f"metadata_patch on {node_id} (ref: {bug_ref})"
    )
    result["git_before"] = git_before

    conn.execute(
        "UPDATE nodes SET metadata = ? WHERE id = ?",
        (json.dumps(new_meta), node_id),
    )

    _log_edit(conn, "surgical_metadata_patch", node_id, node["type"], {
        "old_metadata": old_meta,
        "patch_applied": json_patch,
        "reason": reason,
        "bug_ref": bug_ref,
        "invariant_overrides": applied_overrides,
    })

    conn.commit()
    conn.close()

    git_after = _git_snapshot_after("metadata_patch", node_id)
    result["phase"] = "executed"
    result["git_after"] = git_after
    return result


def field_patch(phase: str, node_id: str, field_name: str, new_value: str,
                reason: str, bug_ref: str, token: str = "",
                overrides: list[str] | None = None) -> dict:
    """Patch a specific field on a node (claim, quoted_text, interpretation, etc.)."""
    # Allowlist of patchable fields — prevent accidental ID or type changes
    ALLOWED_FIELDS = {
        "claim", "quoted_text", "interpretation", "logical_chain",
        "source_url", "source_title", "source_domain", "source_date",
        "predicted_event", "status", "importance_base", "importance_score",
    }
    if field_name not in ALLOWED_FIELDS:
        return {"error": f"Field '{field_name}' not in allowed list: {sorted(ALLOWED_FIELDS)}"}

    conn = _get_db()
    node = _fetch_node(conn, node_id)
    if node is None:
        return {"error": f"Node {node_id} not found"}

    old_value = node.get(field_name)

    op_args = {"node_id": node_id, "field_name": field_name,
               "new_value": new_value, "reason": reason, "bug_ref": bug_ref}

    findings, applied_overrides, refuse = _run_invariant_gate(
        conn, "field_patch", phase, op_args, overrides,
        {"node_id": node_id, "field": field_name, "reason": reason, "bug_ref": bug_ref},
    )
    if refuse:
        conn.close()
        return refuse

    expected_token = _generate_confirmation_token("field_patch", op_args)

    result = {
        "operation": "field_patch",
        "node_id": node_id,
        "node_type": node["type"],
        "field": field_name,
        "old_value": str(old_value)[:300] if old_value is not None else None,
        "new_value": str(new_value)[:300],
        "reason": reason,
        "bug_ref": bug_ref,
        "confirmation_token": expected_token,
        "invariant_findings": findings,
    }

    if phase == "preview":
        result["phase"] = "preview"
        result["instruction"] = (
            f"Review the change above. To execute, run with "
            f"--token {expected_token}"
        )
        conn.close()
        return result

    if token != expected_token:
        conn.close()
        return {"error": "Token mismatch", "expected": expected_token, "got": token}

    git_before = _git_snapshot_before(
        f"field_patch {field_name} on {node_id} (ref: {bug_ref})"
    )
    result["git_before"] = git_before

    conn.execute(
        f"UPDATE nodes SET {field_name} = ? WHERE id = ?",
        (new_value, node_id),
    )

    _log_edit(conn, "surgical_field_patch", node_id, node["type"], {
        "field": field_name,
        "old_value": str(old_value)[:500] if old_value is not None else None,
        "new_value": str(new_value)[:500],
        "reason": reason,
        "bug_ref": bug_ref,
        "invariant_overrides": applied_overrides,
    })

    conn.commit()
    conn.close()

    git_after = _git_snapshot_after("field_patch", node_id)
    result["phase"] = "executed"
    result["git_after"] = git_after
    return result


def edge_add(phase: str, source_id: str, target_id: str, relation: str,
             reason: str, bug_ref: str, token: str = "",
             overrides: list[str] | None = None) -> dict:
    """Add a missing edge between two nodes."""
    ALLOWED_RELATIONS = {
        "cites", "supported_by", "contradicts", "resolves",
        "derives_from", "supersedes", "retracts", "tensions",
    }
    if relation not in ALLOWED_RELATIONS:
        return {"error": f"Relation '{relation}' not in allowed list: {sorted(ALLOWED_RELATIONS)}"}

    conn = _get_db()
    source = _fetch_node(conn, source_id)
    target = _fetch_node(conn, target_id)
    if source is None:
        return {"error": f"Source node {source_id} not found"}
    if target is None:
        return {"error": f"Target node {target_id} not found"}

    # Check for existing edge
    existing = conn.execute(
        "SELECT * FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
        (source_id, target_id, relation),
    ).fetchone()
    if existing:
        conn.close()
        return {"error": f"Edge already exists: {source_id} --{relation}--> {target_id}"}

    op_args = {"source_id": source_id, "target_id": target_id,
               "relation": relation, "reason": reason, "bug_ref": bug_ref}

    findings, applied_overrides, refuse = _run_invariant_gate(
        conn, "edge_add", phase, op_args, overrides,
        {"source_id": source_id, "target_id": target_id, "relation": relation,
         "reason": reason, "bug_ref": bug_ref},
    )
    if refuse:
        conn.close()
        return refuse

    expected_token = _generate_confirmation_token("edge_add", op_args)

    result = {
        "operation": "edge_add",
        "source_id": source_id,
        "source_type": source["type"],
        "source_claim": (source.get("claim") or "")[:100],
        "target_id": target_id,
        "target_type": target["type"],
        "target_claim": (target.get("claim") or "")[:100],
        "relation": relation,
        "reason": reason,
        "bug_ref": bug_ref,
        "confirmation_token": expected_token,
        "invariant_findings": findings,
    }

    if phase == "preview":
        result["phase"] = "preview"
        result["instruction"] = (
            f"Review the change above. To execute, run with "
            f"--token {expected_token}"
        )
        conn.close()
        return result

    if token != expected_token:
        conn.close()
        return {"error": "Token mismatch", "expected": expected_token, "got": token}

    git_before = _git_snapshot_before(
        f"edge_add {source_id} --{relation}--> {target_id} (ref: {bug_ref})"
    )
    result["git_before"] = git_before

    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, ?)",
        (source_id, target_id, relation, _now()),
    )

    _log_edit(conn, "surgical_edge_add", source_id, source["type"], {
        "target_id": target_id,
        "relation": relation,
        "reason": reason,
        "bug_ref": bug_ref,
        "invariant_overrides": applied_overrides,
    })

    conn.commit()
    conn.close()

    git_after = _git_snapshot_after("edge_add", f"{source_id}->{target_id}")
    result["phase"] = "executed"
    result["git_after"] = git_after
    return result


def edge_remove(phase: str, source_id: str, target_id: str, relation: str,
                reason: str, bug_ref: str, token: str = "",
                overrides: list[str] | None = None) -> dict:
    """Remove an incorrect edge between two nodes."""
    conn = _get_db()

    existing = conn.execute(
        "SELECT * FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
        (source_id, target_id, relation),
    ).fetchone()
    if not existing:
        conn.close()
        return {"error": f"Edge not found: {source_id} --{relation}--> {target_id}"}

    source = _fetch_node(conn, source_id)
    target = _fetch_node(conn, target_id)

    op_args = {"source_id": source_id, "target_id": target_id,
               "relation": relation, "reason": reason, "bug_ref": bug_ref}

    findings, applied_overrides, refuse = _run_invariant_gate(
        conn, "edge_remove", phase, op_args, overrides,
        {"source_id": source_id, "target_id": target_id, "relation": relation,
         "reason": reason, "bug_ref": bug_ref},
    )
    if refuse:
        conn.close()
        return refuse

    expected_token = _generate_confirmation_token("edge_remove", op_args)

    result = {
        "operation": "edge_remove",
        "source_id": source_id,
        "source_type": source["type"] if source else "unknown",
        "source_claim": (source.get("claim") or "")[:100] if source else "",
        "target_id": target_id,
        "target_type": target["type"] if target else "unknown",
        "target_claim": (target.get("claim") or "")[:100] if target else "",
        "relation": relation,
        "reason": reason,
        "bug_ref": bug_ref,
        "confirmation_token": expected_token,
        "invariant_findings": findings,
    }

    if phase == "preview":
        result["phase"] = "preview"
        result["instruction"] = (
            f"Review the change above. To execute, run with "
            f"--token {expected_token}"
        )
        conn.close()
        return result

    if token != expected_token:
        conn.close()
        return {"error": "Token mismatch", "expected": expected_token, "got": token}

    git_before = _git_snapshot_before(
        f"edge_remove {source_id} --{relation}--> {target_id} (ref: {bug_ref})"
    )
    result["git_before"] = git_before

    conn.execute(
        "DELETE FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
        (source_id, target_id, relation),
    )

    _log_edit(conn, "surgical_edge_remove", source_id,
              source["type"] if source else "unknown", {
        "target_id": target_id,
        "relation": relation,
        "reason": reason,
        "bug_ref": bug_ref,
        "invariant_overrides": applied_overrides,
    })

    conn.commit()
    conn.close()

    git_after = _git_snapshot_after("edge_remove", f"{source_id}->{target_id}")
    result["phase"] = "executed"
    result["git_after"] = git_after
    return result


# ── CLI interface ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ENGRAM Surgical Tools — data correction with audit trail",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("phase", choices=["preview", "execute"],
                        help="preview (read-only) or execute (mutating)")
    parser.add_argument("operation",
                        choices=["recompute_confidence", "metadata_patch",
                                 "field_patch", "edge_add", "edge_remove"],
                        help="Which surgical operation to perform")
    parser.add_argument("--node-id", help="Target node ID")
    parser.add_argument("--source-id", help="Source node for edge ops")
    parser.add_argument("--target-id", help="Target node for edge ops")
    parser.add_argument("--relation", help="Edge relation type")
    parser.add_argument("--field", help="Field name for field_patch")
    parser.add_argument("--value", help="New value (confidence float, JSON patch, or field value)")
    parser.add_argument("--reason", required=True, help="Why this surgery is needed")
    parser.add_argument("--bug-ref", required=True,
                        help="ENGRAM node ID (qu_ or ob_) documenting the bug")
    parser.add_argument("--token", default="",
                        help="Confirmation token from preview phase")
    parser.add_argument("--override-invariant", action="append", default=[],
                        dest="override_invariant", metavar="NAME",
                        help="Downgrade a named refuse-severity invariant finding to "
                             "warn (repeatable). The default is refuse; name the "
                             "invariant you have verified is a false positive -- "
                             "never blanket-override.")

    args = parser.parse_args()

    # Validate bug-ref format
    if not (args.bug_ref.startswith("qu_") or args.bug_ref.startswith("ob_") or
            args.bug_ref.startswith("ls_") or args.bug_ref.startswith("dv_")):
        print(json.dumps({"error": "Bug ref must be an ENGRAM node ID (qu_, ob_, ls_, or dv_)"}))
        sys.exit(1)

    if args.operation == "recompute_confidence":
        if not args.node_id:
            print(json.dumps({"error": "recompute_confidence requires --node-id"}))
            sys.exit(1)
        result = recompute_confidence(
            args.phase, args.node_id,
            args.reason, args.bug_ref, args.token,
            args.override_invariant,
        )

    elif args.operation == "metadata_patch":
        if not args.node_id or not args.value:
            print(json.dumps({"error": "metadata_patch requires --node-id and --value (JSON)"}))
            sys.exit(1)
        try:
            patch = json.loads(args.value)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"--value must be valid JSON: {e}"}))
            sys.exit(1)
        result = metadata_patch(
            args.phase, args.node_id, patch,
            args.reason, args.bug_ref, args.token,
            args.override_invariant,
        )

    elif args.operation == "field_patch":
        if not args.node_id or not args.field or args.value is None:
            print(json.dumps({"error": "field_patch requires --node-id, --field, and --value"}))
            sys.exit(1)
        result = field_patch(
            args.phase, args.node_id, args.field, args.value,
            args.reason, args.bug_ref, args.token,
            args.override_invariant,
        )

    elif args.operation == "edge_add":
        if not args.source_id or not args.target_id or not args.relation:
            print(json.dumps({"error": "edge_add requires --source-id, --target-id, --relation"}))
            sys.exit(1)
        result = edge_add(
            args.phase, args.source_id, args.target_id, args.relation,
            args.reason, args.bug_ref, args.token,
            args.override_invariant,
        )

    elif args.operation == "edge_remove":
        if not args.source_id or not args.target_id or not args.relation:
            print(json.dumps({"error": "edge_remove requires --source-id, --target-id, --relation"}))
            sys.exit(1)
        result = edge_remove(
            args.phase, args.source_id, args.target_id, args.relation,
            args.reason, args.bug_ref, args.token,
            args.override_invariant,
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
