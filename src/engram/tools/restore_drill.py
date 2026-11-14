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
       mutable      recall counts, last-recalled times, importance and utility scores, lifecycle
                    state, metadata and similar bookkeeping. A difference is REPORTED with
                    per-column counts, never failed: a working seat changes them after the dump.
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
     must equal the restored vectors exactly. A live vector that differs from the restored one
     (embedded before its summary existed, and so on) is counted in ``live_vectors_stale``, never
     failed: the live vectors are not the reference. "Differs" means any element off by more than
     1e-5, so float noise between machines is not read as staleness.
  6. Replays the daily transcript archive (``archive_transcripts.replay``) into
     ``<scratch>/transcripts`` and compares each replayed file with the live transcript up to the
     archived offset (sha256 of the first N bytes). Bytes past the offset are reported
     (``transcript_live_only_bytes``), not failed.

The live graph is only READ. The live database is opened read-only (``mode=ro``); when it has no
``-wal``/``-shm`` side files yet, ``immutable=1`` is used instead, because a read-only open of a
quiescent WAL-mode database would itself create those files. The drill records the size and mtime of
the live db, its side files and ``sessions/`` before and after. With ``--assert-quiescent`` (a
fixture, or a seat that is stopped) any change fails the drill and the live db's sha256 must be
unchanged; on a working seat the changes are listed as "observed during drill, not attributed".

Exit codes: 0 pass; 1 the drill FAILED (a comparison failed); 2 it could not run (no dump, an
unreadable db, a scratch that is not empty or lies in the live home). Nothing is written on exit 2.

Cost: about two minutes on a real graph, almost all of it the re-embed; run on demand, not per sleep.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
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
        "status", "resolved_by", "confidence", "confidence_history", "superseded_by", "is_current",
        "metadata", "importance_base", "importance_score", "recall_turn", "recall_count",
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
UNCLASSIFIED = "unclassified"


def classify(table: str, column: str) -> str:
    """write_once | mutable | unclassified (the last is TREATED as write-once by the comparison)."""
    if table in _WRITE_ONCE_TABLES:
        return WRITE_ONCE
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

    def to_json(self) -> dict:
        d = asdict(self)
        d["passed"] = not self.failures
        return d


def _fail(report: DrillReport, message: str) -> None:
    report.failures.append(message)


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
    """Read-only. mode=ro when the side files already exist (a live seat); immutable=1 when they do not,
    because a read-only open of a quiescent WAL-mode db creates -wal and -shm (measured)."""
    base = db.resolve().as_uri()
    has_side = os.path.exists(str(db) + "-wal") or os.path.exists(str(db) + "-shm")
    uri = base + ("?mode=ro" if has_side else "?immutable=1")
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


def _scratch_env(scratch: str, *, embedder_off: bool) -> dict:
    """The subprocess environment: ENGRAM_HOME and HOME both the scratch dir. Every inherited ENGRAM_* and
    CLAUDE_* variable is dropped (a test bypass or a live path must not ride in)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ENGRAM_", "CLAUDE_"))}
    env["ENGRAM_HOME"] = scratch
    env["HOME"] = scratch
    env["PYTHONPATH"] = _PKG_ROOT
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if embedder_off:
        env["ENGRAM_NO_EMBEDDINGS"] = "1"
    else:
        # HOME is the scratch dir, so the model cache must be named explicitly (a read of the existing cache).
        env.setdefault("HF_HOME", os.path.join(os.path.expanduser("~"), ".cache", "huggingface"))
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


def _embed_texts(texts: list, scratch: str) -> list:
    """THE embedding seam. One vector (list of floats) or None per text, in order. The real
    implementation runs the server's embedder in a subprocess rooted at the scratch home; tests replace
    this function, so the model is never loaded in a test."""
    if not texts:
        return []
    res = _run_scratch_python(_EMBED_SCRIPT, [], scratch, embedder_off=False,
                              stdin=json.dumps(texts), timeout=_EMBED_TIMEOUT_S)
    return res["vectors"]


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


def _table_names(conn: sqlite3.Connection) -> list:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def _is_excluded_table(name: str) -> bool:
    return name.startswith(_EXCLUDED_PREFIXES)


def _columns(conn: sqlite3.Connection, table: str) -> list:
    """[(name, pk_position)] in table order."""
    return [(r[1], r[5]) for r in conn.execute(f"PRAGMA table_info({_q(table)})").fetchall()]


def _compare_tables(report: DrillReport, restored: sqlite3.Connection, live: sqlite3.Connection) -> None:
    rtables = [t for t in _table_names(restored) if not _is_excluded_table(t)]
    ltables = set(_table_names(live))
    excluded_tables = sorted(t for t in _table_names(restored) if _is_excluded_table(t))
    excluded_tables += sorted(t for t in ltables if _is_excluded_table(t) and t not in excluded_tables)
    live_only_tables = sorted(t for t in ltables if not _is_excluded_table(t) and t not in rtables)
    report.excluded = {"columns": list(_EXCLUDED_COLUMNS), "tables": sorted(set(excluded_tables)),
                       "live_only_tables": live_only_tables}
    for table in rtables:
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
        fails_here = 0
        for key, rvals in rrows.items():
            lvals = lrows.get(key)
            if lvals is None:
                fails_here += 1
                if fails_here <= _MAX_FAIL_LINES_PER_TABLE:
                    _fail(report, f"{table} {_keystr(keycols, key)}: in the dump, missing from live (deleted)")
                continue
            report.rows_compared += 1
            if rvals == lvals:
                continue
            for c, rv, lv in zip(common, rvals, lvals):
                if rv == lv:
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
        for nid, vec in zip(picked, again):
            stored = json.loads(conn.execute("SELECT embedding FROM nodes WHERE id = ?", (nid,)).fetchone()[0])
            diff = _max_abs_diff(vec, stored)
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
            _compare_tables(report, rconn, live)
        finally:
            rconn.close()
        nfail = len(report.failures)
        report.sections.append(
            f"rows: {report.nodes_restored} nodes restored; {report.rows_compared} rows compared; "
            f"{len(report.live_only)} live_only nodes; {sum(report.mutable_diffs.values())} mutable-column "
            f"differences reported; {nfail} failures")
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
                    f"sample {report.vector_sample_checked} re-embedded and equal to the restored copy (within {SAMPLE_TOLERANCE:g}); "
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
    lines.append("classification (write-once failures / mutable reported / unclassified treated as write-once):")
    for table, c in sorted(report.classification.items()):
        lines.append(f"  {table}: write_once={','.join(c[WRITE_ONCE]) or '-'}; mutable={','.join(c[MUTABLE]) or '-'}; "
                     f"unclassified={','.join(c[UNCLASSIFIED]) or '-'}")
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
                 f"live_only={len(report.live_only)} vectors_missing={len(report.vectors_missing)} "
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
    except (DrillRefused, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_json(), indent=2, sort_keys=True) if args.json else format_report(report))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
