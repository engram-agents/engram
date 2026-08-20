#!/usr/bin/env python3
"""Functional audit of lesson situation_patterns.  v4 (2026-08-14).

Designed and built by Ariadne (agent) — found live-fire, cross-checked against
two agents' graphs (Borges's and her own). All four bugs in the history below
were found and fixed by her and Borges, in the order listed, before this tool
ever ran anywhere but her own home directory. Landed in the repo by Sol per
engram-alpha#2018; the tool and its bug history are hers, this commit is only
the landing.

Asks what the shape-based censuses cannot: compiled, does a pattern match any
match-target the tripwire hook will actually build?

Targets are built exactly as the hook's build_match_target() does (imported
directly from src/engram/hooks/claude/engram-lesson-tripwire-hook.py — no
re-derivation, so this cannot silently drift from what the hook actually does):
  Bash  -> the command string
  MCP   -> "{tool_name} {compact_json_args}"
  other -> "" (skipped by the hook, so never a target)

Targets are built by the hook's SOURCE build_match_target; if the deployed
plugin tree has drifted from this repo, the audit measures the code you are
about to ship rather than the code currently firing on any given agent.

BUG HISTORY -- every one found by running it, none by reading it:

  v1 hand-written targets.
  BUG 1 (Borges): CONJUNCTIVE false negatives. A pattern needing e.g. a
    channel AND a node id matched nothing, because no single hand-written
    sample carried both. It called one of his freshly-repaired, working
    lessons DEAD. Fixed by drawing targets from REAL traffic, where channel
    and content co-occur naturally.
  BUG 2 (Borges): DEAD OVERREAD. DEAD here means "no action-moment precision
    against this inventory" -- NOT "no reach". The incident-match and
    scaffolding_nudge channels are separate and live; he confirmed a lesson
    this audit calls DEAD firing at him at prompt-time in the same session.
    LIVE is therefore a LOWER BOUND.
  BUG 3 (mine): SELF-REFERENTIAL CONTAMINATION. Real traffic includes my own
    diagnostic commands, whose text contains the channel names the patterns
    match -- so the audit counted its own investigation as evidence that
    lessons fire in ordinary work. It inflated LIVE by one. Excluded via
    CORPUS_INTROSPECTION. Structural, not occasional: it appears exactly when
    someone is auditing, which is the only time this runs.
  BUG 4 (Borges): INTROSPECTION *CONVERSATION*. That filter catches
    introspection COMMANDS, not introspection TALK -- a session spent
    discussing lessons over say/send_crossing yields targets that ARE the
    audit while carrying none of the filtered literals. So every verdict is
    reported split CURRENT-session vs PRIOR-session. A LIVE resting only on
    today is indistinguishable from the investigation itself.
    ** Trust the prior-session column. **

A DEAD verdict is a statement about the observed transcript inventory, not
a verdict on the pattern's design. A short channel list is sometimes correct,
deliberate scoping -- e.g. a pattern whose hazard is shell-metacharacter-
specific and deliberately excludes MCP-only verbs -- and a DEAD result does
not by itself mean the channel list should be auto-widened. Read the lesson's
own stated hazard before treating DEAD as a bug to fix.

Set ENGRAM_AUDIT_SESSION=<session-id> (or pass --session) to mark which
transcript is "current"; otherwise the newest transcript by mtime is assumed
to be the current one. --session, if passed, takes precedence over the env
var.
"""
import argparse
import glob
import importlib.util
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOK_PATH = REPO_ROOT / "src/engram/hooks/claude/engram-lesson-tripwire-hook.py"

DEFAULT_DB = os.path.expanduser("~/.engram/knowledge.db")
DEFAULT_TRANSCRIPT_GLOB = os.path.expanduser("~/.claude/projects/**/*.jsonl")
DEFAULT_N_TRANSCRIPTS = 6

CORPUS_INTROSPECTION = re.compile(
    r"error_incidents\.json|situation_pattern|lesson-pattern-audit|"
    r"pattern_audit|type='lesson'|scaffolding_nudge", re.I)


def _load_build_match_target():
    """Import build_match_target from the hook file (hyphenated filename, not
    importable via normal `import`). Same importlib idiom as
    tests/test_lesson_tripwire.py's _import_hook() -- don't invent a new one."""
    spec = importlib.util.spec_from_file_location(
        "lesson_tripwire_hook_for_audit", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_match_target


build_match_target = _load_build_match_target()


def collect_targets(transcript_glob=DEFAULT_TRANSCRIPT_GLOB,
                     limit_files=DEFAULT_N_TRANSCRIPTS, session=""):
    """Return (current_targets, prior_targets, filenames).

    Provenance is tracked per target so a LIVE verdict resting only on the
    current session can be distinguished from one with independent support.
    """
    files = sorted(glob.glob(transcript_glob, recursive=True),
                   key=os.path.getmtime, reverse=True)[:limit_files]
    if not files:
        return [], [], []
    marker = session or os.environ.get("ENGRAM_AUDIT_SESSION", "")
    current_file = None
    if marker:
        for f in files:
            if marker in os.path.basename(f):
                current_file = f
                break
    if current_file is None:
        current_file = files[0]          # newest by mtime

    cur, prior, seen = [], [], set()
    for f in files:
        bucket = cur if f == current_file else prior
        try:
            fh = open(f, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                msg = o.get("message")
                if not isinstance(msg, dict):
                    continue          # malformed line -- not a dict "message"
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not (isinstance(c, dict) and c.get("type") == "tool_use"):
                        continue
                    name = c.get("name") or ""
                    ti = c.get("input") or {}
                    t = build_match_target({"tool_name": name, "input": ti})
                    if not t:
                        continue          # hook yields "" -> not a target
                    if CORPUS_INTROSPECTION.search(t):
                        continue         # v3: auditing is not traffic
                    key = (f == current_file, hash(t))
                    if key in seen:
                        continue
                    seen.add(key)
                    bucket.append(t)
    return cur, prior, [os.path.basename(f) for f in files]


def run_audit(db_path=DEFAULT_DB, transcript_glob=DEFAULT_TRANSCRIPT_GLOB,
              limit_files=DEFAULT_N_TRANSCRIPTS, session="", out=None,
              err=None):
    """Run the audit and print the report. Returns the process exit code.

    out/err default to sys.stdout/sys.stderr resolved at CALL time (not
    def-time) so callers using contextlib.redirect_stdout/stderr see the
    output -- binding the default at def-time would capture the stream
    object current at import and never see a later redirect."""
    if out is None:
        out = sys.stdout
    if err is None:
        err = sys.stderr
    cur, prior, srcs = collect_targets(transcript_glob=transcript_glob,
                                        limit_files=limit_files, session=session)
    if not (cur or prior):
        print("no transcript-derived targets; refusing to report a coverage "
              "number from an empty inventory", file=err)
        return 2

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute("select id,claim,metadata from nodes "
                        "where type='lesson' and is_current=1").fetchall()

    unarmed = dead = broken = 0
    live_supported = live_today_only = 0
    rep = []
    for nid, claim, md in rows:
        try:
            m = json.loads(md) if md else {}
        except Exception:
            m = {}
        pat = m.get("situation_pattern") or ""
        if not pat:
            unarmed += 1
            continue
        try:
            rx = re.compile(pat, re.I)
        except re.error as e:
            broken += 1
            rep.append((nid, 2, "BROKEN", str(e)[:60], claim[:55]))
            continue
        hc = [t for t in cur if rx.search(t)]
        hp = [t for t in prior if rx.search(t)]
        if hp:
            live_supported += 1
            ex = hp[0]
            ex = ex.split(" ", 1)[0] if ex.startswith("mcp__") else ex[:40]
            rep.append((nid, 0, "LIVE",
                        f"today={len(hc)} prior={len(hp)}  e.g. {ex}", claim[:55]))
        elif hc:
            live_today_only += 1
            ex = hc[0]
            ex = ex.split(" ", 1)[0] if ex.startswith("mcp__") else ex[:40]
            rep.append((nid, 1, "LIVE?",
                        f"today={len(hc)} prior=0  RESTS ON THIS SESSION ONLY "
                        f"-- may be the audit itself; e.g. {ex}", claim[:55]))
        else:
            # A short channel list is sometimes correct, deliberate scoping --
            # e.g. a pattern whose hazard is shell-metacharacter-specific and
            # deliberately excludes MCP-only verbs. DEAD does not by itself
            # mean the channel list should be auto-widened; read the lesson's
            # own stated hazard before treating DEAD as a bug to fix.
            dead += 1
            rep.append((nid, 3, "DEAD", "no match in real traffic", claim[:55]))

    armed = live_supported + live_today_only + dead + broken
    print(f"targets: {len(cur)} current-session + {len(prior)} prior-session "
          f"({len(srcs)} transcripts)", file=out)
    print(f"lessons: {len(rows)}   unarmed: {unarmed}   armed: {armed}", file=out)
    print(f"  LIVE(prior-supported) {live_supported}   "
          f"LIVE?(today-only) {live_today_only}   DEAD {dead}   BROKEN {broken}",
          file=out)
    print(f"  ACTION-MOMENT COVERAGE, LOWER BOUND: "
          f"{live_supported}/{len(rows)} with independent support", file=out)
    print("  DEAD = no action-moment precision here; NOT 'no reach' "
          "(incident-match + nudge channels are separate and live).\n", file=out)
    for nid, _rank, st, det, cl in sorted(rep, key=lambda r: (r[1], r[0])):
        print(f"[{nid}] {st:6} {det}", file=out)
        print(f"           {cl}", file=out)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Functional audit of lesson situation_patterns: compiles each "
            "current lesson's pattern and matches it against real "
            "transcript-derived targets (closes engram-alpha#2018)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB,
        help="Path to the ENGRAM sqlite DB (default: ~/.engram/knowledge.db).",
    )
    parser.add_argument(
        "--transcript-glob", default=DEFAULT_TRANSCRIPT_GLOB,
        help="Glob for transcript .jsonl files "
             "(default: ~/.claude/projects/**/*.jsonl).",
    )
    parser.add_argument(
        "--transcripts", type=int, default=DEFAULT_N_TRANSCRIPTS,
        help="Number of most-recent transcripts (by mtime) to read (default: 6).",
    )
    parser.add_argument(
        "--session", default="",
        help="Session id marking which transcript is 'current' (falls back to "
             "ENGRAM_AUDIT_SESSION env var if not passed; this flag wins if both "
             "are set).",
    )
    args = parser.parse_args()
    return run_audit(db_path=args.db, transcript_glob=args.transcript_glob,
                      limit_files=args.transcripts, session=args.session)


if __name__ == "__main__":
    sys.exit(main())
