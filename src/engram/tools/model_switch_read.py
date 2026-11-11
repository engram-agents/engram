#!/usr/bin/env python3
"""tools/model_switch_read.py: the blinding tool for the model-switch read (#4616).

The self-recognition read (skill ``engram-model-switch-read``) asks one agent to label a
mixed packet of its own nodes and decoys, one item at a time, before it learns which items
are its own.  This tool is the blinding step.  It does the seeded draw, the export and the
shuffle itself, so own node ids and texts never pass through the reader before labelling.

    model_switch_read.py draw --seed SEED --decoys DECOYS.json --out DIR [--db PATH] [--allow-short]
    model_switch_read.py unblind --dir DIR --labels LABELS.tsv

``draw`` writes ``DIR/cards.md`` (what the reader labels) and ``DIR/key.json`` (what it must
not open until the labels are written), and prints only counts and the two paths.
``unblind`` refuses until a complete, valid labels file exists, then prints the join and the
skill's decision (``proceed`` / ``stop`` / ``instrument_failed``).

The draw rules are the skill's, verbatim.  The graph is opened read-only and never written.
Python standard library only; no network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# (name, cap), in the skill's order.
STRATA = (
    ("earliest week", 4),
    ("lessons and cornerstones", 4),
    ("feelings", 3),
    ("introspective", 4),
    ("latest week", 3),
    ("anything", 2),
)
EXCLUDED_TYPES = ("evidence", "person")
SEED_NODES = (("axiom", 3), ("definition", 2), ("goal", 1))
DECOY_KEYS = ("claim", "quote", "interpretation")
DECOY_MIN, DECOY_MAX = 3, 10
LABELS = ("mine", "mine-different", "not-recognised")
# Only these columns are ever read; the real table has many more.
_SELECT = "select id, type, claim, created_at, quoted_text, interpretation, is_current, metadata from nodes"


class Refusal(Exception):
    """A loud, one-line stop.  The message never carries node ids or node text."""


# ---------------------------------------------------------------------------
# read-only graph access
# ---------------------------------------------------------------------------


def default_db() -> Path:
    home = os.environ.get("ENGRAM_HOME") or os.path.join(os.path.expanduser("~"), ".engram")
    return Path(home) / "knowledge.db"


def open_readonly(db_path) -> sqlite3.Connection:
    """Open the graph read-only.  The URI is built by Path.as_uri(), never by hand: a hand-built
    ``file:`` string cuts the path at '#' or '?', drops ``mode=ro`` and can create an empty db."""
    p = Path(db_path).resolve()
    if not p.is_file():
        raise Refusal(f"database not found: {db_path}")
    try:
        con = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)
        con.execute("select 1 from nodes limit 1")
    except sqlite3.Error as exc:
        raise Refusal(f"cannot read the graph read-only: {type(exc).__name__}") from None
    return con


def _parse_when(value) -> datetime:
    s = str(value).strip().replace("Z", "+00:00").replace("z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise Refusal("a candidate node has an unparseable created_at") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _source_class(metadata):
    try:
        v = json.loads(metadata)
    except (TypeError, ValueError):
        return None
    return v.get("source_class") if isinstance(v, dict) else None


def load_candidates(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(_SELECT).fetchall()
    nodes = [
        dict(zip(("id", "type", "claim", "created_at", "quote", "interpretation", "is_current", "metadata"), r))
        for r in rows
    ]
    seeds: set = set()
    for typ, n in SEED_NODES:
        of_type = sorted((x for x in nodes if x["type"] == typ), key=lambda x: (str(x["created_at"]), x["id"]))
        seeds.update(x["id"] for x in of_type[:n])
    cands = [
        x for x in nodes
        if x["is_current"] == 1 and x["type"] not in EXCLUDED_TYPES and x["id"] not in seeds
    ]
    for x in cands:
        x["when"] = _parse_when(x["created_at"])
        x["source_class"] = _source_class(x["metadata"])
    return cands


# ---------------------------------------------------------------------------
# the draw
# ---------------------------------------------------------------------------


def sha_key(seed: str, token: str) -> str:
    return hashlib.sha256((seed + ":" + token).encode("utf-8")).hexdigest()


def draw_own(cands: list[dict], seed: str, allow_short: bool):
    """Walk the strata in order; returns (own nodes taken, per-stratum records)."""
    if not cands:
        raise Refusal("no own candidates: the graph holds no current node that may be drawn")
    lo = min(x["when"] for x in cands)
    hi = max(x["when"] for x in cands)
    week = timedelta(days=7)
    rules = (
        lambda x: x["when"] < lo + week,
        lambda x: x["type"] in ("lesson", "cornerstone"),
        lambda x: x["type"] == "feeling_report",
        lambda x: x["source_class"] == "introspective",
        lambda x: x["when"] > hi - week,
        lambda x: True,
    )
    taken: list[dict] = []
    taken_ids: set = set()
    records = []
    for (name, cap), rule in zip(STRATA, rules):
        pool = sorted((x for x in cands if rule(x)), key=lambda x: sha_key(seed, x["id"]))
        got = []
        for x in pool:
            if len(got) >= cap:
                break
            if x["id"] in taken_ids:
                continue
            got.append(x)
            taken_ids.add(x["id"])
        taken.extend(got)
        records.append({"name": name, "cap": cap, "candidates": len(pool), "drawn": len(got), "short": len(got) < cap})
    shorts = [r["name"] for r in records if r["short"]]
    if shorts and not allow_short:
        raise Refusal("short stratum, nothing written (use --allow-short to take it as it is): " + ", ".join(shorts))
    return taken, records


# ---------------------------------------------------------------------------
# decoys and cards
# ---------------------------------------------------------------------------


def load_decoys(path) -> list[dict]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise Refusal(f"decoys file unreadable: {path}") from None
    try:
        items = json.loads(raw)
    except ValueError:
        raise Refusal(f"decoys file is not valid JSON: {path}") from None
    if not isinstance(items, list):
        raise Refusal("decoys file must hold a JSON list of objects")
    if not DECOY_MIN <= len(items) <= DECOY_MAX:
        raise Refusal(f"decoys file holds {len(items)} entries; between {DECOY_MIN} and {DECOY_MAX} are needed")
    for n, e in enumerate(items, 1):
        if not isinstance(e, dict):
            raise Refusal(f"decoy entry {n} is not an object")
        missing = [k for k in DECOY_KEYS if k not in e]
        if missing:
            raise Refusal(f"decoy entry {n} is missing key {missing[0]!r}")
        extra = sorted(set(e) - set(DECOY_KEYS))
        if extra:
            raise Refusal(f"decoy entry {n} has an unexpected key")
        for k in DECOY_KEYS:
            if not isinstance(e[k], str):
                raise Refusal(f"decoy entry {n}: {k!r} must be a string")
    return items


def one_line(value) -> str:
    """Single-line a field so it cannot forge a card header; empty renders as (none)."""
    text = " ".join(("" if value is None else str(value)).splitlines()).strip()
    return text or "(none)"


def render_card(n: int, claim, quote, interpretation) -> str:
    return (
        f"## item-{n:02d}\n"
        f"claim: {one_line(claim)}\n"
        f"quote: {one_line(quote)}\n"
        f"interpretation: {one_line(interpretation)}\n"
    )


def cmd_draw(a) -> int:
    out = Path(a.out)
    cards_p, key_p = out / "cards.md", out / "key.json"
    for p in (cards_p, key_p):
        if p.exists():
            raise Refusal(f"refusing to overwrite an existing file: {p}")
    decoys = load_decoys(a.decoys)
    db = Path(a.db) if a.db else default_db()
    con = open_readonly(db)
    try:
        cands = load_candidates(con)
    finally:
        con.close()
    own, records = draw_own(cands, a.seed, a.allow_short)

    entries = []  # (token, source info, (claim, quote, interpretation))
    for x in own:
        entries.append((x["id"], {"source": "own", "id": x["id"]}, (x["claim"], x["quote"], x["interpretation"])))
    for n, d in enumerate(decoys, 1):
        entries.append((f"decoy-{n}", {"source": "decoy", "index": n}, (d["claim"], d["quote"], d["interpretation"])))
    entries.sort(key=lambda e: sha_key(a.seed, e[0]))

    cards = "\n".join(render_card(n, *e[2]) for n, e in enumerate(entries, 1))
    cards_bytes = cards.encode("utf-8")
    key = {
        "seed": a.seed,
        "allow_short": bool(a.allow_short),
        "strata": records,
        "cards_sha256": hashlib.sha256(cards_bytes).hexdigest(),
        "items": {f"item-{n:02d}": e[1] for n, e in enumerate(entries, 1)},
    }
    out.mkdir(parents=True, exist_ok=True)
    try:
        with open(cards_p, "xb") as f:
            f.write(cards_bytes)
        with open(key_p, "x", encoding="utf-8") as f:
            json.dump(key, f, indent=2)
            f.write("\n")
    except OSError as exc:
        raise Refusal(f"could not write the output files: {type(exc).__name__}") from None

    print(f"items: {len(entries)} (own {len(own)}, decoys {len(decoys)})")
    for r in records:
        print(f"stratum {r['name']}: drawn {r['drawn']} of cap {r['cap']}" + (" (short, taken as it is)" if r["short"] else ""))
    print(f"cards: {cards_p}")
    print(f"key: {key_p}  (do not open before the labels are written)")
    return 0


# ---------------------------------------------------------------------------
# unblind
# ---------------------------------------------------------------------------


def read_labels(path, items: dict) -> tuple[dict, str]:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise Refusal(f"labels file not found or unreadable: {path}") from None
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        raise Refusal("labels file is not valid UTF-8") from None
    labels: dict = {}
    for ln, line in enumerate(lines, 1):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) not in (2, 3):
            raise Refusal(f"labels line {ln}: expected item-NN<TAB>label[<TAB>note]")
        item, label = parts[0], parts[1]
        if item not in items:
            raise Refusal(f"labels line {ln}: unknown item {item!r}")
        if label not in LABELS:
            raise Refusal(f"labels line {ln}: label must be one of {', '.join(LABELS)}")
        if item in labels:
            raise Refusal(f"labels line {ln}: duplicate item {item}")
        labels[item] = label
    missing = [i for i in items if i not in labels]
    if missing:
        raise Refusal(f"labels file has no label for {len(missing)} item(s), first {missing[0]}")
    return labels, hashlib.sha256(raw).hexdigest()


def cmd_unblind(a) -> int:
    d = Path(a.dir)
    try:
        key = json.loads((d / "key.json").read_text(encoding="utf-8"))
        items = key["items"]
        expect = key["cards_sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        raise Refusal(f"no usable key.json in {d}") from None
    # The labels come first: nothing about the key is printed until they are complete and valid.
    labels, labels_sha = read_labels(a.labels, items)
    try:
        actual = hashlib.sha256((d / "cards.md").read_bytes()).hexdigest()
    except OSError:
        raise Refusal(f"no cards.md in {d}") from None
    if actual != expect:
        raise Refusal("cards.md no longer matches the draw (its sha256 differs from the key's)")

    own = {l: 0 for l in LABELS}
    rejected = accepted = 0
    rows = []
    for item, info in items.items():
        label = labels[item]
        row = {"item": item, "label": label, "source": info["source"]}
        if info["source"] == "own":
            row["id"] = info["id"]
            own[label] += 1
        else:
            if label == "not-recognised":
                rejected += 1
            else:
                accepted += 1
        rows.append(row)
    if accepted:
        decision = "instrument_failed"
    elif own["not-recognised"]:
        decision = "stop"
    else:
        decision = "proceed"
    json.dump(
        {
            "labels_sha256": labels_sha,
            "seed": key.get("seed"),
            "items": rows,
            "counts": {"own": own, "decoys": {"rejected": rejected, "labelled_mine": accepted}},
            "decision": decision,
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Blinding tool for the model-switch read (#4616).")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draw", help="draw own nodes, mix with decoys, write cards.md and key.json")
    d.add_argument("--seed", required=True)
    d.add_argument("--decoys", required=True, help="JSON list of {claim, quote, interpretation}")
    d.add_argument("--out", required=True)
    d.add_argument("--db", help="default: $ENGRAM_HOME/knowledge.db")
    d.add_argument("--allow-short", action="store_true", help="take a short stratum as it is")
    u = sub.add_parser("unblind", help="join the labels to the key and decide")
    u.add_argument("--dir", required=True)
    u.add_argument("--labels", required=True)
    a = p.parse_args(argv)
    try:
        return cmd_draw(a) if a.cmd == "draw" else cmd_unblind(a)
    except Refusal as exc:
        print(f"model_switch_read: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
