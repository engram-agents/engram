#!/usr/bin/env python3
"""Arm E helper: the high-IDF "novel terms" of a step's thinking (stdin).

    python3 novel_terms.py --state <probeDir>/<sessionId>/novel-terms-seen.json \
        [--db ~/.engram/knowledge.db] [--top-k 8] < thinking.txt

Prints one JSON object on stdout: {"terms": [...], "novel": [...], "db": "<graph used>"}.
"novel" = terms not seen earlier in THIS probe session; the seen set is the
state file, rewritten atomically after every run.

Parity with today's in-turn recall (engram-lesson-tripwire-hook.py): the same
`engram_idf.extract_keywords` call, the same stopwords (STOPWORDS | JUNK_STOPLIST)
and the same graph-size-relative floor, min_idf = 0.7 * ln(n_current_nodes)
(`min_idf_ratio` default 0.7, tripwire hook `_in_turn_config()`, line 1066).
The graph (default $ENGRAM_HOME/knowledge.db, else ~/.engram/knowledge.db) is opened read-only. Stdlib + engram_idf only.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

MIN_IDF_RATIO = 0.7  # tripwire hook _in_turn_config() default, line 1066
DEFAULT_TOP_K = 8


def default_db() -> Path:
    """$ENGRAM_HOME/knowledge.db when ENGRAM_HOME is set, else ~/.engram/knowledge.db.

    A seat with a non-default ENGRAM_HOME must not silently measure arm E against
    another graph; the resolved path is printed in the output (`db`).
    """
    home = os.environ.get("ENGRAM_HOME")
    return (Path(home) if home else Path.home() / ".engram") / "knowledge.db"


def _has_idf(directory: Path) -> Path | None:
    """The dir holding engram_idf.py: `directory` itself, or its src/engram."""
    for candidate in (directory, directory / "src" / "engram"):
        if (candidate / "engram_idf.py").is_file():
            return candidate
    return None


def resolve_runtime_dir() -> Path:
    """Locate engram_idf.py without ever assuming the source layout: explicit
    override, then $CLAUDE_PLUGIN_ROOT and the installed plugin dir (each
    walked upward), then this file's own ancestors. Never a fixed parents[N]."""
    roots: list[Path] = []
    for env in ("ENGRAM_RUNTIME_DIR", "CLAUDE_PLUGIN_ROOT"):
        if os.environ.get(env):
            roots.append(Path(os.environ[env]))
    roots.append(Path.home() / ".engram" / "marketplace" / "plugins" / "engram")
    roots.append(Path(__file__).resolve())
    for root in roots:
        for directory in [root, *root.parents]:
            found = _has_idf(directory)
            if found is not None:
                return found
    raise SystemExit("novel_terms: cannot find engram_idf.py (set ENGRAM_RUNTIME_DIR)")


def load_seen(state: Path) -> set[str]:
    try:
        data = json.loads(state.read_text())
        return {str(t) for t in data.get("seen", [])}
    except FileNotFoundError:
        return set()
    except (OSError, ValueError, AttributeError) as err:
        raise SystemExit(f"novel_terms: unreadable state file {state}: {err}")


def save_seen(state: Path, seen: set[str]) -> None:
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # the temp file below is 0600 (mkstemp)
    fd, tmp = tempfile.mkstemp(dir=state.parent, prefix=".novel-terms-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"seen": sorted(seen)}, f)
        os.replace(tmp, state)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def check_db(db: Path) -> None:
    """Refuse an unusable --db with one line and exit 1 (#3962), before anything opens it: a FIFO would hang the
    `mode=ro` connect. The repo root is found by name (the nearest ancestor holding tools/_owner_read.py, never a
    fixed parents[N]) and appended, never inserted (#2565)."""
    here = Path(__file__).resolve()
    repo_root = next((d for d in here.parents if (d / "tools" / "_owner_read.py").is_file()), None)
    if repo_root is None:
        raise SystemExit(f"novel_terms: cannot find tools/_owner_read.py above {here} (run it from the engram-alpha "
                         "checkout)")
    if str(repo_root) not in sys.path:
        sys.path.append(str(repo_root))
    from tools import _owner_read as owner_read
    try:
        owner_read.check_store_path(db)
    except (FileNotFoundError, owner_read.StoreUnusable) as exc:
        raise SystemExit(f"novel_terms: {exc}") from None


def extract(text: str, db: Path, top_k: int) -> list[str]:
    check_db(db)
    sys.path.insert(0, str(resolve_runtime_dir()))
    from engram_idf import JUNK_STOPLIST, STOPWORDS, extract_keywords  # noqa: E402

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
    try:
        n_docs = conn.execute("SELECT COUNT(*) FROM nodes WHERE is_current = 1").fetchone()[0]
        if n_docs == 0:
            return []
        pairs = extract_keywords(
            conn, text, min_idf=MIN_IDF_RATIO * math.log(n_docs), top_k=top_k,
            stopwords=STOPWORDS | JUNK_STOPLIST)
    finally:
        conn.close()
    return [token for token, _score in pairs]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state", required=True, help="novel-terms-seen.json for this probe session")
    parser.add_argument("--db", default=str(default_db()),
                        help="default: $ENGRAM_HOME/knowledge.db, else ~/.engram/knowledge.db")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    args = parser.parse_args(argv)

    state = Path(args.state)
    terms = extract(sys.stdin.read(), Path(args.db), args.top_k)
    seen = load_seen(state)
    novel = [t for t in terms if t not in seen]
    save_seen(state, seen | set(terms))
    print(json.dumps({"terms": terms, "novel": novel, "db": args.db}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
