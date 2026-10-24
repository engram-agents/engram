#!/usr/bin/env python3
"""s2_score -- scoring script for the S2 clause-check study (engram-alpha#4050).

Implements the signed S2 pre-registration (draft 5, issue comment 6387983396;
sha256 9a0afe35bcc56fcff2269f5ff2d446c67059cccf7f12582fe74bade1d96c51c0 of the text
between its two rules; the seed is pinned to draft 4's value, 3594015630, issue
comment 6385972287), Order step 3: "Scoring script written and committed before any
labels arrive. It scores the planted controls first." The pre-registration is the
contract; where this file and that text disagree, that text wins. This tool only reads local files and
writes a report. It changes no write path, no floor, no refusal.

Usage
    s2_score.py --config config.json --hashes hashes.json --out-dir DIR
                [--scorer-commit SHA]

config.json (local, never committed)
    {"sheets": [ {                           # exactly two, in this order
        "name": "<label>",                   # appears in the report; not a node id
        "key": "<path>/key.json",            # written by s2_clause_sheet.py
        "planted_key": "<path>/planted-key.json",
                                             # {planted_id: {"added_clause": int|null}}
                                             # (1-based, as numbered on the sheet; null for P1/P2)
        "resplit_agreement": "<path>/resplit-agreement.json",
                                             # the seat's stage-1 re-split result (a JSON object,
                                             # reported verbatim)
        "sheet_pass1": "<path>/sheet-pass1.md", "sheet_pass2": "<path>/sheet-pass2.md",
                                             # the rendered sheets (their sha256 must equal the key's)
        "rater_A": "<judge>", "rater_B": "<judge>",
                                             # the fixed columns of every two-rater table
        "returns": {"<judge>": {"pass1": "<path>", "pass2": "<path>"}, ...}
    }, ... ]}

hashes.json: {"<sheet name>": {"<judge>": {"pass1": "<sha256>", "pass2": "<sha256>"}}}
    the hashes each judge posted before any return was opened.

Order of work: every return's sha256 is checked against its posted hash first
(nothing is scored from an unverified return); then structure; then labels; then
the planted controls; then the measures. Any refusal exits 2 and writes nothing.
Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

TOOL_VERSION = "1"
PREREGISTRATION = (
    "engram-alpha#4050 S2 draft 5, issue comment 6387983396, "
    "sha256 9a0afe35bcc56fcff2269f5ff2d446c67059cccf7f12582fe74bade1d96c51c0; "
    "seed pinned to draft 4's value 3594015630 (issue comment 6385972287)")
CI_METHOD = "percentile, linear interpolation at q*(m-1) over the defined resamples"
# A flag whose stripped, case-folded text is one of these is a placeholder, not a flag.
FLAG_PLACEHOLDERS = frozenset({"n/a", "na", "none", "-", "no"})

# The signed seed: the first 8 hex digits (d638538e) of draft 4's sha256, as an integer; pinned,
# so a later draft never moves the sample.
SIGNED_SEED = 3594015630
RESAMPLES = 2000

PASS1_LABELS = ("CARRIED", "NOT", "UNCLEAR")
STATED = "NOT, STATED IN INTERPRETATION"
PASS2_LABELS = PASS1_LABELS + (STATED,)
BIN_LABELS = ("B1", "B2", "B3", "B4")
PASSES = ("pass1", "pass2")
PLANTED_KINDS = ("P1", "P2", "P3", "P4")
ADDED_CLAUSE_KINDS = ("P3", "P4")
STAT_NAMES = ("agreement", "kappa", "ac1", "pabak")

PLANTED_NOTE = (
    "Planted controls are scored first, per judge per sheet, on pass-1 labels. (a) A judge who "
    "misses both P3 and P4 (the added clause not labelled NOT; UNCLEAR counts as missed) is not "
    "pooled. (b) A judge who labels any clause of P1 or P2 NOT is not pooled. (c) 'Labels all "
    "four planted items NOT' (an item counts when ANY of its clauses is NOT) is reported "
    "separately; it is subsumed by (b) under either reading (any clause or all clauses of an "
    "item), because P1 and P2 are fully carried, so a judge meeting (c) always meets (b). A "
    "sheet left with fewer than two pooled judges has an undefined kappa and reads 'not met'. "
    "Planted items are excluded from every other measure."
)


class Refusal(Exception):
    """A refusal the user must act on; main() prints it and returns 2."""


# ---------------------------------------------------------------------------
# Statistics on a 2x2 table t = (n11, n10, n01, n00): rows are column A positive/
# negative, columns are column B positive/negative. Every function returns
# (value, None) or (None, reason); an undefined value is never 0, 1 or NaN.
# ---------------------------------------------------------------------------

def _add(t, u):
    return tuple(x + y for x, y in zip(t, u))


def _po_fraction(t):
    n = sum(t)
    return Fraction(t[0] + t[3], n)


def _kappa_fraction(t):
    n = sum(t)
    if n == 0:
        return None, "undefined: the table is empty"
    a, b, c, d = t
    pe_num = (a + b) * (a + c) + (c + d) * (b + d)
    if pe_num == n * n:
        return None, ("undefined: expected agreement is 1 (both raters give the same single "
                      "answer to every clause), so kappa has no value")
    pe = Fraction(pe_num, n * n)
    return (_po_fraction(t) - pe) / (1 - pe), None


def agreement(t):
    if sum(t) == 0:
        return None, "undefined: the table is empty"
    return float(_po_fraction(t)), None


def cohen_kappa(t):
    k, why = _kappa_fraction(t)
    return (None, why) if k is None else (float(k), None)


def gwet_ac1(t):
    n = sum(t)
    if n == 0:
        return None, "undefined: the table is empty"
    a, b, c, d = t
    pi = Fraction((a + b) + (a + c), 2 * n)
    pe = 2 * pi * (1 - pi)
    if pe == 1:  # cannot happen (pe <= 1/2) but never divide by zero
        return None, "undefined: chance agreement is 1"
    return float((_po_fraction(t) - pe) / (1 - pe)), None


def pabak(t):
    if sum(t) == 0:
        return None, "undefined: the table is empty"
    return float(2 * _po_fraction(t) - 1), None


STAT_FUNCS = (("agreement", agreement), ("kappa", cohen_kappa), ("ac1", gwet_ac1), ("pabak", pabak))


def _stat(fn, t):
    v, why = fn(t)
    return {"value": v, "undefined_reason": why}


def _ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [Fraction(0)] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = Fraction(i + j, 2) + 1  # average rank of a tie group
        i = j + 1
    return ranks


def spearman(xs, ys):
    """Spearman correlation: Pearson on average ranks (ties share their mean rank)."""
    if len(xs) != len(ys):
        raise ValueError("spearman: unequal lengths")
    if len(xs) < 2:
        return None, "undefined: fewer than two observations"
    rx, ry = _ranks(list(xs)), _ranks(list(ys))
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None, "undefined: one variable is constant (all values tie)"
    return float(sxy) / math.sqrt(float(sxx * syy)), None


# ---------------------------------------------------------------------------
# Bootstrap: a cluster bootstrap over observations (real items), not clauses.
# ---------------------------------------------------------------------------

def resample_tables(rng, sheets):
    """One resample. ``sheets`` is a list (config order) of lists of per-observation
    tables. Each sheet draws as many observations as it has, with replacement, from
    ITSELF only; the pooled table adds the sheets' resampled tables cell by cell."""
    per_sheet = []
    for items in sheets:
        m = len(items)
        t = (0, 0, 0, 0)
        for _ in range(m):
            t = _add(t, items[rng.randrange(m)])
        per_sheet.append(t)
    pooled = (0, 0, 0, 0)
    for t in per_sheet:
        pooled = _add(pooled, t)
    return per_sheet, pooled


def percentile_interval(values):
    """95% percentile interval: linear interpolation at q*(m-1) between order statistics."""
    vs = sorted(values)
    m = len(vs)
    if m == 0:
        return None, None

    def at(q):
        pos = q * (m - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, m - 1)
        return vs[lo] + (vs[hi] - vs[lo]) * (pos - lo)
    return at(0.025), at(0.975)


def bootstrap(rng, sheets, n=RESAMPLES):
    """Per scope (each sheet in config order, then pooled) and statistic: the list of
    resample values (None where undefined). Draw order is fixed, so a re-run is identical."""
    scopes = len(sheets) + 1
    vals = [{name: [] for name in STAT_NAMES} for _ in range(scopes)]
    for _ in range(n):
        per_sheet, pooled = resample_tables(rng, sheets)
        for si, t in enumerate(per_sheet + [pooled]):
            for name, fn in STAT_FUNCS:
                vals[si][name].append(fn(t)[0])
    return vals


def _ci_block(values, point):
    defined = [v for v in values if v is not None]
    lo, hi = percentile_interval(defined)
    blk = {"point": point, "lo": lo, "hi": hi, "resamples": len(values),
           "defined_resamples": len(defined), "undefined_resamples": len(values) - len(defined)}
    if not defined:
        blk["undefined_reason"] = "undefined: no resample had a defined value"
    return blk


# ---------------------------------------------------------------------------
# The criterion (measure 5). Compared exactly (Fractions), never on rounded floats.
# ---------------------------------------------------------------------------

KAPPA_POOLED_MIN = Fraction(3, 5)
AGREEMENT_POOLED_MIN = Fraction(4, 5)
KAPPA_SHEET_MIN = Fraction(1, 2)
SHORT_VERDICT = "judges do not agree well enough"


def evaluate_criterion(pooled_table, sheet_tables, names, unpooled=()):
    """Met only if pooled kappa >= 0.60 AND pooled raw agreement >= 0.80 AND each sheet's
    kappa >= 0.50. ``pooled_table`` / a sheet table may be None (undefined). ``unpooled``
    names sheets left with fewer than two pooled judges (they fall short)."""
    reasons, short = [], []
    if pooled_table is None:
        reasons.append("pooled kappa is undefined (a sheet has fewer than two pooled judges)")
    else:
        k, why = _kappa_fraction(tuple(pooled_table))
        if k is None:
            reasons.append(f"pooled kappa is undefined ({why})")
        elif k < KAPPA_POOLED_MIN:
            reasons.append(f"pooled kappa {float(k):.4f} is below 0.60")
        if sum(pooled_table) == 0:
            reasons.append("pooled agreement is undefined (empty table)")
        elif _po_fraction(tuple(pooled_table)) < AGREEMENT_POOLED_MIN:
            reasons.append(f"pooled raw agreement {float(_po_fraction(tuple(pooled_table))):.4f} is below 0.80")
    for name, t in zip(names, sheet_tables):
        if name in unpooled or t is None:
            short.append(name)
            reasons.append(f"{name}: fewer than two pooled judges, kappa undefined")
            continue
        k, why = _kappa_fraction(tuple(t))
        if k is None:
            short.append(name)
            reasons.append(f"{name}: kappa is undefined ({why})")
        elif k < KAPPA_SHEET_MIN:
            short.append(name)
            reasons.append(f"{name}: kappa {float(k):.4f} is below 0.50")
    met = not reasons
    if met:
        text = "criterion met: the rubric is reliable enough to try an automated check against"
    else:
        text = SHORT_VERDICT
        if short:
            text += "; sheet(s) that fell short: " + ", ".join(short)
        if reasons:
            text += " (" + "; ".join(reasons) + ")"
    return {"met": met, "verdict": text, "short_sheets": short, "reasons": reasons}


# ---------------------------------------------------------------------------
# Reading inputs
# ---------------------------------------------------------------------------

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path, what):
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise Refusal(f"cannot read {what} {path}: {exc.strerror or exc}") from None


def _json(data: bytes, path):
    try:
        return json.loads(data)
    except ValueError as exc:
        raise Refusal(f"{path} is not valid JSON: {exc}") from None


ITEM_RE = re.compile(r"^##\s+Item\s+(\d+)\s*$")
CLAUSE_RE = re.compile(r"^(\d+)\.\s*(.*)$")
LABEL_RE = re.compile(r"^Label\s*:(.*)$", re.I)
FLAG_RE = re.compile(r"^Flag\s*\(optional\)\s*:(.*)$", re.I)
CLAUSES_RE = re.compile(r"^\*\*Clauses:?\*\*:?$")
SECTION_RE = re.compile(r"^\*\*(Quote|Interpretation)\b")


NOTES_HEADING_PATTERN = r"^#{0,6}\s*notes\s*:?$"
NOTES_HEADING_RE = re.compile(NOTES_HEADING_PATTERN, re.I)


def notes_hint(notes_line):
    """Appended to a refusal when a Notes heading may have hidden part of the return."""
    if notes_line is None:
        return ""
    return (f" (a '## Notes' line at line {notes_line} ends item parsing and hides everything after it; "
            "it must come after the last clause of the last item)")


def parse_sheet(text, where):
    """Parse a rendered sheet or a judge's return into {item number: [clause records]}.
    A record is {"text", "label" (raw string after 'Label:'), "flag" (str|None)}. Anything
    that is not recognised inside an item refuses: nothing is silently dropped."""
    return _parse(text, where, allow_notes=False)[0]


def parse_return(text, where):
    """Like parse_sheet, for a judge's return: a line matching NOTES_HEADING_PATTERN (``## Notes``,
    ``Notes:``, ``### notes`` ...; case and surrounding whitespace ignored) ends item parsing; a
    heading between a clause's Label line and its Flag line refuses. Everything after it is the judge's free
    notes, never read as labels or flags. Returns (items, notes, notes_line); notes is the text
    after the heading line, verbatim (None when there is no heading)."""
    return _parse(text, where, allow_notes=True)


def _parse(text, where, allow_notes):
    items = {}
    notes, notes_line = None, None
    cur = None
    in_clauses = False
    clause = None
    flag_open = False
    lines = text.splitlines()
    for lineno, raw in enumerate(lines, 1):
        s = raw.strip()
        if allow_notes and NOTES_HEADING_RE.match(s):
            after = next((t.strip() for t in lines[lineno:] if t.strip()), "")
            if clause is not None and clause["flag"] is None and FLAG_RE.match(after):
                # the heading sits between a clause's Label and its Flag line: ending the parse
                # here would drop that flag without a word
                raise Refusal(f"{where}: item {cur}, clause {len(items[cur])}: a Notes heading (line {lineno}) "
                              "comes before this clause's Flag line and would hide it; the heading must come "
                              "after the last clause's Flag line" + notes_hint(lineno))
            notes, notes_line = "\n".join(lines[lineno:]), lineno
            break
        m = ITEM_RE.match(s)
        if m:
            n = int(m.group(1))
            if n in items:
                raise Refusal(f"{where}: item {n} appears twice")
            items[n] = []
            cur, in_clauses, clause, flag_open = n, False, None, False
            continue
        if cur is None or s == "":
            continue  # the header before the first item, and blank lines
        if not in_clauses:
            if CLAUSES_RE.match(s):
                in_clauses = True
            elif SECTION_RE.match(s) or s.startswith(">"):
                pass  # the quote / interpretation blocks are not read
            else:
                raise Refusal(f"{where}: line {lineno} in item {cur} is not recognised: {s[:60]!r}")
            continue
        m = LABEL_RE.match(s)
        if m:
            if clause is None:
                raise Refusal(f"{where}: item {cur}: a Label line before any clause (line {lineno})")
            if clause["label"] is not None:
                raise Refusal(f"{where}: item {cur}, clause {len(items[cur])}: two Label lines")
            clause["label"] = m.group(1)
            flag_open = False
            continue
        m = FLAG_RE.match(s)
        if m:
            if clause is None:
                raise Refusal(f"{where}: item {cur}: a Flag line before any clause (line {lineno})")
            if clause["flag"] is not None:
                raise Refusal(f"{where}: item {cur}, clause {len(items[cur])}: two Flag lines")
            clause["flag"] = m.group(1).strip()
            flag_open = True
            continue
        m = CLAUSE_RE.match(s)
        if m and int(m.group(1)) == len(items[cur]) + 1:
            if clause is not None and clause["label"] is None:
                raise Refusal(f"{where}: item {cur}, clause {len(items[cur])}: no Label line")
            clause = {"text": m.group(2).strip(), "label": None, "flag": None}
            items[cur].append(clause)
            flag_open = False
            continue
        if flag_open and clause is not None:
            clause["flag"] = (clause["flag"] + "\n" + s).strip()  # a flag may run over several lines
            continue
        raise Refusal(f"{where}: item {cur}: line {lineno} is not recognised (expected clause "
                      f"{len(items[cur]) + 1} or a Label line): {s[:60]!r}")
    for n, cl in items.items():
        for k, c in enumerate(cl, 1):
            if c["label"] is None:
                raise Refusal(f"{where}: item {n}, clause {k}: no Label line" + notes_hint(notes_line))
    return items, notes, notes_line


def norm_label(raw: str) -> str:
    t = " ".join(raw.split()).upper()
    return re.sub(r"\s*,\s*", ", ", t)


def _load_sheet_inputs(cfg, base, inputs):
    """Read everything for one sheet except the returns."""
    for field in ("name", "key", "planted_key", "resplit_agreement", "sheet_pass1", "sheet_pass2",
                  "rater_A", "rater_B", "returns"):
        if field not in cfg:
            raise Refusal(f"config: a sheet lacks \"{field}\"")
    name = cfg["name"]
    if cfg["rater_A"] == cfg["rater_B"]:
        raise Refusal(f"config: {name}: rater_A and rater_B are the same judge")
    ret = cfg["returns"]
    if not isinstance(ret, dict) or set(ret) != {cfg["rater_A"], cfg["rater_B"]}:
        raise Refusal(f"config: {name}: returns must name exactly the two raters")

    def path(p):
        q = Path(p)
        return q if q.is_absolute() else base / q

    sheet = {"name": name, "A": cfg["rater_A"], "B": cfg["rater_B"]}
    kpath = path(cfg["key"])
    kdata = _read(kpath, "the key")
    inputs[f"{name}/key"] = _sha(kdata)
    key = _json(kdata, kpath)
    if not isinstance(key, dict) or "render" not in key or "items" not in key:
        raise Refusal(f"{kpath}: not a rendered s2_clause_sheet key (no render / items)")
    if key.get("seed") != SIGNED_SEED:
        raise Refusal(f"{kpath}: key seed {key.get('seed')!r} is not the signed seed {SIGNED_SEED}")
    sheet["key"] = key

    render_items = key["render"].get("items")
    if not isinstance(render_items, dict) or not render_items:
        raise Refusal(f"{kpath}: render.items missing")
    sheet["items"] = {int(n): v for n, v in render_items.items()}
    by_id = {i["node_id"]: i for i in key["items"]}
    real, planted = {}, {}
    for n, v in sheet["items"].items():
        if v.get("kind") in PLANTED_KINDS:
            planted[n] = v
        elif v.get("kind") is None:
            if v["id"] not in by_id:
                raise Refusal(f"{kpath}: item {n} is not among the key's selected items")
            real[n] = v
        else:
            raise Refusal(f"{kpath}: item {n} has an unknown kind {v.get('kind')!r}")
    if sorted(v["kind"] for v in planted.values()) != sorted(PLANTED_KINDS):
        raise Refusal(f"{kpath}: the key must hold exactly one planted item of each kind P1..P4")
    sheet["real"] = sorted(real, key=lambda n: real[n]["id"])  # canonical order: by node id, not by number
    sheet["planted"] = {v["kind"]: n for n, v in planted.items()}
    sheet["bin"] = {n: by_id[real[n]["id"]]["bin"] for n in real}
    sheet["sim"] = {n: by_id[real[n]["id"]]["similarity"] for n in real}

    pkpath = path(cfg["planted_key"])
    pdata = _read(pkpath, "the planted key")
    inputs[f"{name}/planted_key"] = _sha(pdata)
    pk = _json(pdata, pkpath)
    if not isinstance(pk, dict) or set(pk) != {v["id"] for v in planted.values()}:
        raise Refusal(f"{pkpath}: must hold exactly the four planted ids of this sheet")
    sheet["added"] = {}
    for kind, n in sheet["planted"].items():
        entry = pk[sheet["items"][n]["id"]]
        added = entry.get("added_clause") if isinstance(entry, dict) else "bad"
        if kind in ADDED_CLAUSE_KINDS:
            if (not isinstance(added, int) or isinstance(added, bool)
                    or not 1 <= added <= sheet["items"][n]["clauses"]):
                raise Refusal(f"{pkpath}: {kind} needs an integer added_clause within its clauses (1-based)")
            sheet["added"][kind] = added
        elif added is not None:
            raise Refusal(f"{pkpath}: {kind} must have added_clause null")

    rpath = path(cfg["resplit_agreement"])
    rdata = _read(rpath, "the re-split agreement file")
    inputs[f"{name}/resplit_agreement"] = _sha(rdata)
    resplit = _json(rdata, rpath)
    if not isinstance(resplit, dict):
        raise Refusal(f"{rpath}: must be a JSON object")
    sheet["resplit"] = resplit

    sheet["reference"] = {}
    for p, field in (("pass1", "sheet_pass1"), ("pass2", "sheet_pass2")):
        spath = path(cfg[field])
        sdata = _read(spath, "the rendered sheet")
        sha = _sha(sdata)
        inputs[f"{name}/{field}"] = sha
        fname = "sheet-pass1.md" if p == "pass1" else "sheet-pass2.md"
        if key["render"].get("sha256", {}).get(fname) != sha:
            raise Refusal(f"{spath}: sha256 does not match the key's recorded hash for {fname}")
        ref = parse_sheet(sdata.decode("utf-8"), str(spath))
        if set(ref) != set(sheet["items"]):
            raise Refusal(f"{spath}: items differ from the key's")
        for n, cl in ref.items():
            if len(cl) != sheet["items"][n]["clauses"]:
                raise Refusal(f"{spath}: item {n} has {len(cl)} clauses, the key has {sheet['items'][n]['clauses']}")
        sheet["reference"][p] = ref
    sheet["return_paths"] = {j: {p: path(ret[j][p]) for p in PASSES} for j in ret}
    return sheet


def _check_structure(sheet, ref, got, where):
    for n in sorted(set(ref) - set(got)):
        raise Refusal(f"{where}: item {n} is missing")
    for n in sorted(set(got) - set(ref)):
        raise Refusal(f"{where}: item {n} is not in the key")
    for n in sorted(ref):
        if len(got[n]) != len(ref[n]):
            raise Refusal(f"{where}: item {n} has {len(got[n])} clauses, the key has {len(ref[n])}")
        for k, (r, g) in enumerate(zip(ref[n], got[n]), 1):
            if r["text"] != g["text"]:
                raise Refusal(f"{where}: item {n}, clause {k}: clause text differs from the rendered sheet")


def _labels_of(parsed, allowed, where, bad):
    """{(item, clause): (effective label, blank?)}; unknown tokens are appended to ``bad``."""
    out = {}
    for n, cl in parsed.items():
        for k, c in enumerate(cl, 1):
            raw = c["label"]
            if raw.strip() == "":
                out[(n, k)] = ("UNCLEAR", True)
                continue
            lab = norm_label(raw)
            if lab not in allowed:
                bad.append(f"{where}: item {n}, clause {k}: {raw.strip()!r} is not an allowed label")
                continue
            out[(n, k)] = (lab, False)
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _planted_record(sheet, judge, role, p1):
    """(a)-(c) for one judge on one sheet, from pass-1 labels."""
    lab = lambda n, k: p1[(n, k)][0]  # noqa: E731
    not_in = lambda n: any(lab(n, k) == "NOT" for k in range(1, sheet["items"][n]["clauses"] + 1))  # noqa: E731
    missed = {kind: lab(sheet["planted"][kind], sheet["added"][kind]) != "NOT" for kind in ADDED_CLAUSE_KINDS}
    p12 = any(not_in(sheet["planted"][kind]) for kind in ("P1", "P2"))
    all4 = all(not_in(sheet["planted"][kind]) for kind in PLANTED_KINDS)
    both = missed["P3"] and missed["P4"]
    reasons = []
    if both:
        reasons.append("(a) misses both P3 and P4")
    if p12:
        reasons.append("(b) labels a clause of P1 or P2 NOT")
    if all4:
        reasons.append("(c) labels all four planted items NOT")
    return {"column": role, "missed_p3": missed["P3"], "missed_p4": missed["P4"], "misses_both": both,
            "p1_p2_labelled_not": p12, "labels_all_four_not": all4, "pooled": not reasons, "reasons": reasons}


def _obs_table(la, lb, n_clauses, positives):
    n11 = n10 = n01 = n00 = 0
    for k in range(1, n_clauses + 1):
        a, b = la[k] in positives, lb[k] in positives
        if a and b:
            n11 += 1
        elif a:
            n10 += 1
        elif b:
            n01 += 1
        else:
            n00 += 1
    return (n11, n10, n01, n00)


def _t2(t):
    return [[t[0], t[1]], [t[2], t[3]]]


def _share(num, den):
    return {"numerator": num, "denominator": den,
            "value": float(Fraction(num, den)) if den else None,
            "undefined_reason": None if den else "undefined: no observations"}


def score(sheets, labels1, labels2, flag_counts, bootstrap_rng):
    """All the numbers. ``labels1[si][judge]`` / ``labels2`` are {(item, clause): (label, blank)};
    ``flag_counts[(si, judge)]`` is {"pass1": n, "pass2": n} over the real items."""
    n_sheets = len(sheets)
    # --- planted controls first -------------------------------------------------
    planted = {}
    for si, sh in enumerate(sheets):
        recs = {}
        for role in ("A", "B"):
            j = sh[role]
            recs[j] = _planted_record(sh, j, role, labels1[si][j])
        sh["pooled_ok"] = sum(r["pooled"] for r in recs.values()) >= 2
        n_pooled = sum(r["pooled"] for r in recs.values())
        planted[sh["name"]] = {
            "judges": recs, "pooled_judges": n_pooled,
            "kappa_status": "defined" if sh["pooled_ok"] else "undefined: fewer than two pooled judges (reads: not met)"}
    unpooled = [sh["name"] for sh in sheets if not sh["pooled_ok"]]

    # --- per-observation tables --------------------------------------------------
    def per_judge_labels(si, judge, n):
        return {k: labels1[si][judge][(n, k)][0] for k in range(1, sheets[si]["items"][n]["clauses"] + 1)}

    measures = {}
    obs_tables = {}
    for cname, positives in (("primary", {"NOT"}), ("second_collapse", {"NOT", "UNCLEAR"})):
        obs_tables[cname] = []
        for si, sh in enumerate(sheets):
            tabs = []
            for n in sh["real"]:
                la, lb = per_judge_labels(si, sh["A"], n), per_judge_labels(si, sh["B"], n)
                tabs.append(_obs_table(la, lb, sh["items"][n]["clauses"], positives))
            obs_tables[cname].append(tabs)

    for cname in ("primary", "second_collapse"):
        boot = bootstrap(bootstrap_rng, obs_tables[cname])  # fixed order: collapse, then sheets, then pooled
        sheet_tabs = [tuple(map(sum, zip(*tabs))) if tabs else (0, 0, 0, 0) for tabs in obs_tables[cname]]
        blocks = {}
        for si, sh in enumerate(sheets):
            t = sheet_tabs[si]
            blk = {"table": _t2(t), "n_clauses": sum(t), "n_observations": len(obs_tables[cname][si]),
                   "pooled_judges": sh["pooled_ok"]}
            if sh["pooled_ok"]:
                for name, fn in STAT_FUNCS:
                    blk[name] = _stat(fn, t)
                blk["ci"] = {name: _ci_block(boot[si][name], blk[name]["value"]) for name in STAT_NAMES}
            else:
                why = "undefined: fewer than two pooled judges on this sheet"
                for name in STAT_NAMES:
                    blk[name] = {"value": None, "undefined_reason": why}
                blk["ci"] = {}
                blk["diagnostic_only"] = {name: _stat(fn, t) for name, fn in STAT_FUNCS}
            blocks[sh["name"]] = blk
        if unpooled:
            why = "undefined: sheet(s) with fewer than two pooled judges: " + ", ".join(unpooled)
            pooled_blk = {"table": None, "n_clauses": None, "n_observations": None, "ci": {},
                          **{name: {"value": None, "undefined_reason": why} for name in STAT_NAMES}}
            pooled_t = None
        else:
            pooled_t = (0, 0, 0, 0)
            for t in sheet_tabs:
                pooled_t = _add(pooled_t, t)
            pooled_blk = {"table": _t2(pooled_t), "n_clauses": sum(pooled_t),
                          "n_observations": sum(len(x) for x in obs_tables[cname])}
            for name, fn in STAT_FUNCS:
                pooled_blk[name] = _stat(fn, pooled_t)
            pooled_blk["ci"] = {name: _ci_block(boot[n_sheets][name], pooled_blk[name]["value"])
                                for name in STAT_NAMES}
        measures[cname] = {"sheets": blocks, "pooled": pooled_blk,
                           "_tables": (sheet_tabs, pooled_t)}

    sheet_tabs, pooled_t = measures["primary"].pop("_tables")
    measures["second_collapse"].pop("_tables")
    crit = evaluate_criterion(
        None if pooled_t is None else pooled_t,
        [t if sh["pooled_ok"] else None for t, sh in zip(sheet_tabs, sheets)],
        [sh["name"] for sh in sheets], unpooled=unpooled)
    verdict = {"met": crit["met"], "text": crit["verdict"], "short_sheets": crit["short_sheets"],
               "reasons": crit["reasons"]}

    # --- marginals ---------------------------------------------------------------
    marginals = {}
    for si, sh in enumerate(sheets):
        marginals[sh["name"]] = {}
        for role in ("A", "B"):
            j = sh[role]
            labs = [labels1[si][j][(n, k)][0] for n in sh["real"] for k in range(1, sh["items"][n]["clauses"] + 1)]
            three = {l: labs.count(l) for l in PASS1_LABELS}
            marginals[sh["name"]][j] = {
                "column": role, "three_way": three,
                "primary_collapse": {"NOT": three["NOT"], "not_NOT": three["CARRIED"] + three["UNCLEAR"]},
                "second_collapse": {"NOT_or_UNCLEAR": three["NOT"] + three["UNCLEAR"], "CARRIED": three["CARRIED"]}}

    # --- secondaries -------------------------------------------------------------
    share_bins, spear_in = {}, {}
    per_sheet_obs = []
    for si, sh in enumerate(sheets):
        obs = []
        for n in sh["real"]:
            ncl = sh["items"][n]["clauses"]
            la, lb = per_judge_labels(si, sh["A"], n), per_judge_labels(si, sh["B"], n)
            obs.append({"bin": sh["bin"][n], "sim": sh["sim"][n],
                        "a": sum(la[k] == "NOT" for k in la), "b": sum(lb[k] == "NOT" for k in lb),
                        "both": sum(la[k] == "NOT" and lb[k] == "NOT" for k in range(1, ncl + 1))})
        per_sheet_obs.append(obs)

    def bins_of(obs_list):
        out = {}
        for b in BIN_LABELS:
            sel = [o for o in obs_list if o["bin"] == b]
            blk = {"observations": len(sel), "observations_with_both_not": sum(o["both"] > 0 for o in sel)}
            blk["share"] = _share(blk["observations_with_both_not"], blk["observations"])
            out[b] = blk
        return out

    def spearmans(obs_list):
        sims = [o["sim"] for o in obs_list]
        return {name: dict(zip(("value", "undefined_reason"), spearman(sims, [o[key] for o in obs_list])))
                for name, key in (("A_not_count", "a"), ("B_not_count", "b"), ("both_not_count", "both"))}

    pooled_obs = [o for si, sh in enumerate(sheets) if sh["pooled_ok"] for o in per_sheet_obs[si]]
    for si, sh in enumerate(sheets):
        share_bins[sh["name"]] = bins_of(per_sheet_obs[si])
        spear_in[sh["name"]] = spearmans(per_sheet_obs[si])
    share_bins["pooled"] = bins_of(pooled_obs)
    spear_in["pooled"] = spearmans(pooled_obs)

    per_judge = {}
    for si, sh in enumerate(sheets):
        per_judge[sh["name"]] = {}
        for role in ("A", "B"):
            j = sh[role]
            clauses = [(n, k) for n in sh["real"] for k in range(1, sh["items"][n]["clauses"] + 1)]
            nots = [c for c in clauses if labels1[si][j][c][0] == "NOT"]
            stated = [c for c in nots if labels2[si][j][c][0] == STATED]
            unclear = [c for c in clauses if labels1[si][j][c][0] == "UNCLEAR"]
            p2 = _share(len(stated), len(nots))
            ur = _share(len(unclear), len(clauses))
            per_judge[sh["name"]][j] = {
                "column": role, "pass2_rate": p2, "unclear_rate": ur, "unclear_count": len(unclear),
                "flag_count": flag_counts[(si, j)]}
    secondaries = {"both_not_share_by_bin": share_bins, "pooled_sheets_included": [
        sh["name"] for sh in sheets if sh["pooled_ok"]], "spearman": spear_in,
        "clause_split_agreement": {sh["name"]: sh["resplit"] for sh in sheets}, "per_judge": per_judge}

    return {"planted": planted, "measures": measures, "verdict": verdict, "marginals": marginals,
            "secondaries": secondaries}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _is_flag(text):
    """A flag is non-empty text that is not a bare placeholder ('n/a', 'none', '-', ...)."""
    return bool(text) and text.strip().casefold() not in FLAG_PLACEHOLDERS


def _scorer_commit(arg):
    if arg:
        return arg
    try:
        out = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True, timeout=30)
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        raise Refusal("cannot read this checkout's commit; pass --scorer-commit") from None


def _create_new(path: Path, text: str) -> None:
    """Create-only (O_EXCL): never overwrites, never follows a symlink."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise Refusal(f"{path} already exists; refusing to overwrite") from None
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def run(args) -> int:
    cpath = Path(args.config)
    cdata = _read(cpath, "the config")
    hpath = Path(args.hashes)
    hdata = _read(hpath, "the posted hashes")
    inputs = {"config": _sha(cdata), "hashes": _sha(hdata)}
    config = _json(cdata, cpath)
    posted = _json(hdata, hpath)
    if (not isinstance(config, dict) or not isinstance(config.get("sheets"), list)
            or len(config["sheets"]) != 2):
        raise Refusal("config: \"sheets\" must be a list of exactly two sheets (the signed design)")
    if not isinstance(posted, dict):
        raise Refusal("hashes.json must be an object {sheet: {judge: {pass1, pass2}}}")
    out_dir = Path(args.out_dir)
    for f in ("report.json", "report.md"):
        if os.path.lexists(out_dir / f):
            raise Refusal(f"{out_dir / f} already exists; refusing to overwrite")
    commit = _scorer_commit(args.scorer_commit)

    base = cpath.resolve().parent
    sheets = [_load_sheet_inputs(c, base, inputs) for c in config["sheets"]]
    names = [sh["name"] for sh in sheets]
    if len(set(names)) != 2:
        raise Refusal("config: the two sheets need different names")
    if sheets[0]["A"] != sheets[1]["A"]:
        raise Refusal(f"config: rater_A differs across sheets ({sheets[0]['name']}: {sheets[0]['A']!r}, "
                      f"{sheets[1]['name']}: {sheets[1]['A']!r}); measure 3 fixes column A as the same "
                      f"judge on both sheets (rater_B may differ)")

    # 1. Hash-first: nothing is read as labels from an unverified return.
    returns, bad_hash = {}, []
    for sh in sheets:
        for judge, paths in sh["return_paths"].items():
            for p in PASSES:
                data = _read(paths[p], "a judge's return")
                sha = _sha(data)
                inputs[f"{sh['name']}/{judge}/{p}"] = sha
                by_sheet = posted.get(sh["name"])
                by_judge = by_sheet.get(judge) if isinstance(by_sheet, dict) else None
                want = by_judge.get(p) if isinstance(by_judge, dict) else None
                if not isinstance(want, str) or want.strip().lower() != sha:
                    bad_hash.append(f"{paths[p]} (sha256 {sha}, posted {want!r})")
                returns[(sh["name"], judge, p)] = data
    if bad_hash:
        raise Refusal("return hash does not match the posted hash; nothing scored: " + "; ".join(bad_hash))

    # 2. Structure, for every return, before any label is read.
    parsed, notes = {}, {}
    for si, sh in enumerate(sheets):
        for judge, paths in sh["return_paths"].items():
            for p in PASSES:
                where = str(paths[p])
                got, note, note_line = parse_return(returns[(sh["name"], judge, p)].decode("utf-8"), where)
                try:
                    _check_structure(sh, sh["reference"][p], got, where)
                except Refusal as exc:
                    raise Refusal(str(exc) + notes_hint(note_line)) from None
                parsed[(si, judge, p)] = got
                notes[(si, judge, p)] = note

    # 3. Labels: every bad cell is collected, then the run refuses.
    bad, labels = [], {}
    for si, sh in enumerate(sheets):
        for judge, paths in sh["return_paths"].items():
            for p, allowed in (("pass1", PASS1_LABELS), ("pass2", PASS2_LABELS)):
                labels[(si, judge, p)] = _labels_of(parsed[(si, judge, p)], allowed, str(paths[p]), bad)
    if bad:
        raise Refusal("labels that are not allowed (the whole run is refused):\n  " + "\n  ".join(bad))
    labels1 = [{j: labels[(si, j, "pass1")] for j in sh["return_paths"]} for si, sh in enumerate(sheets)]
    labels2 = [{j: labels[(si, j, "pass2")] for j in sh["return_paths"]} for si, sh in enumerate(sheets)]

    # Protocol deviations (all items) and flag counts (real items only).
    deviations, flag_counts = {}, {}
    for si, sh in enumerate(sheets):
        deviations[sh["name"]] = {}
        for judge in sh["return_paths"]:
            l1, l2 = labels1[si][judge], labels2[si][judge]
            changes = [{"item": n, "clause": k, "pass1": l1[(n, k)][0], "pass2": l2[(n, k)][0]}
                       for (n, k) in sorted(l1)
                       if l2[(n, k)][0] != l1[(n, k)][0]
                       and not (l1[(n, k)][0] == "NOT" and l2[(n, k)][0] == STATED)]
            deviations[sh["name"]][judge] = {
                "blank_labels": {"pass1": sum(b for _, b in l1.values()), "pass2": sum(b for _, b in l2.values())},
                "pass2_deviations": changes}
            flag_counts[(si, judge)] = {
                p: sum(1 for n in sh["real"] for c in parsed[(si, judge, p)][n] if _is_flag(c["flag"]))
                for p in PASSES}

    rng = random.Random(SIGNED_SEED)
    res = score(sheets, labels1, labels2, flag_counts, rng)
    report = {
        "report_version": 1,
        "preregistration": PREREGISTRATION,
        "planted_controls_note": PLANTED_NOTE,
        "planted_controls": res["planted"],
        "verdict": res["verdict"],
        "measures": res["measures"],
        "marginals": res["marginals"],
        "secondaries": res["secondaries"],
        "protocol_deviations": deviations,
        "flag_placeholders": sorted(FLAG_PLACEHOLDERS),
        "ci_method": CI_METHOD,
        "notes_heading_pattern": NOTES_HEADING_PATTERN,
        "notes": {sh["name"]: {judge: {p: notes[(si, judge, p)] for p in PASSES} for judge in sh["return_paths"]}
                  for si, sh in enumerate(sheets)},
        "inputs": inputs,
        "scorer": {"commit": commit, "tool_version": TOOL_VERSION,
                   "file_sha256": _sha(Path(__file__).read_bytes())},
        "rng": {"seed": SIGNED_SEED, "resamples": RESAMPLES,
                "method": "one random.Random(seed); per collapse (primary, then second), per resample, "
                          "each sheet in config order draws its observations (sorted by node id) with "
                          "replacement from itself; pooled = cell-wise sum under the fixed columns"},
        "sheet_order": names,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    _create_new(out_dir / "report.json", json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    _create_new(out_dir / "report.md", render_md(report, names))
    print(f"scored; verdict: {'met' if res['verdict']['met'] else 'not met'}", file=sys.stderr)
    return 0


def _f(v, nd=4):
    return "undefined" if v is None else f"{v:.{nd}f}"


def _stat_line(blk, label):
    s = blk[label]
    if s["value"] is None:
        return f"{label}: undefined ({s['undefined_reason']})"
    ci = blk.get("ci", {}).get(label)
    tail = ""
    if ci and ci.get("lo") is not None:
        tail = (f"  [95% bootstrap {_f(ci['lo'])} to {_f(ci['hi'])}; "
                f"{ci['undefined_resamples']} of {ci['resamples']} resamples undefined]")
    return f"{label}: {_f(s['value'])}{tail}"


def render_md(rep, names) -> str:
    """Human-readable report. Item numbers only: never a node id."""
    L = ["# S2 scoring report", "", f"Pre-registration: {rep['preregistration']}",
         f"Scorer commit: {rep['scorer']['commit']}; RNG seed: {rep['rng']['seed']} "
         f"({rep['rng']['resamples']} resamples).", "", "## Planted controls (scored first)", "",
         rep["planted_controls_note"], ""]
    for name in names:
        pc = rep["planted_controls"][name]
        L.append(f"### {name}: {pc['pooled_judges']} pooled judge(s); agreement status: {pc['kappa_status']}")
        for judge, r in pc["judges"].items():
            L.append(f"- {judge} (column {r['column']}): missed P3 {r['missed_p3']}, missed P4 {r['missed_p4']}, "
                     f"misses both {r['misses_both']}; (b) labels P1/P2 NOT {r['p1_p2_labelled_not']}; "
                     f"(c) labels all four NOT {r['labels_all_four_not']}; "
                     f"{'pooled' if r['pooled'] else 'NOT pooled: ' + '; '.join(r['reasons'])}")
        L.append("")
    v = rep["verdict"]
    L += ["## Verdict (measure 5)", "", ("MET. " if v["met"] else "NOT MET. ") + v["text"], ""]
    for cname, title in (("primary", "Primary (measure 1): NOT vs not-NOT"),
                         ("second_collapse", "Second collapse (measure 2): NOT-or-UNCLEAR vs CARRIED")):
        L += [f"## {title}", ""]
        for name in names + ["pooled"]:
            blk = rep["measures"][cname]["pooled"] if name == "pooled" else rep["measures"][cname]["sheets"][name]
            L.append(f"### {name}")
            if blk.get("table") is not None:
                L.append(f"table (rows: column A NOT/not-NOT, columns: column B): {blk['table']}; "
                         f"{blk['n_clauses']} clauses in {blk['n_observations']} observations")
            for st in STAT_NAMES:
                L.append("- " + _stat_line(blk, st))
            if "diagnostic_only" in blk:
                L.append("- diagnostic only (this sheet is not pooled): " + ", ".join(
                    f"{st} {_f(blk['diagnostic_only'][st]['value'])}" for st in STAT_NAMES))
            L.append("")
    L += ["## Marginals", ""]
    for name in names:
        for judge, m in rep["marginals"][name].items():
            L.append(f"- {name} / {judge} (column {m['column']}): three-way {m['three_way']}; "
                     f"collapsed {m['primary_collapse']}; {m['second_collapse']}")
    L += ["", "## Secondaries (no thresholds)", "",
          "### Share of observations with a clause both judges label NOT, per bin", ""]
    for name in names + ["pooled"]:
        cells = ", ".join(f"{b}: {x['observations_with_both_not']}/{x['observations']}"
                          for b, x in rep["secondaries"]["both_not_share_by_bin"][name].items())
        L.append(f"- {name}: {cells}")
    L += ["", "### Spearman: similarity vs NOT counts", ""]
    for name in names + ["pooled"]:
        cells = ", ".join(f"{k} {_f(x['value'])}" for k, x in rep["secondaries"]["spearman"][name].items())
        L.append(f"- {name}: {cells}")
    L += ["", "### Clause-split agreement (stage 1)", ""]
    for name in names:
        L.append(f"- {name}: {json.dumps(rep['secondaries']['clause_split_agreement'][name], sort_keys=True)}")
    L += ["", "### Per judge", ""]
    for name in names:
        for judge, x in rep["secondaries"]["per_judge"][name].items():
            L.append(f"- {name} / {judge}: pass-2 rate {_f(x['pass2_rate']['value'])} "
                     f"({x['pass2_rate']['numerator']}/{x['pass2_rate']['denominator']}); "
                     f"UNCLEAR rate {_f(x['unclear_rate']['value'])}; flags {x['flag_count']}; "
                     f"notes: " + ", ".join(
                         f"{p} {'yes' if rep['notes'][name][judge][p] is not None else 'no'}" for p in PASSES))
    L += ["", "## Protocol deviations", ""]
    for name in names:
        for judge, d in rep["protocol_deviations"][name].items():
            ch = "; ".join(f"item {c['item']} clause {c['clause']}: {c['pass1']} -> {c['pass2']}"
                           for c in d["pass2_deviations"]) or "none"
            L.append(f"- {name} / {judge}: blank labels {d['blank_labels']}; pass-2 changes other than the "
                     f"allowed one: {ch}")
    L += ["", "## Inputs (sha256)", ""]
    L += [f"- {k}: {h}" for k, h in rep["inputs"].items()]
    return "\n".join(L) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--hashes", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--scorer-commit", default=None)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return run(args)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
