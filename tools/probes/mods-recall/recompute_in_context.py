#!/usr/bin/env python3
"""Recompute the probe route ledger's `in_context` OFFLINE from the session transcript (engram-alpha#3556 Half A).

    recompute_in_context.py <transcript.jsonl> <routes dir> --out <dir> [--persisted-root DIR ...]

The live hook flag never resets at compaction. This tool recomputes it from the Claude Code
session transcript with a compaction-aware definition and writes a SIDECAR; the ledger and the
transcript are never modified. Stdlib only.

Definition (Kepler's ruling on #3556 + the brief's D1-D3). For a ledger line at `ts` = t (ms) and a
candidate x:
  * boundary B = the LAST transcript line with `isCompactSummary: true` whose timestamp < t.
  * window W = transcript lines with B.timestamp < timestamp < t (strict both ends).
  * No boundary before t: the window is [first transcript line timestamp, t) (start inclusive, end
    strict) and the `summary` step does not exist. The flag is ALWAYS computed from the transcript
    text; the hook's own in_context / in_context_why are never copied into it (a pre-#3554 hook wrote
    none, which left every pre-first-boundary candidate unknown on a real seat). They ride beside it as
    in_context_hook / in_context_hook_why.
  * FIRST match wins:
      summary    x is in the text of B's own message (boundary rows only)
      shown      x is in the text of a line in W
      persisted  x is in a `<persisted-output>` file a line in W points at (see below)
      created    the candidate's created_at (naive = UTC) is in [window start, t); the start is B's
                 timestamp, or the first transcript line's timestamp when there is no boundary
      focused    the hook's own in_context_why is 'focused' (the live focus set cannot be rebuilt offline)
      none
  * in_context_offline = why is not none. null (unknown, never false) only when created_at is missing or
    unparsable, nothing else matched, and the hook did not say 'focused'.
  * Timestamps: a trailing Z is normalised to +00:00 before parsing (Python < 3.11 rejects Z); run the
    tool with the venv python anyway.
  * A ledger `ts` outside [transcript min timestamp - 1 day, transcript max timestamp + 1 day] is almost
    certainly in the wrong unit: one stderr warning per ledger file, counted in summary.json
    `ledger_ts_implausible_rows`; the row is still processed.

Text of a line = every string in it, recursively, EXCLUDING isSidechain lines, the metadata line
types (custom-title, agent-name, mode, permission-mode, atis-latch, last-prompt, queue-operation,
pr-link, file-history-snapshot, file-history-delta, cost-state, bridge-session, system), and any line
containing the probe's rating-request marker (REQUEST_MARKER). For `hook_success` the `stdout` JSON is
decoded and its hookSpecificOutput.additionalContext is added (a `\\uXXXX` escape right before an id
hides it from the id regex in the raw string). The compaction-summary line itself is never dropped
for quoting the marker.

Persisted output: a line whose text holds `Output too large (...). Full output saved to: <path>` has
that file's ids added, but only if the path resolves (realpath) to a regular file <= 5 MiB inside
~/.claude/projects/, the transcript's directory, or a --persisted-root DIR. Anything else is refused, counted
missing, and named on stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Copied from tools/probes/mods-recall/hooks/rating.ts (REQUEST_MARKER); a test pins it to that file.
REQUEST_MARKER = "[engram-probe · turn "
# Copied from tools/probes/mods-recall/hooks/step.ts (ID_PATTERN).
ID_PATTERN = re.compile(r"\b[a-z]{2}_\d{4,}\b")

SKIP_TYPES = frozenset({
    "custom-title", "agent-name", "mode", "permission-mode", "atis-latch", "last-prompt",
    "queue-operation", "pr-link", "file-history-snapshot", "file-history-delta", "cost-state",
    "bridge-session", "system",
})
ROUTES = ("prompt_surface", "tool_recall")
MAX_PERSISTED_BYTES = 5 * 1024 * 1024
STUB_PATTERN = re.compile(r"Output too large \([^)\n]*\)\. Full output saved to: ([^\n]+)")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_US = timedelta(microseconds=1)


class ToolError(Exception):
    pass


def parse_us(value):
    """ISO-8601 -> integer microseconds since epoch (naive = UTC); None if unparsable."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _EPOCH) // _US


def _strings(obj):
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            yield cur
        elif isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _line_strings(d):
    out = list(_strings(d))
    att = d.get("attachment")
    if isinstance(att, dict) and att.get("type") == "hook_success" and isinstance(att.get("stdout"), str):
        try:
            dec = json.loads(att["stdout"])
            ctx = dec["hookSpecificOutput"]["additionalContext"]
            if isinstance(ctx, str):
                out.append(ctx)
        except (ValueError, KeyError, TypeError):
            pass
    return out


def _ids(strings):
    ids = set()
    for s in strings:
        ids.update(ID_PATTERN.findall(s))
    return ids


class Persisted:
    """Reads `<persisted-output>` files under the safety contract; counts read / missing."""

    def __init__(self, roots):
        self.roots = [os.path.realpath(r) for r in roots]
        self.read = 0
        self.missing = 0
        self._cache = {}
        self._warned = set()

    def _warn(self, msg, path):
        if path not in self._warned:
            self._warned.add(path)
            print(f"recompute_in_context: {msg}: {path}", file=sys.stderr)

    def _load(self, path):
        try:
            real = os.path.realpath(path)
            inside = any(os.path.commonpath([real, r]) == r for r in self.roots)
        except (OSError, ValueError):  # e.g. an embedded NUL byte in the path
            self._warn("persisted-output path unusable", repr(path))
            return None
        if not inside:
            self._warn("refused persisted-output path outside allowed roots", path)
            return None
        try:
            if not os.path.isfile(real) or os.stat(real).st_size > MAX_PERSISTED_BYTES:
                self._warn("persisted-output file is not a regular file within the size cap", path)
                return None
            with open(real, "rb") as f:
                data = f.read(MAX_PERSISTED_BYTES + 1)
        except (OSError, ValueError):
            self._warn("persisted-output file unreadable", path)
            return None
        if len(data) > MAX_PERSISTED_BYTES:
            self._warn("persisted-output file over the size cap", path)
            return None
        return set(ID_PATTERN.findall(data.decode("utf-8", errors="replace")))

    def ids_for(self, path):
        if path not in self._cache:
            self._cache[path] = self._load(path)
        ids = self._cache[path]
        if ids is None:
            self.missing += 1
            return set()
        self.read += 1
        return ids


def read_transcript(path, persisted):
    """-> (boundaries, records, unparsable, span) with span = (min_ts_us, max_ts_us) over every line that
    has a parsable timestamp, or None when there is none.
    boundary = dict(ts, iso, ids, inclusive); record = (ts_us, ids, pids). Streams the file."""
    boundaries, records = [], []
    unparsable = 0
    span = None
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolError(f"cannot read transcript {path}: {exc.strerror or exc}")
    with fh:
        try:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    unparsable += 1
                    continue
                if not isinstance(d, dict):
                    unparsable += 1
                    continue
                ts = parse_us(d.get("timestamp"))
                if ts is None:
                    continue
                span = (ts, ts) if span is None else (min(span[0], ts), max(span[1], ts))
                if d.get("isSidechain") is True or d.get("type") in SKIP_TYPES:
                    continue
                strings = _line_strings(d)
                if d.get("type") == "user" and d.get("isCompactSummary") is True:
                    boundaries.append({"ts": ts, "iso": d["timestamp"], "ids": _ids(strings), "inclusive": False})
                    continue
                if any(REQUEST_MARKER in s for s in strings):
                    continue
                ids = _ids(strings)
                pids = set()
                for s in strings:
                    for m in STUB_PATTERN.finditer(s):
                        pids |= persisted.ids_for(m.group(1).strip())
                if ids or pids:
                    records.append((ts, ids, pids))
        except OSError as exc:
            raise ToolError(f"cannot read transcript {path}: {exc.strerror or exc}")
    return boundaries, records, unparsable, span


def first_seen(windows, records):
    """Per window start (a boundary, or the pre-boundary pseudo-window): (text_first, persisted_first) =
    id -> earliest timestamp (us) of a line after the start (strictly after a boundary; at or after the
    first transcript line for the pseudo-window). x is in W(t) iff first_seen[x] < t."""
    out = []
    for b in windows:
        tf, pf = {}, {}
        for ts, ids, pids in records:
            if ts < b["ts"] or (ts == b["ts"] and not b["inclusive"]):
                continue
            for i in ids:
                if i not in tf or ts < tf[i]:
                    tf[i] = ts
            for i in pids:
                if i not in pf or ts < pf[i]:
                    pf[i] = ts
        out.append((tf, pf))
    return out


def read_ledgers(routes_dir):
    """-> ({route: [line dicts]}, truncated_count). Raises ToolError on a bad non-final line."""
    lines_by_route, truncated = {}, 0
    for route in ROUTES:
        path = Path(routes_dir) / f"{route}.jsonl"
        if not path.is_file():
            continue
        try:
            raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            raise ToolError(f"cannot read ledger {path}: {exc.strerror or exc}")
        numbered = [(n, l) for n, l in enumerate(raw_lines, start=1) if l.strip()]
        rows = []
        for pos, (n, text) in enumerate(numbered):
            try:
                row = json.loads(text)
            except ValueError:
                if pos == len(numbered) - 1:
                    truncated += 1
                    continue
                raise ToolError(f"unparsable ledger line in {path}: line {n}")
            ts = row.get("ts") if isinstance(row, dict) else None
            if not isinstance(row, dict) or isinstance(ts, bool) or not isinstance(ts, (int, float)) \
                    or not isinstance(row.get("candidates"), list):
                raise ToolError(f"ledger line lacks a numeric ts or a candidates list in {path}: line {n}")
            rows.append(row)
        lines_by_route[route] = rows
    return lines_by_route, truncated


def judge(cand, t_us, boundary, seen, created_us):
    """-> (offline, why) for one candidate against a window start: a boundary, or the pre-boundary
    pseudo-window (empty `ids`, so no `summary`)."""
    cid = cand.get("id") if isinstance(cand, dict) else None
    if not isinstance(cid, str):
        return None, None
    if cid in boundary["ids"]:
        return True, "summary"
    tf, pf = seen
    if cid in tf and tf[cid] < t_us:
        return True, "shown"
    if cid in pf and pf[cid] < t_us:
        return True, "persisted"
    if created_us is not None and boundary["ts"] <= created_us < t_us:
        return True, "created"
    if cand.get("in_context_why") == "focused":
        return True, "focused"
    if created_us is None:
        return None, None
    return False, "none"


def build_rows(route, ledger, boundaries, seen_by_boundary, pre, pre_seen, band):
    """pre/pre_seen: the window for rows with no boundary before t. band: (lo_us, hi_us) plausible t."""
    out, stats = [], {"candidates": 0, "by_why": {}, "disagree": 0, "hook_present": 0, "hook_absent": 0,
                      "implausible": 0, "first_implausible": None}
    for n, line in enumerate(ledger, start=1):
        t_us = int(line["ts"] * 1000)
        if not band[0] <= t_us <= band[1]:
            stats["implausible"] += 1
            if stats["first_implausible"] is None:
                stats["first_implausible"] = n
        bi = None
        for k, b in enumerate(boundaries):
            if b["ts"] < t_us:
                bi = k
        cands = []
        for cand in line["candidates"]:
            hook, hook_why = (cand.get("in_context"), cand.get("in_context_why")) if isinstance(cand, dict) else (None, None)
            created = parse_us(cand.get("created_at")) if isinstance(cand, dict) else None
            if bi is None:
                off, why = judge(cand, t_us, pre, pre_seen, created)
            else:
                off, why = judge(cand, t_us, boundaries[bi], seen_by_boundary[bi], created)
            stats["hook_absent" if hook is None else "hook_present"] += 1
            cands.append({
                "id": cand.get("id") if isinstance(cand, dict) else None,
                "in_context_offline": off,
                "in_context_offline_why": why,
                "in_context_hook": hook,
                "in_context_hook_why": hook_why,
            })
            label = why if why else ("unknown" if off is None else "none")
            stats["candidates"] += 1
            stats["by_why"][label] = stats["by_why"].get(label, 0) + 1
            if isinstance(off, bool) and isinstance(hook, bool) and off != hook:
                stats["disagree"] += 1
        out.append({
            "ts": line["ts"], "route": line.get("route", route), "fire_key": line.get("fire_key"),
            "boundary_index": bi, "boundary_ts": boundaries[bi]["iso"] if bi is not None else None,
            "candidates": cands,
        })
    return out, stats


def _write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def run(args):
    transcript, routes_dir, out = args.transcript, args.routes, Path(args.out)
    if not Path(routes_dir).is_dir():
        raise ToolError(f"routes dir is not a directory: {routes_dir}")
    real_out, real_routes = os.path.realpath(out), os.path.realpath(routes_dir)
    if os.path.commonpath([real_out, real_routes]) == real_routes:
        raise ToolError(f"--out must not be inside the routes dir: {out}")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ToolError(f"--out exists and is not an empty directory: {out}")
    ledgers, truncated = read_ledgers(routes_dir)
    n_lines = sum(len(v) for v in ledgers.values())
    if n_lines == 0:
        raise ToolError(f"no ledger lines in routes dir: {routes_dir}")
    roots = [Path.home() / ".claude" / "projects", Path(transcript).resolve().parent, *map(Path, args.persisted_root)]
    persisted = Persisted(roots)
    boundaries, records, unparsable, span = read_transcript(transcript, persisted)
    if span is None:
        raise ToolError(f"transcript has no parsable conversation lines: {transcript}")
    day_us = 86_400 * 1_000_000
    band = (span[0] - day_us, span[1] + day_us)
    seen = first_seen(boundaries, records)
    pre = {"ts": span[0], "iso": None, "ids": frozenset(), "inclusive": True}
    pre_seen = first_seen([pre], records)[0]

    results, totals = {}, {"candidates": 0, "by_why": {}, "disagree": 0, "hook_present": 0, "hook_absent": 0,
                           "implausible": 0}
    for route, ledger in ledgers.items():
        results[route], st = build_rows(route, ledger, boundaries, seen, pre, pre_seen, band)
        if st["implausible"]:
            print(f"recompute_in_context: ledger ts implausible against the transcript's time span "
                  f"(is ts in ms?): {Path(routes_dir) / (route + '.jsonl')}: {st['implausible']} row(s), "
                  f"first at row {st['first_implausible']}", file=sys.stderr)
        for k in ("candidates", "disagree", "hook_present", "hook_absent", "implausible"):
            totals[k] += st[k]
        for k, v in st["by_why"].items():
            totals["by_why"][k] = totals["by_why"].get(k, 0) + v

    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    for route, rows in results.items():
        _write_private(out / f"{route}.in_context_offline.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
    summary = {
        "transcript": str(transcript),
        "ledger_lines": n_lines,
        "truncated_ledger_lines": truncated,
        "boundaries": len(boundaries),
        "candidates": totals["candidates"],
        "by_why": totals["by_why"],
        "disagree_with_hook": totals["disagree"],
        "hook_flag_present": totals["hook_present"],
        "hook_flag_absent": totals["hook_absent"],
        "ledger_ts_implausible_rows": totals["implausible"],
        "persisted_output_stubs_read": persisted.read,
        "persisted_output_stubs_missing": persisted.missing,
        "unparsable_transcript_lines": unparsable,
    }
    _write_private(out / "summary.json", json.dumps(summary, indent=2) + "\n")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("transcript")
    ap.add_argument("routes", help="the session's routes dir (prompt_surface.jsonl / tool_recall.jsonl)")
    ap.add_argument("--out", required=True, help="new or empty directory for the sidecar")
    ap.add_argument("--persisted-root", action="append", default=[], metavar="DIR",
                    help="extra directory a persisted-output file may live in (repeatable)")
    args = ap.parse_args(argv)
    try:
        run(args)
    except ToolError as exc:
        print(f"recompute_in_context: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
