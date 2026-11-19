#!/usr/bin/env python3
"""Three typed reads of the live store, for the agent documents that used to open knowledge.db themselves
(#2652 unit 02: ledger rows 1 and 42) and for the scripted installer's step-6 node count (#2652 unit 04: row 81).

    store_read.py ids --since <iso|""> [--db PATH]    the ids of the current, non-retracted observations
                                                      (observation_factual, observation_predictive) created at
                                                      or after --since, one per line, sorted ascending;
                                                      --since "" means the whole graph
    store_read.py axiom-count [--db PATH]             the number of axiom nodes (0 = an empty seed)
    store_read.py node-count [--db PATH]              the number of rows in `nodes` (0 = an empty store, which the
                                                      installer re-seeds); NOT axiom-count, which reads 0 for a
                                                      store that has nodes but no axioms

There is no free SQL subcommand: exactly these three reads, each a literal statement registered as a read template
(tools/engramd_read_scan.config.json roots them), so the engramd owner admits them byte for byte.

Which path a run takes (the install self-check's ruled shape, #4118), decided once and named on ONE stderr line:

  flag OFF (engramd.store_read_reads_via_owner, default OFF), or --db is not this home's store
                                       -> store_read: path=direct-ro (flag off or not this home's store)
  flag ON and the owner answers a ping -> store_read: path=owner
  flag ON and the ping is Unavailable with reason `down` (nothing is listening)
                                       -> store_read: path=direct-ro (owner down)
  flag ON and the ping fails any other way (rejected, malformed, timeout, busy, ...), or the owner's read
  fails after a good ping              -> store_read: refused (<the owner's answer>), exit 3, and the store is
                                          never opened: no direct open beside a live owner

An owner socket path over the AF_UNIX limit is reason `path_too_long`, never `down` (#4868), so it is refused
like the rest: an owner started under a shorter spelling of the same ENGRAM_HOME (a symlink) can be live while
this spelling's socket path cannot be addressed.

The tool never writes: every direct open is `mode=ro`.

Exit codes: 0 ok; 1 the store path is not a usable store (missing, uncheckable, not a regular file, or a file
sqlite cannot read as a store; the class is named on stderr); 2 usage (argparse) or a RELATIVE ENGRAM_HOME;
3 the owner refused or its answer could not be used.

Stdlib only. Python 3.10+: the flag-ON path imports the runtime client, which needs 3.10 (the scripted
installer already refuses a python3 older than 3.10.7 before its step 6 calls node-count).
"""

import argparse
import sqlite3
import sys
from pathlib import Path

# Locate the shared tools helper (same pattern as tools/inspect_raw.py).
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import _owner_read as owner_read  # noqa: E402

FLAG_KEY = "store_read_reads_via_owner"
STORE_UNUSABLE_EXIT = 1
BAD_HOME_EXIT = 2
REFUSED_EXIT = 3


class StoreReadOwnerError(Exception):
    """The owner could not give a read honestly (flag ON). Carries the helper's own wording."""


def _one_line(text) -> str:
    return " ".join(str(text).split())


def _say(message: str) -> None:
    sys.stderr.write("store_read: %s\n" % message)
    sys.stderr.flush()


def _spec(home: Path):
    return owner_read.OwnerReadSpec(
        flag_key=FLAG_KEY, error_cls=StoreReadOwnerError, home=home,
        never_empty="An unreadable store is a refusal here, never an empty id list or a zero count.")


def _open_ro(db_path):
    """The one direct open: read-only, the path percent-encoded so a `?` or `#` cannot read as a uri parameter."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def _decide(spec, db_path):
    """(mode, label, refusal): mode is "owner", "direct" or "refused". Decided once per run."""
    if not owner_read.flag_applies(spec, db_path):
        return "direct", "direct-ro (flag off or not this home's store)", None
    try:
        client = owner_read.import_runtime("engramd_client")
        result = client.read("ping", {}, timeout=spec.per_call, socket_path=client.socket_path_for_home(spec.home))
    except (Exception, SystemExit) as exc:  # client.read does not raise by contract; belt
        return "refused", None, "the owner could not be pinged (%s: %s)" % (type(exc).__name__, exc)
    if isinstance(result, client.Ok):
        return "owner", "owner", None
    if isinstance(result, client.Unavailable) and result.reason == "down":
        return "direct", "direct-ro (owner down)", None
    return "refused", None, str(owner_read.failure(spec, client, result))


# The three statements. Each is a literal at its execute call so the read-template scanner registers it
# (tools/engramd_read_scan.config.json roots these three functions).
def read_ids(con, since):
    """The row-1 population: ids sorted ascending in Python. Both paths run the same statement."""
    rows = con.execute(
        "SELECT id FROM nodes WHERE is_current = 1 AND status != 'retracted' "
        "AND type IN ('observation_factual', 'observation_predictive') AND created_at >= ?",
        (since,),
    ).fetchall()
    ids = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 1 or not isinstance(row[0], str):
            raise ValueError("a row of the answer is not one id")
        ids.append(row[0])
    return sorted(ids)


def _one_integer(rows):
    if (not isinstance(rows, (list, tuple)) or len(rows) != 1 or not isinstance(rows[0], (list, tuple))
            or len(rows[0]) != 1 or isinstance(rows[0][0], bool) or not isinstance(rows[0][0], int)):
        raise ValueError("the answer is not one integer")
    return rows[0][0]


def read_axiom_count(con):
    """The row-42 count of axiom nodes."""
    return _one_integer(con.execute("SELECT COUNT(*) FROM nodes WHERE type='axiom'").fetchall())


def read_node_count(con):
    """The installer's row-81 count of all rows in `nodes`.

    On the direct path, a store with no `nodes` table (a zero-byte file, an aborted seed) counts 0: such a file is an
    empty store, and re-seeding it is what the installer has always done. That is the ONE read problem that yields 0.
    Every other failure raises, because 0 means re-seed, and a store we could not read must never be re-seeded over.
    Through the owner the same missing table is the owner's error, so it is a refusal (exit 3), never 0: the safe
    side."""
    try:
        rows = con.execute("SELECT COUNT(*) FROM nodes").fetchall()
    except sqlite3.OperationalError as exc:
        if str(exc) != "no such table: nodes":
            raise
        return 0
    return _one_integer(rows)


def _parser():
    p = argparse.ArgumentParser(prog="store_read.py", description="Three typed read-only reads of the ENGRAM store.")
    sub = p.add_subparsers(dest="cmd", required=True)
    ids = sub.add_parser("ids", help="ids of current non-retracted observations created at or after --since")
    ids.add_argument("--since", required=True, help='ISO timestamp; "" means the whole graph')
    ids.add_argument("--db", default=None, help="store path (default: $ENGRAM_HOME/knowledge.db)")
    cnt = sub.add_parser("axiom-count", help="the number of axiom nodes")
    cnt.add_argument("--db", default=None, help="store path (default: $ENGRAM_HOME/knowledge.db)")
    nodes = sub.add_parser("node-count", help="the number of rows in nodes (the installer's empty-store check)")
    nodes.add_argument("--db", default=None, help="store path (default: $ENGRAM_HOME/knowledge.db)")
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        home = owner_read.engram_home()
    except ValueError as exc:
        _say(str(exc))
        return BAD_HOME_EXIT
    db_path = Path(args.db) if args.db else home / "knowledge.db"
    spec = _spec(home)
    mode, label, refusal = _decide(spec, db_path)
    if mode == "refused":
        _say("refused (%s)" % _one_line(refusal))
        return REFUSED_EXIT
    con = None
    try:
        if mode == "owner":
            con = owner_read.open_reader(spec, db_path, use_owner=True)
        else:
            owner_read.check_store_path(db_path)  # a FIFO is refused here, never opened
            con = _open_ro(db_path)
        _say("path=%s" % label)
        if args.cmd == "ids":
            for one in read_ids(con, args.since):
                print(one)
        elif args.cmd == "axiom-count":
            print(read_axiom_count(con))
        else:
            print(read_node_count(con))
    except StoreReadOwnerError as exc:
        _say("refused (%s)" % _one_line(exc))
        return REFUSED_EXIT
    except ValueError as exc:
        _say("refused (the owner's answer is malformed: %s)" % _one_line(exc))
        return REFUSED_EXIT
    except (owner_read.StoreUnusable, FileNotFoundError) as exc:
        _say("%s: %s" % (type(exc).__name__, _one_line(exc)))
        return STORE_UNUSABLE_EXIT
    except sqlite3.Error as exc:
        _say("%s: %s does not read as a store (%s)" % (type(exc).__name__, db_path, _one_line(exc)))
        return STORE_UNUSABLE_EXIT
    finally:
        if con is not None:
            con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
