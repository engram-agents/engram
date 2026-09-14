#!/usr/bin/env python3
"""#2411 migration verification — run on a byte COPY of YOUR OWN graph.

Two-signal control: an independent structured pre-count (an instrument the
migration does not touch) must equal the migration's own reported node count.
Agreement of the two is the check; the migration's count alone is circular.

Never touches your live DB. Never touches another seat's graph.
"""
import json, os, shutil, sqlite3, subprocess, sys, tempfile

def _first(cands, what):
    """Seat-portable path resolution. Ariadne caught a hardcoded interpreter
    here that was right on MY seat and absent on hers — the same shape as the
    hardcoded-baseline hazard she flagged in her own script an hour earlier.
    Neither of us wrote a wrong VALUE; we each wrote one that was true where
    we were standing, which is exactly what re-reading your own script cannot
    surface. So every seat-specific path is probed, and a miss names every
    candidate tried instead of asserting one."""
    for c in cands:
        if c and os.path.exists(c):
            return c
    sys.exit(f"FAIL: could not locate {what}. Tried:\n" +
             "\n".join(f"  - {c}" for c in cands if c) +
             f"\nSet the matching env var and re-run "
             f"(ENGRAM_HOME / ENGRAM_REPO / ENGRAM_PY).")

ENGRAM_HOME = os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram")
REPO = _first([os.environ.get("ENGRAM_REPO"),
               os.path.expanduser("~/engram-alpha"),
               os.path.expanduser("~/src/engram-alpha"),
               os.path.expanduser("~/GitHub/engram-alpha")], "the engram-alpha checkout")
LIVE = _first([os.path.join(ENGRAM_HOME, "knowledge.db")], "your knowledge.db")
PY   = _first([os.environ.get("ENGRAM_PY"),
               os.path.join(ENGRAM_HOME, "venv/bin/python3"),
               os.path.join(REPO, ".venv/bin/python3"),
               os.path.join(REPO, "venv/bin/python3")], "a python3 with the repo deps")

BASELINE = os.path.join(ENGRAM_HOME, "rc1-baseline.json")

def bank(nodes, entries, events, by_op, anomalies, gate_past):
    """Write this seat's OWN baseline where a post-upgrade check can read it.
    Hand-copying a hardcoded baseline is the failure Ariadne named: a seat
    that substitutes nothing prints MATCH against someone else's numbers,
    which is worse than no check at all. A file removes the substitution step."""
    rec = {"seat": os.environ.get("USER", "unknown"), "measured_at_utc": None,
           "nodes": nodes, "markers": entries, "events": events,
           "by_op": by_op, "anomalies": anomalies,
           "gate_already_past": gate_past,
           "expect_migration_line_tonight": not gate_past}
    import datetime
    rec["measured_at_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        if os.path.exists(BASELINE):
            print(f"NOTE: {BASELINE} already exists — NOT overwriting. "
                  f"A baseline is written once, pre-upgrade; a second write "
                  f"could silently record a post-migration reading.")
            return
        with open(BASELINE, "w") as fh:
            json.dump(rec, fh, indent=2)
        print(f"BANKED -> {BASELINE}")
    except OSError as e:
        print(f"NOTE: could not write {BASELINE} ({e}) — paste the line above instead.")


def _report_clean(exp_nodes, exp_entries, exp_events, by_op, anom, already_migrated=False):
    """Shared CLEAN-result reporting for the two paths that can reach it: a
    migration that just ran this call, and one whose gate was already past
    but whose existing cascade_markers already agree with the independent
    pre-count (added after this exact case gave a false FAIL — see the
    'already migrated, matches' branch in main())."""
    qualifier = " (migrated earlier, not by this run)" if already_migrated else ""
    print(f"\nRESULT: CLEAN{qualifier} — two independent signals agree at "
          f"{exp_nodes} nodes / {exp_entries} markers / {exp_events} events.")
    # CURRENT is the qualifier that decides it: rung 5 selects
    # "WHERE is_current = 1", so NON-current tainted_by exercises nothing.
    # (Ariadne caught my earlier ask omitting this — it would have
    # collected seats reading raw counts who test nothing.)
    if by_op["retract"] == 0:
        print("COVERAGE: tainted_by->retract NOT exercised (0 CURRENT tainted_by).")
    else:
        print(f"COVERAGE: *** this seat exercises tainted_by->retract "
              f"({by_op['retract']} CURRENT) — POST THIS. No measured seat has. ***")
    print(f"COVERAGE: anomaly branch {'exercised (%d)' % anom if anom else 'NOT exercised'}.")
    print("\nPRE-REGISTER THESE for tonight, before you upgrade — paste to #2411:")
    print(f"  seat=<you> nodes={exp_nodes} markers={exp_entries} events={exp_events} "
          f"by_op={{'supersede': {by_op['supersede']}, 'retract': {by_op['retract']}}} anomalies={anom}")
    if already_migrated:
        print("  (the gate is already past on your live DB too — tonight's real "
              "\n   migration will find nothing to do and print no line; these numbers "
              "\n   are your already-migrated baseline instead of a pre-migration one)")
    else:
        print("  (tonight's real migration must print exactly this node count; anything"
              "\n   else is a signal rather than a number with nothing to compare against)")
    bank(exp_nodes, exp_entries, exp_events, by_op, anom, already_migrated)


def _bf_scoped_counts(c):
    """Backfill-origin-only counts (rows, nodes, events, anomalies, by_op),
    scoped via `backfill_provenance IS NOT NULL`.

    A live retract/supersede cascade (engram_revision.py's cascade-marking,
    not the one-time backfill in engram_core.py) inserts into
    cascade_markers with backfill_provenance left NULL -- and precount()
    can only ever see legacy nodes.metadata, which is retired as a store
    the moment a seat is migrated (engram_core.py:7190). An UNSCOPED table
    read therefore diverges from precount() on any already-migrated seat
    that did an ordinary supersede/retract-with-dependents before running
    this script: the SAME false-FAIL class this script exists to remove,
    one case over (Borges's colleague catch, PR #2461 review, 2026-08-30).

    IS NOT NULL, not '= recovered': precount() reconstructs every legacy
    backfill entry regardless of whether its original timestamp was
    recoverable from edit_history -- the same set the migration's own
    self-check counts as its TOTAL marker count (engram_core.py:3409), not
    just the 'recovered' subset it separately reports as a sub-metric
    (:3413). Matching that existing scoping choice, not guessing a new one."""
    rows   = c.execute("SELECT COUNT(*) FROM cascade_markers "
                        "WHERE backfill_provenance IS NOT NULL").fetchone()[0]
    rnodes = c.execute("SELECT COUNT(DISTINCT node_id) FROM cascade_markers "
                        "WHERE backfill_provenance IS NOT NULL").fetchone()[0]
    events = c.execute("SELECT COUNT(DISTINCT cascade_event) FROM cascade_markers "
                        "WHERE backfill_provenance IS NOT NULL").fetchone()[0]
    anom   = c.execute("SELECT COUNT(*) FROM cascade_markers WHERE backfill_anomaly "
                        "AND backfill_provenance IS NOT NULL").fetchone()[0]
    ops    = dict(c.execute("SELECT root_op, COUNT(*) FROM cascade_markers "
                             "WHERE backfill_provenance IS NOT NULL "
                             "GROUP BY root_op").fetchall())
    return rows, rnodes, events, anom, ops


def precount(db):
    """Structured key-presence. NOT truthiness (eats empty lists, under-counts).
    NOT substring (eats the phrase in prose, over-counts). Both were measured
    wrong today, in opposite directions."""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True); c.row_factory = sqlite3.Row
    nodes, entries, roots, by_op = set(), 0, set(), {"supersede": 0, "retract": 0}
    for r in c.execute("SELECT id, is_current, metadata FROM nodes "
                       "WHERE metadata LIKE '%stale_by%' OR metadata LIKE '%tainted_by%'"):
        if not r["is_current"]:
            continue
        try: m = json.loads(r["metadata"] or "{}")
        except Exception: continue
        if not isinstance(m, dict): continue
        for key, op in (("stale_by", "supersede"), ("tainted_by", "retract")):
            v = m.get(key)
            if not v: continue                       # absent OR empty -> not a member
            vals = v if isinstance(v, list) else [v]
            for x in vals:
                nodes.add(r["id"]); entries += 1; by_op[op] += 1
                roots.add((op, x if isinstance(x, str) else "__unparseable__"))
    c.close()
    return len(nodes), entries, len(roots), by_op

def main():
    print(f"USING: repo={REPO}\n       python={PY}\n       graph={LIVE}")
    tmp = tempfile.mkdtemp(prefix="engram_2411_verify_")
    try:
        copy = os.path.join(tmp, "knowledge.db")
        shutil.copy2(LIVE, copy)                      # COPY. Live DB untouched.
        # An always-on MCP server keeps the DB in WAL mode with an active
        # (uncommitted) -wal file; copying only the main file leaves an
        # inconsistent snapshot sqlite3 refuses to open ("unable to open
        # database file"). Copy the -wal/-shm companions too if present --
        # still read-only against the originals (copy2, not move), live DB
        # untouched either way. (Found on a macOS seat whose server stays up
        # continuously all session -- no other measured seat had hit this,
        # since none had that much pending WAL data. #2411, 2026-08-30.)
        for suffix in ("-wal", "-shm"):
            src = LIVE + suffix
            if os.path.exists(src):
                shutil.copy2(src, copy + suffix)
        assert os.path.realpath(copy) != os.path.realpath(LIVE), "refusing to run on the live DB"

        exp_nodes, exp_entries, exp_events, by_op = precount(copy)
        pre = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        had_table = bool(pre.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                     "AND name='cascade_markers'").fetchone())
        pre.close()
        print(f"PRE-COUNT (independent):  {exp_nodes} nodes, {exp_entries} entries, "
              f"{exp_events} distinct (op,root)")
        print(f"  by op: supersede={by_op['supersede']} retract={by_op['retract']}")
        if had_table:
            print("NOTE: this copy ALREADY has cascade_markers — this seat is already "
                  "migrated, so this is not a pre-migration baseline. Report it as such.")

        env = dict(os.environ, ENGRAM_NO_EMBEDDINGS="1")
        out = subprocess.run(
            [PY, "-c",
             "import sys; sys.path.insert(0, %r)\n"
             "import engram_core as core\n"
             "core._configure_paths(%r); core._ensure_data_dir()\n"
             "c = core._get_db(); c.close()\n" % (os.path.join(REPO, "src/engram"), tmp)],
            capture_output=True, text=True, env=env, cwd=REPO)
        line = next((l for l in (out.stdout + out.stderr).splitlines()
                     if "cascade-marker backfill" in l), None)
        # The migration may legitimately NOT report: rung 5 is gated on
        # PRAGMA user_version, so a seat already past that gate skips it.
        # That is BENIGN when there was nothing to migrate and DANGEROUS
        # when there was — the legacy markers would never migrate and
        # nothing would say so. Discriminate, do not conflate.
        # (Sol's seat surfaced this: cascade_markers present, 0 rows.)
        if line is None:
            c = sqlite3.connect(copy)
            try:
                total_rows = c.execute("SELECT COUNT(*) FROM cascade_markers").fetchone()[0]
            except sqlite3.OperationalError:
                total_rows = None          # table absent entirely
            uv = c.execute("PRAGMA user_version").fetchone()[0]
            # Defined unconditionally so the "already migrated, matches" check
            # below never depends on and-chain short-circuit order for safety
            # (a reviewer-fairy flagged the implicit version as fragile to a
            # future reorder — PR #2461 review, 2026-08-30). Backfill-origin
            # scoped (see _bf_scoped_counts) so a live-path marker (from an
            # ordinary post-migration supersede/retract, not the backfill)
            # can't inflate these against a precount that will never see it.
            rows, rnodes, events, anom, ops = 0, 0, 0, 0, {}
            if total_rows:
                rows, rnodes, events, anom, ops = _bf_scoped_counts(c)
            c.close()
            print(f"MIGRATION DID NOT REPORT (user_version={uv}, "
                  f"cascade_markers rows={total_rows if total_rows is not None else 'NO TABLE'} "
                  f"total, {rows} backfill-origin)")
            if exp_nodes == 0 and rows == 0:
                print("\nRESULT: CLEAN — nothing to migrate on this seat, and the "
                      "migration is already gated off. Both signals agree at zero.")
                print("COVERAGE: this seat exercises NO migration branch at all.")
                print(f"\nPRE-REGISTER: seat=<you> nodes=0 markers=0 events=0 "
                      f"(gate already past; expect no migration line tonight)")
                bank(0, 0, 0, {"supersede": 0, "retract": 0}, 0, True)
                return
            # Third case, missed until a real seat hit it (#2411, 2026-08-30):
            # gate already past AND legacy markers exist AND the existing
            # cascade_markers rows already agree with the independent
            # pre-count on every dimension. That is a seat whose migration
            # ran successfully at some earlier DB touch (rung 5 fires lazily
            # on ANY touch, not just tonight's) — a clean pass, not a failure.
            # Conflating it with "gated off and truly unmigrated" produced a
            # false FAIL on a seat whose MCP server had been active all day.
            if (rows and rnodes == exp_nodes and rows == exp_entries
                    and events == exp_events and ops.get("supersede", 0) == by_op["supersede"]
                    and ops.get("retract", 0) == by_op["retract"]):
                print("NOTE: gate already past, but existing cascade_markers already "
                      "agree with the independent pre-count on every dimension — this "
                      "seat's migration completed at an earlier DB touch, not tonight.")
                _report_clean(exp_nodes, exp_entries, exp_events, by_op, anom,
                              already_migrated=True)
                return
            sys.exit(f"RESULT: FAIL — the migration is gated off (user_version={uv}) "
                     f"but this seat has {exp_nodes} nodes of legacy markers to "
                     f"migrate and the existing cascade_markers state does not "
                     f"account for them. They will NOT be migrated and nothing "
                     f"else would have said so. Do not upgrade until this is "
                     f"understood.")
        print(f"MIGRATION SAYS: {line.strip()}")

        got = int(line.split("backfill):")[1].split("nodes")[0].strip())
        c = sqlite3.connect(copy)
        # Same backfill-origin scoping as the gate-already-past branch (see
        # _bf_scoped_counts) — a no-op on a fresh migration's first run
        # (the table can't hold a live-path marker before it exists to
        # cascade-mark into), but keeps both CLEAN-reaching code paths
        # reading cascade_markers the same way rather than one scoped and
        # one not (Borges's Suggestion 1, PR #2461 review).
        rows, rnodes, events, anom, ops = _bf_scoped_counts(c)
        c.close()
        print(f"IN TABLE: {rows} rows, {rnodes} nodes, {events} events, "
              f"{anom} anomalies, by_op={ops}")

        fails = []
        if got != exp_nodes:    fails.append(f"migration nodes {got} != independent pre-count {exp_nodes}")
        if rnodes != exp_nodes: fails.append(f"table nodes {rnodes} != pre-count {exp_nodes}")
        if rows != exp_entries: fails.append(f"table rows {rows} != pre-count entries {exp_entries}")
        if events != exp_events:fails.append(f"table events {events} != pre-count (op,root) {exp_events}")
        if fails:
            print("\n".join("MISMATCH: " + f for f in fails))
            sys.exit("RESULT: FAIL — do not upgrade until this is understood.")
        _report_clean(exp_nodes, exp_entries, exp_events, by_op, anom)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

main()
