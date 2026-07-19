#!/usr/bin/env python3
"""Rec-4: unified before/after in-turn-recall measurement harness (#266, #1780).

Productionizes a reproducible, per-seat before/after measurement harness for
commander charge #266 (Kepler, Lei's charge): recall-fix impact must be
**measured, not guessed**.

Four metrics, always per-seat (never averaged across seats -- render rates
vary ~2x by seat, per #1780):

  1. repeat-render fraction (k=1/3/5/10) -- REUSES tools/recall_repetition_analysis.py
     (mines `$ENGRAM_HOME/logs/index.db`'s `engram.surface.fire` events). This
     module does not reimplement that logic; it imports and wraps it.
  2. junk-fire fraction -- mines `$ENGRAM_HOME/in-turn-recall-ledger.jsonl`
     `novel_terms` against a junk-token stoplist (rec-3). Reports three
     numbers over *rendered* fires: junk-token fraction, any-junk-fire
     fraction, all-junk-fire fraction.
  3. same-session-echo count -- mines `$ENGRAM_HOME/surface-ledger.json`.
     Parses both the pre-#1779 shape (`{ts, ids}`) and the post-#1779 shape
     (`{ts, ids, prompt_embedding, suppressed_echo_ids, decay_events}`)
     identically -- an omitted field is treated as empty, so before/after
     ledgers parse through one code path. Also reports the `decay_events`
     cosine distribution (min/median/max/count) when present, for
     `prompt_similarity_threshold` tuning.
  4. engagement floor -- implements the #1780 union-join design comment
     exactly: for each rendered recall fire, was any of its rendered node
     IDs referenced *later* in the agent's OWN AUTHORED output (assistant
     text blocks + tool_use INPUT params) in the session transcript JSONL?
     Hook-injected `additionalContext` (the `[ENGRAM Recall...]` /
     `[ENGRAM in-turn recall...]` re-surfacing blocks) and tool RESULTS are
     EXCLUDED from "authored output" -- counting those inflates the metric
     from ~38% to ~96% (the whole point of the union-join design). This is
     a FLOOR: an agent that uses a node's content via paraphrase, without
     writing its ID, is uncounted, so true engagement >= measured.

Every metric line in the output states its known-error-direction.

Usage:
    python3 tools/recall_measurement_harness.py
    python3 tools/recall_measurement_harness.py --format json
    python3 tools/recall_measurement_harness.py --engram-home /path/to/.engram

Data-source resolution (all overridable by CLI flag):
    $ENGRAM_HOME (env) > ~/.engram (default), matching
    recall_repetition_analysis.py's _default_db_path() precedent.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import recall_repetition_analysis as rra  # noqa: E402  (reuse, do not reimplement)


def _add_engram_idf_to_path() -> None:
    """Put engram_idf.py on sys.path so the harness shares the canonical
    JUNK_STOPLIST (#1784) instead of keeping its own copy. Dev tree:
    <repo>/src/engram/. Deployed plugin: the plugin root (flat layout).
    Env override: $ENGRAM_RUNTIME_DIR."""
    repo_root = os.path.dirname(_TOOLS_DIR)
    candidates = [
        os.environ.get("ENGRAM_RUNTIME_DIR"),
        os.path.join(repo_root, "src", "engram"),
        repo_root,  # deployed flat layout
    ]
    for cand in candidates:
        if cand and os.path.exists(os.path.join(cand, "engram_idf.py")):
            if cand not in sys.path:
                sys.path.insert(0, cand)
            return


_add_engram_idf_to_path()
from engram_idf import JUNK_STOPLIST  # noqa: E402  (#1784 single source of truth)

# ---------------------------------------------------------------------------
# Node-ID regex -- per the #1780 harness spec (bullet 4). Note this is
# intentionally `\d{4,}` (four-or-more digits), which is the exact pattern
# given in the harness build spec; the #1780 design-comment prose uses
# `\d{4}` (exactly four) as a shorthand illustration. `\d{4,}` is a superset
# that also matches the exact-4-digit IDs actually in use on this host, so
# there is no behavioral divergence today -- flagged in the handoff as a
# spec-vs-comment wording discrepancy worth a glance.
# All 18 node-type prefixes, mirroring engram_core.TYPE_PREFIX -- keep in
# sync. Colleague-review catch (PR #1781): pr/th/ct/gt/ts were missing, so a
# rendered node of those types cited later in authored text silently never
# counted as engaged -- an undisclosed third undercount source. Added.
NODE_ID_RE = re.compile(
    r"\b(?:ob|dv|cs|ls|qu|cj|ev|fl|tk|pn|gl|ax|df|pr|th|ct|gt|ts)_\d{4,}\b")

# Junk-token stoplist (rec-3, #266). Aliased to the canonical
# engram_idf.JUNK_STOPLIST (#1784): the measurement uses the EXACT list the
# live filter (the recall hook) uses, so the two can never disagree about
# "what counts as junk" on the same ledger. The filter's conservative list is
# authoritative; this alias reconciles the measurement DOWN to it (dropping
# this module's former ambiguous extras like json/os/re/cat/print/def, which
# are over-suppression risks the filter deliberately does not carry).
# Overridable via --stoplist-file (one token per line, '#'-prefixed lines and
# blanks ignored).
DEFAULT_STOPLIST = JUNK_STOPLIST


# ---------------------------------------------------------------------------
# Path resolution


def resolve_engram_home(explicit: Optional[str]) -> str:
    """CLI arg > $ENGRAM_HOME env > ~/.engram default (matches
    recall_repetition_analysis.py's _default_db_path() precedent)."""
    if explicit:
        return os.path.expanduser(explicit)
    return os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram")


def resolve_seat_label(engram_home: str, explicit: Optional[str]) -> str:
    """Best-effort seat label for per-seat reporting (never averaged).

    Judgment call (not specified): prefer an explicit --seat flag; else read
    `agent_name` out of $ENGRAM_HOME/config.json; else fall back to the
    engram_home basename; else "unknown-seat".
    """
    if explicit:
        return explicit
    config_path = os.path.join(engram_home, "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        name = cfg.get("agent_name")
        if name:
            return str(name)
    except (OSError, json.JSONDecodeError):
        pass
    base = os.path.basename(os.path.normpath(engram_home))
    return base or "unknown-seat"


def load_stoplist(path: Optional[str]) -> frozenset:
    if not path:
        return DEFAULT_STOPLIST
    tokens = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            tokens.add(line.lower())
    return frozenset(tokens)


# ---------------------------------------------------------------------------
# Metric 1: repeat-render fraction (REUSE recall_repetition_analysis.py)


def compute_repeat_render(index_db_path: str) -> dict:
    """Wrap/aggregate recall_repetition_analysis.py's own functions -- no
    reimplementation of the repeat-render logic."""
    if not os.path.exists(index_db_path):
        return {
            "available": False,
            "reason": f"index.db not found at {index_db_path}",
        }
    by_session = rra.load_events(index_db_path)
    n_sessions = len(by_session)
    n_events = sum(len(v) for v in by_session.values())
    n_ids_total = sum(len(ids) for prompts in by_session.values() for _, ids in prompts)

    windows = {}
    for k in rra.WINDOWS:
        pooled, median, total_ids, total_repeats = rra.repeat_fraction_at_k(by_session, k)
        windows[k] = {
            "pooled_fraction": pooled,
            "median_per_session_fraction": median,
            "ids_checked": total_ids,
            "repeats": total_repeats,
        }
    max_n, p95_n, worst = rra.same_node_distribution(by_session)

    return {
        "available": True,
        "source": index_db_path,
        "n_sessions": n_sessions,
        "n_surface_fire_events": n_events,
        "n_ids_total_incl_repeats": n_ids_total,
        "windows": windows,
        "same_node_max": max_n,
        "same_node_p95": p95_n,
        "worst_offenders": [
            {"session": sid, "node": nid, "times": n} for sid, nid, n in worst
        ],
        "known_error_direction": (
            "Mines only engram.surface.fire events actually logged to "
            "index.db; dropped/pruned log rows UNDERESTIMATE the repeat "
            "fraction (a missing render can't be flagged as a repeat of a "
            "later one)."
        ),
    }


# ---------------------------------------------------------------------------
# Metric 2: junk-fire fraction


def compute_junk_fire(ledger_path: str, stoplist: frozenset) -> dict:
    if not os.path.exists(ledger_path):
        return {"available": False, "reason": f"ledger not found at {ledger_path}"}

    n_rendered_fires = 0
    n_any_junk_fires = 0
    n_all_junk_fires = 0
    n_novel_tokens = 0
    n_junk_tokens = 0

    with open(ledger_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("outcome") != "rendered":
                continue
            n_rendered_fires += 1
            novel_terms = rec.get("novel_terms") or []
            if not novel_terms:
                continue
            junk_flags = [tok.lower() in stoplist for tok in novel_terms]
            n_novel_tokens += len(novel_terms)
            n_junk_tokens += sum(junk_flags)
            if any(junk_flags):
                n_any_junk_fires += 1
            if all(junk_flags):
                n_all_junk_fires += 1

    token_fraction = (n_junk_tokens / n_novel_tokens) if n_novel_tokens else 0.0
    any_junk_fraction = (n_any_junk_fires / n_rendered_fires) if n_rendered_fires else 0.0
    all_junk_fraction = (n_all_junk_fires / n_rendered_fires) if n_rendered_fires else 0.0

    return {
        "available": True,
        "source": ledger_path,
        "n_rendered_fires": n_rendered_fires,
        "n_novel_tokens": n_novel_tokens,
        "n_junk_tokens": n_junk_tokens,
        "token_fraction": token_fraction,
        "any_junk_fraction": any_junk_fraction,
        "all_junk_fraction": all_junk_fraction,
        "known_error_direction": (
            "The stoplist is a seed list (rec-3), not exhaustive. Junk "
            "tokens not on the stoplist are counted as non-junk, so all "
            "three fractions here are a floor -- true junk fraction >= "
            "measured. Small-n ledgers (<1 day old) also carry wide "
            "sampling variance."
        ),
    }


# ---------------------------------------------------------------------------
# Metric 3: same-session-echo count


def compute_same_session_echo(surface_ledger_path: str) -> dict:
    if not os.path.exists(surface_ledger_path):
        return {
            "available": False,
            "reason": f"surface-ledger not found at {surface_ledger_path}",
        }

    try:
        with open(surface_ledger_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return {
            "available": False,
            "reason": f"surface-ledger unreadable/corrupt at {surface_ledger_path}: {e}",
        }
    if not isinstance(data, dict):
        return {
            "available": False,
            "reason": f"surface-ledger at {surface_ledger_path} is not a JSON object",
        }

    n_sessions = len(data)
    n_entries = 0
    n_suppressed_echo_events = 0
    n_entries_with_suppression = 0
    cosines: List[float] = []
    has_post_1779_fields = False

    for _sid, entries in data.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            n_entries += 1
            # Parse BOTH shapes identically: pre-#1779 {ts, ids} and
            # post-#1779 {ts, ids, prompt_embedding, suppressed_echo_ids,
            # decay_events}. Omitted field -> empty, one code path for both.
            suppressed = entry.get("suppressed_echo_ids") or []
            decay_events = entry.get("decay_events") or []
            if "suppressed_echo_ids" in entry or "decay_events" in entry:
                has_post_1779_fields = True
            if suppressed:
                n_entries_with_suppression += 1
            n_suppressed_echo_events += len(suppressed)
            for ev in decay_events:
                cosine = ev.get("cosine")
                if isinstance(cosine, (int, float)):
                    cosines.append(float(cosine))

    cosine_stats = None
    if cosines:
        cosine_stats = {
            "count": len(cosines),
            "min": min(cosines),
            "median": statistics.median(cosines),
            "max": max(cosines),
        }

    return {
        "available": True,
        "source": surface_ledger_path,
        "n_sessions": n_sessions,
        "n_entries": n_entries,
        "suppressed_echo_count": n_suppressed_echo_events,
        "n_entries_with_suppression": n_entries_with_suppression,
        "has_post_1779_fields": has_post_1779_fields,
        "decay_cosine_distribution": cosine_stats,
        "known_error_direction": (
            "Pre-#1779 ledger entries never carry suppressed_echo_ids -- "
            "the omitted-field-is-empty convention makes this metric read "
            "as 0 on a before-ledger, which is UNMEASURED, not a true "
            "zero-echo result. Do not compare a pre-#1779 0 against a "
            "post-#1779 nonzero count as if both were measured on the same "
            "basis."
        ),
    }


# ---------------------------------------------------------------------------
# Metric 4: engagement floor (the union-join, #1780 design comment)


def _parse_iso_ts(ts_str: str) -> Optional[float]:
    if not ts_str:
        return None
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str).timestamp()
    except (ValueError, TypeError):
        return None


def parse_transcript_authored_ids(transcript_path: str) -> Dict[str, float]:
    """Scan a session transcript JSONL and return {node_id: last_ts_seen}
    for node IDs appearing in agent-AUTHORED output only.

    COUNT: assistant message content blocks of type "text"; assistant
    tool_use blocks' "input" params (serialized to a string and scanned).

    EXCLUDE (by construction, since we only ever look at
    ``type == "assistant"`` records): hook-injected additionalContext
    (which appears as separate top-level records with
    ``type == "attachment"`` and ``attachment.type ==
    "hook_additional_context"`` on this host's transcript shape) and tool
    RESULTS (which appear inside ``type == "user"`` records as
    ``tool_result`` content blocks). Neither record type is scanned here,
    so the exclusion is structural, not a secondary filter -- there is no
    code path by which hook text or tool-result text can be misclassified
    as authored.

    "thinking" content blocks are also NOT counted: the #1780 design
    comment enumerates exactly two authored categories (assistant text +
    tool_use params); thinking is latent reasoning, not authored output.
    This is a judgment call -- see handoff.
    """
    last_ts: Dict[str, float] = {}
    if not transcript_path or not os.path.exists(transcript_path):
        return last_ts

    with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "assistant":
                continue
            ts = _parse_iso_ts(rec.get("timestamp"))
            if ts is None:
                continue
            message = rec.get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                blocks = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                blocks = content
            else:
                blocks = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text") or ""
                elif btype == "tool_use":
                    try:
                        text = json.dumps(block.get("input"), default=str)
                    except (TypeError, ValueError):
                        text = str(block.get("input"))
                else:
                    continue
                for match in NODE_ID_RE.findall(text):
                    prev = last_ts.get(match)
                    if prev is None or ts > prev:
                        last_ts[match] = ts
    return last_ts


class Fire:
    __slots__ = ("ts", "ids", "session_id", "transcript_path", "source")

    def __init__(self, ts: float, ids: Set[str], session_id: Optional[str],
                 transcript_path: Optional[str], source: str):
        self.ts = ts
        self.ids = ids
        self.session_id = session_id
        self.transcript_path = transcript_path
        self.source = source


def _load_in_turn_fires(ledger_path: str) -> List[Fire]:
    fires = []
    if not os.path.exists(ledger_path):
        return fires
    with open(ledger_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("outcome") != "rendered":
                continue
            ids = set(rec.get("rendered_ids") or [])
            if not ids:
                continue
            try:
                ts = float(rec["ts"])
            except (KeyError, TypeError, ValueError):
                continue  # malformed/missing ts -- skip this record, don't crash the metric
            fires.append(
                Fire(
                    ts=ts,
                    ids=ids,
                    session_id=rec.get("session_id"),
                    transcript_path=rec.get("transcript_path"),
                    source="in-turn",
                )
            )
    return fires


def _load_surface_fires(surface_ledger_path: str) -> List[Fire]:
    fires = []
    if not os.path.exists(surface_ledger_path):
        return fires
    try:
        with open(surface_ledger_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        # Corrupt/unreadable surface-ledger -- degrade to no surface fires
        # rather than crashing the whole harness (the in-turn ledger still
        # contributes fires to engagement-floor).
        return fires
    if not isinstance(data, dict):
        return fires
    for sid, entries in data.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ids = set(entry.get("ids") or [])
            if not ids:
                continue
            try:
                ts = float(entry["ts"])
            except (KeyError, TypeError, ValueError):
                continue  # malformed/missing ts -- skip this entry
            fires.append(
                Fire(
                    ts=ts,
                    ids=ids,
                    session_id=sid,
                    transcript_path=None,  # resolved separately
                    source="surface",
                )
            )
    return fires


def _resolve_transcript_paths(
    fires: List[Fire],
    explicit_transcripts: Sequence[str],
    transcript_dir: Optional[str],
) -> None:
    """Fill in Fire.transcript_path for surface-ledger fires (in-turn fires
    already carry their own transcript_path from the ledger record).

    Judgment call: transcript files are matched to a session_id by filename
    stem (``<session_id>.jsonl``), which is the real on-host convention
    observed in ``~/.claude/projects/<enc-cwd>/<session_id>.jsonl``. This is
    not asserted anywhere in the ledger data itself -- it is an assumption
    about Claude Code's transcript-naming convention. Flagged in handoff.
    """
    by_stem: Dict[str, str] = {}

    def _index_dir(d: str) -> None:
        try:
            for root, _dirs, files in os.walk(d):
                for fn in files:
                    if fn.endswith(".jsonl"):
                        stem = fn[: -len(".jsonl")]
                        by_stem.setdefault(stem, os.path.join(root, fn))
        except OSError:
            pass

    for path in explicit_transcripts:
        path = os.path.expanduser(path)
        stem = os.path.basename(path)
        if stem.endswith(".jsonl"):
            stem = stem[: -len(".jsonl")]
        by_stem[stem] = path

    if transcript_dir:
        _index_dir(os.path.expanduser(transcript_dir))

    needed_sids = {f.session_id for f in fires if f.transcript_path is None and f.session_id}
    if needed_sids and not needed_sids.issubset(by_stem.keys()):
        # Some needed session transcripts are still unresolved -- fall back to
        # the default Claude Code transcript root (searched lazily). This
        # fires even when --transcript-dir resolved SOME sessions but not the
        # rest, so a partial --transcript-dir no longer silently drops
        # sessions the default (unflagged) invocation would have found.
        # _index_dir uses setdefault, so explicit mappings still win.
        default_root = os.path.expanduser("~/.claude/projects")
        if os.path.isdir(default_root):
            _index_dir(default_root)

    for f in fires:
        if f.transcript_path is None and f.session_id:
            f.transcript_path = by_stem.get(f.session_id)


def compute_engagement_floor(
    ledger_path: str,
    surface_ledger_path: str,
    explicit_transcripts: Sequence[str],
    transcript_dir: Optional[str],
) -> dict:
    fires = _load_in_turn_fires(ledger_path) + _load_surface_fires(surface_ledger_path)
    _resolve_transcript_paths(fires, explicit_transcripts, transcript_dir)

    # Cache parsed authored-id maps per transcript path (a session's
    # transcript is parsed once even if it has many fires).
    authored_cache: Dict[str, Dict[str, float]] = {}

    n_fires_total = len(fires)
    n_fires_with_transcript = 0
    n_engaged = 0
    # Companion "recall attribution" metric (B) is explicitly OUT OF SCOPE
    # for this build -- the #1780 harness build spec lists engagement floor
    # (A) as the required metric; B is marked "companion" in the design
    # comment, not a build must-have. See handoff ambiguous-decisions log.

    for fire in fires:
        if not fire.transcript_path or not os.path.exists(fire.transcript_path):
            continue
        n_fires_with_transcript += 1
        authored = authored_cache.get(fire.transcript_path)
        if authored is None:
            authored = parse_transcript_authored_ids(fire.transcript_path)
            authored_cache[fire.transcript_path] = authored
        engaged = False
        for nid in fire.ids:
            last_ts = authored.get(nid)
            if last_ts is not None and last_ts > fire.ts:
                engaged = True
                break
        if engaged:
            n_engaged += 1

    engagement_floor = (n_engaged / n_fires_with_transcript) if n_fires_with_transcript else 0.0
    coverage = (n_fires_with_transcript / n_fires_total) if n_fires_total else 0.0

    return {
        "available": n_fires_total > 0,
        "n_fires_total": n_fires_total,
        "n_fires_with_transcript": n_fires_with_transcript,
        "transcript_coverage_fraction": coverage,
        "n_engaged": n_engaged,
        "engagement_floor": engagement_floor,
        "known_error_direction": (
            "This is a FLOOR, not a ceiling: an agent that uses a rendered "
            "node's content via paraphrase, without writing its literal ID, "
            "is uncounted, so true engagement >= measured. Additionally, "
            "fires whose session transcript could not be resolved/found "
            "are excluded from the denominator (see "
            "transcript_coverage_fraction) -- if unresolved sessions "
            "systematically differ from resolved ones, this is a further "
            "source of bias in an unknown direction."
        ),
    }


# ---------------------------------------------------------------------------
# Report assembly + rendering


def build_report(args: argparse.Namespace) -> dict:
    engram_home = resolve_engram_home(args.engram_home)
    seat = resolve_seat_label(engram_home, args.seat)

    ledger_path = args.ledger_path or os.path.join(engram_home, "in-turn-recall-ledger.jsonl")
    surface_ledger_path = args.surface_ledger_path or os.path.join(
        engram_home, "surface-ledger.json"
    )
    index_db_path = args.index_db_path or os.path.join(engram_home, "logs", "index.db")
    stoplist = load_stoplist(args.stoplist_file)

    def _safe(fn):
        # A measurement tool must never let one metric's failure discard the
        # others: an unforeseen crash in any single compute_*() degrades that
        # metric to an error record while the rest still report.
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 -- deliberate broad backstop
            return {"available": False, "error": f"{type(e).__name__}: {e}"}

    report = {
        "seat": seat,
        "engram_home": engram_home,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "ledger_path": ledger_path,
            "surface_ledger_path": surface_ledger_path,
            "index_db_path": index_db_path,
        },
        "metrics": {
            "repeat_render": _safe(lambda: compute_repeat_render(index_db_path)),
            "junk_fire": _safe(lambda: compute_junk_fire(ledger_path, stoplist)),
            "same_session_echo": _safe(
                lambda: compute_same_session_echo(surface_ledger_path)),
            "engagement_floor": _safe(lambda: compute_engagement_floor(
                ledger_path,
                surface_ledger_path,
                args.transcript or [],
                args.transcript_dir,
            )),
        },
    }
    return report


def _fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:.1f}%"


def render_md(report: dict) -> str:
    lines = []
    lines.append(f"# Rec-4 recall measurement -- seat: {report['seat']}\n")
    lines.append(f"Generated: {report['generated_at']}")
    lines.append(f"ENGRAM_HOME: {report['engram_home']}\n")

    rr = report["metrics"]["repeat_render"]
    lines.append("## 1. Repeat-render fraction\n")
    if not rr.get("available"):
        lines.append(f"_Unavailable: {rr.get('reason')}_\n")
    else:
        lines.append(
            f"Source: `{rr['source']}` | sessions: {rr['n_sessions']} | "
            f"surface-fire events: {rr['n_surface_fire_events']} | "
            f"total surfaced ids (incl. repeats): {rr['n_ids_total_incl_repeats']}\n"
        )
        lines.append("| k | pooled repeat % | median per-session repeat % | ids checked |")
        lines.append("|---|---|---|---|")
        for k, w in rr["windows"].items():
            lines.append(
                f"| {k} | {_fmt_pct(w['pooled_fraction'])} | "
                f"{_fmt_pct(w['median_per_session_fraction'])} | {w['ids_checked']} |"
            )
        lines.append(
            f"\nSame-node-in-one-session max: {rr['same_node_max']}x, "
            f"p95: {rr['same_node_p95']}x."
        )
        lines.append(f"\n_Known error direction: {rr['known_error_direction']}_\n")

    jf = report["metrics"]["junk_fire"]
    lines.append("## 2. Junk-fire fraction\n")
    if not jf.get("available"):
        lines.append(f"_Unavailable: {jf.get('reason')}_\n")
    else:
        lines.append(
            f"Source: `{jf['source']}` | rendered fires: {jf['n_rendered_fires']} | "
            f"novel tokens: {jf['n_novel_tokens']}\n"
        )
        lines.append("| metric | value |")
        lines.append("|---|---|")
        lines.append(f"| token-fraction (junk / all novel tokens) | {_fmt_pct(jf['token_fraction'])} |")
        lines.append(f"| any-junk-fire fraction | {_fmt_pct(jf['any_junk_fraction'])} |")
        lines.append(f"| all-junk-fire fraction | {_fmt_pct(jf['all_junk_fraction'])} |")
        lines.append(f"\n_Known error direction: {jf['known_error_direction']}_\n")

    se = report["metrics"]["same_session_echo"]
    lines.append("## 3. Same-session-echo count\n")
    if not se.get("available"):
        lines.append(f"_Unavailable: {se.get('reason')}_\n")
    else:
        lines.append(
            f"Source: `{se['source']}` | sessions: {se['n_sessions']} | "
            f"entries: {se['n_entries']} | post-#1779 fields present: "
            f"{se['has_post_1779_fields']}\n"
        )
        lines.append(f"Suppressed-echo events (count of ids in `suppressed_echo_ids`): "
                      f"**{se['suppressed_echo_count']}** "
                      f"({se['n_entries_with_suppression']} entries with >=1 suppression)")
        cs = se["decay_cosine_distribution"]
        if cs:
            lines.append(
                f"\nDecay-event cosine distribution: min={cs['min']:.4f}, "
                f"median={cs['median']:.4f}, max={cs['max']:.4f}, n={cs['count']}"
            )
        else:
            lines.append("\nNo decay_events present in this ledger.")
        lines.append(f"\n_Known error direction: {se['known_error_direction']}_\n")

    ef = report["metrics"]["engagement_floor"]
    lines.append("## 4. Engagement floor\n")
    if not ef.get("available"):
        lines.append("_Unavailable: no rendered fires found._\n")
    else:
        lines.append(
            f"Rendered fires: {ef['n_fires_total']} | with resolved transcript: "
            f"{ef['n_fires_with_transcript']} ({_fmt_pct(ef['transcript_coverage_fraction'])} "
            f"coverage) | engaged: {ef['n_engaged']}\n"
        )
        lines.append(f"**Engagement floor: {_fmt_pct(ef['engagement_floor'])}**\n")
        lines.append(f"_Known error direction: {ef['known_error_direction']}_\n")

    return "\n".join(lines) + "\n"


def render_json(report: dict) -> str:
    return json.dumps(report, indent=2, default=str)


# ---------------------------------------------------------------------------
# CLI


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--engram-home", default=None, help="Override $ENGRAM_HOME resolution.")
    parser.add_argument("--seat", default=None, help="Seat label for the report header.")
    parser.add_argument("--ledger-path", default=None, help="in-turn-recall-ledger.jsonl path.")
    parser.add_argument("--surface-ledger-path", default=None, help="surface-ledger.json path.")
    parser.add_argument("--index-db-path", default=None, help="logs/index.db path.")
    parser.add_argument(
        "--transcript",
        action="append",
        default=[],
        help="Explicit session transcript JSONL path (repeatable).",
    )
    parser.add_argument(
        "--transcript-dir",
        default=None,
        help="Directory to search for <session_id>.jsonl transcripts "
        "(defaults to searching ~/.claude/projects/ if a fire's transcript "
        "can't otherwise be resolved).",
    )
    parser.add_argument(
        "--stoplist-file",
        default=None,
        help="Override the junk-token stoplist (one token per line).",
    )
    parser.add_argument("--format", choices=["md", "json"], default="md")
    args = parser.parse_args(argv)

    report = build_report(args)
    if args.format == "json":
        print(render_json(report))
    else:
        print(render_md(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
