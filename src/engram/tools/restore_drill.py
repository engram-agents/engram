"""Restore drill: prove that the committed backup of the graph, and the transcript archive, restore (#4647 part 1).

Usage:
    python3 restore_drill.py [--home DIR] [--scratch DIR] [--sample N] [--no-embed] [--json]
                             [--dump PATH | --from-git] [--source DIR] [--assert-quiescent]

What it does, in a SCRATCH directory (never in the live home):
  1. Loads the text dump ``knowledge.sql`` into a fresh ``knowledge.db`` with Python's sqlite3
     module. This tool never runs the ``sqlite3`` command-line program (it is missing on some
     machines), and restore instructions should not use it either.
     Dump source: the working-tree ``knowledge.sql`` in the home (default), ``--from-git`` for
     ``git show HEAD:knowledge.sql``, or ``--dump PATH``. The report names the source and its
     mtime (or commit): a working-tree dump can be older than the last nap.
  2. Rebuilds the FTS index (``engram_backup.rebuild_fts_index``).
  3. Compares the restored rows with the live rows, table by table, keyed by primary key. Every
     column of every base table is compared except ``embedding`` and the tables the restore
     regenerates (FTS shadow tables, the sqlite-vec tables, SQLite's own ``sqlite_*`` tables);
     the output LISTS what was not compared. Each column is classified from the schema:
       write-once   claim text, quotes, ids, types, created_at, edge endpoints, log rows ...
                    A difference is a FAILURE, naming table, key and column.
       mutable      recall counts, last-recalled times, importance and utility scores, confidence and
                    similar bookkeeping. A difference is REPORTED with per-column counts, never failed:
                    a working seat changes them after the dump.
       lifecycle    nodes.status, is_current, superseded_by, resolved_by, and the write-once column
                    nodes.quoted_text. THE RULE: a difference is legal only if the live graph's own record
                    explains it as a change made after the dump: (i) an ``edit_history`` row for that node (or
                    the ``retracted_at`` stamp) dated after the dump time, AND (ii) the (restored, live) pair is a
                    transition the code can make (``_STATUS_TRANSITIONS`` and ``_RESOLVED_BY_RULES``, each with
                    the code path as the reason). Retraction and supersession are one way: retracted -> anything
                    else fails, any change to ``superseded_by`` fails. A difference with no record FAILS, naming
                    table, key and column. A record dated between the previous and this commit of an older
                    dump is REPORTED as "ambiguous: dump time unknown" (``ambiguous_count``), not failed.
       audit_gated  nodes.trust_tier, nodes.source_type and the metadata key trust_tier (``AUDIT_GATED_COLUMNS``,
                    one table, a PROVISIONAL ruling): shipped code changes them after creation, so a difference
                    passes only with a live ``edit_history`` row for that node dated after the dump time.
       keyed        nodes.metadata, compared as parsed JSON KEY BY KEY (``_compare_node_metadata``):
                    write-once keys fail on a difference, the retraction stamp keys follow the lifecycle
                    rule, named mutable keys are reported, and any other key is UNKNOWN: listed, and a
                    difference on it fails.
       unclassified a column this tool has no ruling for: treated as write-once, and listed, so
                    the design seat can rule on it.
     Rows written after the dump (in live, not in the dump) are REPORTED (``live_only``).
     A row in the dump that is missing from live is a FAILURE (rows are retracted, not deleted).
  4. First server connect, in a SUBPROCESS whose ENGRAM_HOME and HOME both point at the scratch
     directory (the data home is read from ENGRAM_HOME when the server code is imported, so an
     in-process call could touch the live home). A dump older than the code's schema is migrated
     in the scratch copy, and the report says so. This step runs BEFORE the re-embed (the
     migration adds columns the embedding text reads) and with the embedder switched off, so the
     connect itself never starts a background re-embed.
  5. Re-embeds every node that has embedding text (``_embed_texts``, the seam tests replace) and
     checks the restored copy: no node lacks a vector; a SAMPLE is embedded a second time and
     must equal the restored vectors within 1e-5 per element (``vector_tolerance``; the real model is
     not bit-repeatable across batch shapes). The model identity check is EXACT: the same model name
     and the same vector dimension in both passes, or the drill fails. The report carries the largest
     element difference seen (``vector_max_diff``). A live vector that differs from the restored one
     (embedded before its summary existed, and so on) is counted in ``live_vectors_stale``, never
     failed: the live vectors are not the reference.
  6. Replays the daily transcript archive (``archive_transcripts.replay``) into
     ``<scratch>/transcripts`` and compares each replayed file with the live transcript up to the
     archived offset (sha256 of the first N bytes). Bytes past the offset are reported
     (``transcript_live_only_bytes``), not failed.

Dump time (``dump_time_source`` in the report): the marker line ``-- engram-dumped-at: <UTC ISO time>`` that
``engram_backup.dump_stripped`` writes as the first line of a dump (``marker``, exact, preferred). The marker is
the CAPTURE time: the moment the dump's content was true of the live graph (the snapshot worker, which dumps a
captured copy later, stamps the time of the capture). A commit path that finds a new dump equal to the committed one
except for the marker keeps the old file and its old marker; for an older dump
with no marker, the window between the previous commit of ``knowledge.sql`` and this one (``commit-window``, no slack:
a record before the previous commit fails, one between the two commits is ambiguous, one after this commit passes; a
file with no previous commit has an UNKNOWN earlier bound, never an invented one); for a dump with no marker and no
repo, the file's mtime (``mtime-fallback``, the weakest source).

Known gaps (writers of the audited columns that leave NO ``edit_history`` row, see ``NO_AUDIT_WRITERS``, named by
function): ``engram_core._backfill_resolved_by`` (startup, NULL -> resolver), the ``engram_revision._resolve_impl``
self-heal (re-points ``resolved_by`` on a repeat resolve call), ``engram_core._run_db_one_time_setup`` (startup,
person ``trust_tier`` NULL -> 'unknown') and ``engram_core._backfill_source_type`` (startup, evidence ``source_type``).
A change they made is not told apart from corruption, so it fails; the columns are NOT classified mutable to absorb
it, and the failure names these writers as possible causes. Failure text never prints graph content: a write-once or
unknown metadata key and ``quoted_text`` are named with the value's type and length only.

The drill never creates, modifies or deletes a file in the live home. The live database is opened
read-only (``mode=ro``) when both ``-wal`` and ``-shm`` exist, and with ``immutable=1`` when neither does
(a read-only open of a quiescent WAL-mode database would itself create them). When exactly one of the two
exists the drill REFUSES (exit 2): measured, a read-only open of ``-wal`` without ``-shm`` creates ``-shm``,
and of ``-shm`` without ``-wal`` creates ``-wal`` and reads stale data. The drill records the size and mtime of
the live db, its side files and ``sessions/`` before and after. With ``--assert-quiescent`` (a
fixture, or a seat that is stopped) any change fails the drill and the live db's sha256 must be
unchanged; on a working seat the changes are listed as "observed during drill, not attributed".

Exit codes: 0 pass; 1 the drill FAILED (a comparison failed); 2 it could not run (no dump, an
unreadable db, a live-side sqlite error, a scratch that is not empty or lies in the live home). Nothing is
written to the live home on any exit.

Subprocess environment: an ALLOWLIST (``_scratch_env``), not a filter. The re-embed subprocess gets HF_HOME
(the existing model cache), HF_HUB_OFFLINE=1 and TRANSFORMERS_OFFLINE=1, so it never downloads. Measured on one
full run with the model cached (28.65 s): no file inside HF_HOME was created or modified. Anything it could
still write is limited to HF_HOME (a library's own bookkeeping); nothing is written under the live ENGRAM home.

Cost: 26.8 s on one seat with the model cached (a first run that has to load the model takes longer),
almost all of it the re-embed; run on demand, not per sleep.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import importlib.util
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
# The plugin root (installed) or src/engram (source) is the directory above tools/ in BOTH layouts.
_PKG_ROOT = os.path.dirname(_HERE)
# append, never insert(0): src/engram at position 0 would outrank the repo-root tools/ package.
if _PKG_ROOT not in sys.path:
    sys.path.append(_PKG_ROOT)


def _load_sibling(name: str, path: str):
    """Load a module by FILE path (not by package name, so no `tools`/name clash can shadow it)."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


owner_read = _load_sibling("_restore_drill_owner_read", os.path.join(_HERE, "_owner_read.py"))
at = _load_sibling("_restore_drill_archive_transcripts", os.path.join(_HERE, "archive_transcripts.py"))
engram_backup = _load_sibling("_restore_drill_engram_backup", os.path.join(_PKG_ROOT, "engram_backup.py"))

DEFAULT_SAMPLE = 50
STALE_TOLERANCE = 1e-5
# The sample re-embed is compared with the same tolerance. The real model is not bit-for-bit repeatable when
# the same text is encoded in a different batch (measured on the live seat: 1 of a 50-node sample differed
# from the first pass), so "exact" means: no element differs by more than this.
SAMPLE_TOLERANCE = 1e-5
# The unit of the tolerance, as `_max_abs_diff` computes it: the largest ABSOLUTE difference of any single
# element (not a cosine distance, not a norm).
VECTOR_TOLERANCE = {"value": SAMPLE_TOLERANCE, "unit": "maximum absolute difference per vector element"}
_MAX_FAIL_LINES_PER_TABLE = 20
_MARK = "@@DRILL@@"
_CONNECT_TIMEOUT_S = 600
_EMBED_TIMEOUT_S = 3600

# Tables the restore regenerates or SQLite owns: never compared, always listed.
_EXCLUDED_PREFIXES = ("nodes_fts", "vec_nodes", "sqlite_")
_EXCLUDED_COLUMNS = ("embedding",)


# ------------------------------------------------------------------ classification

# Per table: columns known to be write-once, columns known to be mutable. A column in neither is
# UNCLASSIFIED and is treated as write-once. A table in WRITE_ONCE_TABLES is an append-only log:
# every column is write-once. A table in MUTABLE_TABLES is bookkeeping: every column except the
# listed write-once ones is mutable.
_WRITE_ONCE = {
    "nodes": (
        "id", "type", "claim", "created_at", "source_url", "source_title", "source_domain",
        "source_date", "source_accessed", "content_snippet", "evidence_id", "quoted_text",
        "interpretation", "quote_type", "predicted_event", "logical_chain", "supersedes",
        "reported_state", "trigger_text",
    ),
    "edges": ("id", "source_id", "target_id", "relation", "created_at"),
    "cascade_markers": ("id", "node_id", "cascade_event", "root_node", "root_op", "created_at"),
    "focus_sets": ("name", "created_at"),
    "sequences": ("node_type",),
    "diagnostic_history": ("id", "timestamp", "turn", "checkpoint_mode"),
}
_MUTABLE = {
    "nodes": (
        "confidence", "confidence_history", "importance_base", "importance_score", "recall_turn", "recall_count",
        "memory_status", "utility_score", "last_assessed_turn", "last_assessed_at", "focused_at",
        "focus_reason", "recall_summary", "recall_keywords", "needs_tightening", "summary_missing",
    ),
    "cascade_markers": ("status", "resolution_kind", "resolution_replacement_id", "resolved_at"),
    "focus_sets": ("node_ids", "description", "last_loaded_at", "load_count"),
    "sequences": ("last_num",),
    # measured on the live seat (first real run, 2026-11-14): the newest row's metrics were rewritten in place
    # after the dump (id 714), so the table is not strictly append-only.
    "diagnostic_history": ("metrics",),
}
_WRITE_ONCE_TABLES = ("edit_history", "tool_timing")
_MUTABLE_TABLES = ("focus_state", "embedding_restore_state")

WRITE_ONCE = "write_once"
MUTABLE = "mutable"
LIFECYCLE = "lifecycle"
AUDIT_GATED = "audit_gated"
KEYED = "keyed"
UNCLASSIFIED = "unclassified"

_LIFECYCLE = {"nodes": ("status", "is_current", "superseded_by", "resolved_by")}
# Write-once columns whose difference is checked by `_check_node_row` against the audit record instead of failing
# outright: a requote after the dump is legal, and the live graph says so.
_AUDITED_WRITE_ONCE = {("nodes", "quoted_text")}
_KEYED = {"nodes": ("metadata",)}

# THE ONE TABLE for the columns and metadata keys that shipped code changes after creation and that the design seat
# has not (yet) ruled on (provisional, round 4). A difference passes ONLY with a live `edit_history` row for that node
# (one of `actions`, any action for "*") dated after the dump time: the same audit-record rule as the lifecycle
# columns, with no (restored, live) transition table. A metadata key is addressed as ("nodes.metadata", key). Moving a
# line out of here (to `_MUTABLE`, say) is the whole change if the ruling differs.
# Writers, and whether each leaves an audit row (function names; line numbers rot):
#   trust_tier       engram_trust verb that sets a tier        'trust_tier_set'              audit row: yes
#                    engram_revision.engram_supersede
#                    self-anchor transfer (also is_current)    'self_anchor_transferred'     audit row: yes
#                    engram_core._run_db_one_time_setup
#                    NULL -> 'unknown' for person nodes         (none)                        audit row: NO, known gap
#   source_type      engram_core._backfill_source_type
#                    NULL -> inferred value for evidence nodes (none)                        audit row: NO, known gap
#   metadata.trust_tier  written at person creation only (engram_trust); no shipped code rewrites it, but the
#                    column it mirrors changes, so the key is gated with the column.
# `source_type` has no writer that records an audit action, so any row for the node after the dump explains it
# ("*"): a provisional weakness the ruling should tighten.
# `no_audit` names the writers that change it with no audit row (the possible causes a failure names).
AUDIT_GATED_COLUMNS = {
    ("nodes", "trust_tier"): {"actions": ("trust_tier_set", "self_anchor_transferred"),
                              "no_audit": ("engram_core._run_db_one_time_setup",)},
    ("nodes", "source_type"): {"actions": ("*",), "no_audit": ("engram_core._backfill_source_type",)},
    ("nodes.metadata", "trust_tier"): {"actions": ("trust_tier_set", "self_anchor_transferred"),
                                       "no_audit": ("engram_core._run_db_one_time_setup",)},
}


def classify(table: str, column: str) -> str:
    """write_once | mutable | lifecycle | audit_gated | keyed | unclassified (the last is TREATED as write-once)."""
    if table in _WRITE_ONCE_TABLES:
        return WRITE_ONCE
    if column in _LIFECYCLE.get(table, ()):
        return LIFECYCLE
    if (table, column) in AUDIT_GATED_COLUMNS:
        return AUDIT_GATED
    if column in _KEYED.get(table, ()):
        return KEYED
    if column in _WRITE_ONCE.get(table, ()):
        return WRITE_ONCE
    if column in _MUTABLE.get(table, ()) or table in _MUTABLE_TABLES:
        return MUTABLE
    return UNCLASSIFIED


# ------------------------------------------------------------------ report

class DrillRefused(Exception):
    """The drill could not run (exit 2). Raised before anything is written."""


@dataclass
class DrillReport:
    nodes_restored: int = 0
    rows_compared: int = 0
    live_only: list = field(default_factory=list)  # node ids written after the dump
    live_only_counts: dict = field(default_factory=dict)  # table -> rows written after the dump
    mutable_diffs: dict = field(default_factory=dict)  # "table.column" -> rows that differ
    # the audited columns (lifecycle, quoted_text): column -> {passed, ambiguous, failed_no_record,
    # failed_before_dump, failed_transition}
    lifecycle: dict = field(default_factory=dict)
    lifecycle_transitions: dict = field(default_factory=dict)  # "a -> b" -> {count, reason}: the named moves
    # differences explained only by a record BETWEEN the two commits of knowledge.sql (dump time unknown):
    # reported, not failed. Each: {table, key, column, recorded, difference}
    ambiguous: list = field(default_factory=list)
    known_gaps: list = field(default_factory=lambda: list(NO_AUDIT_WRITERS))  # writers that leave no audit row
    metadata_mutable_diffs: dict = field(default_factory=dict)  # metadata key -> rows that differ (reported)
    metadata_unknown: dict = field(default_factory=dict)  # metadata key the classification does not know -> nodes
    dump_time_source: str = ""  # "marker" | "commit-window" | "mtime-fallback"
    dump_time: str = ""  # the latest time the dump can have been taken (the marker, this commit, the mtime)
    dump_time_earliest: str = ""  # the earliest ("" = unknown: knowledge.sql has no previous commit)
    dump_time_note: str = ""
    vector_tolerance: dict = field(default_factory=lambda: dict(VECTOR_TOLERANCE))
    vector_max_diff: float = 0.0  # the largest element difference seen between the two embedding passes
    embedding_model: str = ""
    vectors_missing: list = field(default_factory=list)
    vectors_embedded: int = 0
    vector_sample_checked: int = 0
    live_vectors_stale: int = 0
    transcripts_replayed: int = 0
    transcript_live_only_bytes: int = 0
    transcripts_live_missing: int = 0
    seconds: float = 0.0
    failures: list = field(default_factory=list)
    skipped: list = field(default_factory=list)  # steps that did not run, and why
    excluded: dict = field(default_factory=dict)  # what was NOT compared
    classification: dict = field(default_factory=dict)  # table -> {write_once, mutable, unclassified}
    dump_source: dict = field(default_factory=dict)
    schema: dict = field(default_factory=dict)
    live_home_observed: list = field(default_factory=list)  # changes seen during the drill, not attributed
    live_sha256_before: str = ""
    live_sha256_after: str = ""
    scratch: str = ""
    embedding: str = ""
    sections: list = field(default_factory=list)  # one summary line per section

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0

    @property
    def ambiguous_count(self) -> int:
        return len(self.ambiguous)

    def to_json(self) -> dict:
        d = asdict(self)
        d["passed"] = not self.failures
        d["ambiguous_count"] = self.ambiguous_count
        return d


def _fail(report: DrillReport, message: str) -> None:
    report.failures.append(message)


# ------------------------------------------------------------------ lifecycle columns, gated by the audit record
#
# THE RULE: a difference between the restore and live is legal ONLY if the live graph's own record explains it
# as a change made after the dump. (i) The live audit trail (an `edit_history` row for that node, or the
# `retracted_at` stamp a retraction writes) shows the change after the dump time; AND (ii) the (restored, live)
# pair is a transition the code can make (the named tables below, each with its code path as the reason).
# A difference with no such record FAILS, naming table, key and column.
#
# How edit_history is written: `engram_core._log_edit` (best effort: it swallows its own exception, so a row can
# be missing even where a writer calls it) and a few direct INSERTs; one row per change with `timestamp` (UTC ISO,
# `engram_core._now`), `action`, `node_id` (the node that CHANGED: the superseded node, the resolved target, the
# retracted node), `node_type` and a `details` JSON (`field` for `surgical_field_patch`).
#
# Every write to nodes.status / is_current / superseded_by / resolved_by / quoted_text in the shipped tree
# (src/engram, searched for every `UPDATE nodes` statement and every helper that builds one; there is no
# un-retract verb and no migration or repair tool that resets them), with whether it writes an audit row:
#   engram_revision.py  retract    (4384, 4546)  status X -> 'retracted', is_current 1 -> 0, metadata.retracted_at
#                       AUDIT ROW: yes, action 'retracted' (4687; 4565 for a paired prediction)         FORWARD
#   engram_revision.py  supersede  (3624)        is_current 1 -> 0, superseded_by NULL -> new id
#                       AUDIT ROW: yes, action 'superseded' (3850)                                      FORWARD
#   engram_cornerstone.py outgrow  (515)         is_current 1 -> 0, superseded_by NULL -> new id
#                       AUDIT ROW: yes, action 'superseded' (527)                                       FORWARD
#   engram_core.py      _backfill_resolved_by (4013)  resolved_by NULL -> resolver, run at startup
#                       AUDIT ROW: NO. A KNOWN GAP (see NO_AUDIT_WRITERS).                              FORWARD
#   engram_core.py      _recompute_resolution_target (4124/4128), run by retract / supersede / the startup
#                       reconciler: status -> 'open' with resolved_by -> NULL when no live resolver remains (BACKWARD),
#                       resolved_by A -> B and status resolved <-> partially_resolved (SIDEWAYS)
#                       AUDIT ROW: yes, action 'resolution_recomputed' (4130); retract also writes 'reopened' (4692)
#   engram_revision.py  _resolve_impl (3284): status open/partially_resolved -> resolved / partially_resolved / a
#                       prediction outcome, last writer wins between outcomes; resolved_by NULL -> X or A -> B
#                       AUDIT ROW: yes, action 'resolved' (3287)                                  FORWARD and SIDEWAYS
#   engram_revision.py  _resolve_impl self-heal (3223): resolved_by re-pointed to the best live resolver on a repeat
#                       resolve call
#                       AUDIT ROW: NO. A KNOWN GAP (see NO_AUDIT_WRITERS).                                SIDEWAYS
#   engram_tasks.py     engram_update_task (543): task status planned/active/done/blocked, any order
#                       AUDIT ROW: yes, action 'status_<old>_to_<new>' (563)                              SIDEWAYS
#   tools/surgical.py   field_patch (820): `status` is on its allowlist, arbitrary old -> new (so is quoted_text)
#                       AUDIT ROW: yes, action 'surgical_field_patch' with details.field (824)
#   engram_observation.py requote (3179): quoted_text old -> new, write-once otherwise
#                       AUDIT ROW: yes, action 'requoted' (3190)
# So "one way" holds for retraction and supersession, and does NOT hold for the resolution family or task status:
# those moves are named below, keyed by the (restored, live) value pair, with the code path as the reason.

_RESOLUTION_NO_AUDIT_WRITERS = (
    "engram_core._backfill_resolved_by: sets resolved_by NULL -> resolver at startup and writes no edit_history row",
    "engram_revision._resolve_impl self-heal: re-points resolved_by on a repeat resolve call and writes no "
    "edit_history row",
)
NO_AUDIT_WRITERS = _RESOLUTION_NO_AUDIT_WRITERS + (
    "engram_core._run_db_one_time_setup: sets nodes.trust_tier NULL -> 'unknown' for person nodes at startup and "
    "writes no edit_history row",
    "engram_core._backfill_source_type: sets nodes.source_type NULL -> an inferred value for evidence nodes at "
    "startup and writes no edit_history row",
)


def _names(writers) -> str:
    return " and ".join(w.split(":")[0] for w in writers)


# appended to a resolved_by (or resolve-path status) failure that has no audit row behind it, so "corruption" is not
# the only explanation a reader is left with
_NO_AUDIT_HINT = (f" (possible causes, writers that leave no audit row: {_names(_RESOLUTION_NO_AUDIT_WRITERS)}; "
                  "this change is not told apart from corruption, and the column is not classified mutable to absorb it)")

_RESOLUTION_STATUSES = ("open", "partially_resolved", "resolved", "confirmed", "partially_confirmed", "refuted",
                        "partially_refuted", "supported", "inconclusive")
_TASK_STATUSES = ("planned", "active", "done", "blocked")
_KIND_RESOLUTION, _KIND_TASK = "resolution", "task"
# the audit actions that record each kind of status move
_KIND_ACTIONS = {_KIND_RESOLUTION: (("resolved", "resolution_recomputed", "reopened"), ()),
                 _KIND_TASK: ((), ("status_*_to_*",))}
_STATUS_TRANSITIONS = {}  # (restored, live) -> (kind, reason)
for _a in _RESOLUTION_STATUSES:
    for _b in _RESOLUTION_STATUSES:
        if _a != _b:
            _STATUS_TRANSITIONS[(_a, _b)] = (_KIND_RESOLUTION,
                "resolution status moves with the live resolvers: engram_core._recompute_resolution_target "
                "(reopen to 'open', threshold re-evaluation) and engram_revision._resolve_impl")
for _a in _TASK_STATUSES:
    for _b in _TASK_STATUSES:
        if _a != _b:
            _STATUS_TRANSITIONS[(_a, _b)] = (_KIND_TASK, "task status in any order: engram_tasks.engram_update_task")
del _a, _b
_FIELD_PATCH_REASON = "operator repair: tools.surgical.field_patch (`status` is on its allowlist)"

# resolved_by is a node id, so its rules are keyed by the SHAPE of the (restored, live) pair, each with a condition
# on the live status that the writing code path also sets.
_RESOLVED_BY_RULES = {
    "value -> NULL": ("live status is 'open'", "engram_core._recompute_resolution_target reopen: "
                      "no live resolver remains, status 'open', resolved_by NULL"),
    "value -> other value": ("live status is a resolution status", "engram_core._recompute_resolution_target / "
                             "engram_revision._resolve_impl self-heal: re-pointed to the best live resolver"),
    "NULL -> value": ("any live status", "engram_revision._resolve_impl, engram_core._recompute_resolution_target "
                      "and engram_core._backfill_resolved_by: a resolver is recorded"),
}

# counters, per column, in DrillReport.lifecycle
_PASS, _AMB, _NOREC, _BEFORE, _NOTRANS = ("passed", "ambiguous", "failed_no_record", "failed_before_dump",
                                          "failed_transition")


def _bump(report: DrillReport, column: str, what: str) -> None:
    c = report.lifecycle.setdefault(column, {})
    c[what] = c.get(what, 0) + 1


def _parse_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        t = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)


@dataclass
class DumpTime:
    """When the dump being restored was taken: the latest and the earliest time it can have been. A marker or an
    mtime is one instant (earliest == latest). The commit window is (previous commit, this commit): the earliest is
    None, UNKNOWN, when knowledge.sql has no previous commit. Never invented."""
    source: str  # "marker" | "commit-window" | "mtime-fallback"
    latest: object  # datetime
    earliest: object = None  # datetime | None
    note: str = ""

    def describe(self) -> str:
        if self.source == "commit-window":
            lo = self.earliest.isoformat() if self.earliest else "unknown"
            return f"between {lo} (previous commit of knowledge.sql) and {self.latest.isoformat()} (this commit)"
        return self.latest.isoformat()


@dataclass
class _Ctx:
    """What the checks need besides the two rows: the live connection (the audit-record source) and the dump time."""
    live: sqlite3.Connection
    dump_time: DumpTime
    has_edit_history: bool = False


def _when(dump: DumpTime, stamp) -> str:
    """none | before | ambiguous | after: where the live record's time falls against the dump time."""
    if stamp is None:
        return "none"
    if stamp >= dump.latest:
        return "after"
    if dump.earliest is not None and stamp < dump.earliest:
        return "before"
    return "ambiguous"  # only a window with an unknown or earlier bound than the stamp gets here


def _latest(*stamps):
    got = [s for s in stamps if s is not None]
    return max(got) if got else None


def _size(v) -> int:
    return len(v) if isinstance(v, (str, bytes, list, dict)) else len(json.dumps(v))


def _shape(v) -> str:
    """The TYPE and LENGTH of a graph value, never the value (a failure pasted into a thread must not carry graph
    content: a person node holds a birthday as a write-once key)."""
    return "(absent)" if v is _ABSENT else f"<{type(v).__name__}, length {_size(v)}>"


def _shape_pair(rv, lv) -> str:
    """`restored <str, length 10>, live <str, length 10> (same length)` for two differing values."""
    both = rv is not _ABSENT and lv is not _ABSENT
    note = (" (same length, the values still differ)" if _size(rv) == _size(lv) else " (differ in length)") if both else ""
    return f"restored {_shape(rv)}, live {_shape(lv)}{note}"


def _edit_stamp(ctx: _Ctx, node_id, actions: tuple = (), globs: tuple = (), field=None):
    """When the live db's own audit log last recorded one of `actions` (exact) or `globs` (action patterns) for
    the node; with `field`, only rows whose details.field is that column (surgical_field_patch)."""
    if not ctx.has_edit_history or not (actions or globs):
        return None
    clauses = []
    params: list = [node_id]
    if "*" in actions:  # any recorded change to the node
        actions, globs = (), ("*",)
    if actions:
        clauses.append(f"action IN ({','.join('?' * len(actions))})")
        params += list(actions)
    for g in globs:
        clauses.append("action GLOB ?")
        params.append(g)
    best = None
    for ts, details in ctx.live.execute(
            f"SELECT timestamp, details FROM edit_history WHERE node_id = ? AND ({' OR '.join(clauses)})", params):
        if field is not None:
            try:
                d = json.loads(details)
            except (TypeError, ValueError):
                continue
            if not isinstance(d, dict) or d.get("field") != field:
                continue
        best = _latest(best, _parse_time(ts))
    return best


def _retract_stamp(ctx: _Ctx, node_id, live_meta: dict):
    """When the node was retracted: the `retracted_at` that engram_revision writes on the retracted node itself, or
    its 'retracted' edit_history row. There is NO metadata stamp for supersede or resolve: those are audit rows."""
    return _latest(_parse_time(live_meta.get("retracted_at")), _edit_stamp(ctx, node_id, ("retracted",)))


def _explain(ctx: _Ctx, report: DrillReport, node_id, column: str, stamp, desc: str, record: str,
             hint: str = "") -> list:
    """Rule part (i): is there a live record of this change, and is it after the dump? Failure fragments."""
    when = _when(ctx.dump_time, stamp)
    if when == "none":
        _bump(report, column, _NOREC)
        return [f"column {column}: {desc}: no audit record in the live graph ({record}); a difference nothing "
                f"records cannot be told from corruption{hint}"]
    if when == "before":
        _bump(report, column, _BEFORE)
        return [f"column {column}: {desc}: the live record is {stamp.isoformat()}, before the dump time "
                f"({ctx.dump_time.describe()}), so the dump should already have had it"]
    if when == "ambiguous":
        _bump(report, column, _AMB)
        report.ambiguous.append({"table": "nodes", "key": str(node_id), "column": column,
                                 "recorded": stamp.isoformat(), "difference": desc})
        return []  # ambiguous: reported, not failed
    _bump(report, column, _PASS)
    return []


def _not_a_transition(report: DrillReport, column: str, desc: str, why: str) -> list:
    _bump(report, column, _NOTRANS)
    return [f"column {column}: {desc}: {why}"]


def _note_transition(report: DrillReport, label: str, reason: str) -> None:
    t = report.lifecycle_transitions.setdefault(label, {"count": 0, "reason": reason})
    t["count"] += 1


def _check_status(ctx, report, node_id, rv, lv, live_meta) -> list:
    if rv == lv:
        return []
    desc = f"restored {rv!r}, live {lv!r}"
    if rv == "retracted":
        return _not_a_transition(report, "status", desc,
                                 "backward (a retraction in the dump is missing from live; no code path un-retracts "
                                 "a node, and a field_patch is not accepted as one either)")
    if lv == "retracted":
        return _explain(ctx, report, node_id, "status", _retract_stamp(ctx, node_id, live_meta), desc,
                        "the retracted_at stamp or a 'retracted' edit_history row")
    named = _STATUS_TRANSITIONS.get((rv, lv))
    patch = _edit_stamp(ctx, node_id, ("surgical_field_patch",), field="status")
    if named is None and patch is None:
        return _not_a_transition(report, "status", desc, "not a transition any code path writes")
    stamp = patch
    if named:
        actions, globs = _KIND_ACTIONS[named[0]]
        stamp = _latest(_edit_stamp(ctx, node_id, actions, globs), patch)
    out = _explain(ctx, report, node_id, "status", stamp, desc,
                   "an edit_history row for this status move" + (" or a field_patch" if named else ", a field_patch"),
                   hint=_NO_AUDIT_HINT if named and named[0] == _KIND_RESOLUTION else "")
    if not out:
        _note_transition(report, f"{rv} -> {lv}", named[1] if named else _FIELD_PATCH_REASON)
    return out


def _check_is_current(ctx, report, node_id, rv, lv, live_status, live_meta) -> list:
    if rv == lv:
        return []
    desc = f"restored {rv!r}, live {lv!r}"
    if rv == 1 and lv == 0:
        if live_status == "retracted":
            stamp, record = _retract_stamp(ctx, node_id, live_meta), "the retracted_at stamp or a 'retracted' row"
        else:
            stamp, record = _edit_stamp(ctx, node_id, ("superseded",)), "a 'superseded' edit_history row"
        return _explain(ctx, report, node_id, "is_current", stamp, desc, record)
    return _not_a_transition(report, "is_current", desc,
                             "backward (no code path makes a node current again)" if (rv, lv) == (0, 1)
                             else "not a transition any code path writes")


def _check_superseded_by(ctx, report, node_id, rv, lv) -> list:
    if rv == lv:
        return []
    desc = f"restored {rv!r}, live {lv!r}"
    if rv is None and lv is not None:
        return _explain(ctx, report, node_id, "superseded_by", _edit_stamp(ctx, node_id, ("superseded",)), desc,
                        "a 'superseded' edit_history row")
    why = "backward (a supersession in the dump is missing from live)" if lv is None else \
        "changed (a node is superseded once; no code path re-points it)"
    return _not_a_transition(report, "superseded_by", desc, why)


def _check_resolved_by(ctx, report, node_id, rv, lv, live_status) -> list:
    if rv == lv:
        return []
    desc = f"restored {rv!r}, live {lv!r}"
    if rv is None:
        shape, ok = "NULL -> value", True
    elif lv is None:
        shape, ok = "value -> NULL", live_status == "open"
    else:
        shape, ok = "value -> other value", live_status in _RESOLUTION_STATUSES
    cond, reason = _RESOLVED_BY_RULES[shape]
    if not ok:
        return _not_a_transition(report, "resolved_by", desc,
                                 f"backward or changed, and not the named transition ({shape}: allowed only when "
                                 f"{cond}; live status is {live_status!r})")
    stamp = _edit_stamp(ctx, node_id, _KIND_ACTIONS[_KIND_RESOLUTION][0])
    out = _explain(ctx, report, node_id, "resolved_by", stamp, desc, "a 'resolved' / 'resolution_recomputed' / "
                   "'reopened' edit_history row", hint=_NO_AUDIT_HINT)
    if not out and shape != "NULL -> value":
        _note_transition(report, f"resolved_by: {shape}", reason)
    return out


def _check_quoted_text(ctx, report, node_id, rv, lv) -> list:
    """quoted_text is WRITE-ONCE under the same rule: evidence, the most claim-bearing column there is. A difference
    passes only when the live record shows a requote (or an operator field_patch of that column) after the dump."""
    if rv == lv:
        return []
    stamp = _latest(_edit_stamp(ctx, node_id, ("requoted",)),
                    _edit_stamp(ctx, node_id, ("surgical_field_patch",), field="quoted_text"))
    return _explain(ctx, report, node_id, "quoted_text", stamp, _shape_pair(rv, lv),
                    "a 'requoted' edit_history row or a field_patch of quoted_text")


def _check_audit_gated(ctx, report, node_id, table, column, rv, lv) -> list:
    """A column (or metadata key, table "nodes.metadata") in AUDIT_GATED_COLUMNS: a difference passes only with a
    live audit row for the node after the dump time. The values are enumerated vocabulary (tiers, source types)."""
    if rv == lv:
        return []
    spec = AUDIT_GATED_COLUMNS[(table, column)]
    shown = column if table == "nodes" else f"metadata.{column}"
    desc = f"restored {rv!r}, live {lv!r}"
    return _explain(ctx, report, node_id, shown, _edit_stamp(ctx, node_id, spec["actions"]), desc,
                    "an edit_history row for this change (" + ", ".join(spec["actions"]) + ")",
                    hint=f" (possible cause, a writer that leaves no audit row: {_names(spec['no_audit'])})")


# ------------------------------------------------------------------ nodes.metadata, key by key
#
# One clearly named place to adjust the rulings (the design seat confirms the narrowing). Method: every
# `UPDATE nodes SET metadata` statement in src/engram (17 sites) and every `meta[...] =` / `.pop(...)` /
# `json_set` key it touches was read. Each writer reads the blob and changes only its own named keys, EXCEPT
# tools/surgical.py metadata_patch (~734, an operator patch of any key: not allowed, a restore that differs
# from live by a patch fails loud) and engram_core.py (~5003) which NULLs an invalid blob. No tool (requote,
# supersede, retract, repair, the retype verb, which retypes EDGES only) rewrites a creation-time key below.

LIFECYCLE_STAMP_METADATA_KEYS = {
    # written when a node is withdrawn, by engram_revision.engram_retract (~4366-4375, and ~4526-4544 for a
    # paired prediction retracted with its basis): absent in the dump and present live is FORWARD; `retracted_at`
    # carries the stamp, the others ride with it.
    "retracted": "engram_revision.py:4366", "error_type": "engram_revision.py:4367",
    "retraction_reason": "engram_revision.py:4368", "retracted_at": "engram_revision.py:4369",
    "initiation": "engram_revision.py:4371", "initiation_gate": "engram_revision.py:4373",
    "initiation_ref": "engram_revision.py:4375", "retracted_via_basis_retraction": "engram_revision.py:4533",
    "status_before_retraction": "engram_revision.py:4544",
}

# Written once, at node creation, and never rewritten by any tool found: a difference FAILS, naming the key.
WRITE_ONCE_METADATA_KEYS = {
    "reasoning_type": "engram_core.py:14097", "reasoning_class": "engram_core.py:14097",
    "warrant": "engram_core.py:14111 (extra_meta, one call site)",
    "built_on_contested": "engram_core.py:14109",
    "built_on_stale": "retired writer (#2324, engram_core.py:14100 note); legacy stamps in old graphs",
    "stale_by": "retired writer (#2013 cascade machine, server.py:7342); legacy", "tainted_by": "retired writer; legacy",
    "stale_replacement": "retired writer (server.py:7342); legacy",
    # found by the first real run (2026-11-14, 9 and 1 nodes): no code path in the shipped tree writes either one
    # into nodes.metadata today, so they are legacy stamps
    "role": "deprecated alias of `relation` (engram_trust.py:100/294, stored as relation): legacy person nodes only",
    "supersede_reason": "no writer into node metadata in the shipped tree (the reason goes to the supersedes edge, "
                        "engram_revision.py:3617, and edit_history): legacy, 1 prediction node",
    "source_class": "engram_observation.py:2125", "quote_verified": "engram_observation.py:2125",
    "git_sha": "engram_observation.py:2127", "content_hash": "engram_observation.py:2129",
    "is_self_report": "engram_tasks.py:736", "epistemic_status": "engram_tasks.py:737",
    "categorical_tag_disclaimer": "engram_tasks.py:740", "motivation": "engram_tasks.py:142",
    "term": "engram_epistemic.py:374", "definition": "engram_epistemic.py:374",
    "tag": "engram_cornerstone.py:158", "title": "engram_cornerstone.py:159",
    "prior_frame": "engram_cornerstone.py:160", "new_frame": "engram_cornerstone.py:161",
    "triggering_experience": "engram_cornerstone.py:162", "outgrew": "engram_cornerstone.py:367",
    "scaffolding_nudge": "engram_epistemic.py:196 / 1246",
    "name": "engram_trust.py:167", "relation": "engram_trust.py:173", "pronouns": "engram_trust.py:175",
    "location_contact": "engram_trust.py:206", "birthday": "engram_trust.py:208", "lineage": "engram_trust.py:211",
    "architecture": "engram_trust.py:213", "first_session_date": "engram_trust.py:215",
}

# Rewritten after creation by a named code path: a difference is REPORTED with a count, never failed.
MUTABLE_METADATA_KEYS = {
    "quote_history": "engram_observation.py:3174 (requote)",
    "support_lost": "engram_core.py:11150/11159, engram_epistemic.py:1833",
    "field_traces": "engram_epistemic.py:1632, engram_trust.py:450", "field_changed_flags": "engram_trust.py:547",
    "aliases": "engram_trust.py:476", "special_moments": "engram_trust.py:687",
    "special_moment_cursor": "engram_moment_cursor.py:80",
    "model_provider": "engram_trust.py:925", "model_family": "engram_trust.py:926",
    "model_version": "engram_trust.py:927", "is_self": "engram_revision.py:3715 (self-anchor transfer)",
    "status_history": "engram_tasks.py:524", "scope": "engram_tasks.py:336",
    "lifecycle_state": "engram_tasks.py:908/978", "deactivated_at": "engram_tasks.py:909",
    "deactivation_reason": "engram_tasks.py:911", "reactivated_at": "engram_tasks.py:979",
    "reactivation_reason": "engram_tasks.py:981",
    "evidence_type": "engram_observation.py:2056 (set on the evidence node when first cited)",
    "original_url": "engram_observation.py:2067",
    "bypass_prose_check": "engram_epistemic.py:1659", "situation_pattern": "engram_epistemic.py:1651 (update_lesson)",
    "prevention_type": "engram_epistemic.py:1651", "tripwire_priority": "engram_epistemic.py:1651",
    "match_scope": "engram_epistemic.py:1651",
}

_ABSENT = object()


def _parse_metadata(raw):
    """(dict, error or None). NULL is an empty object; anything that is not a JSON object is an error."""
    if raw is None:
        return {}, None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return {}, f"not valid JSON ({exc})"
    if not isinstance(value, dict):
        return {}, f"valid JSON but not an object ({type(value).__name__})"
    return value, None


def _note_unknown(report: DrillReport, keys) -> None:
    for k in keys:
        if (k not in LIFECYCLE_STAMP_METADATA_KEYS and k not in WRITE_ONCE_METADATA_KEYS
                and k not in MUTABLE_METADATA_KEYS and ("nodes.metadata", k) not in AUDIT_GATED_COLUMNS):
            report.metadata_unknown[k] = report.metadata_unknown.get(k, 0) + 1


def _scan_metadata_keys(report: DrillReport, node_id, raw) -> None:
    """A row on one side only (written after the dump, or deleted): its keys are still listed."""
    d, _ = _parse_metadata(raw)
    _note_unknown(report, d)


def _compare_node_metadata(ctx, report, node_id, rraw, lraw) -> list:
    """nodes.metadata as parsed JSON, key by key. Returns failure fragments (without the row prefix)."""
    r, rerr = _parse_metadata(rraw)
    l, lerr = _parse_metadata(lraw)
    out = []
    for side, err in (("restored", rerr), ("live", lerr)):
        if err:
            out.append(f"column metadata: the {side} metadata of node {node_id} is {err}")
    if rerr or lerr:
        return out
    keys = sorted(set(r) | set(l))
    _note_unknown(report, keys)
    for k in keys:
        rv, lv = r.get(k, _ABSENT), l.get(k, _ABSENT)
        if rv == lv:
            continue
        desc = _shape_pair(rv, lv)  # the key is named, the value never is (graph content)
        if k in LIFECYCLE_STAMP_METADATA_KEYS:
            if rv is _ABSENT:
                if k == "retracted_at":
                    out += _explain(ctx, report, node_id, f"metadata.{k}", _parse_time(lv), desc,
                                    "the retracted_at stamp itself")
                else:
                    _bump(report, f"metadata.{k}", _PASS)
            else:
                _bump(report, f"metadata.{k}", _NOTRANS)
                out.append(f"column metadata key {k}: {desc}: backward or changed (a withdrawal stamp is written once)")
        elif ("nodes.metadata", k) in AUDIT_GATED_COLUMNS:
            out += _check_audit_gated(ctx, report, node_id, "nodes.metadata", k,
                                      None if rv is _ABSENT else rv, None if lv is _ABSENT else lv)
        elif k in WRITE_ONCE_METADATA_KEYS:
            out.append(f"column metadata key {k}: {desc}: a write-once metadata key changed")
        elif k in MUTABLE_METADATA_KEYS:
            report.metadata_mutable_diffs[k] = report.metadata_mutable_diffs.get(k, 0) + 1
        else:
            out.append(f"column metadata key {k}: {desc} (unknown metadata key, treated as write-once)")
    return out


# ------------------------------------------------------------------ small helpers

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_prefix(path: str, n: int) -> str:
    h = hashlib.sha256()
    left = n
    with open(path, "rb") as f:
        while left > 0:
            chunk = f.read(min(1 << 20, left))
            if not chunk:
                break
            h.update(chunk)
            left -= len(chunk)
    return h.hexdigest()


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _home_state(home: Path) -> dict:
    """size and mtime of the live db, its side files and sessions/ (a read, no side effect)."""
    out: dict = {}
    for name in ("knowledge.db", "knowledge.db-wal", "knowledge.db-shm", "knowledge.db-journal"):
        p = home / name
        try:
            st = os.stat(p)
            out[name] = (st.st_size, st.st_mtime_ns)
        except OSError:
            out[name] = None
    sess = home / "sessions"
    if sess.is_dir():
        for dirpath, dirnames, filenames in os.walk(sess):
            dirnames.sort()
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                rel = "sessions/" + os.path.relpath(full, sess).replace(os.sep, "/")
                try:
                    st = os.stat(full)
                    out[rel] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    out[rel] = None
    return out


def _state_diff(before: dict, after: dict) -> list:
    changes = []
    for k in sorted(set(before) | set(after)):
        if before.get(k) != after.get(k):
            changes.append(f"{k}: {before.get(k)} -> {after.get(k)}")
    return changes


def _open_live_readonly(db: Path) -> sqlite3.Connection:
    """Read-only, and creating nothing. mode=ro when BOTH side files exist (a live seat); immutable=1 when
    neither does, because a read-only open of a quiescent WAL-mode db creates -wal and -shm (measured).
    Exactly one of the two: REFUSED. Measured on SQLite 3.53.4 and again here: mode=ro on -wal without -shm
    creates -shm in the live home; on -shm without -wal it creates -wal and reads stale data."""
    base = db.resolve().as_uri()
    has_wal, has_shm = os.path.exists(str(db) + "-wal"), os.path.exists(str(db) + "-shm")
    if has_wal and not has_shm:
        raise DrillRefused("live home has a -wal file but no -shm file: refusing to open it read-only")
    if has_shm and not has_wal:
        raise DrillRefused("live home has a -shm file but no -wal file: refusing to open it read-only")
    uri = base + ("?mode=ro" if has_wal else "?immutable=1")
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    return conn


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------ the subprocess steps

_CONNECT_SCRIPT = r"""
import json, sys
import engram_core as core
want_texts = sys.argv[1] == "1"
conn = core._get_db()
out = {"user_version": conn.execute("PRAGMA user_version").fetchone()[0],
       "latest": core.LATEST_SCHEMA_VERSION, "texts": {}}
if want_texts:
    for (nid,) in conn.execute("SELECT id FROM nodes ORDER BY id").fetchall():
        t = core._embedding_text_for_node(conn, nid)
        if t:
            out["texts"][nid] = t
conn.close()
print("@@DRILL@@" + json.dumps(out))
"""

_EMBED_SCRIPT = r"""
import json, sys
import engram_core as core
texts = json.load(sys.stdin)
model = core._get_embedding_config().get("model", core.DEFAULT_EMBEDDING_MODEL)
vectors = []
for i in range(0, len(texts), 64):
    batch = texts[i:i + 64]
    got = core._embedder.embed_batch(batch, model)
    if got is None:
        got = [None] * len(batch)
    vectors.extend(got)
print("@@DRILL@@" + json.dumps({"model": model, "vectors": vectors}))
"""


_ENV_ALLOWLIST = ("PATH", "LANG", "LANGUAGE", "TMPDIR", "TZ", "__CF_USER_TEXT_ENCODING")
_ENV_ALLOW_PREFIXES = ("LC_",)


def _scratch_env(scratch: str, *, embedder_off: bool) -> dict:
    """The subprocess environment, built from an ALLOWLIST (nothing else is inherited: a test bypass, a live
    path, XDG_*, HF_*, TRANSFORMERS_*, ENGRAMD_*, a token, must not ride in). ENGRAM_HOME and HOME are both the
    scratch dir. The embed step additionally gets HF_HOME (the existing model cache, read from the parent's own
    HF_HOME or its default location) with HF_HUB_OFFLINE=1 and TRANSFORMERS_OFFLINE=1 so it never downloads;
    the connect step (embedder off) gets none of the three."""
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST or k.startswith(_ENV_ALLOW_PREFIXES)}
    env["ENGRAM_HOME"] = scratch
    env["HOME"] = scratch
    env["PYTHONPATH"] = _PKG_ROOT
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if embedder_off:
        env["ENGRAM_NO_EMBEDDINGS"] = "1"
    else:
        # HOME is the scratch dir, so the model cache must be named explicitly.
        env["HF_HOME"] = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
    return env


def _run_scratch_python(script: str, args: list, scratch: str, *, embedder_off: bool, stdin: str = "",
                        timeout: int) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", script, *args],
        input=stdin, capture_output=True, text=True, timeout=timeout,
        env=_scratch_env(scratch, embedder_off=embedder_off), cwd=scratch,
    )
    if proc.returncode != 0:
        tail = " ".join((proc.stderr or "").strip().splitlines()[-3:])
        raise RuntimeError(f"subprocess exited {proc.returncode}: {tail[:600]}")
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(_MARK):
            return json.loads(line[len(_MARK):])
    raise RuntimeError("subprocess printed no result line")


class EmbedVectors(list):
    """The seam's result: a list (one vector or None per text) that may also NAME the model that produced it.
    A seam that returns a plain list names no model; the identity check then has nothing to compare."""
    model = None


def _embed_texts(texts: list, scratch: str) -> list:
    """THE embedding seam. One vector (list of floats) or None per text, in order; an `EmbedVectors` carries the
    model name. The real implementation runs the server's embedder in a subprocess rooted at the scratch home;
    tests replace this function, so the model is never loaded in a test."""
    if not texts:
        return EmbedVectors()
    res = _run_scratch_python(_EMBED_SCRIPT, [], scratch, embedder_off=False,
                              stdin=json.dumps(texts), timeout=_EMBED_TIMEOUT_S)
    out = EmbedVectors(res["vectors"])
    out.model = res.get("model")
    return out


# ------------------------------------------------------------------ the drill

def _resolve_dump(home: Path, dump, from_git: bool) -> tuple:
    """(text bytes, source description). Raises DrillRefused when there is no dump to load."""
    if dump:
        p = Path(os.path.expanduser(str(dump)))
        if not p.is_file():
            raise DrillRefused(f"{p}: --dump path is not a file")
        st = os.stat(p)
        return p.read_bytes(), {"kind": "path", "path": str(p), "mtime": _iso(st.st_mtime)}
    if from_git:
        try:
            show = subprocess.run(["git", "-C", str(home), "show", "HEAD:knowledge.sql"],
                                  capture_output=True, timeout=300)
            rev = subprocess.run(["git", "-C", str(home), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise DrillRefused(f"git could not be run to read HEAD:knowledge.sql ({exc})") from exc
        if show.returncode != 0 or rev.returncode != 0:
            raise DrillRefused(
                f"{home}: git show HEAD:knowledge.sql failed: {show.stderr.decode('utf-8', 'replace').strip()[:300]}")
        return show.stdout, {"kind": "git", "commit": rev.stdout.strip(), "path": "HEAD:knowledge.sql"}
    p = home / "knowledge.sql"
    if not p.is_file():
        raise DrillRefused(f"{p}: no knowledge.sql in the home (use --from-git or --dump PATH)")
    return p.read_bytes(), {"kind": "working-tree", "path": str(p), "mtime": _iso(os.stat(p).st_mtime)}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


_DUMP_MARKER = re.compile(r"^--\s*engram-dumped-at:\s*(\S+)\s*$")


def _git_out(home: Path, *args) -> str:
    try:
        p = subprocess.run(["git", "-C", str(home), *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def _commit_window(home: Path, from_head: bool):
    """(this commit time, previous commit time or None) of knowledge.sql in the home's repo, or None when there is
    no git, no repo, or knowledge.sql has no commit. `git log -2` lists this commit and the one before it."""
    args = ["log", "-2", "--format=%cI"] + (["HEAD"] if from_head else []) + ["--", "knowledge.sql"]
    lines = [ln for ln in _git_out(home, *args).splitlines() if ln.strip()]
    this = _parse_time(lines[0]) if lines else None
    if this is None:
        return None
    return this, (_parse_time(lines[1]) if len(lines) > 1 else None)


def _dump_time(home: Path, dump_bytes: bytes, source: dict) -> DumpTime:
    """When the dump being restored was taken. In this order:
      marker         a comment line `-- engram-dumped-at: <ISO time>` in the dump's first lines, written by
                     engram_backup.dump_stripped: exact. PREFERRED.
      commit-window  an older dump with no marker: the true time lies between the previous commit of
                     knowledge.sql and this one (`git log -2 --format=%cI -- knowledge.sql` in the home's repo; for a
                     working-tree file only when it is tracked and unmodified). NO slack window. With no previous
                     commit the earlier bound is UNKNOWN (None), never invented.
      mtime-fallback the file's mtime, for a dump with no marker and no repo (a --dump file, a home without
                     git). The WEAKEST source: it treats the whole file time as the dump time."""
    for line in dump_bytes[:4096].decode("utf-8", "replace").splitlines()[:20]:
        m = _DUMP_MARKER.match(line.strip())
        if m and _parse_time(m.group(1)):
            t = _parse_time(m.group(1))
            return DumpTime("marker", t, t)
    window = None
    if source["kind"] == "git":
        window = _commit_window(home, True)
    elif source["kind"] == "working-tree" and not _git_out(home, "status", "--porcelain", "--", "knowledge.sql"):
        window = _commit_window(home, False)
    if window:
        this, prev = window
        note = ("knowledge.sql has no previous commit: the earlier bound of the dump time is unknown" if prev is None
                else "")
        return DumpTime("commit-window", this, prev, note)
    t = _parse_time(source.get("mtime") or _iso(time.time()))
    return DumpTime("mtime-fallback", t, t, "the weakest source: every difference with an audit row after the file's "
                    "mtime passes, none before it does")


def _table_names(conn: sqlite3.Connection) -> list:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def _is_excluded_table(name: str) -> bool:
    return name.startswith(_EXCLUDED_PREFIXES)


def _columns(conn: sqlite3.Connection, table: str) -> list:
    """[(name, pk_position)] in table order."""
    return [(r[1], r[5]) for r in conn.execute(f"PRAGMA table_info({_q(table)})").fetchall()]


def _compare_tables(report: DrillReport, restored: sqlite3.Connection, live: sqlite3.Connection,
                    dump_time=None) -> None:
    """A sqlite error on either side (a malformed page, a lock, a drifted schema) means the comparison could not
    run: DrillRefused (exit 2) naming the table, never a traceback and never exit 1 ("the comparison failed")."""
    progress = ["(listing the tables)"]
    try:
        _compare_tables_impl(report, restored, live, dump_time, progress)
    except sqlite3.Error as exc:
        raise DrillRefused(f"table {progress[0]}: the comparison could not run ({type(exc).__name__}: {exc})") from exc


def _compare_tables_impl(report: DrillReport, restored: sqlite3.Connection, live: sqlite3.Connection,
                         dump_time, progress: list) -> None:
    rtables = [t for t in _table_names(restored) if not _is_excluded_table(t)]
    ltables = set(_table_names(live))
    ctx = _Ctx(live=live, dump_time=dump_time, has_edit_history="edit_history" in ltables)
    excluded_tables = sorted(t for t in _table_names(restored) if _is_excluded_table(t))
    excluded_tables += sorted(t for t in ltables if _is_excluded_table(t) and t not in excluded_tables)
    live_only_tables = sorted(t for t in ltables if not _is_excluded_table(t) and t not in rtables)
    report.excluded = {"columns": list(_EXCLUDED_COLUMNS), "tables": sorted(set(excluded_tables)),
                       "live_only_tables": live_only_tables}
    for table in rtables:
        progress[0] = table
        if table not in ltables:
            _fail(report, f"table {table}: present in the dump, missing from the live db")
            continue
        rcols = _columns(restored, table)
        lcols = _columns(live, table)
        lnames = {c for c, _ in lcols}
        common = [c for c, _ in rcols if c in lnames and c not in _EXCLUDED_COLUMNS]
        schema_only = sorted(({c for c, _ in rcols} ^ lnames) - set(_EXCLUDED_COLUMNS))
        if schema_only:
            report.excluded.setdefault("schema_differs", {})[table] = schema_only
        pk = [c for c, p in sorted(rcols, key=lambda x: x[1]) if p > 0 and c in common]
        classes = {c: classify(table, c) for c in common}
        report.classification[table] = {
            WRITE_ONCE: [c for c in common if classes[c] == WRITE_ONCE],
            MUTABLE: [c for c in common if classes[c] == MUTABLE],
            LIFECYCLE: [c for c in common if classes[c] == LIFECYCLE],
            AUDIT_GATED: [c for c in common if classes[c] == AUDIT_GATED],
            KEYED: [c for c in common if classes[c] == KEYED],
            UNCLASSIFIED: [c for c in common if classes[c] == UNCLASSIFIED],
        }
        keycols = pk if pk else ["rowid"]
        select = ", ".join(_q(c) if c != "rowid" else "rowid" for c in keycols + common)
        nkey = len(keycols)

        def _rows(conn):
            out = {}
            for row in conn.execute(f"SELECT {select} FROM {_q(table)}"):
                out[tuple(row[:nkey])] = row[nkey:]
            return out

        rrows, lrows = _rows(restored), _rows(live)
        if table == "nodes":
            report.nodes_restored = len(rrows)
        lonly = [k for k in lrows if k not in rrows]
        if lonly:
            report.live_only_counts[table] = len(lonly)
            if table == "nodes":
                report.live_only = sorted(str(k[0]) for k in lonly)
        has_meta = table == "nodes" and "metadata" in common
        if has_meta:  # a row on one side only still has its metadata keys listed
            mi = common.index("metadata")
            for k in lonly:
                _scan_metadata_keys(report, k[0], lrows[k][mi])
            for k, rv in rrows.items():
                if k not in lrows:
                    _scan_metadata_keys(report, k[0], rv[mi])
        fails_here = 0
        for key, rvals in rrows.items():
            lvals = lrows.get(key)
            if lvals is None:
                fails_here += 1
                if fails_here <= _MAX_FAIL_LINES_PER_TABLE:
                    _fail(report, f"{table} {_keystr(keycols, key)}: in the dump, missing from live (deleted)")
                continue
            report.rows_compared += 1
            if table == "nodes":
                for msg in _check_node_row(ctx, report, key[0], dict(zip(common, rvals)), dict(zip(common, lvals))):
                    fails_here += 1
                    if fails_here <= _MAX_FAIL_LINES_PER_TABLE:
                        _fail(report, f"{table} {_keystr(keycols, key)} {msg}")
            if rvals == lvals:
                continue
            for c, rv, lv in zip(common, rvals, lvals):
                if rv == lv or classes[c] in (LIFECYCLE, KEYED, AUDIT_GATED) or (table, c) in _AUDITED_WRITE_ONCE:
                    continue
                if classes[c] == MUTABLE:
                    k = f"{table}.{c}"
                    report.mutable_diffs[k] = report.mutable_diffs.get(k, 0) + 1
                else:
                    fails_here += 1
                    if fails_here <= _MAX_FAIL_LINES_PER_TABLE:
                        why = "" if classes[c] == WRITE_ONCE else " (unclassified, treated as write-once)"
                        _fail(report, f"{table} {_keystr(keycols, key)} column {c}: restored != live{why}")
        if fails_here > _MAX_FAIL_LINES_PER_TABLE:
            _fail(report, f"table {table}: {fails_here - _MAX_FAIL_LINES_PER_TABLE} more write-once "
                          f"or deleted-row failures not listed")


def _check_node_row(ctx: _Ctx, report: DrillReport, node_id, r: dict, l: dict) -> list:
    """The lifecycle columns and the metadata keys of one node present on both sides. Failure fragments."""
    out: list = []
    live_meta = _parse_metadata(l.get("metadata"))[0] if "metadata" in l else {}
    live_status = l.get("status")
    if "status" in r:
        out += _check_status(ctx, report, node_id, r["status"], l["status"], live_meta)
    if "is_current" in r:
        out += _check_is_current(ctx, report, node_id, r["is_current"], l["is_current"], live_status, live_meta)
    if "superseded_by" in r:
        out += _check_superseded_by(ctx, report, node_id, r["superseded_by"], l["superseded_by"])
    if "resolved_by" in r:
        out += _check_resolved_by(ctx, report, node_id, r["resolved_by"], l["resolved_by"], live_status)
    if "quoted_text" in r:
        out += _check_quoted_text(ctx, report, node_id, r["quoted_text"], l["quoted_text"])
    for (t, c) in AUDIT_GATED_COLUMNS:
        if t == "nodes" and c in r:
            out += _check_audit_gated(ctx, report, node_id, t, c, r[c], l[c])
    if "metadata" in r:
        out += _compare_node_metadata(ctx, report, node_id, r["metadata"], l["metadata"])
    return out


def _keystr(keycols: list, key: tuple) -> str:
    return ", ".join(f"{c}={v!r}" for c, v in zip(keycols, key))


def _max_abs_diff(a, b) -> float:
    """Largest element difference of two vectors; infinity when either is missing or the lengths differ."""
    if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b):
        return float("inf")
    return max((abs(x - y) for x, y in zip(a, b)), default=0.0)


def _vectors_stale(restored_vec: list, live_raw) -> bool:
    try:
        live_vec = json.loads(live_raw) if isinstance(live_raw, str) else None
    except ValueError:
        return True
    return _max_abs_diff(restored_vec, live_vec) > STALE_TOLERANCE


def _embed_and_check(report: DrillReport, db_path: str, texts: dict, scratch: str, live: sqlite3.Connection,
                     sample: int) -> None:
    ids = sorted(texts)
    vectors = _embed_texts([texts[i] for i in ids], scratch)
    if len(vectors) != len(ids):
        _fail(report, f"embedder returned {len(vectors)} vectors for {len(ids)} texts")
        vectors = (list(vectors) + [None] * len(ids))[:len(ids)]
    conn = sqlite3.connect(db_path)
    try:
        for nid, vec in zip(ids, vectors):
            if vec:
                conn.execute("UPDATE nodes SET embedding = ? WHERE id = ?", (json.dumps(vec), nid))
        conn.commit()
        report.vectors_embedded = sum(1 for v in vectors if v)
        # Completeness, read back from the restored copy: every node that has text must hold a vector.
        have = {r[0] for r in conn.execute("SELECT id FROM nodes WHERE embedding IS NOT NULL")}
        report.vectors_missing = [i for i in ids if i not in have]
        for nid in report.vectors_missing:
            _fail(report, f"node {nid}: no vector after re-embed")
        # Sample: embed again, compare EXACTLY with the restored copy (never the live one).
        rng = random.Random(4647)
        pool = [i for i in ids if i in have]
        picked = sorted(rng.sample(pool, min(sample, len(pool))))
        again = _embed_texts([texts[i] for i in picked], scratch) if picked else []
        if len(again) != len(picked):
            _fail(report, f"embedder returned {len(again)} vectors for the {len(picked)}-node sample")
            again = (list(again) + [None] * len(picked))[:len(picked)]
        report.vector_sample_checked = len(picked)
        # Model identity is EXACT: the same model name and the same vector dimension in both passes.
        first_model, second_model = getattr(vectors, "model", None), getattr(again, "model", None)
        report.embedding_model = first_model or second_model or ""
        if first_model != second_model:
            _fail(report, f"embedding model differs between the passes: first {first_model!r}, sample {second_model!r}")
        first_dims = {len(v) for v in vectors if v}
        second_dims = {len(v) for v in again if v}
        if first_dims and second_dims and first_dims != second_dims:
            _fail(report, f"vector dimension differs between the passes: first {sorted(first_dims)}, "
                          f"re-embedded {sorted(second_dims)}")
        dim_bad = bool(first_dims and second_dims and first_dims != second_dims)
        for nid, vec in zip(picked, again):
            stored = json.loads(conn.execute("SELECT embedding FROM nodes WHERE id = ?", (nid,)).fetchone()[0])
            if dim_bad:
                break  # the dimension failure above says it; a per-node 'infinite difference' would repeat it
            diff = _max_abs_diff(vec, stored)
            if diff != float("inf"):
                report.vector_max_diff = max(report.vector_max_diff, diff)
            if diff > SAMPLE_TOLERANCE:
                _fail(report, f"node {nid}: sample re-embed differs from the restored vector (max element difference {diff:g})")
        # Staleness of the LIVE vectors: counted, never failed.
        stale = 0
        for nid, raw in conn.execute("SELECT id, embedding FROM nodes WHERE embedding IS NOT NULL").fetchall():
            row = live.execute("SELECT embedding FROM nodes WHERE id = ?", (nid,)).fetchone()
            if row is None or row[0] is None:
                continue
            if _vectors_stale(json.loads(raw), row[0]):
                stale += 1
        report.live_vectors_stale = stale
    finally:
        conn.close()


def _walk_jsonl(source: Path) -> dict:
    found: dict = {}
    if not source.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        dirnames.sort()
        for name in filenames:
            if name.endswith(".jsonl"):
                full = os.path.join(dirpath, name)
                found[Path(full).relative_to(source).as_posix()] = full
    return found


def _check_transcripts(report: DrillReport, home: Path, scratch: Path, source: Path) -> None:
    archive_dir = home / "transcript-archive"
    if not archive_dir.is_dir():
        report.skipped.append("transcripts: no transcript-archive directory in the home")
        return
    try:
        marks = at.watermarks(archive_dir)
        dest = scratch / "transcripts"
        written = at.replay(archive_dir, dest)
    except Exception as exc:  # ArchiveError (ReplayGap too), OSError, a corrupt xz stream: a failure, not a traceback
        _fail(report, f"transcript replay failed: {type(exc).__name__}: {exc}")
        return
    report.transcripts_replayed = len(written)
    live_files = _walk_jsonl(source)
    for rel, end in sorted(marks.items()):
        replayed = dest / rel
        if not replayed.is_file() or replayed.stat().st_size != end:
            _fail(report, f"transcript {rel}: replayed file is not {end} bytes")
            continue
        live_path = live_files.get(rel)
        if live_path is None:
            report.transcripts_live_missing += 1
            continue
        size = os.path.getsize(live_path)
        if size < end:
            _fail(report, f"transcript {rel}: live file is {size} bytes, shorter than the archived {end}")
            continue
        if _sha256_prefix(live_path, end) != _sha256_prefix(str(replayed), end):
            _fail(report, f"transcript {rel}: first {end} bytes differ between live and replayed")
            continue
        report.transcript_live_only_bytes += size - end
    for rel, full in live_files.items():
        if rel not in marks:
            report.transcript_live_only_bytes += os.path.getsize(full)


def run_drill(home=None, scratch=None, sample: int = DEFAULT_SAMPLE, embed: bool = True, dump=None,
              from_git: bool = False, source=None, assert_quiescent: bool = False) -> DrillReport:
    """Run the drill. Raises DrillRefused (exit 2) when it cannot run, before writing anything;
    otherwise returns a DrillReport (exit_code 0 pass, 1 failed)."""
    t0 = time.monotonic()
    report = DrillReport()
    home_p = Path(home) if home else owner_read.engram_home()
    home_p = Path(os.path.abspath(os.path.expanduser(str(home_p))))
    db = home_p / "knowledge.db"
    if not db.is_file():
        raise DrillRefused(f"{db}: the live graph is not there")
    home_real = home_p.resolve()
    created_scratch = False
    if scratch:
        scratch_p = Path(os.path.abspath(os.path.expanduser(str(scratch))))
    else:
        scratch_p = None
    if scratch_p is not None:
        s_real = scratch_p.resolve()
        if s_real == home_real or _is_inside(s_real, home_real):
            raise DrillRefused(f"{scratch_p}: the scratch directory is, or is inside, the live home {home_p}")
        if _is_inside(home_real, s_real):
            raise DrillRefused(f"{scratch_p}: the live home {home_p} is inside the scratch directory")
        if scratch_p.exists() and (not scratch_p.is_dir() or any(scratch_p.iterdir())):
            raise DrillRefused(f"{scratch_p}: the scratch directory is not empty")
    dump_bytes, dump_source = _resolve_dump(home_p, dump, from_git)
    try:
        live = _open_live_readonly(db)
    except sqlite3.Error as exc:
        raise DrillRefused(f"{db}: the live db cannot be read ({exc})") from exc
    try:
        if scratch_p is None:
            tmp = tempfile.mkdtemp(prefix="engram-restore-drill-")
            if _is_inside(Path(tmp).resolve(), home_real):
                os.rmdir(tmp)
                raise DrillRefused(f"{tmp}: the temporary directory lies inside the live home")
            scratch_p = Path(tmp)
            created_scratch = True
        else:
            os.makedirs(scratch_p, mode=0o700, exist_ok=True)
        report.scratch = str(scratch_p)
        report.dump_source = dump_source
        try:
            _drill_body(report, home_p, db, scratch_p, live, dump_bytes, sample, embed, source, assert_quiescent)
        finally:
            if created_scratch:
                shutil.rmtree(scratch_p, ignore_errors=True)
    finally:
        live.close()
    report.seconds = round(time.monotonic() - t0, 2)
    return report


def _drill_body(report: DrillReport, home: Path, db: Path, scratch: Path, live: sqlite3.Connection,
                dump_bytes: bytes, sample: int, embed: bool, source, assert_quiescent: bool) -> None:
    before = _home_state(home)
    if assert_quiescent:
        report.live_sha256_before = _sha256_file(db)
    src = report.dump_source
    where = f"commit {src['commit']}" if src["kind"] == "git" else f"{src['path']} (mtime {src['mtime']})"
    report.sections.append(f"dump: {src['kind']} {where}")
    dump_time = _dump_time(home, dump_bytes, src)
    report.dump_time_source, report.dump_time = dump_time.source, dump_time.latest.isoformat()
    report.dump_time_earliest = dump_time.earliest.isoformat() if dump_time.earliest else (
        "" if dump_time.source == "commit-window" else report.dump_time)
    report.dump_time_note = dump_time.note
    if dump_time.source == "commit-window":
        shown = (f"earliest {report.dump_time_earliest or 'unknown'}, latest {report.dump_time}"
                 + (f"; {dump_time.note}" if dump_time.note else ""))
    else:
        shown = report.dump_time + (f"; {dump_time.note}" if dump_time.note else "")
    report.sections.append(f"dump_time_source: {dump_time.source} ({shown})")

    sql_path = scratch / "knowledge.sql"
    with open(sql_path, "xb") as f:
        f.write(dump_bytes)
    db_path = str(scratch / "knowledge.db")
    restored_ok = False
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(dump_bytes.decode("utf-8"))
            report.schema["dump_user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        engram_backup.rebuild_fts_index(db_path)
        restored_ok = True
    except Exception as exc:
        _fail(report, f"the dump did not restore: {type(exc).__name__}: {exc}")

    texts: dict = {}
    if restored_ok:
        rconn = sqlite3.connect(db_path)
        try:
            _compare_tables(report, rconn, live, dump_time)
        finally:
            rconn.close()
        nfail = len(report.failures)
        report.sections.append(
            f"rows: {report.nodes_restored} nodes restored; {report.rows_compared} rows compared; "
            f"{len(report.live_only)} live_only nodes; {sum(report.mutable_diffs.values())} mutable-column "
            f"differences reported; {nfail} failures")
        report.sections.append(
            "audited columns (passed with a record after the dump / ambiguous / failed: no record / failed: record "
            "before the dump / failed: not a transition): " +
            ("; ".join(f"{c} {v.get(_PASS, 0)}/{v.get(_AMB, 0)}/{v.get(_NOREC, 0)}/{v.get(_BEFORE, 0)}/"
                       f"{v.get(_NOTRANS, 0)}" for c, v in sorted(report.lifecycle.items())) or "no differences"))
        _write_scratch_config(home, scratch)
        try:
            res = _run_scratch_python(_CONNECT_SCRIPT, ["1" if embed else "0"], str(scratch), embedder_off=True,
                                      timeout=_CONNECT_TIMEOUT_S)
            report.schema.update(user_version_after=res["user_version"], latest=res["latest"])
            migrated = res["user_version"] != report.schema.get("dump_user_version")
            report.schema["migrated_in_scratch"] = migrated
            texts = res["texts"]
            report.sections.append(
                f"server connect: ok in the scratch home; schema user_version "
                f"{report.schema.get('dump_user_version')} -> {res['user_version']} (code expects "
                f"{res['latest']})" + ("; migrated in the scratch copy" if migrated else ""))
            if res["user_version"] < res["latest"]:
                _fail(report, f"schema still at user_version {res['user_version']} after the first connect "
                              f"(code expects {res['latest']})")
        except Exception as exc:
            _fail(report, f"first server connect failed: {type(exc).__name__}: {exc}")
        if embed and texts:
            try:
                _embed_and_check(report, db_path, texts, str(scratch), live, sample)
                report.embedding = "re-embedded"
                report.sections.append(
                    f"vectors: {report.vectors_embedded} embedded; {len(report.vectors_missing)} missing; "
                    f"sample {report.vector_sample_checked} re-embedded and equal to the restored copy (within "
                    f"{SAMPLE_TOLERANCE:g}, {VECTOR_TOLERANCE['unit']}; largest difference seen "
                    f"{report.vector_max_diff:.3g}; model {report.embedding_model or 'not named by the seam'}); "
                    f"live_vectors_stale {report.live_vectors_stale}")
            except Exception as exc:
                _fail(report, f"re-embed failed: {type(exc).__name__}: {exc}")
        elif not embed:
            report.embedding = "skipped"
            report.skipped.append("re-embed skipped (--no-embed)")
            report.sections.append("vectors: embedding was skipped (--no-embed)")
    else:
        report.skipped.append("rows, server connect and vectors: the dump did not restore")

    src_root = Path(os.path.expanduser(str(source))) if source else Path(os.path.expanduser("~/.claude/projects"))
    _check_transcripts(report, home, scratch, src_root)
    report.sections.append(
        f"transcripts: {report.transcripts_replayed} replayed; live_only_bytes "
        f"{report.transcript_live_only_bytes}; live files missing {report.transcripts_live_missing}")

    after = _home_state(home)
    report.live_home_observed = _state_diff(before, after)
    if assert_quiescent:
        report.live_sha256_after = _sha256_file(db)
        if report.live_sha256_after != report.live_sha256_before:
            _fail(report, "the live db changed during the drill (sha256 differs)")
        if report.live_home_observed:
            _fail(report, "the live home changed during a quiescent drill: " + "; ".join(report.live_home_observed))
    elif report.live_home_observed:
        report.sections.append(
            f"live home: {len(report.live_home_observed)} change(s) observed during drill, not attributed")


def _write_scratch_config(home: Path, scratch: Path) -> None:
    """A minimal config.json in the scratch home (the server refuses to open a graph without one). Only the
    `embedding` key is carried over from the live config, so the same model is used."""
    cfg: dict = {}
    try:
        live_cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
        if isinstance(live_cfg, dict) and isinstance(live_cfg.get("embedding"), dict):
            cfg["embedding"] = live_cfg["embedding"]
    except (OSError, ValueError):
        pass
    with open(scratch / "config.json", "x", encoding="utf-8") as f:
        json.dump(cfg, f)


# ------------------------------------------------------------------ CLI

def format_report(report: DrillReport) -> str:
    lines = list(report.sections)
    lines.append("classification (write-once failures / mutable reported / lifecycle and audit_gated columns gated by the "
                 "audit record / keyed metadata / unclassified treated as write-once):")
    for table, c in sorted(report.classification.items()):
        lines.append(f"  {table}: write_once={','.join(c[WRITE_ONCE]) or '-'}; mutable={','.join(c[MUTABLE]) or '-'}; "
                     f"lifecycle={','.join(c.get(LIFECYCLE, [])) or '-'}; "
                     f"audit_gated={','.join(c.get(AUDIT_GATED, [])) or '-'}; keyed={','.join(c.get(KEYED, [])) or '-'}; "
                     f"unclassified={','.join(c[UNCLASSIFIED]) or '-'}")
    if report.ambiguous:
        lines.append(f"ambiguous: dump time unknown: {len(report.ambiguous)} difference(s) explained only by a record "
                     f"between the two commits of knowledge.sql (reported, not failed): " + "; ".join(
                         f"nodes id={a['key']!r} column {a['column']} recorded {a['recorded']}"
                         for a in report.ambiguous[:20]) + (" ..." if len(report.ambiguous) > 20 else ""))
    lines.append("known gap (a writer that leaves no audit row: a difference it makes fails, it is not told apart "
                 "from corruption): " + " | ".join(report.known_gaps))
    for pair, t in sorted(report.lifecycle_transitions.items()):
        lines.append(f"named transition {pair} x{t['count']} (reported, allowed): {t['reason']}")
    if report.metadata_mutable_diffs:
        lines.append("metadata keys that differ from live (named mutable, reported): " + ", ".join(
            f"{k}={v}" for k, v in sorted(report.metadata_mutable_diffs.items())))
    if report.metadata_unknown:
        lines.append("UNKNOWN metadata keys (not classified; treated as write-once, a difference fails): " + ", ".join(
            f"{k}={v}" for k, v in sorted(report.metadata_unknown.items())))
    if report.excluded:
        lines.append(f"not compared: columns {report.excluded.get('columns')}; tables "
                     f"{report.excluded.get('tables')}; live-only tables {report.excluded.get('live_only_tables')}; "
                     f"schema differences {report.excluded.get('schema_differs', {})}")
    if report.mutable_diffs:
        lines.append("mutable columns that differ from live (reported): " + ", ".join(
            f"{k}={v}" for k, v in sorted(report.mutable_diffs.items())))
    if report.live_only:
        lines.append("live_only nodes: " + ", ".join(report.live_only[:20]) + (" ..." if len(report.live_only) > 20 else ""))
    for s in report.skipped:
        lines.append(f"skipped: {s}")
    for f in report.failures:
        lines.append(f"FAILURE: {f}")
    lines.append(f"restore drill {'FAILED' if report.failures else 'PASSED'} in {report.seconds}s "
                 f"(nodes_restored={report.nodes_restored} rows_compared={report.rows_compared} "
                 f"live_only={len(report.live_only)} ambiguous={report.ambiguous_count} vectors_missing={len(report.vectors_missing)} "
                 f"live_vectors_stale={report.live_vectors_stale} transcripts_replayed={report.transcripts_replayed} "
                 f"transcript_live_only_bytes={report.transcript_live_only_bytes} failures={len(report.failures)}"
                 f"{'; embedding skipped' if report.embedding == 'skipped' else ''})")
    return "\n".join(lines)


def _parse_args(argv):
    p = argparse.ArgumentParser(
        prog="restore_drill",
        description="Restore the committed backup and the transcript archive into a scratch directory and compare them with the live graph.")
    p.add_argument("--home", default=None, help="ENGRAM home (default: $ENGRAM_HOME or ~/.engram).")
    p.add_argument("--scratch", default=None, help="Scratch directory (absent or empty, outside the home). Default: a temp dir, removed at the end.")
    p.add_argument("--sample", type=int, default=DEFAULT_SAMPLE, help="Nodes re-embedded a second time and compared (default %(default)s).")
    p.add_argument("--no-embed", action="store_true", help="Skip the re-embed (cheap smoke test; no model needed).")
    p.add_argument("--json", action="store_true", help="Print the report as JSON.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dump", default=None, metavar="PATH", help="Load this dump file instead of the home's knowledge.sql.")
    g.add_argument("--from-git", action="store_true", help="Load `git show HEAD:knowledge.sql` from the home's repo.")
    p.add_argument("--source", default=None, help="Live transcript root (default: ~/.claude/projects).")
    p.add_argument("--assert-quiescent", action="store_true",
                   help="Treat any change to the live db, its side files or sessions/ during the drill as a failure.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        report = run_drill(home=args.home, scratch=args.scratch, sample=args.sample, embed=not args.no_embed,
                           dump=args.dump, from_git=args.from_git, source=args.source,
                           assert_quiescent=args.assert_quiescent)
    except (DrillRefused, ValueError, OSError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_json(), indent=2, sort_keys=True) if args.json else format_report(report))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
