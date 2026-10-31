#!/usr/bin/env python3
"""quote_quality_survey -- read-only quote-quality survey (engram-alpha#4050 S1).

An instrument, nothing else: it changes no write path, no floor, no refusal.
For every current observation (the same population as anchoring_report) it
measures how a node's quote relates to its claim and interpretation, reports
distributions per stratum, and can export a BLIND judging sheet so a person can
check whether the similarity score tracks "the quote carries the claim".

Usage:
    python quote_quality_survey.py [--db PATH] [--format human|json] [--seed N]
        [--judging-sheet OUT.md --judging-key OUT.json]
        [--exclude-source-class CLASS ...]
        [--swap ITEM_NUMBER ...]            (needs an existing --judging-key)
        [--sheet-part-bytes B]              (with --judging-sheet)
        [--score-sheet FILLED.md [FILLED2.md ...] --judging-key KEY.json]

    --db PATH        knowledge.db; default ENGRAM_HOME/knowledge.db (same
                     discovery and the same read as anchoring_report: the
                     population comes in id-ordered pages through the shared
                     owner-read helper, behind the flag
                     engramd.anchoring_report_reads_via_owner, default OFF;
                     flag OFF it is a mode=ro open. Flag ON with the owner
                     down, refusing or answering malformed: nothing on stdout,
                     one stderr line, exit 3. Exit 3 is also what
                     --score-sheet returns when some answers were not read;
                     the two modes never share a run.)
    --seed N         RNG seed for the judging draw (default 4050).
    --judging-sheet  With --judging-key: draw the blind sample and write the
                     sheet (item number, quote, interpretation, claim ONLY)
                     and, separately, the key (item -> node id, bin, scores).
    --exclude-source-class CLASS
                     Remove nodes of that source_class from the JUDGING SAMPLE
                     only; the distributions still cover the whole population.
    --swap N         Re-draw sheet item N from the same similarity bin (or the
                     terse stratum), deterministically from the seed and the
                     swap order, never reusing a node already drawn. Rewrites
                     sheet and key; the key's meta.swaps goes up by one.
    --sheet-part-bytes B
                     Also write OUT.part1of{N}.md ... (the sheet's stem, then
                     .partKofN, then its suffix), each at most B bytes, whole
                     items only, global item numbers, the sheet's header plus a
                     "Part K of N" line. An item that cannot fit exits 2 and
                     writes nothing. For handing a sheet across a size limit.
    --score-sheet    Score a filled sheet against its key: per-bin yes / partly
                     / no shares and the two pre-registered verdicts. Several
                     files (filled parts) are joined in the order given; an item
                     in two parts is a duplicate header, an item in none is
                     missing.

Similarity is `engram_anchoring.quote_claim_similarity` (the single home); when
the embedder is unavailable the value is null with a reason, never 0.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import engram_anchoring  # noqa: E402
import engram_core as core  # noqa: E402

# The population query and the read-only open live in anchoring_report; load that
# file by path (the `tools` package name is contested, see engram_core #2565 note)
# rather than writing a second SQL query or a second connect.
_spec = importlib.util.spec_from_file_location("_anchoring_report_for_survey", _HERE / "anchoring_report.py")
_anchoring_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_anchoring_report)
_open_readonly = _anchoring_report._open_readonly  # direct-only; tools/s2_clause_sheet.py borrows it from here
_open_population_reader = _anchoring_report._open_population_reader
_fetch_population = _anchoring_report._fetch_population
_PopulationReadError = _anchoring_report.AnchoringReportOwnerReadError

TOOL_VERSION = "1"
DEFAULT_SEED = 4050
PER_BIN_DRAW = 5
MIN_CELL_N = 10
MIN_ANSWERED = 3  # fewer answered items than this in an endpoint bin -> verdict UNDETERMINED
UNDETERMINED = "UNDETERMINED"


class TerseRule(NamedTuple):
    quote_type: str
    max_chars: int
    pattern: "re.Pattern[str]"


TERSE_MEASUREMENT_RULE = TerseRule(
    quote_type="hard_data",
    max_chars=160,
    pattern=re.compile(r"\d|passed|failed|rc=|exit|%"),
)
"""PRE-REGISTERED for engram-alpha#4050 S1 -- do NOT tune after data is seen.

A node is a `terse_measurement` iff quote_type == "hard_data" AND the quote is at
most 160 characters AND re.search(r"\\d|passed|failed|rc=|exit|%", quote) hits.
Changing any part of this after looking at survey output invalidates the
EXEMPT verdict; file a new pre-registration instead.
"""

SIM_BINS = [("[0,0.2)", 0.0, 0.2), ("[0.2,0.4)", 0.2, 0.4), ("[0.4,0.6)", 0.4, 0.6),
            ("[0.6,0.8)", 0.6, 0.8), ("[0.8,1]", 0.8, None)]
SIM_BIN_LABELS = [b[0] for b in SIM_BINS]
LEN_BIN_LABELS = ["<40", "40-79", "80-159", "160-319", ">=320"]
TERSE_STRATUM = "terse"
LOWEST_BIN, HIGHEST_BIN = SIM_BIN_LABELS[0], SIM_BIN_LABELS[-1]


def is_terse_measurement(quote_type: str | None, quote: str | None) -> bool:
    rule = TERSE_MEASUREMENT_RULE
    q = quote or ""
    return (
        quote_type == rule.quote_type
        and len(q) <= rule.max_chars
        and rule.pattern.search(q) is not None
    )


def sim_bin(value: float | None) -> str | None:
    if value is None:
        return None
    for label, lo, hi in SIM_BINS:
        if hi is None or value < hi:
            return label  # below 0 (negative cosine) falls in the lowest bin
    return SIM_BIN_LABELS[-1]


def length_bin(n: int) -> str:
    if n < 40:
        return "<40"
    if n < 80:
        return "40-79"
    if n < 160:
        return "80-159"
    if n < 320:
        return "160-319"
    return ">=320"


_JSON_WS_STRIP = str.maketrans("", "", " \t\n\r")


def _source_class(metadata_raw: str | None) -> str:
    if not metadata_raw:
        return "external"
    meta = core._metadata_dict(metadata_raw)
    if not meta:
        # _metadata_dict maps malformed AND a real empty object to {}. The
        # survey must tell them apart (malformed -> "unknown", so the per-class
        # shares do not silently absorb it), so look at the raw text instead of
        # parsing it a second time: "{}" with JSON whitespace removed is a
        # real empty object. Only the four JSON whitespace characters (space,
        # tab, LF, CR) are ignored, never str.split()'s Unicode whitespace:
        # json.loads rejects e.g. NBSP or U+2028, so those stay "unknown".
        # (engram-alpha#4085)
        if isinstance(metadata_raw, str) and metadata_raw.translate(_JSON_WS_STRIP) == "{}":
            return "external"
        return "unknown"
    return meta.get("source_class") or "external"


# ---------------------------------------------------------------------------
# Per-node measurement
# ---------------------------------------------------------------------------

def build_records(rows) -> list[dict]:
    out = []
    for row in rows:
        quote = row["quoted_text"] or ""
        claim = row["claim"] or ""
        interp = row["interpretation"] or ""
        a = engram_anchoring.anchor(claim, quote)
        sim_c, reason_c = engram_anchoring.quote_claim_similarity(quote, claim)
        sim_i, reason_i = engram_anchoring.quote_claim_similarity(quote, interp)
        created = row["created_at"] or ""
        out.append({
            "id": row["id"],
            "quote": quote, "claim": claim, "interp": interp,
            "quote_len_chars": len(quote),
            "quote_len_words": len(quote.split()),
            "body_to_quote_ratio": (len(interp) + len(claim)) / max(len(quote), 1),
            "anchoring_ratio": a.ratio,
            "hard_tokens": a.hard_tokens,
            "sim_quote_claim": sim_c, "sim_quote_claim_reason": reason_c,
            "sim_quote_interp": sim_i, "sim_quote_interp_reason": reason_i,
            "quote_type": row["quote_type"],
            "source_class": _source_class(row["metadata"]),
            "created_month": created[:7] or None,
            "terse_measurement": is_terse_measurement(row["quote_type"], quote),
        })
    return out


# ---------------------------------------------------------------------------
# Distributions
# ---------------------------------------------------------------------------

def _deciles(values: list[float]):
    if not values:
        return None
    if len(values) == 1:
        return [values[0]] * 9
    return statistics.quantiles(values, n=10, method="inclusive")


def _sim_bin_counts(values: list[float]) -> dict:
    counts = {label: 0 for label in SIM_BIN_LABELS}
    for v in values:
        counts[sim_bin(v)] += 1
    return counts


def _stratum(records: list[dict]) -> dict:
    sc = [r["sim_quote_claim"] for r in records if r["sim_quote_claim"] is not None]
    si = [r["sim_quote_interp"] for r in records if r["sim_quote_interp"] is not None]
    lens = [r["quote_len_chars"] for r in records]
    ratios = [r["anchoring_ratio"] for r in records if r["anchoring_ratio"] is not None]
    length_counts = {label: 0 for label in LEN_BIN_LABELS}
    for n in lens:
        length_counts[length_bin(n)] += 1
    return {
        "n": len(records),
        "deciles": {
            "sim_quote_claim": _deciles(sc),
            "sim_quote_interp": _deciles(si),
            "quote_len_chars": _deciles(lens),
            "anchoring_ratio": _deciles(ratios),
        },
        "sim_quote_claim_bins": _sim_bin_counts(sc),
        "sim_quote_interp_bins": _sim_bin_counts(si),
        "length_bins": length_counts,
    }


def build_strata(records: list[dict]) -> dict:
    strata = {"all": _stratum(records)}
    by_type: dict[str, list[dict]] = {}
    by_cell: dict[tuple, list[dict]] = {}
    for r in records:
        qt = r["quote_type"] or "(none)"
        by_type.setdefault(qt, []).append(r)
        by_cell.setdefault((qt, r["source_class"]), []).append(r)
    for qt, rs in sorted(by_type.items()):
        strata[f"quote_type={qt}"] = _stratum(rs)
    for (qt, sc), rs in sorted(by_cell.items()):
        if len(rs) >= MIN_CELL_N:
            strata[f"quote_type={qt}|source_class={sc}"] = _stratum(rs)
    strata["terse_measurement=true"] = _stratum([r for r in records if r["terse_measurement"]])
    strata["terse_measurement=false"] = _stratum([r for r in records if not r["terse_measurement"]])
    return strata


def build_by_month(records: list[dict]) -> dict:
    months: dict[str, list[dict]] = {}
    for r in records:
        months.setdefault(r["created_month"] or "(unknown)", []).append(r)
    out = {}
    for m, rs in sorted(months.items()):
        vals = [r["sim_quote_claim"] for r in rs if r["sim_quote_claim"] is not None]
        out[m] = {"n": len(rs), "median_sim_quote_claim": statistics.median(vals) if vals else None}
    return out


def _null_report(records: list[dict], field: str) -> dict:
    reasons = Counter()
    for r in records:
        if r[field] is None:
            reasons[r[field + "_reason"] or "unknown"] += 1
    return {"count": sum(reasons.values()), "reasons": dict(reasons)}


def _model_meta() -> dict:
    try:
        cfg = core._get_embedding_config()
    except Exception:  # noqa: BLE001
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    try:
        available = bool(core._embedder.is_available())
    except Exception:  # noqa: BLE001
        available = False
    return {
        "model": cfg.get("model", core.DEFAULT_EMBEDDING_MODEL),
        "enabled": cfg.get("enabled"),
        "embedder_available": available,
    }


# ---------------------------------------------------------------------------
# Blind judging sample
# ---------------------------------------------------------------------------

def _stratum_of(rec: dict, label: str) -> bool:
    if label == TERSE_STRATUM:
        return rec["terse_measurement"]
    return sim_bin(rec["sim_quote_claim"]) == label


def _eligible(records: list[dict], excluded: set[str]) -> list[dict]:
    return sorted((r for r in records if r["source_class"] not in excluded), key=lambda r: r["id"])


def _item_key_entry(rec: dict, stratum: str) -> dict:
    return {
        "node_id": rec["id"],
        "bin": sim_bin(rec["sim_quote_claim"]),
        "stratum": stratum,
        "sim_quote_claim": rec["sim_quote_claim"],
        "sim_quote_interp": rec["sim_quote_interp"],
        "quote_type": rec["quote_type"],
        "terse": rec["terse_measurement"],
    }


def draw_sample(records: list[dict], seed: int, excluded: set[str]) -> tuple[list[tuple[str, dict]], dict]:
    """5 per similarity bin plus 5 terse, without replacement across the draw,
    then shuffled. Returns ([(stratum, record)...] in item order, shortfalls)."""
    rng = random.Random(seed)
    pool = _eligible(records, excluded)
    drawn: set[str] = set()
    chosen: list[tuple[str, dict]] = []
    shortfalls: dict = {}
    for label in SIM_BIN_LABELS + [TERSE_STRATUM]:
        cands = [r for r in pool if r["id"] not in drawn and _stratum_of(r, label)]
        k = min(PER_BIN_DRAW, len(cands))
        if k < PER_BIN_DRAW:
            shortfalls[label] = {"requested": PER_BIN_DRAW, "drawn": k}
        for r in rng.sample(cands, k):
            drawn.add(r["id"])
            chosen.append((label, r))
    rng.shuffle(chosen)
    return chosen, shortfalls


def excluded_pool_count(records: list[dict], excluded: set[str]) -> int:
    return sum(
        1 for r in records
        if r["source_class"] in excluded and (r["terse_measurement"] or r["sim_quote_claim"] is not None)
    )


_ID_TOKEN_RE = re.compile(r"\b[a-z]{2}_[0-9]{4,}\b")
ID_MASK = "[id]"


def mask_ids(text: str) -> tuple[str, int]:
    """Replace node-id-shaped tokens with [id]; SHEET TEXT ONLY (never the key
    or the distributions). Returns (masked_text, number_of_tokens_masked)."""
    return _ID_TOKEN_RE.subn(ID_MASK, text or "")


def masked_token_count(items: list[dict]) -> int:
    return sum(mask_ids(it[f])[1] for it in items for f in ("quote", "interp", "claim"))


# The template's own question lines; the scorer anchors on these exact prefixes.
QUESTION_A = "(a) Does the quote by itself carry the claim? yes / partly / no -- Answer:"
QUESTION_B = "(b) Is it a verbatim measurement line? yes / no -- Answer:"
PRIVACY_NOTICE = ("Read the sheet before sending it to another seat: --exclude-source-class does not "
                  "screen personal strings (emails, addresses, names).")


def _blockquote(text: str) -> str:
    text = mask_ids(text)[0]
    lines = (text or "").splitlines() or ["(empty)"]
    return "\n".join("> " + ln for ln in lines)


_SHEET_HEADER = [
    "# Quote judging sheet (node ids are masked as [id])\n",
    "For each item read the quote, the interpretation and the "
    "claim, then fill in the answers after `Answer:`. Items are in random order.\n",
]


def _render_item(it: dict) -> str:
    return (
        f"## Item {it['number']}\n\n"
        f"**Quote:**\n{_blockquote(it['quote'])}\n\n"
        f"**Interpretation:**\n{_blockquote(it['interp'])}\n\n"
        f"**Claim:**\n{_blockquote(it['claim'])}\n\n"
        f"{QUESTION_A} \n\n"
        f"{QUESTION_B} \n\n"
        "(c) Note: \n"
    )


def render_sheet(items: list[dict]) -> str:
    """items: dicts with number, quote, interp, claim -- nothing else is shown."""
    return "\n".join(_SHEET_HEADER + [_render_item(it) for it in sorted(items, key=lambda i: i["number"])])


def _part_line(k: int, n: int, first: int, last: int, total: int) -> str:
    return f"Part {k} of {n}: items {first}-{last} of {total}.\n"


def plan_sheet_parts(items: list[dict], max_bytes: int) -> list[str]:
    """Split the sheet into parts of at most `max_bytes` UTF-8 bytes each: whole items
    only, in the sheet's order, with the sheet's own item numbers. Every part starts with
    the sheet's header plus a "Part K of N: items A-B of T." line. Raises ValueError (naming
    the item and its size) when one item plus the header cannot fit: it never exceeds the
    bound. The part line's width is reserved at its widest (every number as wide as the
    total), so the part count, which the line itself contains, needs no second pass."""
    ordered = sorted(items, key=lambda i: i["number"])
    total = len(ordered)
    top = max((it["number"] for it in ordered), default=0)
    widest = len(_part_line(total, total, top, top, total).encode())
    # header + part line, then the "\n" that joins them to the first item
    overhead = len("\n".join(_SHEET_HEADER).encode()) + 1 + widest + 1
    groups: list[list[tuple[int, str]]] = []
    used = 0
    for it in ordered:
        text = _render_item(it)
        size = len(text.encode())
        if overhead + size > max_bytes:
            raise ValueError(f"item {it['number']} is {size} bytes and needs {overhead + size} bytes with "
                             f"the header and part line, over --sheet-part-bytes {max_bytes}")
        if groups and used + 1 + size <= max_bytes:  # +1: the "\n" joining two items
            groups[-1].append((it["number"], text))
            used += 1 + size
        else:
            groups.append([(it["number"], text)])
            used = overhead + size
    n = len(groups)
    return ["\n".join(_SHEET_HEADER + [_part_line(k, n, g[0][0], g[-1][0], total)] + [t for _, t in g])
            for k, g in enumerate(groups, start=1)]


def sheet_part_path(sheet: Path, k: int, n: int) -> Path:
    return sheet.with_name(f"{sheet.stem}.part{k}of{n}{sheet.suffix}")


def remove_stale_parts(sheet: Path) -> int:
    """Delete the part files an earlier run left next to `sheet`: names matching exactly
    <stem>.part<K>of<N><suffix>, in the same directory, and only files or symlinks (a
    directory of that name is left alone). A symlink is unlinked itself and never followed:
    leaving it would let the next write go through it to its target. Returns the count.
    Call it only after every validation has passed, so a refused run deletes nothing."""
    pat = re.compile(re.escape(sheet.stem) + r"\.part[0-9]+of[0-9]+" + re.escape(sheet.suffix))
    removed = 0
    for entry in sorted(sheet.parent.iterdir()):
        if not pat.fullmatch(entry.name):
            continue
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
            removed += 1
    return removed


def _refresh_sheet_parts(sheet: Path, parts: list[str]) -> int:
    """Remove stale parts, write the new ones (if any), say so on stderr. Returns the stale count."""
    removed = remove_stale_parts(sheet)
    _write_parts(sheet, parts)
    if removed:
        print(f"removed {removed} stale sheet part file(s) next to {sheet}", file=sys.stderr)
    return removed


def _write_parts(sheet: Path, parts: list[str]) -> None:
    for k, text in enumerate(parts, start=1):
        _write_sheet(sheet_part_path(sheet, k, len(parts)), text)


def _sheet_items(key_items: dict, by_id: dict) -> list[dict]:
    out = []
    for num, entry in key_items.items():
        r = by_id[entry["node_id"]]
        out.append({"number": int(num), "quote": r["quote"], "interp": r["interp"], "claim": r["claim"]})
    return out


def _write_sheet(path: Path, text: str) -> None:
    """The sheet is for another seat to read: normal default mode (0o666 less the umask,
    which is what write_text gave). O_NOFOLLOW: never write through a symlink."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o666)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def _write_key(path: Path, text: str) -> None:
    """The key is private. Created 0600 at open(), so there is no window where it
    exists with a looser mode; fchmod covers a key file that already existed.
    O_NOFOLLOW: a symlink at the path is never followed, so fchmod can never
    change the mode of some other file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(text)


def swap_items(key: dict, records: list[dict], swap_numbers: list[int], seed: int,
               excluded: set[str]) -> dict:
    drawn = set(key["meta"].get("drawn_node_ids") or [e["node_id"] for e in key["items"].values()])
    pool = _eligible(records, excluded)
    for num in swap_numbers:
        entry = key["items"].get(str(num))
        if entry is None:
            raise SystemExit(f"quote_quality_survey: no item {num} in the key")
        stratum = entry["stratum"]
        cands = [r for r in pool if r["id"] not in drawn and _stratum_of(r, stratum)]
        if not cands:
            raise SystemExit(f"quote_quality_survey: no unused node left in stratum {stratum} for item {num}")
        key["meta"]["swaps"] = key["meta"].get("swaps", 0) + 1
        rng = random.Random(f"{seed}:swap:{key['meta']['swaps']}")
        pick = rng.choice(cands)
        drawn.add(pick["id"])
        key["items"][str(num)] = _item_key_entry(pick, stratum)
    key["meta"]["drawn_node_ids"] = sorted(drawn)
    return key


# ---------------------------------------------------------------------------
# Scoring a filled sheet
# ---------------------------------------------------------------------------

_ITEM_RE = re.compile(r"^## Item (\d+)[ \t]*$", re.M)
_OPTIONS = {"a": ("yes", "partly", "no"), "b": ("yes", "no")}
_TEMPLATE = {"a": QUESTION_A, "b": QUESTION_B}
_STRIP = " \t*_.,;:!"  # whitespace, markdown emphasis and trailing punctuation; NOT "?"
R_HEDGED = "not one allowed answer"
R_NO_TEMPLATE = "no template line"
R_DUPLICATE = "duplicate item header"
R_MISSING = "item missing from the sheet"


def _answer(block: str, letter: str):
    """Returns (answer_or_None, status, reason). status: "ok", "blank" (unanswered) or "unparsed".

    Only lines that begin with the template's own question text count, so a
    judge's note such as "(a) would be yes ..." can never replace the answer.
    The first such line is used. The answer text, after stripping whitespace,
    emphasis and trailing punctuation, must EQUAL one allowed word: anything
    hedged or compound ("yes / no", "no idea", "yes?") is unparsed, not a vote."""
    for line in block.splitlines():
        if line.startswith(_TEMPLATE[letter]):
            tail = line[len(_TEMPLATE[letter]):]
            if not tail.strip():
                return None, "blank", None
            word = tail.strip(_STRIP).lower()
            if word in _OPTIONS[letter]:
                return word, "ok", None
            return None, "unparsed", R_HEDGED
    return None, "unparsed", R_NO_TEMPLATE


def parse_filled_sheet(text: str) -> dict:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    marks = list(_ITEM_RE.finditer(text))
    numbers = [int(m.group(1)) for m in marks]
    out = {}
    for i, m in enumerate(marks):
        n = int(m.group(1))
        if numbers.count(n) > 1:
            # a repeated header: neither block is used, whichever order they came in
            out[n] = {"a": None, "b": None, "unparsed": ["a", "b"],
                      "reasons": {"a": R_DUPLICATE, "b": R_DUPLICATE}}
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        block = text[m.end():end]
        a, sa, ra = _answer(block, "a")
        b, sb, rb = _answer(block, "b")
        out[n] = {"a": a, "b": b,
                  "unparsed": [l for l, st in (("a", sa), ("b", sb)) if st == "unparsed"],
                  "reasons": {l: r for l, st, r in (("a", sa, ra), ("b", sb, rb)) if st == "unparsed"}}
    return out


def _shares(answers: list[str | None], options: tuple) -> dict:
    given = [a for a in answers if a in options]
    res = {"n_items": len(answers), "n_answered": len(given)}
    for o in options:
        res[o] = (given.count(o) / len(given)) if given else None
    return res


def score_sheet(key: dict, filled: dict) -> dict:
    strata: dict[str, list] = {}
    strata_b: dict[str, list] = {}
    unparsed: dict[int, dict] = {}
    counted: dict[int, dict] = {}
    for num, entry in key["items"].items():
        ans = filled.get(int(num), {"a": None, "b": None, "unparsed": ["a", "b"],
                                    "reasons": {"a": R_MISSING, "b": R_MISSING}})
        if ans["unparsed"]:
            unparsed[int(num)] = dict(ans["reasons"])
            counted[int(num)] = {"a": ans["a"], "b": ans["b"]}
        strata.setdefault(entry["stratum"], []).append(ans["a"])
        strata_b.setdefault(entry["stratum"], []).append(ans["b"])
    per_bin = {}
    for label in SIM_BIN_LABELS + [TERSE_STRATUM]:
        if label in strata:
            per_bin[label] = _shares(strata[label], ("yes", "partly", "no"))
            per_bin[label]["verbatim_measurement_yes"] = _shares(strata_b[label], ("yes", "no"))["yes"]

    counts = {l: per_bin[l]["n_answered"] if l in per_bin else 0 for l in SIM_BIN_LABELS + [TERSE_STRATUM]}
    thin_endpoints = [l for l in (LOWEST_BIN, HIGHEST_BIN) if counts[l] < MIN_ANSWERED]
    sep = {"rule": "'no' share non-increasing lowest->highest bin AND lowest minus highest >= 0.40",
           "min_answered": MIN_ANSWERED,
           "answered_per_bin": {l: counts[l] for l in SIM_BIN_LABELS}}
    if thin_endpoints:
        # Never move the endpoints to the next answered bin: the registered rule is about
        # the lowest and highest REGISTERED bins.
        sep.update({
            "value": UNDETERMINED,
            "reason": f"endpoint bin(s) {thin_endpoints} have fewer than {MIN_ANSWERED} answered items",
        })
    else:
        used = [l for l in SIM_BIN_LABELS if counts[l] > 0]
        nos = [per_bin[l]["no"] for l in used]
        monotonic = all(round(nos[i] - nos[i + 1], 9) >= 0 for i in range(len(nos) - 1))
        diff = nos[0] - nos[-1]
        sep.update({
            "bins_used": used,
            "no_shares": dict(zip(used, nos)),
            "no_share_lowest": nos[0], "no_share_highest": nos[-1],
            "monotonic": monotonic, "difference": diff,
            "thin_middle_bins": [l for l in used[1:-1] if counts[l] < MIN_ANSWERED],
            "note": f"middle bins with fewer than {MIN_ANSWERED} answered items are listed in "
                    "thin_middle_bins and still enter the monotonic check",
            "value": bool(monotonic and round(diff, 9) >= 0.40),
        })

    ex = {"rule": "terse stratum 'yes' share >= highest registered bin 'yes' share",
          "min_answered": MIN_ANSWERED,
          "answered_terse": counts[TERSE_STRATUM], "answered_top_bin": counts[HIGHEST_BIN]}
    thin_ex = [name for name, n in (("terse", counts[TERSE_STRATUM]), (HIGHEST_BIN, counts[HIGHEST_BIN]))
               if n < MIN_ANSWERED]
    if thin_ex:
        ex.update({
            "value": UNDETERMINED,
            "reason": f"{thin_ex} have fewer than {MIN_ANSWERED} answered items",
        })
    else:
        terse_yes, top = per_bin[TERSE_STRATUM]["yes"], per_bin[HIGHEST_BIN]["yes"]
        ex.update({"terse_yes": terse_yes, "top_bin": HIGHEST_BIN, "top_bin_yes": top,
                   "value": bool(round(terse_yes - top, 9) >= 0)})
    return {
        "per_bin": per_bin,
        "verdicts": {"SEPARATES": sep, "EXEMPT": ex},
        "n_unparsed": len(unparsed),
        "unparsed_items": sorted(unparsed),
        "unparsed_fields": {str(n): unparsed[n] for n in sorted(unparsed)},
        "unparsed_counted_fields": {str(n): [l for l in ("a", "b") if counted[n][l] is not None]
                                    for n in sorted(unparsed)},
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _print_survey_human(payload: dict) -> None:
    meta = payload["meta"]
    print(f"quote_quality_survey v{meta['tool_version']}  population={meta['population']}  db={meta['db']}")
    emb = meta["embedding"]
    print(f"embedding model={emb['model']} enabled={emb['enabled']} embedder_available={emb['embedder_available']}")
    nulls = meta["null_similarity"]
    for f in ("sim_quote_claim", "sim_quote_interp"):
        print(f"null {f}: {nulls[f]['count']} {nulls[f]['reasons']}")
    print()
    print(f"{'stratum':<56}{'n':>6}{'med_sim_c':>10}{'med_qlen':>9}")
    for name, s in payload["strata"].items():
        d = s["deciles"]
        med_s = d["sim_quote_claim"][4] if d["sim_quote_claim"] else None
        med_l = d["quote_len_chars"][4] if d["quote_len_chars"] else None
        print(f"{name:<56}{s['n']:>6}{('-' if med_s is None else format(med_s, '.2f')):>10}"
              f"{('-' if med_l is None else format(med_l, '.0f')):>9}")
    allb = payload["strata"]["all"]
    print()
    print("sim_quote_claim bins (all):", allb["sim_quote_claim_bins"])
    print("length bins (all):", allb["length_bins"])
    print("by created_month (n, median sim_quote_claim):")
    for m, v in payload["by_created_month"].items():
        print(f"  {m}: n={v['n']} median={'-' if v['median_sim_quote_claim'] is None else format(v['median_sim_quote_claim'], '.2f')}")


def _print_score_human(res: dict) -> None:
    if res["n_unparsed"]:
        print(f"WARNING: n_unparsed={res['n_unparsed']} items={res['unparsed_items']}  "
              "(these answers were NOT read; fix the sheet text and rescore)")
        for n in res["unparsed_items"]:
            fields = res["unparsed_fields"][str(n)]
            counted = res["unparsed_counted_fields"][str(n)]
            parts = [f"({l}) not read" for l in sorted(fields)]
            parts += [f"({l}) counted" for l in counted if l not in fields]
            why = ", ".join(f"{l}: {r}" for l, r in sorted(fields.items()))
            print(f"  item {n}: {'; '.join(parts)}  [{why}]")
    for label, s in res["per_bin"].items():
        fmt = lambda v: "-" if v is None else f"{v:.2f}"  # noqa: E731
        print(f"{label:<10} answered {s['n_answered']}/{s['n_items']}  yes={fmt(s['yes'])} "
              f"partly={fmt(s['partly'])} no={fmt(s['no'])}")
    for name, v in res["verdicts"].items():
        print(f"{name}: {v['value']}  {json.dumps({k: x for k, x in v.items() if k != 'value'})}")
    if not res["n_unparsed"]:
        print("n_unparsed=0")


def _plan_parts_or_exit(parser, sheet_items: list[dict], max_bytes: int | None) -> list[str]:
    """[] when no bound was asked for. An item that cannot fit exits 2 BEFORE anything is
    written, so a refused run leaves no sheet, parts or key behind."""
    if max_bytes is None:
        return []
    try:
        return plan_sheet_parts(sheet_items, max_bytes)
    except ValueError as exc:
        parser.error(str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=None)
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--judging-sheet", default=None)
    parser.add_argument("--judging-key", default=None)
    parser.add_argument("--score-sheet", nargs="+", default=None,
                        help="one filled sheet, or several filled parts (read in the order given)")
    parser.add_argument("--sheet-part-bytes", type=int, default=None,
                        help="also write the sheet as parts of at most this many bytes")
    parser.add_argument("--exclude-source-class", action="append", default=[])
    parser.add_argument("--swap", action="append", type=int, default=[])
    args = parser.parse_args(argv)

    if args.sheet_part_bytes is not None:
        if args.sheet_part_bytes <= 0:
            parser.error("--sheet-part-bytes must be positive")
        if args.score_sheet or not args.judging_sheet:
            parser.error("--sheet-part-bytes goes with --judging-sheet")

    if args.score_sheet:
        if not args.judging_key:
            parser.error("--score-sheet needs --judging-key")
        key = json.loads(Path(args.judging_key).read_text())
        # one file keeps today's behaviour; several are joined in the order given
        filled_text = "\n".join(Path(p).read_text() for p in args.score_sheet)
        res = score_sheet(key, parse_filled_sheet(filled_text))
        if args.format == "json":
            print(json.dumps(res, indent=2))
        else:
            _print_score_human(res)
        # verdicts are always printed; exit 3 flags that some answers were not read
        return 3 if res["n_unparsed"] else 0

    if bool(args.judging_sheet) != bool(args.judging_key) and not args.swap:
        parser.error("--judging-sheet and --judging-key go together")
    if args.judging_sheet and args.judging_key and \
            Path(args.judging_sheet).resolve() == Path(args.judging_key).resolve():
        parser.error("--judging-sheet and --judging-key must be different paths")
    if args.swap and not (args.judging_key and args.judging_sheet):
        parser.error("--swap needs the existing --judging-key and the --judging-sheet to rewrite")
    # Before anything is read from the graph, written or deleted: the sheet and key writers
    # never follow a symlink, so a link at either path is refused here, by name.
    for flag, given in (("--judging-sheet", args.judging_sheet), ("--judging-key", args.judging_key)):
        if given and os.path.islink(given):
            parser.error(f"{flag} {given} is a symlink; refusing to write through it "
                         "(remove the link or give a real path)")

    db_path = Path(args.db) if args.db else (core.DATA_DIR / "knowledge.db")
    # The population is read in id-ordered pages through the owner-read helper (the same function and the same
    # flag as anchoring_report). A failure prints nothing on stdout, one stderr line, and exits 3.
    try:
        conn = _open_population_reader(db_path)
        try:
            rows = _fetch_population(conn, None)
        finally:
            conn.close()
    except FileNotFoundError:
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 1
    except (_anchoring_report.owner_read.StoreUnusable, _anchoring_report.sqlite3.Error) as exc:
        print(f"ERROR: AnchoringReportOwnerReadError: the store at {db_path} could not be read ("
              f"{' '.join(str(exc).split())}); fix the path or the store.", file=sys.stderr)
        return 3
    except _PopulationReadError as exc:
        print(f"ERROR: {type(exc).__name__}: {' '.join(str(exc).split())}", file=sys.stderr)
        return 3
    records = build_records(rows)

    excluded = set(args.exclude_source_class)

    if args.swap:
        key_path = Path(args.judging_key)
        key = json.loads(key_path.read_text())
        seed = args.seed if args.seed is not None else key["meta"].get("seed", DEFAULT_SEED)
        excluded |= set(key["meta"].get("excluded_source_classes") or [])
        key["meta"]["excluded_source_classes"] = sorted(excluded)
        swap_items(key, records, args.swap, seed, excluded)
        by_id = {r["id"]: r for r in records}
        sheet_items = _sheet_items(key["items"], by_id)
        key["meta"]["masked_id_tokens"] = masked_token_count(sheet_items)
        parts = _plan_parts_or_exit(parser, sheet_items, args.sheet_part_bytes)
        _write_sheet(Path(args.judging_sheet), render_sheet(sheet_items))
        _refresh_sheet_parts(Path(args.judging_sheet), parts)
        _write_key(key_path, json.dumps(key, indent=2))
        print(f"swapped items {args.swap}; swaps={key['meta']['swaps']}"
              + (f"; sheet_parts={len(parts)} sheet_part_bytes={args.sheet_part_bytes}" if parts else ""),
              file=sys.stderr)
        print(PRIVACY_NOTICE, file=sys.stderr)
        return 0

    seed = args.seed if args.seed is not None else DEFAULT_SEED
    meta = {
        "tool_version": TOOL_VERSION,
        "db": str(db_path),
        "population": len(records),
        "null_similarity": {
            "sim_quote_claim": _null_report(records, "sim_quote_claim"),
            "sim_quote_interp": _null_report(records, "sim_quote_interp"),
        },
        "embedding": _model_meta(),
        "seed": seed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "excluded_source_classes": sorted(excluded),
        "excluded_from_sample_pool": excluded_pool_count(records, excluded),
        "privacy_notice": PRIVACY_NOTICE,
    }
    payload = {
        "meta": meta,
        "strata": build_strata(records),
        "by_created_month": build_by_month(records),
    }

    note_stream = sys.stderr if args.format == "json" else sys.stdout
    if args.judging_sheet:
        chosen, shortfalls = draw_sample(records, seed, excluded)
        key_items, sheet_items = {}, []
        for number, (stratum, rec) in enumerate(chosen, start=1):
            key_items[str(number)] = _item_key_entry(rec, stratum)
            sheet_items.append({"number": number, "quote": rec["quote"], "interp": rec["interp"],
                                "claim": rec["claim"]})
        key = {
            "meta": {
                "seed": seed, "swaps": 0, "tool_version": TOOL_VERSION,
                "generated_at": meta["generated_at"],
                "excluded_source_classes": sorted(excluded),
                "shortfalls": shortfalls,
                "masked_id_tokens": masked_token_count(sheet_items),
                "drawn_node_ids": sorted(e["node_id"] for e in key_items.values()),
            },
            "items": key_items,
        }
        parts = _plan_parts_or_exit(parser, sheet_items, args.sheet_part_bytes)
        _write_sheet(Path(args.judging_sheet), render_sheet(sheet_items))
        _refresh_sheet_parts(Path(args.judging_sheet), parts)
        _write_key(Path(args.judging_key), json.dumps(key, indent=2))
        if parts:
            meta["sheet_parts"] = len(parts)
            meta["sheet_part_bytes"] = args.sheet_part_bytes
        print(PRIVACY_NOTICE, file=sys.stderr)
        print(f"wrote {len(key_items)} items; sheet={args.judging_sheet} key={args.judging_key}"
              + (f"; sheet_parts={len(parts)} sheet_part_bytes={args.sheet_part_bytes}" if parts else "")
              + (f"; smaller than requested: {shortfalls}" if shortfalls else ""), file=note_stream)

    if args.format == "json":
        print(json.dumps(payload, indent=2))
    else:
        _print_survey_human(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
