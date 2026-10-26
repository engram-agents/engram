#!/usr/bin/env python3
"""s2_clause_check -- phase 1 of the S2 next step (engram-alpha#4201, part of #4050):
the clause-check harness, the baselines B0/B1/B2 and the deterministic check M1.

Design contract: issue #4201 (body, addendum comment 6417250456, and the two reads
that precede it). This tool only reads local files and writes a report. It changes no
write path, no floor, no refusal, and it is not wired into any hook (phase 3 decides).

A check answers one question per clause of a node: does the quote, by itself, state
this clause? Verdicts are STATED or NOT. The ground truth is the S2 consensus of the
two judges' pass-1 labels: NOT if both say NOT, STATED if neither does, BAND if
exactly one does (the column that said NOT is recorded). Kappa and STATED-class
precision/recall are scored on the agreed clauses only; BAND clauses are split, not
scored. Planted controls are excluded exactly as s2_score excludes them (they are
not "real" items).

Usage
    s2_clause_check.py freeze --fold {1,2} --out FREEZE.json --commit SHA [--exclude EXCL.json]
                               [--prior-report FOLD1_REPORT.json]
    s2_clause_check.py run --fold {1,2} --config C --hashes H --freeze FREEZE.json --out-dir D
                           --commit SHA [--exclude EXCL.json]

One freeze per fold: a fold-2 manifest also carries the sha256 of the fold-1 report, so any
change to the check between folds shows in the hashes. ``run --fold N`` evaluates only fold N.
``--exclude`` is a JSON list of test-fold clauses dropped before any statistic; it never
touches a dev fold.

``freeze`` refuses on a dirty tree (this file, engram_anchoring.py or s2_score.py uncommitted) and outside a
git work tree; a fold-2 freeze records whether any pinned field differs from the fold-1 report.
``run`` refuses (exit 2, nothing written) unless the running code and its two dependencies (engram_anchoring,
s2_score), the stopword set,
the stemmer suffixes, the tuning grid, the tie rule and the LINE text all match the manifest,
the manifest's fold equals --fold, the exclusion file's sha256 equals the manifest's, and the
commit equals the manifest's. Config and hashes have the s2_score format and every
return's sha256 is checked against its posted hash before any label is read.

Order: ``run`` verifies the freeze before it opens any sheet, return or label (a refused run reads none).
Model pin: B2's embedding model (the seat's config, resolved as quote_claim_similarity resolves it) is in
the manifest; ``run`` refuses if the seat's model differs, and the report carries it.

Bound: ``engram_core`` (imported by engram_anchoring) is not hashed by the freeze; the frozen hashes
cover this tool, engram_anchoring.py and s2_score.py only.

Every number a report carries comes from the real S2 clauses; the synthetic fixtures
in the tests exercise code paths only. Stdlib + engram imports only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import engram_anchoring  # noqa: E402
from engram_anchoring import _hard_tokens_with_class, anchor, quote_claim_similarity  # noqa: E402

# s2_score is reused by import, not copied (loaded by path: the `tools` package name is contested,
# as in s2_clause_sheet). Its private helpers (_load_sheet_inputs, _check_structure, _labels_of,
# _ci_block, _create_new, _scorer_commit, _sha, _read, _json) are imported on purpose.
_spec = importlib.util.spec_from_file_location("_s2_score_for_clause_check", _HERE / "s2_score.py")
_s2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s2)

Refusal = _s2.Refusal

TOOL_VERSION = "1"
STATED, NOT, UNAVAILABLE = "STATED", "NOT", "unavailable"
HINT = "widen the quote, or move the fact to the interpretation"
MASK_TOKEN = "[id]"

# The line the check encodes, as frozen text (design-owner ruling, #4201). Two parts: the verbatim
# sentence from its source, and the listed additions. Its sha256 is in the freeze manifest.
LINE = {
    "sentence": ("CARRIED only where the quoted text itself states every substantive element of the clause. "
                 "Where a clause added a relation, a date, a count or a cause the quote does not contain, "
                 "I labelled NOT even when the quote states the neighbouring half."),
    "source": "#4050 comment 6397593890",
    "additions": [{"element": "actor", "attribution": "Aleph, #4201"}],
}

# The fixed stopword set for content-word extraction. Hashed into the freeze manifest.
STOPWORDS = frozenset("""
about above after again against all also and any are because been before being below between both but
can could did does doing during each from further had has have her here him his how into its just may
might more most must nor not off once only onto other our out over own per same shall she should some
such than that the their them then there these they this those through too under until upon very via
was were what when where which while who whom whose why will with within without would yet you one
""".split())

# The stemmer: suffix rules in this order, first match wins; a rule matches only when the remainder is
# >= 3 chars; "es" also needs s, x, z, ch or sh just before it; "s" also needs the remainder not to end
# in s, u or i (so process, status, bus, analysis keep their final s).
STEMMER_SUFFIXES = ("ing", "ed", "es", "s")
_ES_AFTER = ("s", "x", "z", "ch", "sh")
_S_NOT_AFTER = ("s", "u", "i")
# Round 4: a plural ending ses/zes/ces/ges drops only its final s (cases -> case, sizes -> size), tried
# before the 'es' rule, unless the word minus 'es' ends in ss, zz, us or is (classes, processes, buzzes,
# buses, statuses keep the 'es' rule). Names in the hashed code, not in STEMMER_SUFFIXES.
_S_AFTER_E = ("ses", "zes", "ces", "ges")
_S_AFTER_E_NOT = ("ss", "zz", "us", "is")
MIN_STEM = 3
MIN_WORD = 3

GRID_START, GRID_STOP, GRID_STEP = 0.0, 1.0, 0.01
TIE_RULE = "smallest"

_WORD_RE = re.compile(r"[^\W\d_]+")
_DATE_CLASSES = frozenset({"date", "time"})
_REF_CLASSES = frozenset({"node_id", "issue_ref", "hex_sha"})


# ---------------------------------------------------------------------------
# M1: deterministic containment
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """NFKC, casefold, drop markdown backticks and asterisks, collapse whitespace."""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = t.replace("`", "").replace("*", "")
    return " ".join(t.split())


def stem(word: str) -> str:
    """The tiny fixed stemmer: the first of the rules ing, ed, es, s that applies (see STEMMER_SUFFIXES),
    with the round-4 -ses/-zes/-ces/-ges rule (see _S_AFTER_E) tried before 'es'; then (round 5) one final
    step: a result ending in 'e' drops it when at least MIN_STEM chars remain (merge/merged -> merg,
    cache/caches -> cach, house/houses -> hous; 'use' stays 'use').

    Known residuals (a clause word and a quote word that differ this way do not match): lens/lenses
    (lens -> len, lenses -> lens: the 's' rule strips lens' own s), and use/used/using (use and uses
    meet at 'use', but 'used' and 'using' leave under 3 chars so stay whole)."""
    stemmed = _strip_suffix(word)
    if stemmed.endswith("e") and len(stemmed) - 1 >= MIN_STEM:
        return stemmed[:-1]
    return stemmed


def _strip_suffix(word: str) -> str:
    for suf in STEMMER_SUFFIXES:
        if (suf == "es" and word.endswith(_S_AFTER_E) and len(word) - 1 >= MIN_STEM
                and not word[:-2].endswith(_S_AFTER_E_NOT)):
            return word[:-1]
        if not word.endswith(suf) or len(word) - len(suf) < MIN_STEM:
            continue
        base = word[: -len(suf)]
        if suf == "es" and not base.endswith(_ES_AFTER):
            continue
        if suf == "s" and base.endswith(_S_NOT_AFTER):
            continue
        return base
    return word


def _content_stems(norm_text: str) -> list[str]:
    """Stems of the content words (alphabetic, >= 3 chars, not a stopword), in order, de-duplicated."""
    out: list[str] = []
    for w in _WORD_RE.findall(norm_text):
        if len(w) >= MIN_WORD and w not in STOPWORDS:
            s = stem(w)
            if s not in out:
                out.append(s)
    return out


def _capitalized_stems(clause: str) -> set[str]:
    """Stems of the clause's content words that are Capitalized in the original text. Token position 0
    is capitalized by sentence convention, so it counts only if the same word is also capitalized at
    a later position in the clause."""
    raw = unicodedata.normalize("NFKC", clause or "").replace("`", "").replace("*", "")
    words = _WORD_RE.findall(raw)
    out = set()
    for i, w in enumerate(words):
        lw = w.casefold()
        if w[0].isupper() and len(lw) >= MIN_WORD and lw not in STOPWORDS:
            if i == 0 and not any(u[0].isupper() and u.casefold() == lw for u in words[1:]):
                continue
            out.add(stem(lw))
    return out


def _clause_hard_tokens(clause: str) -> list[tuple[str, str]]:
    """Hard tokens of the clause with class tags. A bare number that sits inside another hard token
    (the 123 of #123, the 2026 of a date) is part of that token, not a separate count."""
    toks = _hard_tokens_with_class(clause)
    return [(t, c) for t, c in toks
            if not (c == "number" and any(t != u and t in u for u, cu in toks if cu != "number"))]


_WORD_CHARS = "0-9A-Za-z_"


def _token_present(norm_quote: str, norm_token: str) -> bool:
    """True iff the normalized token occurs in the normalized quote bounded on both sides by the
    start/end or a character that is not [0-9A-Za-z_]. A leading '#' belongs to the token and must
    match literally; so 404 is not in 4040 and #123 is not in #1234. (A trailing '%' would also match
    literally, so 3.5% would not be in 13.5%; that carve-out is defensive: the current tokenizer emits
    no '%' token, so M1 never sees one.)

    Numeric boundaries (round 4): for a token that starts with a digit, a '.' or ',' that touches a
    digit is part of a number, not a boundary. A '.' or ',' before the token that is itself preceded by
    a digit, or after the token and followed by a digit, rejects the match: 404 is not in 4.404, 404.5
    or 5,404, but it is in 'error 404.' and '404, then'."""
    pat = rf"(?<![{_WORD_CHARS}]){re.escape(norm_token)}(?![{_WORD_CHARS}])"
    if norm_token[:1].isdigit():
        pat = rf"(?<![0-9][.,]){pat}(?![.,][0-9])"
    return re.search(pat, norm_quote) is not None


def check_m1(quote: str, clause: str, theta: float) -> dict:
    """STATED iff (a) every hard token of the clause occurs in the normalized quote, matched on token
    boundaries (see _token_present: 404 is not in 4040) and (b) the share
    of the clause's content-word stems found among the quote's stems is >= theta (a clause with no
    content word passes (b)). Returns {"verdict", "missing", "missing_type"}.

    missing_type, by first matching rule over what is absent: a date/time token -> "date"; a number ->
    "count"; an issue ref, hex sha or node-id-shaped token -> "ref"; a content word that was
    Capitalized in the original clause -> "actor"; anything else -> "other". "relation" is reserved
    for phase 2 (the model check) and is never emitted by M1. For STATED it is None.

    The line this implements is the module constant LINE (frozen text, hashed into the manifest):
    a clause is STATED only where the quote itself carries every substantive element of it, and
    "actor" is the one listed addition to the sentence it quotes."""
    nq = normalize(quote)
    missing_hard = [(normalize(t), c) for t, c in _clause_hard_tokens(clause) if not _token_present(nq, normalize(t))]
    clause_stems = _content_stems(normalize(clause))
    quote_stems = set(_content_stems(nq))
    missing_stems = [s for s in clause_stems if s not in quote_stems]
    share_ok = (not clause_stems) or (len(clause_stems) - len(missing_stems)) / len(clause_stems) >= theta
    if not missing_hard and share_ok:
        return {"verdict": STATED, "missing": [t for t in missing_stems], "missing_type": None}
    classes = {c for _t, c in missing_hard}
    if classes & _DATE_CLASSES:
        mtype = "date"
    elif "number" in classes:
        mtype = "count"
    elif classes & _REF_CLASSES:
        mtype = "ref"
    elif set(missing_stems) & _capitalized_stems(clause):
        mtype = "actor"
    else:
        mtype = "other"
    return {"verdict": NOT, "missing": [t for t, _c in missing_hard] + missing_stems, "missing_type": mtype}


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------

def check_b0(quote: str, clause: str) -> dict:
    """Always NOT."""
    return {"verdict": NOT}


def check_b1(quote: str, clause: str) -> dict:
    """NOT iff anchor(clause, quote) leaves a hard token unanchored. anchor() matches hard tokens by
    substring: that is the shipped advisory's behaviour, kept as-is here for the baseline."""
    unanchored = anchor(clause, quote).unanchored
    return {"verdict": NOT if unanchored else STATED, "unanchored": unanchored}


def check_b2(quote: str, clause: str, cutoff: float, sim_fn=quote_claim_similarity) -> dict:
    """STATED iff similarity(quote, clause) >= cutoff. ``sim_fn`` returns a float, None, or the real
    function's (similarity, reason) pair. A None similarity is "unavailable": it is recorded and the
    clause is excluded from this method's tables; it is never guessed."""
    res = sim_fn(quote, clause)
    sim = res[0] if isinstance(res, tuple) else res
    if sim is None:
        return {"verdict": UNAVAILABLE, "similarity": None}
    return {"verdict": STATED if sim >= cutoff else NOT, "similarity": sim}


# ---------------------------------------------------------------------------
# Tuning (dev fold only)
# ---------------------------------------------------------------------------

def tuning_grid() -> list[float]:
    return [round(i * GRID_STEP, 2) for i in range(int(round((GRID_STOP - GRID_START) / GRID_STEP)) + 1)]


def _table(pairs):
    """pairs of (method, truth) in STATED/NOT -> (n11, n10, n01, n00), STATED positive."""
    n11 = sum(1 for m, t in pairs if m == STATED and t == STATED)
    n10 = sum(1 for m, t in pairs if m == STATED and t == NOT)
    n01 = sum(1 for m, t in pairs if m == NOT and t == STATED)
    n00 = sum(1 for m, t in pairs if m == NOT and t == NOT)
    return (n11, n10, n01, n00)


def tune(dev_records, predict) -> dict:
    """Pick the grid value (0.00..1.00, step 0.01) maximizing Cohen's kappa against consensus on the
    dev fold's agreed clauses (consensus NOT or STATED; BAND is not tuned on). ``predict(rec, value)``
    returns STATED, NOT or None (unavailable: skipped). Ties go to the smallest value. A value whose
    kappa is undefined never wins; if every value is undefined the result's value is None."""
    best_v, best_k, best_n = None, None, 0
    for v in tuning_grid():
        pairs = []
        for r in dev_records:
            if r["consensus"] not in (NOT, STATED):
                continue
            p = predict(r, v)
            if p is None:
                continue
            pairs.append((p, r["consensus"]))
        k, _why = _s2.cohen_kappa(_table(pairs))
        if k is not None and (best_k is None or k > best_k):
            best_v, best_k, best_n = v, k, len(pairs)
    return {"value": best_v, "kappa": best_k, "n": best_n}


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------

def consensus_of(label_a: str, label_b: str):
    """(consensus, band_column) from the two judges' pass-1 labels."""
    a, b = label_a == "NOT", label_b == "NOT"
    if a and b:
        return NOT, None
    if not a and not b:
        return STATED, None
    return "BAND", ("A" if a else "B")


def make_record(item, k, quote, clause, label_a, label_b) -> dict:
    cons, col = consensus_of(label_a, label_b)
    return {"item": item, "k": k, "quote": quote, "clause": clause, "a": label_a, "b": label_b,
            "consensus": cons, "band_column": col}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _precision(t):
    d = t[0] + t[1]
    return (t[0] / d, None) if d else (None, "undefined: the method called no clause STATED "
                                              "(no clause in the denominator)")


def _recall(t):
    d = t[0] + t[2]
    return (t[0] / d, None) if d else (None, "undefined: no agreed clause has consensus STATED")


def _stat_block(fn, tables, n):
    """Point value on the summed table; 95% percentile interval from a cluster bootstrap over
    observations (items), seeded from the pre-registration seed constant in s2_score."""
    total = (0, 0, 0, 0)
    for t in tables:
        total = _s2._add(total, t)
    value, why = fn(total)
    rng = random.Random(_s2.SIGNED_SEED)
    vals = []
    for _ in range(n):
        per_sheet, _pooled = _s2.resample_tables(rng, [tables])
        vals.append(fn(per_sheet[0])[0])
    return {"value": value, "undefined_reason": why, "ci": _s2._ci_block(vals, value)}


def evaluate_method(records, predict, value, n_resamples: int = _s2.RESAMPLES) -> dict:
    """Score one method on one test sheet's records. ``predict(rec, value)`` -> STATED / NOT / None."""
    scored, unavailable = [], 0
    for r in records:
        p = predict(r, value)
        if p is None:
            unavailable += 1
        else:
            scored.append((r, p))
    items = sorted({r["item"] for r in records})
    agreed_t = {i: [] for i in items}
    a_t = {i: [] for i in items}
    b_t = {i: [] for i in items}
    band = {"NOT": 0, "STATED": 0, "by_column": {"A": {"NOT": 0, "STATED": 0}, "B": {"NOT": 0, "STATED": 0}}}
    for r, p in scored:
        if r["consensus"] in (NOT, STATED):
            agreed_t[r["item"]].append((p, r["consensus"]))
        elif r["consensus"] == "BAND":
            band[p] += 1
            band["by_column"][r["band_column"]][p] += 1
        for tbl, lab in ((a_t, r["a"]), (b_t, r["b"])):
            tbl[r["item"]].append((p, NOT if lab == "NOT" else STATED))

    def per_item(tbl):
        return [_table(tbl[i]) for i in items]

    agreed_tables, a_tables, b_tables = per_item(agreed_t), per_item(a_t), per_item(b_t)
    total = (0, 0, 0, 0)
    for t in agreed_tables:
        total = _s2._add(total, t)
    return {
        "parameter_value": value,
        "n_clauses_scored": len(scored),
        "unavailable": unavailable,
        "n_agreed": sum(total),
        "agreed_table": [[total[0], total[1]], [total[2], total[3]]],
        "kappa": _stat_block(_s2.cohen_kappa, agreed_tables, n_resamples),
        "stated_precision": _stat_block(_precision, agreed_tables, n_resamples),
        "stated_recall": _stat_block(_recall, agreed_tables, n_resamples),
        "band_split": band,
        # pass-1 labels collapsed NOT vs not-NOT, over ALL real clauses (agreed or band)
        "kappa_vs_column_A": _stat_block(_s2.cohen_kappa, a_tables, n_resamples),
        "kappa_vs_column_B": _stat_block(_s2.cohen_kappa, b_tables, n_resamples),
    }


def parse_exclusions(data: bytes, where) -> list[dict]:
    """The exclusion file: {"exclude": [{"sheet": str, "item": int, "clauses": [int, ...] | "all"}]}.
    Refuses anything else (a malformed list must not silently exclude nothing)."""
    obj = _s2._json(data, where)
    lst = obj.get("exclude") if isinstance(obj, dict) else None
    if not isinstance(lst, list):
        raise Refusal(f"{where}: expected an object {{\"exclude\": [...]}}")
    out = []
    for i, e in enumerate(lst):
        ok = (isinstance(e, dict) and set(e) == {"sheet", "item", "clauses"} and isinstance(e["sheet"], str)
              and isinstance(e["item"], int) and not isinstance(e["item"], bool)
              and (e["clauses"] == "all" or (isinstance(e["clauses"], list) and e["clauses"]
                                             and all(isinstance(k, int) and not isinstance(k, bool)
                                                     for k in e["clauses"]))))
        if not ok:
            raise Refusal(f"{where}: entry {i} must be {{\"sheet\": name, \"item\": int, "
                          f"\"clauses\": [int, ...] or \"all\"}}")
        out.append({"sheet": e["sheet"], "item": e["item"], "clauses": e["clauses"]})
    return out


def validate_exclusions(entries, sheets) -> None:
    """Every entry must name a sheet, a real item and real clauses that exist (a typo must not
    silently exclude nothing). Planted items are not real items, so naming one refuses."""
    present = {s["name"]: {(r["item"], r["k"]) for r in s["records"]} for s in sheets}
    for e in entries:
        if e["sheet"] not in present:
            raise Refusal(f"exclusion names a sheet that does not exist: {e['sheet']!r}")
        have = present[e["sheet"]]
        if not any(it == e["item"] for it, _k in have):
            raise Refusal(f"exclusion names item {e['item']} which does not exist on {e['sheet']}")
        if e["clauses"] != "all":
            for k in e["clauses"]:
                if (e["item"], k) not in have:
                    raise Refusal(f"exclusion names clause {k} of item {e['item']} which does not exist "
                                  f"on {e['sheet']}")


def apply_exclusions(test_sheet, entries):
    """(records, per-entry report). Removes clauses from the TEST sheet only: an entry naming any
    other sheet removes nothing here (a dev fold is never touched by this list)."""
    records, rows = list(test_sheet["records"]), []
    for e in entries:
        before = len(records)
        if e["sheet"] == test_sheet["name"]:
            records = [r for r in records
                       if not (r["item"] == e["item"] and (e["clauses"] == "all" or r["k"] in e["clauses"]))]
        rows.append({"sheet": e["sheet"], "item": e["item"], "clauses": e["clauses"],
                     "removed": before - len(records)})
    return records, rows


def mask_counts(sheets) -> dict:
    """Per sheet: clauses and quotes that contain the mask token (the quotes come from the masked
    sheet the judges saw, so a token in a quote is visible here rather than silent)."""
    out = {}
    for s in sheets:
        quotes = {r["item"]: r["quote"] for r in s["records"]}
        out[s["name"]] = {"clauses": len(s["records"]),
                          "clauses_with_token": sum(1 for r in s["records"] if MASK_TOKEN in r["clause"]),
                          "quotes": len(quotes),
                          "quotes_with_token": sum(1 for q in quotes.values() if MASK_TOKEN in q)}
    return {"token": MASK_TOKEN, "sheets": out}


def build_report(sheets, sim_fn=None, resamples: int = _s2.RESAMPLES, fold=None, exclusions=None,
                 exclusion_sha256=None) -> dict:
    """Two folds: tune on sheet[0] and test on sheet[1], then swapped. ``sheets`` is a list of two
    {"name", "records"}. Tuning sees only the dev sheet's records. ``fold`` (1 or 2) evaluates only
    that fold (None: both). ``exclusions`` (parse_exclusions output) drop clauses from the TEST
    sheet only, before any statistic."""
    if len(sheets) != 2:
        raise Refusal("the design has exactly two sheets (two folds)")
    exclusions = exclusions or []
    validate_exclusions(exclusions, sheets)
    sim = sim_fn if sim_fn is not None else engram_anchoring.quote_claim_similarity
    memo: dict = {}

    def cached_sim(q, c):
        key = (q, c)
        if key not in memo:
            memo[key] = sim(q, c)
        return memo[key]

    def p_b0(r, v):
        return check_b0(r["quote"], r["clause"])["verdict"]

    def p_b1(r, v):
        return check_b1(r["quote"], r["clause"])["verdict"]

    def p_b2(r, v):
        out = check_b2(r["quote"], r["clause"], v, sim_fn=cached_sim)["verdict"]
        return None if out == UNAVAILABLE else out

    def p_m1(r, v):
        return check_m1(r["quote"], r["clause"], v)["verdict"]

    folds = []
    for fold_no, (di, ti) in enumerate(((0, 1), (1, 0)), 1):
        if fold is not None and fold_no != fold:
            continue
        dev = sheets[di]
        test_records, ex_rows = apply_exclusions(sheets[ti], exclusions)
        test = {"name": sheets[ti]["name"], "records": test_records}
        tuned = {"M1": dict(tune(dev["records"], p_m1), parameter="theta"),
                 "B2": dict(tune(dev["records"], p_b2), parameter="cutoff")}
        methods = {}
        for name, fn, val in (("B0", p_b0, None), ("B1", p_b1, None),
                              ("B2", p_b2, tuned["B2"]["value"]), ("M1", p_m1, tuned["M1"]["value"])):
            if name in tuned and val is None:
                methods[name] = {"undefined_reason": "undefined: no tuning value had a defined kappa on the dev sheet"}
                continue
            methods[name] = evaluate_method(test["records"], fn, val, resamples)
        counts = {c: sum(1 for r in test["records"] if r["consensus"] == c) for c in (NOT, STATED, "BAND")}
        folds.append({"fold": fold_no, "tune_sheet": dev["name"], "test_sheet": test["name"], "tuned": tuned,
                      "exclusion": {"sha256": exclusion_sha256, "entries": ex_rows,
                                    "removed_total": sum(e["removed"] for e in ex_rows)},
                      "test": {"consensus_counts": counts, "methods": methods}})
    return {"report_version": 1, "tool_version": TOOL_VERSION, "fold": fold,
            "seed": _s2.SIGNED_SEED, "resamples": resamples, "ci_method": _s2.CI_METHOD,
            "sheet_order": [s["name"] for s in sheets], "mask_counts": mask_counts(sheets), "folds": folds}


# ---------------------------------------------------------------------------
# Per-node advisory shape (phase 3 uses it; nothing calls it yet)
# ---------------------------------------------------------------------------

def advise(quote: str, clauses: list[str], theta: float) -> dict:
    """Only the NOT clauses, numbered from 1. No node-level score, no pass/fail field."""
    listed = []
    for i, c in enumerate(clauses, 1):
        r = check_m1(quote, c, theta)
        if r["verdict"] == NOT:
            listed.append({"n": i, "text": c, "missing_type": r["missing_type"]})
    return {"not_stated": len(listed), "of": len(clauses), "clauses": listed, "hint": HINT}


# ---------------------------------------------------------------------------
# Reading the S2 inputs
# ---------------------------------------------------------------------------

def _quotes_of(sheet_text: str) -> dict:
    """{item number: quote text} from a rendered pass-1 sheet. s2_score's parser does not read the
    quote blocks, so this reads them (the sheet's hash was already verified by _load_sheet_inputs)."""
    quotes, cur, in_quote = {}, None, False
    for raw in sheet_text.splitlines():
        s = raw.strip()
        m = _s2.ITEM_RE.match(s)
        if m:
            cur, in_quote = int(m.group(1)), False
            continue
        if cur is None:
            continue
        if s.startswith("**Quote"):
            in_quote = True
            quotes[cur] = []
            continue
        if in_quote and s.startswith(">"):
            quotes[cur].append(s[1:].lstrip(" ") if s != ">" else "")
            continue
        in_quote = False
    return {n: "\n".join(lines) for n, lines in quotes.items() if lines}


def load_study(config_path, hashes_path, inputs=None) -> list[dict]:
    """Read two sheets through s2_score's loaders. Every return's sha256 is checked against its
    posted hash before any label is read. Returns [{"name", "rater_A", "rater_B", "records"}] where a
    record is one REAL clause (planted items are not among the sheet's "real" items)."""
    inputs = {} if inputs is None else inputs
    cpath, hpath = Path(config_path), Path(hashes_path)
    cdata, hdata = _s2._read(cpath, "the config"), _s2._read(hpath, "the posted hashes")
    inputs["config"], inputs["hashes"] = _s2._sha(cdata), _s2._sha(hdata)
    config, posted = _s2._json(cdata, cpath), _s2._json(hdata, hpath)
    if (not isinstance(config, dict) or not isinstance(config.get("sheets"), list)
            or len(config["sheets"]) != 2):
        raise Refusal("config: \"sheets\" must be a list of exactly two sheets (the signed design)")
    if not isinstance(posted, dict):
        raise Refusal("hashes.json must be an object {sheet: {judge: {pass1, pass2}}}")
    base = cpath.resolve().parent
    sheets = [_s2._load_sheet_inputs(c, base, inputs) for c in config["sheets"]]
    if len({sh["name"] for sh in sheets}) != 2:
        raise Refusal("config: the two sheets need different names")
    if sheets[0]["A"] != sheets[1]["A"]:
        raise Refusal("config: rater_A differs across sheets (column A is one judge on both sheets)")

    # Hash-first, exactly as s2_score.run does (that check is inline in run(), so it is restated here).
    # Mirrors s2_score.run() lines 821-835 (returns/bad_hash loop and its Refusal); a change there must be made here too.
    returns, bad_hash = {}, []
    for sh in sheets:
        for judge, paths in sh["return_paths"].items():
            for p in _s2.PASSES:
                data = _s2._read(paths[p], "a judge's return")
                sha = _s2._sha(data)
                inputs[f"{sh['name']}/{judge}/{p}"] = sha
                by_sheet = posted.get(sh["name"])
                by_judge = by_sheet.get(judge) if isinstance(by_sheet, dict) else None
                want = by_judge.get(p) if isinstance(by_judge, dict) else None
                if not isinstance(want, str) or want.strip().lower() != sha:
                    bad_hash.append(f"{paths[p]} (sha256 {sha}, posted {want!r})")
                returns[(sh["name"], judge, p)] = data
    if bad_hash:
        raise Refusal("return hash does not match the posted hash; nothing scored: " + "; ".join(bad_hash))

    out = []
    for si, sh in enumerate(sheets):
        qpath = Path(config["sheets"][si]["sheet_pass1"])
        qpath = qpath if qpath.is_absolute() else base / qpath
        quotes = _quotes_of(qpath.read_text(encoding="utf-8"))
        labels = {}
        for role in ("A", "B"):
            judge = sh[role]
            where = str(sh["return_paths"][judge]["pass1"])
            got, _notes, note_line = _s2.parse_return(returns[(sh["name"], judge, "pass1")].decode("utf-8"), where)
            try:
                _s2._check_structure(sh, sh["reference"]["pass1"], got, where)
            except Refusal as exc:
                raise Refusal(str(exc) + _s2.notes_hint(note_line)) from None
            bad: list[str] = []
            labels[role] = _s2._labels_of(got, _s2.PASS1_LABELS, where, bad)
            if bad:
                raise Refusal("labels that are not allowed (the whole run is refused):\n  " + "\n  ".join(bad))
        records = []
        for n in sh["real"]:
            if n not in quotes:
                raise Refusal(f"sheet {sh['name']!r}: item {n} has no quote (no '> ' lines under **Quote:**) in "
                              f"{qpath}; refusing rather than treating it as an empty quote")
            for k, c in enumerate(sh["reference"]["pass1"][n], 1):
                records.append(make_record(n, k, quotes[n], c["text"],
                                           labels["A"][(n, k)][0], labels["B"][(n, k)][0]))
        out.append({"name": sh["name"], "rater_A": sh["A"], "rater_B": sh["B"], "records": records})
    return out


# ---------------------------------------------------------------------------
# Freeze
# ---------------------------------------------------------------------------

def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _embedding_model() -> str:
    """The embedding model B2 would use on this seat, resolved exactly as quote_claim_similarity
    resolves it (one helper, so freeze and run cannot diverge)."""
    core = engram_anchoring.core
    return core._get_embedding_config().get("model", core.DEFAULT_EMBEDDING_MODEL)


def _freeze_fields() -> dict:
    """What the manifest pins, recomputed from the running code. The two dependencies are hashed from
    the imported modules' __file__, so the bytes hashed are the code actually running."""
    return {
        "embedding_model": _embedding_model(),
        "code_sha256": _file_sha256(__file__),
        "engram_anchoring_sha256": _file_sha256(engram_anchoring.__file__),
        "s2_score_sha256": _file_sha256(_s2.__file__),
        "stopwords_sha256": _sha_text("\n".join(sorted(STOPWORDS))),
        "stemmer_suffixes": list(STEMMER_SUFFIXES),
        "tuning_grid": {"start": GRID_START, "stop": GRID_STOP, "step": GRID_STEP},
        "tie_rule": TIE_RULE,
        "line_sha256": _sha_text(json.dumps(LINE, sort_keys=True, separators=(",", ":"), ensure_ascii=False)),
    }


def make_manifest(commit: str, fold: int, exclusion_sha256=None, prior_report_sha256=None, changed=None) -> dict:
    m = {"manifest_version": 2, "tool_version": TOOL_VERSION, **_freeze_fields(), "fold": fold,
         "exclusion_sha256": exclusion_sha256, "engram_alpha_commit": commit}
    if fold == 2:
        m["prior_report_sha256"] = prior_report_sha256
        if changed is not None:
            m["code_changed_since_fold1"], m["changed_fields_since_fold1"] = changed
    return m


def verify_freeze(manifest, commit: str, fold: int, exclusion_sha256=None) -> None:
    if not isinstance(manifest, dict):
        raise Refusal("the freeze manifest must be a JSON object")
    now = _freeze_fields()
    bad = [k for k in now if manifest.get(k) != now[k]]
    if bad:
        raise Refusal("the running code does not match the freeze manifest in: " + ", ".join(bad))
    if manifest.get("fold") != fold:
        raise Refusal(f"--fold {fold} but the manifest froze fold {manifest.get('fold')!r}")
    if manifest.get("exclusion_sha256") != exclusion_sha256:
        raise Refusal(f"the exclusion file's sha256 ({exclusion_sha256!r}) is not the manifest's "
                      f"({manifest.get('exclusion_sha256')!r})")
    if fold == 2 and not (isinstance(manifest.get("prior_report_sha256"), str) and manifest["prior_report_sha256"]):
        raise Refusal("a fold-2 manifest must record the sha256 of the fold-1 report; this one has none")
    if fold == 2 and not (isinstance(manifest.get("code_changed_since_fold1"), bool)
                          and isinstance(manifest.get("changed_fields_since_fold1"), list)):
        raise Refusal("a fold-2 manifest must record code_changed_since_fold1 and the differing fields; "
                      "this one does not")
    if manifest.get("engram_alpha_commit") != commit:
        raise Refusal(f"commit {commit!r} is not the manifest's commit {manifest.get('engram_alpha_commit')!r}")


def _exclusion_input(path):
    """(entries, sha256) of the exclusion file, or ([], None) when none is given."""
    if not path:
        return [], None
    p = Path(path)
    data = _s2._read(p, "the exclusion file")
    return parse_exclusions(data, p), _s2._sha(data)


def _require_clean_tree(paths) -> None:
    """Refuse unless the repo containing the tool is a git work tree in which ``paths`` (the tool and
    its two dependencies) have no uncommitted change: a dirty tree cannot be frozen."""
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=Path(paths[0]).resolve().parent,
                         capture_output=True, text=True)
    if top.returncode != 0:
        raise Refusal(f"{paths[0]} is not inside a git work tree; refusing to freeze code that cannot be "
                      f"checked for uncommitted changes")
    st = subprocess.run(["git", "status", "--porcelain", "--"] + [str(Path(p).resolve()) for p in paths],
                        cwd=top.stdout.strip(), capture_output=True, text=True)
    if st.returncode != 0:
        raise Refusal(f"git status failed for the frozen files: {st.stderr.strip()}")
    if st.stdout.strip():
        raise Refusal("a dirty tree cannot be frozen; uncommitted changes in:\n" + st.stdout.rstrip())


def _compare_with_prior(prior: dict, where) -> tuple:
    """(code_changed_since_fold1, differing field names) between the fold-1 report's recorded hash
    fields and the running code. Refuses if the report does not carry them."""
    rec = prior.get("freeze")
    now = _freeze_fields()
    if not isinstance(rec, dict) or any(k not in rec for k in now):
        raise Refusal(f"{where}: the fold-1 report does not carry the manifest hash fields "
                      f"({', '.join(now)}); cannot compare the code across folds")
    diff = [k for k in now if rec[k] != now[k]]
    return bool(diff), diff


def cmd_freeze(args) -> int:
    commit = _s2._scorer_commit(args.commit)
    _require_clean_tree([__file__, engram_anchoring.__file__, _s2.__file__])
    _entries, excl_sha = _exclusion_input(args.exclude)
    prior_sha, changed = None, None
    if args.fold == 2:
        if not args.prior_report:
            raise Refusal("a fold-2 freeze needs --prior-report (the fold-1 report.json)")
        ppath = Path(args.prior_report)
        pdata = _s2._read(ppath, "the prior report")
        prior = _s2._json(pdata, ppath)
        if not isinstance(prior, dict) or prior.get("fold") != 1:
            raise Refusal(f"{ppath} is not a fold-1 report (its \"fold\" is not 1)")
        changed = _compare_with_prior(prior, ppath)
        prior_sha = _s2._sha(pdata)
    elif args.prior_report:
        raise Refusal("--prior-report belongs to a fold-2 freeze only")
    _s2._create_new(Path(args.out), json.dumps(make_manifest(commit, args.fold, excl_sha, prior_sha, changed), indent=2) + "\n")
    print(f"froze fold {args.fold}: {args.out}", file=sys.stderr)
    return 0


def cmd_run(args) -> int:
    commit = _s2._scorer_commit(args.commit)
    fpath = Path(args.freeze)
    manifest = _s2._json(_s2._read(fpath, "the freeze manifest"), fpath)
    entries, excl_sha = _exclusion_input(args.exclude)
    verify_freeze(manifest, commit, args.fold, excl_sha)
    out_dir = Path(args.out_dir)
    for f in ("report.json", "report.md"):
        if os.path.lexists(out_dir / f):
            raise Refusal(f"{out_dir / f} already exists; refusing to overwrite")
    inputs: dict = {}
    sheets = load_study(args.config, args.hashes, inputs)
    report = build_report(sheets, fold=args.fold, exclusions=entries, exclusion_sha256=excl_sha)
    report["engram_alpha_commit"] = commit
    report["embedding_model"] = _embedding_model()
    report["freeze"] = manifest
    if args.fold == 2:
        report["code_changed_since_fold1"] = manifest["code_changed_since_fold1"]
        report["changed_fields_since_fold1"] = manifest["changed_fields_since_fold1"]
    report["freeze_sha256"] = _s2._sha(_s2._read(fpath, "the freeze manifest"))
    report["inputs"] = inputs
    report["columns"] = {"A": sheets[0]["rater_A"], "B": {s["name"]: s["rater_B"] for s in sheets}}
    report["note"] = ("Every number comes from the real S2 clauses; B2 clauses whose similarity was "
                      "unavailable are excluded from that method's tables and counted, never guessed.")
    text_json = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    text_md = render_md(report)
    out_dir.mkdir(parents=True, exist_ok=True)
    _s2._create_new(out_dir / "report.json", text_json)
    _s2._create_new(out_dir / "report.md", text_md)
    print("report written", file=sys.stderr)
    return 0


def _f(v):
    return "null" if v is None else f"{v:.4f}"


def _line(label, blk):
    ci = blk["ci"]
    tail = ""
    if ci.get("lo") is not None:
        tail = f" [95% bootstrap {_f(ci['lo'])} to {_f(ci['hi'])}; {ci['undefined_resamples']} of {ci['resamples']} undefined]"
    why = f" ({blk['undefined_reason']})" if blk["value"] is None else ""
    return f"- {label}: {_f(blk['value'])}{why}{tail}"


def render_md(rep) -> str:
    """Item numbers only: never a node id."""
    L = ["# S2 clause-check report (phase 1)", ""]
    if "code_changed_since_fold1" in rep:
        L += [f"Code changed since fold 1: {str(rep['code_changed_since_fold1']).lower()}; "
              f"differing fields: {rep['changed_fields_since_fold1']}", ""]
    L += [
         f"engram-alpha commit: {rep.get('engram_alpha_commit')}; freeze sha256: {rep.get('freeze_sha256')}; "
         f"embedding model: {rep.get('embedding_model')}; "
         f"seed {rep['seed']}, {rep['resamples']} resamples; fold {rep.get('fold')}.", "",
         f"Mask token {rep['mask_counts']['token']} (read from the masked sheet the judges saw), per sheet: "
         f"{rep['mask_counts']['sheets']}", ""]
    for f in rep["folds"]:
        ex = f["exclusion"]
        L += [f"## Fold {f['fold']}: tuned on {f['tune_sheet']}, tested on {f['test_sheet']}", "",
              f"Exclusions (test sheet only): file sha256 {ex['sha256']}; removed {ex['removed_total']}; "
              f"entries {ex['entries']}",
              "Tuned: " + ", ".join(f"{k} {v['parameter']}={v['value']} (dev kappa {_f(v['kappa'])}, n {v['n']})"
                                    for k, v in f["tuned"].items()),
              f"Consensus on the test sheet: {f['test']['consensus_counts']}", ""]
        for name, m in f["test"]["methods"].items():
            L.append(f"### {name}")
            if "undefined_reason" in m:
                L += [f"- {m['undefined_reason']}", ""]
                continue
            L += [f"- parameter: {m['parameter_value']}; scored {m['n_clauses_scored']}, "
                  f"unavailable (excluded) {m['unavailable']}; agreed table {m['agreed_table']}",
                  _line("kappa vs consensus", m["kappa"]),
                  _line("STATED precision", m["stated_precision"]),
                  _line("STATED recall", m["stated_recall"]),
                  f"- band split: {m['band_split']}",
                  _line("kappa vs column A alone", m["kappa_vs_column_A"]),
                  _line("kappa vs column B alone", m["kappa_vs_column_B"]), ""]
    L += ["## Inputs (sha256)", ""] + [f"- {k}: {h}" for k, h in rep["inputs"].items()]
    return "\n".join(L) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    fr = sub.add_parser("freeze")
    fr.add_argument("--fold", type=int, choices=(1, 2), required=True)
    fr.add_argument("--out", required=True)
    fr.add_argument("--commit", default=None)
    fr.add_argument("--exclude", default=None)
    fr.add_argument("--prior-report", default=None)
    ru = sub.add_parser("run")
    ru.add_argument("--fold", type=int, choices=(1, 2), required=True)
    ru.add_argument("--exclude", default=None)
    ru.add_argument("--config", required=True)
    ru.add_argument("--hashes", required=True)
    ru.add_argument("--freeze", required=True)
    ru.add_argument("--out-dir", required=True)
    ru.add_argument("--commit", default=None)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "freeze":
            return cmd_freeze(args)
        return cmd_run(args)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
