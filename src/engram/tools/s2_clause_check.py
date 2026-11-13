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

Phase 3a (#4201, ruling 6424929510): ``split_claim`` is a deterministic clause splitter (no model). Two commands
measure it, and ``advise`` can use it:
    s2_clause_check.py split-eval --config C --bar '{"<sheet>": rate, ...}' --out-dir D
        exact clause-count agreement of split_claim(claim) with the frozen clauses (the gated number, against
        the per-sheet --bar declared at the call), the same on the stage-1 first ten, and the per-pair Jaccard
        boundary check on count-matched items. It reads no judge's return and no label.
    s2_clause_check.py freeze ... --clauses split --config C   /   run ... --clauses split
        end to end: M1 and the baselines re-run on the splitter's clauses, each inheriting the label of the
        frozen clause with the highest Jaccard (ties NOT; zero overlap NONE and excluded), the frozen-split
        numbers printed beside. Each sheet's "claims" path (a claims.json, [{"node_id", "claim"}]) is in the
        config; the manifest pins clauses_mode, splitter_version and the claims files' sha256. ``--clauses
        frozen`` is the default and is what phase 1 did.

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
import heapq
import importlib.util
import json
import math
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
from engram_anchoring import _HEX_SHA_CLASS, _hard_tokens_with_class, quote_claim_similarity  # noqa: E402

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
    """NOT iff the historical substring check leaves a hard token unanchored. This baseline is the
    shipped advisory's pre-#4206 behaviour -- ``tok in quote``, case-insensitive for the hex-sha
    class only -- implemented here rather than borrowed from engram_anchoring.anchor(): that
    matcher became token-boundary-aware in #4206 and may move again, and a baseline must not move
    with the thing it is a baseline of."""
    quote_lower = quote.lower()
    unanchored = [
        tok
        for tok, cls in _hard_tokens_with_class(clause)
        if not (tok.lower() in quote_lower if cls == _HEX_SHA_CLASS else tok in quote)
    ]
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
                 exclusion_sha256=None, eval_sheets=None, fixed_theta=None) -> dict:
    """Two folds: tune on sheet[0] and test on sheet[1], then swapped. ``sheets`` is a list of two
    {"name", "records"}. Tuning sees only the dev sheet's records. ``fold`` (1 or 2) evaluates only
    that fold (None: both). ``exclusions`` (parse_exclusions output) drop clauses from the TEST
    sheet only, before any statistic. ``eval_sheets`` (end-to-end mode: split_sheets output) are the sheets
    that are tuned on and scored in place of ``sheets``; exclusions are still validated against the frozen
    ``sheets`` and apply to the splitter's clauses by mapping (apply_exclusions_mapped).

    End-to-end mode (``eval_sheets``) needs ``fixed_theta`` and reports two columns in one pass. The GATED
    column evaluates M1 at ``fixed_theta`` (the value that ships) and never tunes it; B2, a baseline that is
    not shipped, keeps its cutoff tuned on the dev fold's split clauses. The METHOD CHECK column (not gated;
    ``method_check_folds``) re-tunes M1's theta on each dev fold's split clauses, as the frozen pass does, and
    evaluates only M1: how far that value lands from ``fixed_theta`` is a finding about the splitter's grain."""
    if len(sheets) != 2:
        raise Refusal("the design has exactly two sheets (two folds)")
    if (eval_sheets is None) != (fixed_theta is None):
        raise ValueError("fixed_theta goes with eval_sheets (end-to-end mode) and with nothing else")
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

    folds, method_check = [], []
    for fold_no, (di, ti) in enumerate(((0, 1), (1, 0)), 1):
        if fold is not None and fold_no != fold:
            continue
        if eval_sheets is None:
            dev = sheets[di]
            test_records, ex_rows = apply_exclusions(sheets[ti], exclusions)
            test = {"name": sheets[ti]["name"], "records": test_records}
        else:
            dev = eval_sheets[di]
            test_records, ex_rows = apply_exclusions_mapped(eval_sheets[ti], exclusions)
            test = {"name": eval_sheets[ti]["name"], "records": test_records}
        if eval_sheets is None:
            tuned = {"M1": dict(tune(dev["records"], p_m1), parameter="theta"),
                     "B2": dict(tune(dev["records"], p_b2), parameter="cutoff")}
        else:       # gated column: M1 is not tuned, it is evaluated at the shipped value
            tuned = {"M1": {"value": fixed_theta, "kappa": None, "n": None, "parameter": "theta", "fixed": True},
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
        if eval_sheets is not None:      # the method check: re-tune M1 on this dev fold, evaluate it, gate nothing
            retuned = dict(tune(dev["records"], p_m1), parameter="theta")
            mc = ({"undefined_reason": "undefined: no tuning value had a defined kappa on the dev sheet"}
                  if retuned["value"] is None else evaluate_method(test["records"], p_m1, retuned["value"], resamples))
            method_check.append({"fold": fold_no, "tune_sheet": dev["name"], "test_sheet": test["name"],
                                 "tuned": {"M1": retuned},
                                 "test": {"consensus_counts": counts, "methods": {"M1": mc}}})
    out = {"report_version": 1, "tool_version": TOOL_VERSION, "fold": fold,
           "seed": _s2.SIGNED_SEED, "resamples": resamples, "ci_method": _s2.CI_METHOD,
           "sheet_order": [s["name"] for s in sheets], "mask_counts": mask_counts(sheets), "folds": folds}
    if eval_sheets is not None:
        out["method_check_folds"] = method_check
    return out


# ---------------------------------------------------------------------------
# The deterministic clause splitter (B): no model, no network (#4201 phase 3a)
# ---------------------------------------------------------------------------

SPLITTER_VERSION = "B2"
SPLIT_ABBREVIATIONS = ("e.g.", "i.e.", "vs.", "cf.", "approx.")
SPLIT_BREAK_RE = re.compile(r"[.;!?]\s+")
# Rule (b), colons: a ':' followed by whitespace; the text since the previous colon cut needs at least
# SPLIT_COLON_LEFT_STEMS content stems and the text after the colon at least SPLIT_COLON_RIGHT_STEMS.
SPLIT_COLON_RE = re.compile(r":\s+")
SPLIT_COLON_LEFT_STEMS = 1
SPLIT_COLON_RIGHT_STEMS = 2
# Rule (c), parentheticals: a '( ... )' with no parenthesis inside, lifted out when its inner text has at
# least SPLIT_PAREN_MIN_STEMS content stems.
SPLIT_PAREN_RE = re.compile(r"\(([^()]*)\)")
SPLIT_PAREN_MIN_STEMS = 3
# Rule (d), commas: ', ' at parenthesis depth 0 outside code spans; the text since the previous comma cut and
# the chunk after this comma (up to the next ', ') each need at least SPLIT_COMMA_MIN_STEMS content stems.
SPLIT_COMMA = ", "
SPLIT_COMMA_MIN_STEMS = 1
# Rule (e), coordination: the connectors, tried in this order at each position (no connector is a prefix of
# another, and a ', and ' that starts one character before the ' and ' inside it wins as the leftmost match).
# Each side needs SPLIT_COORD_MIN_STEMS content stems and must be clause-like: B1's _clause_like, or a word of
# at least SPLIT_ED_MIN_CHARS characters ending in SPLIT_ED_SUFFIX. Rule (d) runs first and already cuts at a
# depth-0 ', ' between stemmed sides, so the comma connectors here fire only where (d) was blocked (inside a
# parenthesis, or a side with no content stem); after a (d) cut the right piece keeps its leading 'and' (a
# stopword, so counts and stem metrics are unchanged). That order is intended (#4201 option 2, B2 design).
SPLIT_COORD_CONNECTORS = (", whereas ", ", while ", ", and ", ", then ", ", but ", ", yet ", ", so ",
                          " and ", " but ")
SPLIT_COORD_RE = re.compile("|".join(re.escape(c) for c in SPLIT_COORD_CONNECTORS))
SPLIT_COORD_MIN_STEMS = 1
SPLIT_ED_MIN_CHARS = 5
SPLIT_ED_SUFFIX = "ed"
# Rule (f), qualifiers. Two introducers can match at the same position (' so that ' and ' so '): the cut
# position is the same either way, and _next_qualifier_cut visits each position once.
SPLIT_QUALIFIERS = (" after ", " because ", " so that ", " which ", " while ", " when ", " before ", " since ",
                    " until ", " with ", " so ")
SPLIT_MIN_STEMS = 3
SPLIT_STRIP = " \t\r\n.,;:!?-–—"
# Input cap (round 4): a claim longer than this many characters is NOT split. split_claim returns it as one
# piece (rule (h) only), and the split-eval and end-to-end reports count such claims as ``claims_over_cap``
# (computed by over_split_cap, so there is no module-level state to reset). The study's claims are far
# shorter; the cap exists so one pasted log can never make the splitter slow, and 0 is the expected count.
SPLIT_MAX_CHARS = 20000
_CODE_SPAN_RE = re.compile(r"`[^`]*`")
# Verb-like tokens: auxiliaries and a few irregular forms literally; the lemmas below by stem() (so merge
# matches merged and merges). A deliberately small list; see split_claim for what that costs.
SPLIT_VERB_FORMS = frozenset("""
is are was were be been being am has have had do does did will would can could should must may might shall
ran went made took gave got found wrote saw came kept set put left built began
""".split())
SPLIT_VERB_LEMMAS = tuple("""
merge approve review pass fail run add remove fix build test land ship open close read write call return create
delete update change move start stop show state report find use need make take give get keep hold cause block
depend include contain match check load send receive resolve introduce replace handle apply enable disable
complete finish begin drop trigger record file post name claim accept reject expect lead follow support require
produce store log rename revert cover miss
""".split())
_SPLIT_VERB_STEMS = frozenset(stem(w) for w in SPLIT_VERB_LEMMAS)


def _code_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _CODE_SPAN_RE.finditer(text)]


def _in_spans(pos: int, spans) -> bool:
    return any(a <= pos < b for a, b in spans)


def _ends_with_abbreviation(text: str, dot_at: int) -> bool:
    head = text[: dot_at + 1].lower()
    for ab in SPLIT_ABBREVIATIONS:
        if head.endswith(ab):
            before = head[: len(head) - len(ab)]
            if not before or not before[-1].isalnum():
                return True
    return False


def _split_sentences(text: str) -> list[str]:
    spans = _code_spans(text)
    pieces, start = [], 0
    for m in SPLIT_BREAK_RE.finditer(text):
        i = m.start()
        if _in_spans(i, spans) or (text[i] == "." and _ends_with_abbreviation(text, i)):
            continue
        pieces.append(text[start:i])
        start = m.end()
    pieces.append(text[start:])
    return pieces


def _clause_like(side: str) -> bool:
    """A side can stand as a clause (heuristically) if it has a verb-like token or a hard token."""
    for w in _WORD_RE.findall(normalize(side)):
        if w in SPLIT_VERB_FORMS or stem(w) in _SPLIT_VERB_STEMS:
            return True
    return bool(_hard_tokens_with_class(side))


def _add_stems(text: str, seen: set, limit: int | None = None) -> bool:
    """Add the distinct content stems of text to ``seen`` (the stems _content_stems would give). With a
    limit, stop as soon as len(seen) reaches it. Returns whether len(seen) >= limit (False without one)."""
    for w in _WORD_RE.findall(normalize(text)):
        if len(w) >= MIN_WORD and w not in STOPWORDS:
            seen.add(stem(w))
            if limit is not None and len(seen) >= limit:
                return True
    return limit is not None and len(seen) >= limit


def _has_stems(text: str, n: int) -> bool:
    """Whether text has at least n distinct content stems (nstems(text) >= n)."""
    return n <= 0 or _add_stems(text, set(), n)


def _span_mask(text: str) -> bytearray:
    """One byte per character of text: 1 inside a backtick code span, else 0 (O(1) lookups, whatever the
    number of spans)."""
    mask = bytearray(len(text))
    for a, b in _code_spans(text):
        mask[a:b] = b"\x01" * (b - a)
    return mask


def _split_colons(sentence: str) -> list[str]:
    """Rule (b). Linear: the sentence is read once, segment by segment (every segment boundary is a colon
    followed by whitespace, so no word is split and the stems of a segment add up exactly). The right side of
    a colon is everything after it up to the end of the sentence; its stem count only shrinks along the
    sentence, so it is computed once, backwards."""
    mask = _span_mask(sentence)
    colons = [m for m in SPLIT_COLON_RE.finditer(sentence) if not mask[m.start()]]
    if not colons:
        return [sentence]
    right_ok = [False] * len(colons)
    seen: set = set()
    for i in range(len(colons) - 1, -1, -1):
        if len(seen) < SPLIT_COLON_RIGHT_STEMS:
            end = colons[i + 1].start() if i + 1 < len(colons) else len(sentence)
            _add_stems(sentence[colons[i].end():end], seen, SPLIT_COLON_RIGHT_STEMS)
        right_ok[i] = len(seen) >= SPLIT_COLON_RIGHT_STEMS
    pieces, piece_start, done, left_ok = [], 0, 0, False
    for i, m in enumerate(colons):
        if not left_ok:
            left_ok = _has_stems(sentence[done:m.start()], SPLIT_COLON_LEFT_STEMS)
        done = m.end()
        if left_ok and right_ok[i]:
            pieces.append(sentence[piece_start:m.start()])
            piece_start, left_ok = m.end(), False
    pieces.append(sentence[piece_start:])
    return pieces


def _split_parentheticals(piece: str) -> list[str]:
    """Rule (c). A parenthesis inside a backtick code span is not a parenthesis (it is masked before the
    regex runs; the text cut out is taken from the original). Each ( ... ) with no parenthesis inside and at
    least SPLIT_PAREN_MIN_STEMS content stems is replaced in the host by one space and comes back as its own
    piece after the host, left to right. Linear: one regex pass, and each inner text is read once."""
    if "(" not in piece:
        return [piece]
    mask = _span_mask(piece)
    masked = piece
    if any(mask):
        masked = "".join("_" if (mask[i] and ch in "()") else ch for i, ch in enumerate(piece))
    host, inners, last = [], [], 0
    for m in SPLIT_PAREN_RE.finditer(masked):
        inner = piece[m.start() + 1:m.end() - 1]
        if _has_stems(inner, SPLIT_PAREN_MIN_STEMS):
            host.append(piece[last:m.start()])
            host.append(" ")
            inners.append(inner)
            last = m.end()
    if not inners:
        return [piece]
    host.append(piece[last:])
    return ["".join(host)] + inners


def _split_commas(piece: str) -> list[str]:
    """Rule (d). Linear: the piece is cut into chunks at every ', ' and each chunk's has-a-stem flag is
    computed once, on demand. A comma is a cut candidate when it is outside code spans and at parenthesis
    depth 0 (parentheses in code spans are not counted; the depth never goes below 0). The left side is the
    text since the previous cut (it has a stem exactly when one of its chunks has); the right side is the next
    chunk."""
    commas = [m.start() for m in re.finditer(re.escape(SPLIT_COMMA), piece)]
    if not commas:
        return [piece]
    mask = _span_mask(piece)
    depths, depth, j = [], 0, 0
    for pos, ch in enumerate(piece):
        if not mask[pos]:
            if ch == "(":
                depth += 1
            elif ch == ")" and depth > 0:
                depth -= 1
        if pos == commas[j]:
            depths.append(depth)
            j += 1
            if j == len(commas):
                break
    chunk_start = [0] + [c + len(SPLIT_COMMA) for c in commas]
    chunk_end = commas + [len(piece)]
    flags: dict = {}

    def has(i: int) -> bool:
        if i not in flags:
            flags[i] = _has_stems(piece[chunk_start[i]:chunk_end[i]], SPLIT_COMMA_MIN_STEMS)
        return flags[i]

    pieces, piece_start, left_ok = [], 0, False
    for i, c in enumerate(commas):
        left_ok = left_ok or has(i)
        if depths[i] == 0 and not mask[c] and left_ok and has(i + 1):
            pieces.append(piece[piece_start:c])
            piece_start, left_ok = c + len(SPLIT_COMMA), False
    pieces.append(piece[piece_start:])
    return pieces


def _has_ed_word(side: str) -> bool:
    """Rule (e)'s extra clause-like arm: a word of SPLIT_ED_MIN_CHARS or more characters ending in
    SPLIT_ED_SUFFIX (a rough past-tense test; B1's verb list misses most past-tense verbs)."""
    return any(len(w) >= SPLIT_ED_MIN_CHARS and w.endswith(SPLIT_ED_SUFFIX)
               for w in _WORD_RE.findall(normalize(side)))


def _coord_clause_like(side: str) -> bool:
    """Rule (e)'s clause test: B1's _clause_like, or a word of 5+ characters ending in 'ed'."""
    return _clause_like(side) or _has_ed_word(side)


def _split_coordination(piece: str) -> list[str]:
    """Rule (e). Linear: the piece is cut into chunks at the candidate connectors and each chunk's clause-like
    and has-a-stem flags are computed once, on demand. The left side of a candidate is the text since the last
    cut taken, which may hold several chunks that were kept together; it can stand as a clause (and has a
    stem) exactly when one of those chunks can (a connector adds neither a verb-like token, an 'ed' word, a
    hard token nor a stem)."""
    mask = _span_mask(piece)
    cuts = [(m.start(), m.end()) for m in SPLIT_COORD_RE.finditer(piece) if not mask[m.start()]]
    if not cuts:
        return [piece]
    chunk_start = [0] + [b for _a, b in cuts]
    chunk_end = [a for a, _b in cuts] + [len(piece)]
    like_flags: dict = {}
    stem_flags: dict = {}

    def like(i: int) -> bool:
        if i not in like_flags:
            like_flags[i] = _coord_clause_like(piece[chunk_start[i]:chunk_end[i]])
        return like_flags[i]

    def has(i: int) -> bool:
        if i not in stem_flags:
            stem_flags[i] = _has_stems(piece[chunk_start[i]:chunk_end[i]], SPLIT_COORD_MIN_STEMS)
        return stem_flags[i]

    pieces, piece_start, left_like, left_has = [], 0, False, False
    for idx, (a, b) in enumerate(cuts):
        left_like = left_like or like(idx)
        left_has = left_has or has(idx)
        if left_like and left_has and like(idx + 1) and has(idx + 1):
            pieces.append(piece[piece_start:a])
            piece_start = b
            left_like = left_has = True     # the next left side starts at chunk idx + 1, which can stand
    pieces.append(piece[piece_start:])
    return pieces


def _next_qualifier_cut(piece: str):
    """The first position of a qualifier introducer (not in a code span) whose left and right sides each have
    at least SPLIT_MIN_STEMS distinct content stems, or None. Candidates are visited in position order.
    Linear in the piece: the left side only grows from one candidate to the next, so its stems are collected
    once, segment by segment (every cut is at a space, so no word is split); the right side only shrinks, so
    the first candidate whose right side is too short ends the search."""
    spans = _code_spans(piece)
    found = heapq.merge(*(
        (m.start() for m in re.finditer(re.escape(intro), piece)) for intro in SPLIT_QUALIFIERS))
    left_stems: set = set()
    done, last = 0, -1
    for pos in found:
        if pos == last or _in_spans(pos, spans):       # two introducers at one position (' so that ' / ' so ')
            continue                                   # are one candidate: the cut position is the same
        last = pos
        _add_stems(piece[done:pos], left_stems)
        done = pos
        if len(left_stems) < SPLIT_MIN_STEMS:
            continue
        return pos if _add_stems(piece[pos + 1:], set(), SPLIT_MIN_STEMS) else None
    return None


def _split_qualifiers(piece: str) -> list[str]:
    """Rule (f), iteratively (a long chain of qualifiers must not recurse): cut at the first qualifier that
    qualifies, then repeat on the right-hand text, whose code spans are found afresh."""
    out: list[str] = []
    while True:
        pos = _next_qualifier_cut(piece)
        if pos is None:
            out.append(piece)
            return out
        out.append(piece[:pos])
        piece = piece[pos + 1:]


def over_split_cap(text) -> bool:
    return len(text or "") > SPLIT_MAX_CHARS


def split_claim(text: str) -> list[str]:
    """Split a claim into clauses (the S2 unit: one separately checkable assertion), deterministically.
    No model and no network. The rules are a heuristic, applied in this order; it will miss some clauses
    and cut some that humans would not, which is what the split-eval harness measures against the frozen
    human splits. SPLITTER_VERSION, the rule constants and this code are part of the freeze's code hash.

    (a) Sentences: break on '. ', '; ', '! ', '? ' (a mark followed by whitespace), except inside a
        backtick code span or after an abbreviation in SPLIT_ABBREVIATIONS. A number such as 3.5 has no
        whitespace after its dot, so it is never a break.
        'The run took 3.5 hours. It passed.' -> 'The run took 3.5 hours' | 'It passed'
    (b) Colons: inside a sentence, cut at a ':' followed by whitespace (not in a code span) when the text
        since the previous colon cut has at least 1 content stem and the text after the colon has at least 2.
        The colon and its whitespace are dropped. 'Quillon recorded the outcome: the suite passed cleanly'
            -> 'Quillon recorded the outcome' | 'the suite passed cleanly'
        'Quillon recorded the outcome: passed' stays whole (one stem on the right).
    (c) Parentheticals: each '( ... )' with no parenthesis inside it (parentheses in code spans do not
        count) whose inner text has at least SPLIT_PAREN_MIN_STEMS (3) content stems is taken out of its
        host (replaced by one space) and comes back as its own piece right after the host, left to right.
        One with fewer stems stays in the host. Each resulting piece goes on through (d), (e), (f).
        'Quillon merged the branch (reviewers finished careful inspection)'
            -> 'Quillon merged the branch' | 'reviewers finished careful inspection'
        'merged the branch (see notes)' stays whole (two stems).
    (d) Commas: cut at ', ' outside code spans and at parenthesis depth 0 (counted from the start of the
        piece, never below 0) when the text since the previous comma cut and the chunk after this comma (up
        to the next ', ' or the end) each have at least 1 content stem. The comma is dropped; a ', and' leaves
        its 'and' on the right piece, because this rule runs before (e).
        'Quillon ran the suite, Marrowby approved it' -> 'Quillon ran the suite' | 'Marrowby approved it'
        'Quillon ran, x' stays whole (no stem after the comma).
    (e) Coordination: cut at one of SPLIT_COORD_CONNECTORS (', whereas ', ', while ', ', and ', ', then ',
        ', but ', ', yet ', ', so ', ' and ', ' but '; not inside a code span) when BOTH sides can stand as a
        clause and each has at least 1 content stem. A side can stand as a clause if it has a verb-like
        token (SPLIT_VERB_FORMS, or a stem in SPLIT_VERB_LEMMAS), a hard token (engram_anchoring: a number of
        3+ digits, #ref, date, sha, node id), or a word of 5+ characters ending in 'ed'. The left side is
        the text since the previous cut, the right side runs to the next connector. The connector is dropped.
        The verb list is small on purpose; the 'ed' arm catches some of what it misses.
        'Quillon cleared the queue and Marrowby drained everything'
            -> 'Quillon cleared the queue' | 'Marrowby drained everything'
        'bread and butter' -> one clause
    (f) Trailing qualifiers: split before ' after ', ' because ', ' so that ', ' which ', ' while ', ' when ',
        ' before ', ' since ', ' until ', ' with ', ' so ' only when the text on each side has at least
        SPLIT_MIN_STEMS (3) distinct content stems. The introducer stays with the right-hand clause. ' so
        that ' and ' so ' match at the same position, so they are one cut. 'merged after review' stays whole:
        one side is too short to be its own assertion.
        'Quillon merged the hotfix branch after reviewers finished careful inspection'
            -> 'Quillon merged the hotfix branch' | 'after reviewers finished careful inspection'
    (h) Strip whitespace and . , ; : ! ? - from both ends of each piece (SPLIT_STRIP) and drop empty
        pieces. '  ...The suite ran,  ' -> 'The suite ran'
    (There is no rule (g); the letters match the pre-registered rule list.)

    Input cap: a text longer than SPLIT_MAX_CHARS characters is not split at all; it comes back as the one
    piece (rule (h) applied). over_split_cap(text) says which claims that happened to, and the reports count
    them as claims_over_cap. Rules (b) to (e) are each linear in their text and rule (f) is iterative (no
    recursion), so below the cap the cost stays small whatever the shape of the text. split_claim never
    raises on a str and every piece it returns is non-empty.

    Returns [] when nothing is left."""
    text = text or ""
    if over_split_cap(text):
        return [p for p in [text.strip(SPLIT_STRIP)] if p]
    out: list[str] = []
    for sentence in _split_sentences(text):
        for colon_piece in _split_colons(sentence):
            for paren_piece in _split_parentheticals(colon_piece):
                for comma_piece in _split_commas(paren_piece):
                    for piece in _split_coordination(comma_piece):
                        out.extend(_split_qualifiers(piece))
    return [p for p in (x.strip(SPLIT_STRIP) for x in out) if p]


def _stem_set(text: str) -> set:
    return set(_content_stems(normalize(text)))


def jaccard(a: set, b: set) -> float:
    """Jaccard overlap of two stem sets; 0.0 when both are empty (no shared content word)."""
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def map_split_clauses(item, quote, split_texts, frozen_records):
    """(records, n_none). Each split clause inherits the pass-1 labels of the frozen clause of the same item
    with the highest Jaccard of stemmed content words. A tie for the highest is NOT (labels NOT/NOT), with
    every tied frozen clause in ``mapped_ks``; a Jaccard of 0 to every frozen clause is NONE: counted, and
    no record is made, so it enters no statistic. ``k`` is the lowest mapped frozen clause number.
    ``tie_all_stated`` marks a tie in which every tied frozen clause has consensus STATED: the one case where
    the tie rule moves the label away from every candidate (a tie that includes a NOT clause keeps one)."""
    frozen = [(r, _stem_set(r["clause"])) for r in frozen_records]
    out, n_none = [], 0
    for i, text in enumerate(split_texts, 1):
        stems = _stem_set(text)
        scores = [(jaccard(stems, fs), r) for r, fs in frozen]
        best = max((s for s, _r in scores), default=0.0)
        if best <= 0.0:
            n_none += 1
            continue
        tied = [r for s, r in scores if s == best]
        if len(tied) > 1:
            rec = make_record(item, min(r["k"] for r in tied), quote, text, "NOT", "NOT")
        else:
            rec = make_record(item, tied[0]["k"], quote, text, tied[0]["a"], tied[0]["b"])
        rec.update({"split_k": i, "mapped_ks": sorted(r["k"] for r in tied), "jaccard": best, "tie": len(tied) > 1,
                    "tie_all_stated": len(tied) > 1 and all(r["consensus"] == STATED for r in tied)})
        out.append(rec)
    return out, n_none


def split_sheets(sheets, claims):
    """(eval sheets, per-sheet mapping stats): each sheet's real items re-expressed as the splitter's clauses
    with mapped labels. ``claims`` is {sheet name: {node id: claim}} (load_claims). The stats report the
    mapping rate (mapped / split clauses) with, beside it: ``ties_total`` and ``ties_stated_stated`` (ties in
    which every tied frozen clause is STATED: the rule moved the label away from all its candidates, so it
    bounds the direction of the tie bias); the coverage the rate does not show, ``frozen_clauses`` and
    ``frozen_unmapped`` (frozen clauses that no split clause maps to, a tie counting as mapping to every
    tied clause); ``items_empty_split`` (items for which split_claim returned nothing); and
    ``claims_over_cap`` (claims longer than SPLIT_MAX_CHARS, which were not split: expected 0)."""
    eval_sheets, stats = [], {}
    for sh in sheets:
        by_item: dict = {}
        for r in sh["records"]:
            by_item.setdefault(r["item"], []).append(r)
        recs, n_split, n_none, n_empty, n_frozen, n_unmapped, n_over = [], 0, 0, 0, 0, 0, 0
        for item in sorted(by_item):
            frozen = by_item[item]
            claim = claims[sh["name"]][sh["ids"][item]]
            n_over += over_split_cap(claim)
            parts = split_claim(claim)
            mapped, none = map_split_clauses(item, frozen[0]["quote"], parts, frozen)
            recs.extend(mapped)
            n_split += len(parts)
            n_none += none
            n_empty += 0 if parts else 1
            chosen = {k for r in mapped for k in r["mapped_ks"]}
            n_frozen += len(frozen)
            n_unmapped += sum(1 for r in frozen if r["k"] not in chosen)
        eval_sheets.append({"name": sh["name"], "records": recs})
        stats[sh["name"]] = {"split_clauses": n_split, "mapped": n_split - n_none, "none": n_none,
                             "mapping_rate": (n_split - n_none) / n_split if n_split else None,
                             "ties_total": sum(1 for r in recs if r["tie"]),
                             "ties_stated_stated": sum(1 for r in recs if r["tie_all_stated"]),
                             "frozen_clauses": n_frozen, "frozen_unmapped": n_unmapped,
                             "items_empty_split": n_empty, "claims_over_cap": n_over}
    return eval_sheets, stats


def apply_exclusions_mapped(test_sheet, entries):
    """Like apply_exclusions, for the splitter's records: a split clause is removed when it is mapped to
    any excluded frozen clause (every tied clause counts)."""
    records, rows = list(test_sheet["records"]), []
    for e in entries:
        before = len(records)
        if e["sheet"] == test_sheet["name"]:
            records = [r for r in records
                       if not (r["item"] == e["item"] and (e["clauses"] == "all"
                                                           or any(k in e["clauses"] for k in r["mapped_ks"])))]
        rows.append({"sheet": e["sheet"], "item": e["item"], "clauses": e["clauses"],
                     "removed": before - len(records)})
    return records, rows


# ---------------------------------------------------------------------------
# Per-node advisory shape (phase 3 uses it; nothing calls it yet)
# ---------------------------------------------------------------------------

def advise(quote: str, clauses: list[str] | None = None, theta: float | None = None, claim: str | None = None) -> dict:
    """Only the NOT clauses, numbered from 1. No node-level score, no pass/fail field. ``clauses`` is the
    node's clauses; when it is None they come from split_claim(claim). Explicit ``clauses`` win over
    ``claim``. ``theta`` has no default: the shipped value is a ruling, not this function's to pick."""
    if theta is None:
        raise ValueError("advise needs theta")
    if clauses is None:
        if claim is None:
            raise ValueError("advise needs clauses or a claim to split")
        clauses = split_claim(claim)
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
        out.append({"name": sh["name"], "rater_A": sh["A"], "rater_B": sh["B"], "records": records,
                    "ids": {n: sh["items"][n]["id"] for n in sh["real"]}})
    return out


# ---------------------------------------------------------------------------
# Claims (phase 3a): the node claims the splitter works on
# ---------------------------------------------------------------------------

def _claims_paths(config_path) -> list:
    """[(sheet name, claims.json path)] in config order. A sheet with no "claims" path refuses, naming it."""
    cpath = Path(config_path)
    config = _s2._json(_s2._read(cpath, "the config"), cpath)
    if (not isinstance(config, dict) or not isinstance(config.get("sheets"), list)
            or len(config["sheets"]) != 2 or not all(isinstance(c, dict) for c in config["sheets"])):
        raise Refusal("config: \"sheets\" must be a list of exactly two sheets (the signed design)")
    base = cpath.resolve().parent
    out = []
    for c in config["sheets"]:
        name, p = c.get("name"), c.get("claims")
        if not isinstance(p, str) or not p:
            raise Refusal(f"config: sheet {name!r} has no \"claims\" path (the splitter needs that sheet's claims.json)")
        q = Path(p)
        out.append((name, q if q.is_absolute() else base / q))
    return out


def _read_claims(config_path) -> dict:
    """{sheet name: (path, bytes)}: read once, so the bytes hashed are the bytes parsed."""
    return {name: (p, _s2._read(p, "the claims file")) for name, p in _claims_paths(config_path)}


def _claims_shas(raw) -> dict:
    return {name: _s2._sha(data) for name, (_p, data) in raw.items()}


def parse_claims(raw, sheets) -> dict:
    """{sheet name: {node id: claim}}. Every real item of a sheet has exactly one claim; a missing id, an
    extra id (a planted id is an extra one) or a repeated id refuses, listing them."""
    out = {}
    for sh in sheets:
        path, data = raw[sh["name"]]
        rows = _s2._json(data, path)
        if not isinstance(rows, list) or not all(isinstance(r, dict) and isinstance(r.get("node_id"), str)
                                                 and isinstance(r.get("claim"), str) for r in rows):
            raise Refusal(f"{path}: expected a list of {{\"node_id\": str, \"claim\": str}}")
        ids = [r["node_id"] for r in rows]
        want = set(sh["ids"].values())
        dup = sorted({i for i in ids if ids.count(i) > 1})
        missing, extra = sorted(want - set(ids)), sorted(set(ids) - want)
        if dup or missing or extra:
            raise Refusal(f"claims for sheet {sh['name']!r} ({path}): every real item needs exactly one claim; "
                          f"missing ids {missing}; extra ids {extra}; repeated ids {dup}")
        out[sh["name"]] = {r["node_id"]: r["claim"] for r in rows}
    return out


def load_claims(config_path, sheets, raw=None) -> dict:
    """Read and join the sheets' claims.json (path in the config, per sheet) to ``sheets`` (load_study)
    by node id, through each sheet's key. ``raw`` is _read_claims output, when the caller already has it."""
    return parse_claims(_read_claims(config_path) if raw is None else raw, sheets)


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


def _freeze_fields(clauses_mode: str = "frozen", claims_sha256=None, fixed_theta=None) -> dict:
    """What the manifest pins, recomputed from the running code. The two dependencies are hashed from
    the imported modules' __file__, so the bytes hashed are the code actually running. ``clauses_mode``
    ("frozen" or "split"), ``claims_sha256`` ({sheet name: sha256 of its claims.json}, None in frozen
    mode) and ``fixed_theta`` (the theta the gated end-to-end column evaluates M1 at; None in frozen mode)
    are the run's own choices, pinned beside the code's SPLITTER_VERSION. ``gate_column`` records which
    column gates: "fixed_theta" when a theta is fixed (the re-tuned method check is never the gate)."""
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
        "splitter_version": SPLITTER_VERSION,
        "clauses_mode": clauses_mode,
        "claims_sha256": claims_sha256,
        "fixed_theta": fixed_theta,
        "gate_column": "fixed_theta" if fixed_theta is not None else None,
    }


def make_manifest(commit: str, fold: int, exclusion_sha256=None, prior_report_sha256=None, changed=None,
                  clauses_mode: str = "frozen", claims_sha256=None, fixed_theta=None) -> dict:
    m = {"manifest_version": 2, "tool_version": TOOL_VERSION,
         **_freeze_fields(clauses_mode, claims_sha256, fixed_theta),
         "fold": fold,
         "exclusion_sha256": exclusion_sha256, "engram_alpha_commit": commit}
    if fold == 2:
        m["prior_report_sha256"] = prior_report_sha256
        if changed is not None:
            m["code_changed_since_fold1"], m["changed_fields_since_fold1"] = changed
    return m


def verify_freeze(manifest, commit: str, fold: int, exclusion_sha256=None, clauses_mode: str = "frozen",
                  claims_sha256=None, fixed_theta=None) -> None:
    if not isinstance(manifest, dict):
        raise Refusal("the freeze manifest must be a JSON object")
    now = _freeze_fields(clauses_mode, claims_sha256, fixed_theta)
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


def _compare_with_prior(prior: dict, where, clauses_mode: str = "frozen", claims_sha256=None,
                        fixed_theta=None) -> tuple:
    """(code_changed_since_fold1, differing field names) between the fold-1 report's recorded hash
    fields and the running code. Refuses if the report does not carry them. A fold-1 report made before
    ``fixed_theta`` and ``gate_column`` were pinned does not carry them and is refused here."""
    rec = prior.get("freeze")
    now = _freeze_fields(clauses_mode, claims_sha256, fixed_theta)
    if not isinstance(rec, dict) or any(k not in rec for k in now):
        raise Refusal(f"{where}: the fold-1 report does not carry the manifest hash fields "
                      f"({', '.join(now)}); cannot compare the code across folds")
    diff = [k for k in now if rec[k] != now[k]]
    return bool(diff), diff


def _fixed_theta_arg(args):
    """The --theta of an end-to-end command: required (and within [0, 1], the tuning grid's range) with
    ``--clauses split``, refused with ``--clauses frozen`` (frozen mode tunes M1; it has no theta to fix).
    Checked before anything is read."""
    if args.clauses != "split":
        if args.theta is not None:
            raise Refusal("--theta belongs to --clauses split only; the frozen-clause run tunes M1")
        return None
    if args.theta is None:
        raise Refusal("--clauses split needs --theta: the gated end-to-end column evaluates M1 at a fixed "
                      "theta (the shipped value) and does not tune it")
    if not (math.isfinite(args.theta) and 0.0 <= args.theta <= 1.0):
        raise Refusal(f"--theta {args.theta!r} is not a number between 0 and 1")
    return args.theta


def cmd_freeze(args) -> int:
    fixed_theta = _fixed_theta_arg(args)
    commit = _s2._scorer_commit(args.commit)
    _require_clean_tree([__file__, engram_anchoring.__file__, _s2.__file__])
    _entries, excl_sha = _exclusion_input(args.exclude)
    claims_sha = None
    if args.clauses == "split":
        if not args.config:
            raise Refusal("freeze --clauses split needs --config (the claims files it pins are named there)")
        claims_sha = _claims_shas(_read_claims(args.config))
    prior_sha, changed = None, None
    if args.fold == 2:
        if not args.prior_report:
            raise Refusal("a fold-2 freeze needs --prior-report (the fold-1 report.json)")
        ppath = Path(args.prior_report)
        pdata = _s2._read(ppath, "the prior report")
        prior = _s2._json(pdata, ppath)
        if not isinstance(prior, dict) or prior.get("fold") != 1:
            raise Refusal(f"{ppath} is not a fold-1 report (its \"fold\" is not 1)")
        changed = _compare_with_prior(prior, ppath, args.clauses, claims_sha, fixed_theta)
        prior_sha = _s2._sha(pdata)
    elif args.prior_report:
        raise Refusal("--prior-report belongs to a fold-2 freeze only")
    _s2._create_new(Path(args.out), json.dumps(make_manifest(commit, args.fold, excl_sha, prior_sha, changed, args.clauses, claims_sha, fixed_theta), indent=2) + "\n")
    print(f"froze fold {args.fold}: {args.out}", file=sys.stderr)
    return 0


def cmd_run(args) -> int:
    fixed_theta = _fixed_theta_arg(args)
    commit = _s2._scorer_commit(args.commit)
    fpath = Path(args.freeze)
    manifest = _s2._json(_s2._read(fpath, "the freeze manifest"), fpath)
    entries, excl_sha = _exclusion_input(args.exclude)
    claims_raw, claims_sha = None, None
    if args.clauses == "split":
        claims_raw = _read_claims(args.config)           # hashing the claims files reads no label
        claims_sha = _claims_shas(claims_raw)
    verify_freeze(manifest, commit, args.fold, excl_sha, args.clauses, claims_sha, fixed_theta)
    out_dir = Path(args.out_dir)
    for f in ("report.json", "report.md"):
        if os.path.lexists(out_dir / f):
            raise Refusal(f"{out_dir / f} already exists; refusing to overwrite")
    inputs: dict = {}
    sheets = load_study(args.config, args.hashes, inputs)
    report = build_report(sheets, fold=args.fold, exclusions=entries, exclusion_sha256=excl_sha)
    if args.clauses == "split":
        # M1, B1, B2, tune, LINE, stopwords and the stemmer are the same code as in the frozen run above.
        for name, sha in claims_sha.items():
            inputs[f"{name}/claims"] = sha
        eval_sheets, mapping = split_sheets(sheets, load_claims(args.config, sheets, claims_raw))
        e2e = build_report(sheets, fold=args.fold, exclusions=entries, exclusion_sha256=excl_sha,
                           eval_sheets=eval_sheets, fixed_theta=fixed_theta)
        report["clauses_mode"], report["splitter_version"] = "split", SPLITTER_VERSION
        report["end_to_end"] = {
            "gated": {"column": "fixed_theta", "theta": fixed_theta, "folds": e2e["folds"]},
            "method_check": {"column": "retuned_theta", "gated": False, "folds": e2e["method_check_folds"]},
            "mapping": mapping,
            "note": ("Each split clause inherits the label of the frozen clause with the highest Jaccard of "
                     "stemmed content words; a tie is NOT; no overlap is NONE and enters no statistic. The GATED "
                     "column evaluates M1 at the fixed theta (B2, a baseline that is not shipped, keeps its "
                     "dev-fold-tuned cutoff); the METHOD CHECK column re-tunes M1's theta on each dev fold's "
                     "split clauses and gates nothing. The frozen-split numbers are in \"folds\".")}
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


def _method_lines(methods, marker="###") -> list:
    L = []
    for name, m in methods.items():
        L.append(f"{marker} {name}")
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
    return L


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
    if "end_to_end" in rep:
        L += ['## Frozen human-split clauses (reference only, NOT the gate; the gated numbers are in '
              'the GATED column section below)', ""]
    for f in rep["folds"]:
        ex = f["exclusion"]
        L += [f"## Fold {f['fold']}: tuned on {f['tune_sheet']}, tested on {f['test_sheet']}", "",
              f"Exclusions (test sheet only): file sha256 {ex['sha256']}; removed {ex['removed_total']}; "
              f"entries {ex['entries']}",
              "Tuned: " + ", ".join(f"{k} {v['parameter']}={v['value']} (dev kappa {_f(v['kappa'])}, n {v['n']})"
                                    for k, v in f["tuned"].items()),
              f"Consensus on the test sheet: {f['test']['consensus_counts']}", ""]
        L += _method_lines(f["test"]["methods"])
    if "end_to_end" in rep:
        e = rep["end_to_end"]
        L += [f"## End-to-end on the splitter's clauses (splitter {rep['splitter_version']}; the frozen-split numbers are above)",
              "", e["note"], ""]
        for name, st in e["mapping"].items():
            rate = "null" if st["mapping_rate"] is None else f"{st['mapping_rate']:.4f}"
            L.append(f"- {name}: split clauses {st['split_clauses']}, mapped {st['mapped']}, NONE (excluded) "
                     f"{st['none']}, mapping rate {rate}; ties total {st['ties_total']} (labelled NOT), "
                     f"STATED-STATED ties {st['ties_stated_stated']}; frozen clauses never mapped to "
                     f"{st['frozen_unmapped']} of {st['frozen_clauses']}; items with an empty split "
                     f"{st['items_empty_split']}; claims over the split cap {st['claims_over_cap']}")
        L.append("")
        fixed = e["gated"]["theta"]
        L += [f"## End-to-end, GATED column (GATED, theta fixed at {fixed}): M1 at the fixed theta (not tuned); B2 keeps its dev-tuned cutoff", ""]
        for f in e["gated"]["folds"]:
            ex = f["exclusion"]
            tuned = ", ".join(f"M1 theta={v['value']} (fixed, not tuned)" if v.get("fixed") else
                              f"{k} {v['parameter']}={v['value']} (dev kappa {_f(v['kappa'])}, n {v['n']})"
                              for k, v in f["tuned"].items())
            L += [f"### Fold {f['fold']} end-to-end (gated): dev {f['tune_sheet']}, tested on {f['test_sheet']}", "",
                  f"Exclusions (by mapping): removed {ex['removed_total']}; entries {ex['entries']}",
                  "Parameters: " + tuned,
                  f"Consensus on the test sheet's split clauses: {f['test']['consensus_counts']}", ""]
            L += _method_lines(f["test"]["methods"], "####")
        L += ["## End-to-end, method check (UNGATED): M1's theta re-tuned on each dev fold's split clauses", "",
              "re-tuned theta per fold vs fixed: "
              + ", ".join(f"{f['tuned']['M1']['value']} vs {fixed}" for f in e["method_check"]["folds"]), ""]
        for f in e["method_check"]["folds"]:
            m1 = f["tuned"]["M1"]
            L += [f"### Fold {f['fold']} method check: tuned on {f['tune_sheet']}, tested on {f['test_sheet']}", "",
                  f"Tuned: M1 theta={m1['value']} (dev kappa {_f(m1['kappa'])}, n {m1['n']})", ""]
            L += _method_lines(f["test"]["methods"], "####")
    L += ["## Inputs (sha256)", ""] + [f"- {k}: {h}" for k, h in rep["inputs"].items()]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# split-eval: the splitter against the frozen human splits (no label is read)
# ---------------------------------------------------------------------------

FIRST_N = 10


def _sort_hash(seed_hex: str, ident: str) -> str:
    """sha256(seed_hex || id), hex digest compared as a string: the sheet builder's selection key
    (s2_clause_sheet.sort_hash; a cell pins the two together)."""
    return hashlib.sha256((seed_hex + ident).encode("utf-8")).hexdigest()


def _load_sheets_nolabels(config_path, inputs) -> list:
    """The two sheets' frozen clauses and keys, WITHOUT opening any return: no label loader is called
    (load_study, parse_return, _labels_of are not reached). Duplicates load_study's config checks."""
    cpath = Path(config_path)
    cdata = _s2._read(cpath, "the config")
    inputs["config"] = _s2._sha(cdata)
    config = _s2._json(cdata, cpath)
    if (not isinstance(config, dict) or not isinstance(config.get("sheets"), list)
            or len(config["sheets"]) != 2):
        raise Refusal("config: \"sheets\" must be a list of exactly two sheets (the signed design)")
    base = cpath.resolve().parent
    loaded = [_s2._load_sheet_inputs(c, base, inputs) for c in config["sheets"]]
    if len({sh["name"] for sh in loaded}) != 2:
        raise Refusal("config: the two sheets need different names")
    out = []
    for sh in loaded:
        key = sh["key"]
        seed_hex = key["seed_hex"] if isinstance(key.get("seed_hex"), str) else format(key["seed"], "08x")
        out.append({"name": sh["name"], "seed_hex": seed_hex,
                    "ids": {n: sh["items"][n]["id"] for n in sh["real"]},
                    "frozen": {n: [c["text"] for c in sh["reference"]["pass1"][n]] for n in sh["real"]}})
    return out


def _parse_bar(text, names) -> dict:
    """--bar: a JSON object {sheet name: minimum exact clause-count agreement rate in [0, 1]}, one entry per
    sheet and no other; declared at the call, never hard-coded."""
    try:
        bar = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise Refusal(f"--bar is not valid JSON: {exc}") from None
    if not isinstance(bar, dict) or set(bar) != set(names):
        raise Refusal(f"--bar must be a JSON object with exactly one rate per sheet ({sorted(names)})")
    for n, v in bar.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
            raise Refusal(f"--bar: the rate for {n!r} must be a number between 0 and 1")
    return bar


def _split_block(rows, first_items=None) -> dict:
    """Count agreement and the boundary check over ``rows`` (item, agree, jaccards)."""
    agree = sum(1 for _i, a, _j in rows if a)
    js = [j for _i, a, js_ in rows if a for j in js_]
    block = {"n_items": len(rows), "count_agree": agree,
             "count_agree_rate": agree / len(rows) if rows else None,
             "boundary": {"n_pairs": len(js), "mean_jaccard": sum(js) / len(js) if js else None,
                          "share_ge_0_5": sum(1 for j in js if j >= 0.5) / len(js) if js else None}}
    if first_items is not None:
        ft = [r for r in rows if r[0] in first_items]
        fa = sum(1 for _i, a, _j in ft if a)
        block["first_ten"] = {"items": sorted(first_items), "n": len(ft), "count_agree": fa,
                              "count_agree_rate": fa / len(ft) if ft else None}
    return block


def measure_splits(sheets, claims, bar) -> dict:
    """Per sheet and pooled: exact clause-count agreement of split_claim(claim) with the frozen clauses over
    all real items (the gated number); the same on the stage-1 first ten (first 10 by sort_hash ascending);
    and, on count-matched items, the per-pair Jaccard of stemmed content words in order (mean, share >= 0.5).
    ``bar`` {sheet: minimum rate}. Reads no label."""
    out, all_rows, all_first, all_over = {}, [], set(), 0
    for sh in sheets:
        rows, n_over = [], 0
        for n in sorted(sh["ids"]):
            claim = claims[sh["name"]][sh["ids"][n]]
            n_over += over_split_cap(claim)
            parts = split_claim(claim)
            frozen = sh["frozen"][n]
            agree = len(parts) == len(frozen)
            js = [jaccard(_stem_set(a), _stem_set(b)) for a, b in zip(parts, frozen)] if agree else []
            rows.append((n, agree, js))
        first = set(sorted(sh["ids"], key=lambda n: _sort_hash(sh["seed_hex"], sh["ids"][n]))[:FIRST_N])
        block = _split_block(rows, first)
        block["claims_over_cap"] = n_over
        block["bar"] = bar[sh["name"]]
        block["pass"] = block["count_agree_rate"] is not None and block["count_agree_rate"] >= bar[sh["name"]]
        out[sh["name"]] = block
        all_rows += [((sh["name"], i), a, j) for i, a, j in rows]
        all_first |= {(sh["name"], i) for i in first}
        all_over += n_over
    pooled = _split_block(all_rows, all_first)
    pooled["claims_over_cap"] = all_over
    pooled["first_ten"].pop("items")
    failed = [n for n, b in out.items() if not b["pass"]]
    verdict = {"pass": not failed,
               "line": ("PASS: every sheet's exact clause-count agreement is at or above its bar" if not failed
                        else "FAIL: below the bar on " + ", ".join(failed))}
    return {"report_version": 1, "tool_version": TOOL_VERSION, "splitter_version": SPLITTER_VERSION,
            "code_sha256": _file_sha256(__file__), "bar": dict(bar), "sheets": out, "pooled": pooled,
            "verdict": verdict,
            "note": ("Count agreement is necessary, not sufficient; the boundary Jaccard is reported beside it. "
                     "No label was read. Every number comes from the real S2 clauses; the synthetic fixtures "
                     "test code paths only.")}


def render_split_md(rep) -> str:
    """Item numbers only: never a node id."""
    L = ["# S2 clause splitter report (split-eval)", "",
         f"splitter {rep['splitter_version']}; code sha256 {rep['code_sha256']}; bar {rep['bar']}", "",
         rep["verdict"]["line"], ""]
    for name, b in rep["sheets"].items():
        ft, bd = b["first_ten"], b["boundary"]
        L += [f"## {name}", "",
              f"- exact clause-count agreement, all {b['n_items']} real items: {b['count_agree']} "
              f"({_f(b['count_agree_rate'])}); bar {b['bar']}: {'PASS' if b['pass'] else 'FAIL'}",
              f"- first ten (items {ft['items']}): {ft['count_agree']} of {ft['n']} ({_f(ft['count_agree_rate'])})",
              f"- boundary on {bd['n_pairs']} count-matched pairs: mean Jaccard {_f(bd['mean_jaccard'])}, "
              f"share >= 0.5 {_f(bd['share_ge_0_5'])}",
              f"- claims over the split cap ({SPLIT_MAX_CHARS} characters; not split): {b['claims_over_cap']}", ""]
    p = rep["pooled"]
    L += ["## pooled", "",
          f"- count agreement: {p['count_agree']} of {p['n_items']} ({_f(p['count_agree_rate'])})",
          f"- first ten: {p['first_ten']['count_agree']} of {p['first_ten']['n']} ({_f(p['first_ten']['count_agree_rate'])})",
          f"- boundary on {p['boundary']['n_pairs']} pairs: mean Jaccard {_f(p['boundary']['mean_jaccard'])}, "
          f"share >= 0.5 {_f(p['boundary']['share_ge_0_5'])}",
          f"- claims over the split cap ({SPLIT_MAX_CHARS} characters; not split): {p['claims_over_cap']}",
          "", rep["note"], "", "## Inputs (sha256)", ""]
    L += [f"- {k}: {h}" for k, h in rep["inputs"].items()]
    return "\n".join(L) + "\n"


def cmd_split_eval(args) -> int:
    out_dir = Path(args.out_dir)
    for f in ("report-split.json", "report-split.md"):
        if os.path.lexists(out_dir / f):
            raise Refusal(f"{out_dir / f} already exists; refusing to overwrite")
    inputs: dict = {}
    sheets = _load_sheets_nolabels(args.config, inputs)
    bar = _parse_bar(args.bar, [sh["name"] for sh in sheets])
    raw = _read_claims(args.config)
    claims = parse_claims(raw, sheets)
    for name, sha in _claims_shas(raw).items():
        inputs[f"{name}/claims"] = sha
    report = measure_splits(sheets, claims, bar)
    report["inputs"] = inputs
    text_json = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    out_dir.mkdir(parents=True, exist_ok=True)
    _s2._create_new(out_dir / "report-split.json", text_json)
    _s2._create_new(out_dir / "report-split.md", render_split_md(report))
    print("split report written", file=sys.stderr)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    fr = sub.add_parser("freeze")
    fr.add_argument("--fold", type=int, choices=(1, 2), required=True)
    fr.add_argument("--out", required=True)
    fr.add_argument("--commit", default=None)
    fr.add_argument("--exclude", default=None)
    fr.add_argument("--prior-report", default=None)
    fr.add_argument("--clauses", choices=("frozen", "split"), default="frozen")
    fr.add_argument("--config", default=None)
    fr.add_argument("--theta", type=float, default=None)
    ru = sub.add_parser("run")
    ru.add_argument("--clauses", choices=("frozen", "split"), default="frozen")
    ru.add_argument("--theta", type=float, default=None)
    ru.add_argument("--fold", type=int, choices=(1, 2), required=True)
    ru.add_argument("--exclude", default=None)
    ru.add_argument("--config", required=True)
    ru.add_argument("--hashes", required=True)
    ru.add_argument("--freeze", required=True)
    ru.add_argument("--out-dir", required=True)
    ru.add_argument("--commit", default=None)
    se = sub.add_parser("split-eval")
    se.add_argument("--config", required=True)
    se.add_argument("--bar", required=True)
    se.add_argument("--out-dir", required=True)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "freeze":
            return cmd_freeze(args)
        if args.cmd == "split-eval":
            return cmd_split_eval(args)
        return cmd_run(args)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
