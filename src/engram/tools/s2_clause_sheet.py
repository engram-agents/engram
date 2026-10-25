#!/usr/bin/env python3
"""s2_clause_sheet -- clause-check sheet builder (engram-alpha#4050 S2).

Implements the S2 pre-registration, draft 5 (issue comment 6387983396; change-log
entry 14, presentation order; entry 15, secret per-sheet salt, issue comment
6390535527). The seed stays pinned to draft 4's value
(SEED_HEX d638538e, from issue comment 6385972287) so the sample does not move.
The pre-registration is the contract: where this file and that text disagree,
that text wins. This tool only builds the sheets. It changes no write path, no
floor, no refusal.

Presentation order (draft 5, entry 15): render numbers all items, real and planted, by
sha256(seed_hex || 'order' || sheet_salt || id) ascending, hex digest compared as a
string (ORDER_SALT = "order"). sheet_salt is a secret 16-character lowercase hex
string per sheet, from os.urandom(8).hex() on the operator's side. It is committed
publicly as sha256(sheet_salt) (UTF-8 of the 16-char string) before pass 1 and
revealed only after all four return hashes for that sheet (both judges, both
passes) are posted (change-log entry 16, issue comment 6390564047). This tool
never reveals it. It appears only in the private
key.json, never in either sheet. key.json must stay private: besides the salt it
holds every item's order_hash, which gives the presentation order away. The selection sort key, swaps, the re-split set,
splits, planted text and roster keep sha256(seed_hex || id).

Subcommands
    select --db PATH --out-dir DIR [--seed-hex HEX8] [--build-commit SHA]
        Draw the stratified sample (10 per similarity bin B1..B4) and write
        claims.json (node_id + claim only, for the author to split), screen.md
        (node_id, quote, claim and interpretation per item, in selection order:
        what the judges will see, for the privacy screen; never shown to judges,
        recorded nowhere in the sheets) and the private key.json. Refuses if
        any of them exists. The seed is pinned to the signed value (SEED_HEX);
        any other --seed-hex is refused.
    swap --db PATH --out-dir DIR --node NODE_ID --reason TEXT
        Privacy screen: replace NODE_ID with the next unselected candidate of
        the same bin's sort order, and refresh screen.md. The reason is stored
        in key.json only. Refused once any sheet (final or DRAFT) exists.
    render --db PATH --out-dir DIR --splits splits.json --planted planted.json
           --scrub-roster roster.json --sheet-salt HEX16 [--draft]
        --sheet-salt is required for every render (--draft included) and must be
        exactly 16 characters of [0-9a-f]; otherwise the render is refused before
        any sheet is written. render prints sheet_salt_sha256=<hex> to stderr, never
        the salt.
        Build sheet-pass1.md and sheet-pass2.md and extend key.json with the
        item map, clause counts, sheet hashes and the scrub regexes. The names
        and handles to scrub are NOT in this file: --scrub-roster is a local
        JSON file {"names": [...], "handles": [...]} that the seat provides and
        never commits (refused if missing, malformed, or either list is empty).
        key.json records the roster file's sha256 and the counts, never the
        names (the key travels to the other seat). Refuses
        to overwrite an existing sheet. --planted must hold exactly four items,
        kinds P1..P4, each with a non-empty quote and clauses; without it the
        render is refused unless --draft is given, which writes
        sheet-pass1.DRAFT.md / sheet-pass2.DRAFT.md instead of the final names. (--db is needed because claims.json
        carries only the claim, while the sheet shows the quote and, in
        pass 2, the interpretation.)

The DB is only ever opened read-only (mode=ro). There is no default DB: the
tool never falls back to the live graph.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import engram_anchoring  # noqa: E402

# The S1 helpers (read-only open, source_class, id mask, embedding meta) are
# reused by import, not copied. Load the S1 file by path (the `tools` package
# name is contested, as in the S1 tool itself).
_spec = importlib.util.spec_from_file_location("_quote_quality_survey_for_s2", _HERE / "quote_quality_survey.py")
_s1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s1)
_open_readonly = _s1._open_readonly
_source_class = _s1._source_class
_model_meta = _s1._model_meta
mask_ids = _s1.mask_ids

TOOL_VERSION = "2"

# The signed seed: first 8 hex digits of the sha256 of the signed text (draft 4).
# Draft 5 pins it to this value; it is unchanged.
SEED_HEX = "d638538e"

# Presentation-order salt (draft 5, change-log entry 14): the five ASCII letters.
ORDER_SALT = "order"
ORDER_KEY_DESCRIPTION = "sha256(seed_hex || 'order' || sheet_salt || id)"

# Pinned by the pre-registration ("Sample"): not a flag.
WINDOW_START = "2026-10-10T15:00:00Z"
WINDOW_END = "2026-10-24T15:00:00Z"  # exclusive
PER_BIN = 10
BIN_LABELS = ["B1", "B2", "B3", "B4"]
# (label, upper bound exclusive); B4 is [0.6, 1] and also takes anything above.
BIN_EDGES = [("B1", 0.2), ("B2", 0.4), ("B3", 0.6)]
PLANTED_KINDS = ("P1", "P2", "P3", "P4")

# Pass-2 scrub, fixed by the pre-registration ("Pass 2"). Handles run before
# names so a handle that contains a name is one [handle], not "[name]-rest".
# The name and handle lists are NOT in this file: the seat supplies them in a
# local roster file (--scrub-roster) that is never committed.
SCRUB_NODE_ID_REGEX = r"[a-z]{2}_\d{4}"
NODE_PLACEHOLDER, NAME_PLACEHOLDER, HANDLE_PLACEHOLDER = "[node]", "[name]", "[handle]"


def _word_regex(words: list[str]) -> str:
    return r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b"


_NODE_RE = re.compile(SCRUB_NODE_ID_REGEX)


class Refusal(Exception):
    """A refusal the user must act on; main() prints it and returns 2."""


class Scrubber:
    """The pass-2 scrub, built from a roster file the seat provides."""

    def __init__(self, names: list[str], handles: list[str], roster_sha256: str):
        self.names, self.handles, self.roster_sha256 = names, handles, roster_sha256
        self.handle_regex = _word_regex(handles)
        self.name_regex = _word_regex(names)
        self._handle_re = re.compile(self.handle_regex, re.I)
        self._name_re = re.compile(self.name_regex, re.I)

    def scrub(self, text: str) -> str:
        text = _NODE_RE.sub(NODE_PLACEHOLDER, text or "")
        text = self._handle_re.sub(HANDLE_PLACEHOLDER, text)
        return self._name_re.sub(NAME_PLACEHOLDER, text)

    def spec(self) -> dict:
        """What key.json records: placeholders, order, roster sha and counts. Never the names."""
        return {
            "node_id_regex": SCRUB_NODE_ID_REGEX,
            "node_id_placeholder": NODE_PLACEHOLDER,
            "names_flags": "IGNORECASE",
            "names_placeholder": NAME_PLACEHOLDER,
            "names_count": len(self.names),
            "handles_flags": "IGNORECASE",
            "handles_placeholder": HANDLE_PLACEHOLDER,
            "handles_count": len(self.handles),
            "roster_sha256": self.roster_sha256,
            "order": ["node_id", "handles", "names"],
        }


def load_scrubber(path: str | None) -> Scrubber:
    if not path:
        raise Refusal("--scrub-roster is required: a local JSON file {\"names\": [...], \"handles\": [...]}")
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise Refusal(f"cannot read the scrub roster {path}: {exc.strerror or exc}") from None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise Refusal(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise Refusal("the scrub roster must be an object {\"names\": [...], \"handles\": [...]}")
    for field in ("names", "handles"):
        v = data.get(field)
        if (not isinstance(v, list) or not v
                or not all(isinstance(w, str) and w.strip() for w in v)):
            raise Refusal(f"the scrub roster's \"{field}\" must be a non-empty list of non-empty strings")
    return Scrubber(data["names"], data["handles"], hashlib.sha256(raw).hexdigest())


# ---------------------------------------------------------------------------
# Selection rule
# ---------------------------------------------------------------------------

def sort_hash(seed_hex: str, ident: str) -> str:
    """sha256(seed_hex || id), hex digest, compared as a string."""
    return hashlib.sha256((seed_hex + ident).encode("utf-8")).hexdigest()


SHEET_SALT_RE = re.compile(r"[0-9a-f]{16}")


def order_hash(seed_hex: str, ident: str, sheet_salt: str) -> str:
    """Presentation key: sha256(seed_hex || ORDER_SALT || sheet_salt || id), hex digest, compared as a string."""
    return hashlib.sha256((seed_hex + ORDER_SALT + sheet_salt + ident).encode("utf-8")).hexdigest()


def _check_sheet_salt(value) -> str:
    # fullmatch, not match: "$" would accept a trailing newline.
    if not isinstance(value, str) or not SHEET_SALT_RE.fullmatch(value):
        raise Refusal("--sheet-salt is required for every render and must be exactly 16 lowercase hex "
                      "characters [0-9a-f] (change-log entry 15; generate with os.urandom(8).hex())")
    return value


def bin_of(sim: float) -> str:
    for label, upper in BIN_EDGES:
        if sim < upper:
            return label  # below 0 falls in B1
    return "B4"  # 0.6 up to and including 1.0


def _parse_utc(text: str | None) -> datetime | None:
    if not text:
        return None
    t = text.strip()
    if t.endswith(("Z", "z")):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # stored timestamps without an offset are UTC
    return dt.astimezone(timezone.utc)


_W0 = _parse_utc(WINDOW_START)
_W1 = _parse_utc(WINDOW_END)


def read_population(db_path: Path, seed_hex: str) -> dict:
    """Read-only. Returns candidates per bin (sorted by the sort rule) plus counts."""
    conn = _open_readonly(db_path)
    try:
        rows = conn.execute(
            "SELECT id, quote_type, claim, quoted_text, interpretation, metadata, created_at "
            "FROM nodes WHERE type = 'observation_factual' AND is_current = 1 ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    bins: dict[str, list[dict]] = {b: [] for b in BIN_LABELS}
    counts = {"user_stated_excluded": 0, "unparsable_created_at": 0, "null_similarity": 0}
    null_reasons: dict[str, int] = {}
    for row in rows:
        created = _parse_utc(row["created_at"])
        if created is None:
            counts["unparsable_created_at"] += 1
            continue
        if not (_W0 <= created < _W1):
            continue
        if _source_class(row["metadata"]) == "user_stated":
            counts["user_stated_excluded"] += 1
            continue
        sim, reason = engram_anchoring.quote_claim_similarity(row["quoted_text"] or "", row["claim"] or "")
        if sim is None:
            counts["null_similarity"] += 1
            null_reasons[reason or "unknown"] = null_reasons.get(reason or "unknown", 0) + 1
            continue
        bins[bin_of(sim)].append({
            "node_id": row["id"], "claim": row["claim"] or "", "quote": row["quoted_text"] or "",
            "interpretation": row["interpretation"] or "", "quote_type": row["quote_type"],
            "similarity": sim, "sort_hash": sort_hash(seed_hex, row["id"]),
        })
    for b in BIN_LABELS:
        bins[b].sort(key=lambda c: c["sort_hash"])
    return {"bins": bins, "counts": counts, "null_reasons": null_reasons}


# ---------------------------------------------------------------------------
# File writers
# ---------------------------------------------------------------------------

def _create_new(path: Path, text: str, mode: int) -> None:
    """Create-only (O_EXCL): never overwrites, never follows a symlink."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    except FileExistsError:
        raise Refusal(f"{path} already exists; refusing to overwrite") from None
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def _rewrite_private(path: Path, text: str) -> None:
    """Rewrite an existing private file (key.json, claims.json) in place, 0600."""
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(fd, "w") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(text)


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise Refusal(f"{path} not found") from None
    except json.JSONDecodeError as exc:
        raise Refusal(f"{path} is not valid JSON: {exc}") from None


def _build_commit(arg: str | None) -> str:
    if arg:
        return arg
    try:
        out = subprocess.run(["git", "-C", str(_HERE), "rev-parse", "HEAD"], capture_output=True,
                             text=True, check=True, timeout=30)
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        raise Refusal("cannot read this checkout's commit; pass --build-commit") from None


# ---------------------------------------------------------------------------
# select / swap
# ---------------------------------------------------------------------------

def cmd_select(args) -> int:
    out_dir = Path(args.out_dir)
    claims_path, key_path = out_dir / "claims.json", out_dir / "key.json"
    screen_path = out_dir / "screen.md"
    for p in (claims_path, key_path, screen_path):
        if os.path.lexists(p):
            raise Refusal(f"{p} already exists; refusing to overwrite")
    commit = _build_commit(args.build_commit)
    pop = read_population(Path(args.db), args.seed_hex)
    items, shortfalls = [], {}
    for b in BIN_LABELS:
        chosen = pop["bins"][b][:PER_BIN]
        if len(chosen) < PER_BIN:
            shortfalls[b] = PER_BIN - len(chosen)
        for c in chosen:
            items.append({"node_id": c["node_id"], "bin": b, "similarity": c["similarity"],
                          "sort_hash": c["sort_hash"], "quote_type": c["quote_type"],
                          "claim": c["claim"], "quote": c["quote"], "interpretation": c["interpretation"]})
    key = {
        "tool_version": TOOL_VERSION,
        "preregistration": "engram-alpha#4050 S2 draft 5, issue comment 6387983396, change-log entry 15, issue comment 6390535527 (seed pinned to draft 4, issue comment 6385972287)",
        "seed_hex": args.seed_hex,
        "seed": int(args.seed_hex, 16),
        "window": [WINDOW_START, WINDOW_END],
        "dev_commit": commit,
        "sheet_build_date_utc": datetime.now(timezone.utc).date().isoformat(),
        "embedding_model": _model_meta(),
        "population_per_bin": {b: len(pop["bins"][b]) for b in BIN_LABELS},
        "null_similarity_count": pop["counts"]["null_similarity"],
        "null_similarity_reasons": pop["null_reasons"],
        "user_stated_excluded_count": pop["counts"]["user_stated_excluded"],
        "unparsable_created_at_count": pop["counts"]["unparsable_created_at"],
        "shortfalls": shortfalls,
        "items": [{k: v for k, v in it.items() if k not in ("claim", "quote", "interpretation")}
                  for it in items],
        "swaps": [],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    _create_new(claims_path, _dump([{"node_id": it["node_id"], "claim": it["claim"]} for it in items]), 0o600)
    _create_new(screen_path, _screen_text(items), 0o600)
    _create_new(key_path, _dump(key), 0o600)
    print(f"selected {len(items)} items; shortfalls={shortfalls}; null_similarity={key['null_similarity_count']}",
          file=sys.stderr)
    print("Read screen.md (quote, claim, interpretation) for private matters before splitting; "
          "swap any that need it.", file=sys.stderr)
    return 0


def cmd_swap(args) -> int:
    out_dir = Path(args.out_dir)
    existing = [n for n in PASS_FILES + DRAFT_FILES if os.path.lexists(out_dir / n)]
    if existing:
        raise Refusal(f"{', '.join(existing)} already exist(s); swap is only allowed before render")
    key = _load_json(out_dir / "key.json")
    claims = _load_json(out_dir / "claims.json")
    entry = next((i for i in key["items"] if i["node_id"] == args.node), None)
    if entry is None:
        raise Refusal(f"{args.node} is not a selected item")
    pop = read_population(Path(args.db), key["seed_hex"])
    b = entry["bin"]
    ordered = pop["bins"][b]
    if not any(c["node_id"] == args.node for c in ordered):
        raise Refusal(f"{args.node} is not in bin {b} of this database any more; wrong --db?")
    taken = {i["node_id"] for i in key["items"]} | {s["removed"] for s in key["swaps"]}
    nxt = next((c for c in ordered if c["node_id"] not in taken), None)
    if nxt is None:
        raise Refusal(f"bin {b} has no unselected candidate left; cannot swap {args.node}")
    pos = key["items"].index(entry)
    key["items"][pos] = {"node_id": nxt["node_id"], "bin": b, "similarity": nxt["similarity"],
                         "sort_hash": nxt["sort_hash"], "quote_type": nxt["quote_type"]}
    key["swaps"].append({"removed": args.node, "added": nxt["node_id"], "bin": b, "reason": args.reason})
    claims = [{"node_id": nxt["node_id"], "claim": nxt["claim"]} if c["node_id"] == args.node else c
              for c in claims]
    by_id = {c["node_id"]: c for bl in pop["bins"].values() for c in bl}
    screen_entries = []
    for it in key["items"]:
        if it["node_id"] not in by_id:
            raise Refusal(f"{it['node_id']} is not in this database any more; wrong --db?")
        screen_entries.append(by_id[it["node_id"]])
    _rewrite_private(out_dir / "screen.md", _screen_text(screen_entries))
    _rewrite_private(out_dir / "claims.json", _dump(claims))
    _rewrite_private(out_dir / "key.json", _dump(key))
    print(f"swapped {args.node} -> {nxt['node_id']} (bin {b}); swaps={len(key['swaps'])}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

PASS_FILES = ("sheet-pass1.md", "sheet-pass2.md")
DRAFT_FILES = ("sheet-pass1.DRAFT.md", "sheet-pass2.DRAFT.md")


def _blockquote(text: str) -> str:
    lines = (text or "").splitlines() or ["(empty)"]
    return "\n".join("> " + ln for ln in lines)


def _screen_text(entries: list[dict]) -> str:
    """The privacy-screen file: what the judges will see (quote, interpretation) plus the claim."""
    parts = ["# Privacy screen (private; never shown to judges)\n\n"
             "Read every item for a human's private matters; swap any that need it.\n\n"]
    for e in entries:
        parts.append(f"## {e['node_id']}\n\n**Quote:**\n{_blockquote(e['quote'])}\n\n"
                     f"**Claim:**\n{_blockquote(e['claim'])}\n\n"
                     f"**Interpretation:**\n{_blockquote(e['interpretation'])}\n\n")
    return "".join(parts)


def _validate_clauses(owner: str, clauses) -> list[str]:
    if (not isinstance(clauses, list) or not clauses
            or not all(isinstance(c, str) and c.strip() for c in clauses)):
        raise Refusal(f"{owner}: clauses must be a non-empty list of non-empty strings")
    return clauses


def _validate_planted(planted) -> None:
    """Exactly four items, kinds exactly {P1, P2, P3, P4}, each with a non-empty quote and clauses."""
    if not isinstance(planted, list) or len(planted) != len(PLANTED_KINDS):
        raise Refusal(f"planted.json must be a list of exactly {len(PLANTED_KINDS)} items (P1..P4)")
    for p in planted:
        if not isinstance(p, dict) or not isinstance(p.get("planted_id"), str) or not p["planted_id"].strip():
            raise Refusal("planted.json: each entry needs a planted_id")
        if not isinstance(p.get("quote"), str) or not p["quote"].strip():
            raise Refusal(f"planted.json: {p['planted_id']} needs a non-empty quote")
        if not isinstance(p.get("interpretation"), str):
            raise Refusal(f"planted.json: {p['planted_id']} lacks interpretation")
        _validate_clauses(p["planted_id"], p.get("clauses"))
    kinds = sorted(p.get("kind") if isinstance(p.get("kind"), str) else "" for p in planted)
    if kinds != sorted(PLANTED_KINDS):
        raise Refusal("planted.json: kinds must be exactly P1, P2, P3, P4 (one each)")


def _load_items(out_dir: Path, db_path: Path, splits: dict, planted: list | None, key: dict) -> list[dict]:
    selected = [i["node_id"] for i in key["items"]]
    missing = [n for n in selected if n not in splits]
    extra = [n for n in splits if n not in set(selected)]
    if missing:
        raise Refusal(f"splits.json lacks a split for selected item(s): {', '.join(missing)}")
    if extra:
        raise Refusal(f"splits.json names unselected node(s): {', '.join(extra)}")
    conn = _open_readonly(db_path)
    try:
        rows = {}
        for n in selected:
            r = conn.execute("SELECT quoted_text, interpretation FROM nodes WHERE id = ?", (n,)).fetchone()
            if r is None:
                raise Refusal(f"{n} not found in {db_path}; wrong --db?")
            rows[n] = r
    finally:
        conn.close()
    items = [{"id": n, "kind": "real", "quote": rows[n]["quoted_text"] or "",
              "interp": rows[n]["interpretation"] or "",
              "clauses": _validate_clauses(n, splits[n])} for n in selected]
    for p in planted or []:
        items.append({"id": p["planted_id"], "kind": p["kind"], "quote": p["quote"],
                      "interp": p["interpretation"],
                      "clauses": _validate_clauses(p["planted_id"], p.get("clauses"))})
    ids = [i["id"] for i in items]
    if len(set(ids)) != len(ids):
        raise Refusal("an id appears twice across real and planted items")
    return items


def _render_item(num: int, it: dict, pass2: bool, scrubber: Scrubber) -> tuple[str, int]:
    quote, masked = mask_ids(it["quote"])
    parts = [f"## Item {num}\n\n**Quote:**\n{_blockquote(quote)}\n\n"]
    if pass2:
        parts.append(f"**Interpretation (the author's own text, not evidence):**\n"
                     f"{_blockquote(scrubber.scrub(it['interp']))}\n\n")
    parts.append("**Clauses:**\n\n")
    for k, clause in enumerate(it["clauses"], 1):
        text, m = mask_ids(clause)
        masked += m
        parts.append(f"{k}. {text}\n   Label: \n   Flag (optional): \n\n")
    return "".join(parts), masked


_NOTES_SENTENCE = ("When you have labelled every clause, you may add a final line `## Notes` at the very end "
                   "and write free notes below it. Use it to note any node that a hook injected into your "
                   "context and that bore on a label. Nothing below that line is read as a label or flag. "
                   "A line like that anywhere before the last clause stops the return from being scored, "
                   "so write notes only at the very end.")
_HEADER1 = ("# Clause-check sheet, pass 1\n\n"
            "For each clause, write one label after `Label:`: CARRIED (the quote by itself states it), "
            "NOT (the quote does not state it), or UNCLEAR. Label from the text shown only. "
            "Label the clauses as given; a free note on a split you think is wrong goes after `Flag`. "
            "Node ids are masked as [id]. " + _NOTES_SENTENCE + "\n\n")
_HEADER2 = ("# Clause-check sheet, pass 2\n\n"
            "Same items and clauses, with the author's interpretation added. You may mark a pass-1 NOT as "
            "NOT, STATED IN INTERPRETATION after `Label:`; the clause stays NOT. Node ids, agent names and "
            "GitHub handles in the interpretation are replaced by placeholders. " + _NOTES_SENTENCE + "\n\n")


def cmd_render(args) -> int:
    sheet_salt = _check_sheet_salt(args.sheet_salt)  # first: refuse before reading or writing anything
    salt_sha256 = hashlib.sha256(sheet_salt.encode("utf-8")).hexdigest()
    out_dir = Path(args.out_dir)
    scrubber = load_scrubber(args.scrub_roster)
    key_path = out_dir / "key.json"
    key = _load_json(key_path)
    splits = _load_json(Path(args.splits))
    if not isinstance(splits, dict):
        raise Refusal("splits.json must be an object {node_id: [clause, ...]}")
    planted = _load_json(Path(args.planted)) if args.planted else None
    if planted is None and not args.draft:
        raise Refusal("--planted is required for the final sheet (pass --draft for a draft without planted controls)")
    if planted is not None:
        _validate_planted(planted)
    names = DRAFT_FILES if args.draft else PASS_FILES
    p1_path, p2_path = out_dir / names[0], out_dir / names[1]
    for p in (p1_path, p2_path):
        if os.path.lexists(p):
            raise Refusal(f"{p} already exists; refusing to overwrite")
    items = _load_items(out_dir, Path(args.db), splits, planted, key)
    seed_hex = key["seed_hex"]
    items.sort(key=lambda i: order_hash(seed_hex, i["id"], sheet_salt))  # presentation order, not the selection key
    text1, text2, item_map, masked_total = [_HEADER1], [_HEADER2], {}, 0
    for num, it in enumerate(items, 1):
        body1, masked = _render_item(num, it, pass2=False, scrubber=scrubber)
        body2, _ = _render_item(num, it, pass2=True, scrubber=scrubber)
        text1.append(body1)
        text2.append(body2)
        masked_total += masked
        entry = {"id": it["id"], "clauses": len(it["clauses"]), "order_hash": order_hash(seed_hex, it["id"], sheet_salt)}
        if it["kind"] != "real":
            entry["kind"] = it["kind"]
        item_map[str(num)] = entry
    s1, s2 = "".join(text1), "".join(text2)
    _create_new(p1_path, s1, 0o666)
    _create_new(p2_path, s2, 0o666)
    key["scrub"] = scrubber.spec()
    key["render_draft" if args.draft else "render"] = {
        "order_key": ORDER_KEY_DESCRIPTION,
        "order_salt": ORDER_SALT,
        "sheet_salt": sheet_salt,
        "sheet_salt_sha256": salt_sha256,
        "items": item_map,
        "masked_id_tokens": masked_total,
        "planted_included": bool(planted),
        "sha256": {names[0]: hashlib.sha256(s1.encode("utf-8")).hexdigest(),
                   names[1]: hashlib.sha256(s2.encode("utf-8")).hexdigest()},
    }
    _rewrite_private(key_path, _dump(key))
    print(f"rendered {len(items)} items; masked_id_tokens={masked_total}", file=sys.stderr)
    print(f"sheet_salt_sha256={salt_sha256}", file=sys.stderr)
    if args.draft:
        print("DRAFT: written under .DRAFT.md names; not the final sheet.", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("--db", required=True)
    s.add_argument("--out-dir", required=True)
    s.add_argument("--seed-hex", default=SEED_HEX)
    s.add_argument("--build-commit", default=None)
    w = sub.add_parser("swap")
    w.add_argument("--db", required=True)
    w.add_argument("--out-dir", required=True)
    w.add_argument("--node", required=True)
    w.add_argument("--reason", required=True)
    r = sub.add_parser("render")
    r.add_argument("--db", required=True)
    r.add_argument("--out-dir", required=True)
    r.add_argument("--splits", required=True)
    r.add_argument("--planted", default=None)
    # Required, but checked in load_scrubber so a missing flag refuses (exit 2) like every other refusal.
    r.add_argument("--scrub-roster", default=None)
    # Required for every render, checked in cmd_render so a missing flag refuses (exit 2) like every other refusal.
    r.add_argument("--sheet-salt", default=None)
    r.add_argument("--draft", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "select" and args.seed_hex != SEED_HEX:
        print(f"refused: --seed-hex is pinned to the signed seed {SEED_HEX}", file=sys.stderr)
        return 2
    try:
        if args.cmd == "select":
            return cmd_select(args)
        if args.cmd == "swap":
            return cmd_swap(args)
        return cmd_render(args)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
