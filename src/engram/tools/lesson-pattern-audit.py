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
directly from engram-lesson-tripwire-hook.py — no re-derivation, so this cannot
silently drift from what the hook actually does):
  Bash  -> the command string
  MCP   -> "{tool_name} {compact_json_args}"
  other -> "" (skipped by the hook, so never a target)

Targets are built by the build_match_target of the hook file found NEXT TO THIS
TOOL's tree (alpha#3941): the installed plugin's hooks/ when run from an install, the
source tree's hooks/claude/ when run from a checkout.  The hook is found by
$CLAUDE_PLUGIN_ROOT/hooks first, then in this tool's own tree only: the parent of
this file's directory (the plugin root in an install, src/engram in a checkout), at hooks/<name>
then hooks/claude/<name>.  Nothing above that parent is looked at, so a stale hook left in an ancestor
directory (e.g. an old scatter install) is never measured; and never a fixed parents[N] guess, which is
correct in one layout and silently wrong in the other (the
plugin build flattens hooks/claude/* to hooks/*).  If that tree has drifted from the
code firing on a given agent, the audit measures the tree it was run from.

The hook is tier `convenience` and this tool `essential`, so the hook may be absent (an Essential-only
install), and it is loaded lazily, on first use.  Absent is decided by the hook FILE's absence alone, through
the lookup above, never by catching an import error.  Then `--write-ranks` exits 0, prints "no tripwire hook
installed; rank file left as is" and writes nothing (no hook, no consumer of the file); the audit and
`--compare-views` modes need its matcher and exit 1 with one line.  A hook file that is there but fails to
load (a syntax error, a missing dependency, a failing top-level statement, its own guard exiting) ends the run
non-zero with the error in every mode.  A RELATIVE ENGRAM_HOME is refused first, in every mode, with one
stderr line and exit 1: the hook's own top-level guard would otherwise end the process with exit 0.

Defaults follow ENGRAM_HOME (alpha#3941): --db is $ENGRAM_HOME/knowledge.db and the
rank file is $ENGRAM_HOME/lesson-fire-ranks.json; the transcript glob stays Claude
Code's projects directory.  The store is read through the shared owner-read helper
(tools/_owner_read.py), behind engramd.lesson_pattern_audit_reads_via_owner (default
OFF).  The tool is read-only on the graph and on the transcripts; --write-ranks
writes only the one rank file.

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
import sys
from pathlib import Path

# `tools` is the package next to this file in every layout (src/engram/tools/ in a checkout,
# <plugin root>/tools/ installed); the repo-root tools/ entry is a symlink, and __file__ is resolved.
_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from tools import _owner_read as owner_read  # noqa: E402

_HOOK_NAME = "engram-lesson-tripwire-hook.py"


def _find_hook_path() -> "Path | None":
    """The tripwire hook file, or None when the FILE is not there.  Looked up in this order, and nowhere else:
    $CLAUDE_PLUGIN_ROOT/hooks/<name> (when set), then <tool dir's parent>/hooks/<name>, then
    <tool dir's parent>/hooks/claude/<name>.  The tool dir's parent is the plugin root in an installed layout
    and src/engram in a checkout (the repo-root tools/ symlink resolves into src/engram/tools), so one anchor
    covers both layouts (CLAUDE.md, "Plugin build restructures code paths": the installed hook sits one level
    shallower than its source).  There is NO walk past that parent: a hook in an ancestor directory is not
    this tool's tree (an Essential-only install must not measure a stale hook left above it).  "Not
    installed" is decided HERE, by the file's absence, and nowhere else: the hook is tier `convenience` and
    this tool is `essential`, so an Essential-only install has no hook."""
    candidates = []
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if plugin_root:
        candidates.append(Path(plugin_root) / "hooks" / _HOOK_NAME)
    tool_tree = Path(__file__).resolve().parent.parent
    candidates.append(tool_tree / "hooks" / _HOOK_NAME)
    candidates.append(tool_tree / "hooks" / "claude" / _HOOK_NAME)
    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


_HOOK_PATH = _find_hook_path()


class HookUnavailable(RuntimeError):
    """The tripwire hook's matcher is needed and cannot be had: its file is not installed, or it is there and
    failed to load.  Both end the run non-zero with one line; only `--write-ranks` treats the first as
    'nothing to rank' (it checks the file itself, before any load)."""

OWNER_FLAG_KEY = "lesson_pattern_audit_reads_via_owner"

DEFAULT_TRANSCRIPT_GLOB = os.path.expanduser("~/.claude/projects/**/*.jsonl")
DEFAULT_N_TRANSCRIPTS = 6

CORPUS_INTROSPECTION = re.compile(
    r"error_incidents\.json|situation_pattern|lesson-pattern-audit|"
    r"pattern_audit|type='lesson'|scaffolding_nudge", re.I)


_HOOK_MODULE = None


def _hook_module():
    """The tripwire hook module, loaded on first use (hyphenated filename, not importable via normal
    `import`; the importlib idiom of tests/test_lesson_tripwire.py's _import_hook()).  Lazy so that main() can
    validate ENGRAM_HOME before the hook's own top-level guard runs, and so a missing hook file does not
    break the import of this tool.  A hook file that IS there and fails to load (a syntax error, a missing
    dependency, a failing top-level statement, its guard exiting) raises HookUnavailable with the error: it
    is never read as 'not installed'."""
    global _HOOK_MODULE
    if _HOOK_MODULE is not None:
        return _HOOK_MODULE
    if _HOOK_PATH is None:
        raise HookUnavailable(f"{_HOOK_NAME} is not installed (looked under $CLAUDE_PLUGIN_ROOT/hooks and "
                              f"hooks/, hooks/claude/ of {Path(__file__).resolve().parent.parent}); this "
                              "mode needs the tripwire hook's matcher")
    spec = importlib.util.spec_from_file_location("lesson_tripwire_hook_for_audit", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        for attr in ("build_match_target", "build_match_views"):
            getattr(mod, attr)
    except SystemExit as exc:   # the hook's top-level guards sys.exit(); from here that is a failure, not a success
        raise HookUnavailable(f"{_HOOK_NAME} exited while loading (code {exc.code!r}): {_HOOK_PATH}") from exc
    except Exception as exc:
        raise HookUnavailable(f"{_HOOK_NAME} failed to load: {type(exc).__name__}: {exc}") from exc
    _HOOK_MODULE = mod
    return mod


def build_match_target(*args, **kwargs):
    return _hook_module().build_match_target(*args, **kwargs)


def build_match_views(*args, **kwargs):
    return _hook_module().build_match_views(*args, **kwargs)


class LessonAuditOwnerReadError(RuntimeError):
    """The owner could not give the lesson rows (flag ON), or gave rows that do not fit.  Raised instead of
    reporting from an empty list: an empty list would read downstream as "no lesson fires" (and, written,
    as a rank file of nothing), a clean bill the store never gave."""


def _owner_spec(home: "Path | None" = None) -> "owner_read.OwnerReadSpec":
    return owner_read.OwnerReadSpec(
        flag_key=OWNER_FLAG_KEY,
        error_cls=LessonAuditOwnerReadError,
        home=Path(home) if home is not None else owner_read.engram_home(),
        never_empty="Not answering from an empty lesson list: that would read as 'no lesson fires'.",
    )


def read_lesson_rows(db_path, home: "Path | None" = None, use_owner: "bool | None" = None) -> list:
    """The current lessons of `db_path` as (id, claim, metadata) rows, read through the shared owner-read helper
    only.  Behind engramd.lesson_pattern_audit_reads_via_owner (default OFF) the read goes to the engramd owner
    when `db_path` is this home's store; flag ON and the owner down, refusing or answering malformed RAISES
    LessonAuditOwnerReadError (never an empty list, never a direct fallback).  Flag OFF opens the store
    read-only after `open_reader` classified the path (a FIFO or a directory is refused before any open)."""
    db_path = Path(db_path)
    spec = _owner_spec(home)
    if use_owner is None:
        use_owner = owner_read.flag_applies(spec, db_path)
    if use_owner:
        owner_read.ping(spec)
    conn = owner_read.open_reader(spec, db_path, use_owner)
    try:
        rows = conn.execute("select id,claim,metadata from nodes where type='lesson' and is_current=1").fetchall()
    finally:
        conn.close()
    if use_owner and not all(isinstance(r, (list, tuple)) and len(r) == 3 for r in rows):
        raise LessonAuditOwnerReadError(
            "the engramd owner answered rows that are not (id, claim, metadata); check the owner build, or set "
            f"engramd.{OWNER_FLAG_KEY} to false to read the store directly. {spec.never_empty}")
    return rows


def _static_glob_prefix(pattern):
    """The leading path components of a glob pattern that hold none of `*`, `?`, `[` (alpha#4396): the
    part of the path the caller chose, as opposed to the part the glob discovered."""
    prefix = []
    for part in Path(pattern).parts:
        if any(ch in part for ch in "*?["):
            break
        prefix.append(part)
    return tuple(prefix)


def collect_targets(transcript_glob=DEFAULT_TRANSCRIPT_GLOB,
                     limit_files=DEFAULT_N_TRANSCRIPTS, session="", pairs=False,
                     exclude_subagents=False):
    """Return (current_targets, prior_targets, filenames).

    Provenance is tracked per target so a LIVE verdict resting only on the
    current session can be distinguished from one with independent support.

    pairs=True (alpha#3920): each target is a (full, command) tuple of the
    hook's two views of the call instead of the full string alone; de-duplication
    and the introspection filter still key on the full string.

    exclude_subagents=True (alpha#4396): drop every file with a path COMPONENT named `subagents` below the glob's static root (sub-agent
    transcripts) BEFORE the mtime sort and the limit, so the window is the newest `limit_files` MAIN files.
    The default keeps all traffic: the coverage audit asks whether a pattern matches real calls.
    """
    _hook_module()   # fail on a missing or broken hook before reading anything, whatever the inventory holds
    files = glob.glob(transcript_glob, recursive=True)
    if exclude_subagents:
        # Test only the part BELOW the glob's static root: a root the caller chose that is itself named
        # `subagents` must not drop every file.  A path that does not start with the prefix (should not
        # happen) falls back to its full parts.
        prefix = _static_glob_prefix(transcript_glob)

        def _below_root(f):
            parts = Path(f).parts
            return parts[len(prefix):] if parts[:len(prefix)] == prefix else parts
        files = [f for f in files if "subagents" not in _below_root(f)]
    files = sorted(files, key=os.path.getmtime, reverse=True)[:limit_files]
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
                    bucket.append(
                        (t, build_match_views({"tool_name": name, "input": ti})[1])
                        if pairs else t)
    return cur, prior, [os.path.basename(f) for f in files]


def run_audit(db_path=None, transcript_glob=DEFAULT_TRANSCRIPT_GLOB,
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

    db_path = db_path or str(owner_read.default_db_path())
    rows = read_lesson_rows(db_path)

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


def run_view_comparison(db_path=None, transcript_glob=DEFAULT_TRANSCRIPT_GLOB,
                        limit_files=DEFAULT_N_TRANSCRIPTS, session="", out=None, err=None):
    """alpha#3920 migration aid: for each armed lesson, how many real targets does its pattern
    match on the FULL view vs the COMMAND view of the call, and which full-view matches would
    be lost under `command`. Every lesson defaults to `full`; a lesson whose lost matches are all
    prose that only mentions its command is a candidate to opt into match_scope "command"
    (engram_update_lesson); one whose lost matches are legitimate (it depends on heredoc or
    prose-field text) must stay `full`. Reports; never
    writes. Exit 0, or 2 on an empty inventory."""
    if out is None:
        out = sys.stdout
    if err is None:
        err = sys.stderr
    cur, prior, srcs = collect_targets(transcript_glob=transcript_glob, limit_files=limit_files,
                                       session=session, pairs=True)
    pairs = cur + prior
    if not pairs:
        print("no transcript-derived targets; refusing to report from an empty inventory", file=err)
        return 2
    db_path = db_path or str(owner_read.default_db_path())
    rows = read_lesson_rows(db_path)
    rep = []
    for nid, claim, md in rows:
        try:
            m = json.loads(md) if md else {}
        except Exception:
            m = {}
        pat = m.get("situation_pattern") or ""
        if not pat:
            continue
        try:
            rx = re.compile(pat, re.I)
        except re.error:
            continue
        scope = m.get("match_scope") if m.get("match_scope") in ("command", "full") else "full"
        n_full = sum(1 for f, _c in pairs if rx.search(f))
        n_cmd = sum(1 for _f, c in pairs if rx.search(c))
        lost = [f for f, c in pairs if rx.search(f) and not rx.search(c)]
        rep.append((nid, scope, n_full, n_cmd, lost, claim[:55]))
    print(f"targets: {len(pairs)} ({len(cur)} current-session + {len(prior)} prior, "
          f"{len(srcs)} transcripts)", file=out)
    print(f"armed lessons: {len(rep)}   with matches lost under `command`: "
          f"{sum(1 for r in rep if r[4])}\n", file=out)
    for nid, scope, n_full, n_cmd, lost, cl in sorted(rep, key=lambda r: (-len(r[4]), r[0])):
        flag = "REVIEW" if lost else "ok"
        print(f"[{nid}] {flag:6} scope={scope:7} full={n_full} command={n_cmd} lost={len(lost)}", file=out)
        print(f"           {cl}", file=out)
        for t in lost[:3]:
            ex = t.split(" ", 1)[0] if t.startswith("mcp__") else t.replace("\n", " ")[:70]
            print(f"             lost e.g. {ex}", file=out)
    return 0


RANKS_FILENAME = "lesson-fire-ranks.json"


def _default_ranks_path() -> str:
    return str(owner_read.engram_home() / RANKS_FILENAME)


def _preserve_acl(src: str, dst: str) -> None:
    """Copy a POSIX ACL (the system.posix_acl_* xattrs) from src to dst when the platform has them.
    No subprocess, no `getfacl`/`setfacl`: where xattrs or ACLs are unsupported this raises or is a
    no-op, and the caller swallows the error (the ACL step must never fail the run)."""
    if not hasattr(os, "listxattr"):
        return
    for name in os.listxattr(src):
        if name.startswith("system.posix_acl"):
            os.setxattr(dst, name, os.getxattr(src, name))


def _write_whole_file_atomic(path: str, text: str) -> None:
    """Write `text` as the WHOLE file at `path`: temp file in the same directory, then os.replace.
    The temp file takes the existing file's mode (0o640 for a new file) and, best effort, its ACL (Linux
    POSIX ACLs only, copied as `system.posix_acl*` xattrs; macOS ACLs are NOT preserved, measured by the
    colleague review of PR 3948, so on macOS only the mode survives): a
    bare mkstemp+rename leaves a 0o600 file with no ACL, which silently revokes another account's
    read (and an ACL's mask). The ACL step's errors are swallowed; nothing else's are."""
    import tempfile
    path = os.path.realpath(path)  # a symlink at PATH is followed: the link stays, its target is rewritten
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o640
        existing = False
    else:
        existing = True
    fd, tmp = tempfile.mkstemp(prefix=".lesson-fire-ranks.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        if existing:
            try:
                _preserve_acl(path, tmp)
            except Exception:
                pass
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run_write_ranks(db_path=None, transcript_glob=DEFAULT_TRANSCRIPT_GLOB,
                    limit_files=DEFAULT_N_TRANSCRIPTS, session="", path=None, out=None, err=None):
    """alpha#3924: for each armed lesson, fires per 1,000 targets over the replayed corpus, written as
    the WHOLE rank file the tripwire hook reads to order a cluster block (rarest-firing lesson gets
    the full nudge). A lesson is counted the way the hook matches it: on the `full` view, or on the
    `command` view when its match_scope is "command". Armed = a non-empty, compilable
    situation_pattern; a lesson that fires on nothing gets 0.0 (rated, rarest). With NO transcripts (or
    transcripts that yield no target) it says so, exits 0 and writes NOTHING: an empty rank file would read
    as "every lesson is rare", a false tie-break worse than a stale file (which the hook's age rule already
    handles).  The store is not read then.

    alpha#4396: the corpus is MAIN-session transcripts only (files under a `subagents/` directory are dropped
    before the window limit); a tree holding only sub-agent files is the "no transcripts" case above."""
    from datetime import datetime, timezone
    if out is None:
        out = sys.stdout
    if err is None:
        err = sys.stderr
    path = path or _default_ranks_path()
    if _HOOK_PATH is None:
        # The hook is `convenience`, this tool `essential`: no hook file means no consumer of the rank file,
        # so there is nothing to rank.  Decided by the FILE's absence only (never by catching a load error: a
        # hook that is there but broken stays loud, in collect_targets), and BEFORE anything is read or written.
        print("no tripwire hook installed; rank file left as is", file=out)
        return 0
    cur, prior, srcs = collect_targets(transcript_glob=transcript_glob, limit_files=limit_files,
                                       session=session, pairs=True, exclude_subagents=True)
    pairs = cur + prior
    if not pairs:
        if not srcs:
            print("no transcripts found; rank file left as is", file=out)
        else:
            print(f"no tool-call targets in the transcripts ({len(srcs)} read); rank file left as is", file=out)
        return 0
    db_path = db_path or str(owner_read.default_db_path())
    rows = read_lesson_rows(db_path)
    ranks = {}
    for nid, _claim, md in rows:
        try:
            m = json.loads(md) if md else {}
        except Exception:
            m = {}
        if not isinstance(m, dict):
            continue  # metadata 'null' or a JSON list: one bad row must not abort the whole file
        pat = m.get("situation_pattern") or ""
        if not isinstance(pat, str) or not pat:
            continue
        try:
            rx = re.compile(pat, re.I)
        except Exception:  # re.error, TypeError, RecursionError, OverflowError ...
            continue
        use_command = m.get("match_scope") == "command"
        fires = sum(1 for f, c in pairs if rx.search(c if use_command else f))
        ranks[nid] = round(fires * 1000.0 / len(pairs), 3)
    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": (f"{len(srcs)} transcripts: {len(cur)} current-session + {len(prior)} "
                   f"prior-session targets"),
        "targets": len(pairs),
        "ranks": ranks,
    }
    _write_whole_file_atomic(path, json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"wrote {len(ranks)} lesson rates over {len(pairs)} targets to {path}", file=out)
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
        "--db", default=None,
        help="Path to the ENGRAM sqlite DB (default: $ENGRAM_HOME/knowledge.db, else ~/.engram/knowledge.db).",
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
    parser.add_argument(
        "--compare-views", action="store_true",
        help="alpha#3920: instead of the coverage audit, compare each lesson's matches on the "
             "hook's FULL view vs its COMMAND view (heredoc bodies / prose fields hidden) and "
             "list the matches that would be lost, to decide which lessons may opt "
             "into match_scope=command (all lost matches are prose). Reports only.",
    )
    parser.add_argument(
        "--write-ranks", nargs="?", const="", default=None, metavar="PATH",
        help="alpha#3924: instead of the coverage audit, write each armed lesson's fires per 1,000 "
             "targets, counted over main-session transcripts only (the whole file, mode and ACL preserved) to PATH (default: "
             "$ENGRAM_HOME/lesson-fire-ranks.json). The tripwire hook reads it to choose which "
             "co-firing lesson gets the full nudge.",
    )
    args = parser.parse_args()
    try:
        # Validate the home FIRST, in every mode and before any hook load: the hook's own top-level guard
        # sys.exit(0)s on a relative ENGRAM_HOME, which would end this run as a silent success.
        owner_read.engram_home()
    except ValueError as exc:
        print(" ".join(str(exc).split()), file=sys.stderr)
        return 1
    try:
        if args.write_ranks is not None:
            return run_write_ranks(db_path=args.db, transcript_glob=args.transcript_glob,
                                   limit_files=args.transcripts, session=args.session,
                                   path=args.write_ranks or None)
        if args.compare_views:
            return run_view_comparison(db_path=args.db, transcript_glob=args.transcript_glob,
                                       limit_files=args.transcripts, session=args.session)
        return run_audit(db_path=args.db, transcript_glob=args.transcript_glob,
                          limit_files=args.transcripts, session=args.session)
    except (LessonAuditOwnerReadError, owner_read.StoreUnusable, FileNotFoundError, HookUnavailable) as exc:
        # One stderr line and exit 1 (the last line is what the sleep step records).  Nothing was written:
        # every store read happens before the rank file is touched.
        print(" ".join(str(exc).split()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
