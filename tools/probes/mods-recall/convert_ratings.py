#!/usr/bin/env python3
"""Convert one probe session (step files + ratings) into recall-harness units.

    python3 convert_ratings.py <probeDir>/<sessionId> --out <dir> [--overwrite]

Reads  <session>/steps/*.json, ratings/*.json, requests/*.json  (written by the
engram-probe mod) and writes, under --out:

    test/<turnId>-<index>-<arm>.json   one TEST RankingResultUnit per (step, arm)
    golden/q_t<turnId8>-s<index>.json  one GOLDEN per RATED step (loader shape)
    pairs.jsonl                        unblinded A-vs-B verdicts
    summary.json                       the completeness summary (also printed)

The units are the repo's own (`tools/recall_harness`, reused unchanged).

Completeness contract: the returned / printed summary satisfies
    steps_with_arms == steps_rated + steps_unrated
    test_rrus_written == sum(arms per step-with-arms) == files on disk
or the run exits non-zero naming the mismatch. A step is never silently dropped:
every input is validated BEFORE anything is written, so a malformed file stops
the run with nothing partial left behind.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# good -> 2, noise -> 0, surfaced-but-unmarked -> 1. Module-level so the
# contract test's control can swap a value and watch the mapping test go red.
RELEVANCE = {"good": 2, "noise": 0, "neutral": 1}

VERDICTS = ("better", "same", "worse")

# Ids and arm names become FILE NAMES under --out, so they are validated before
# anything is written. SAFE_ID duplicates hooks/record.ts SAFE_ID, the source of
# truth (the mod only ever writes ids that match it); keep the two in step.
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
KNOWN_ARMS = ("thinking_full", "thinking_tail", "extract_model", "tool_input", "novel_terms")
BLIND_LETTER = re.compile(r"^[A-Z]$")


def _check_turn_id(path: Path, data: dict) -> str:
    turn = data.get("turnId")
    if not isinstance(turn, str) or not SAFE_ID.fullmatch(turn):
        _die(f"convert_ratings: {path.name} field 'turnId' {turn!r} is not a safe id "
             f"(letters, digits, _ and -, 1-100 chars)")
    return turn


def _check_index(path: Path, value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _die(f"convert_ratings: {path.name} field 'index' {value!r} is not an integer >= 0")
    return value


def _check_step(path: Path, data: dict) -> None:
    for arm in (data.get("arms") or {}):
        if arm not in KNOWN_ARMS:
            _die(f"convert_ratings: {path.name} field 'arms' names arm {arm!r}, "
                 f"not one of {KNOWN_ARMS}")
    blind = data.get("armBlind")
    if blind is not None:
        if not isinstance(blind, dict):
            _die(f"convert_ratings: {path.name} field 'armBlind' is not an object")
        for letter, arm in blind.items():
            if not isinstance(letter, str) or not BLIND_LETTER.fullmatch(letter) or arm not in KNOWN_ARMS:
                _die(f"convert_ratings: {path.name} field 'armBlind' maps {letter!r} -> {arm!r}; "
                     f"letters must be A-Z and arms one of {KNOWN_ARMS}")


def _die(message: str):
    raise SystemExit(message)


def _write_private(path: Path, text: str) -> None:
    """Create `path` 0600 (never Path.write_text: that is 0644 under umask 022).
    An existing file is removed first so O_EXCL still guarantees the new mode."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def _check_out_dir(out: Path) -> None:
    """A pre-existing --out must be ours and private (no group/other bits):
    outputs carry every arm query, including arm D's tool-call arguments."""
    if not out.exists():
        return
    st = out.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        _die(f"convert_ratings: --out {out} is mode {oct(st.st_mode & 0o777)} "
             f"(owner uid {st.st_uid}); it must be owned by you with no group/other "
             "access (chmod 700) -- outputs are as sensitive as the step files")


def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as err:
        _die(f"convert_ratings: cannot read {path.name} ({path}): {err}")


def _turn8(turn_id: str) -> str:
    # Alphanumerics only: golden_loader silently skips file names containing
    # "_v", so the id used in q_<turn8>-s<n>.json must not be able to carry it.
    return re.sub(r"[^A-Za-z0-9]", "", turn_id)[:8] or "turn"


def _query_id(turn_id: str, index: int) -> str:
    # Constant "t" prefix: a turn id starting with "v" would otherwise make the
    # golden file "q_v...", which the harness loader treats as an archived
    # version name and skips (convert exited 0, eval exited 1).
    return f"t{_turn8(turn_id)}-s{index}"


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _ms_over_step(ms, step_ms):
    ms, step_ms = _num(ms), _num(step_ms)
    if ms is None or not step_ms:  # missing or 0 -> unknown, never a fake 0
        return None
    return ms / step_ms


def _read_dir(session: Path, sub: str) -> list[tuple[Path, dict]]:
    out = []
    for path in sorted((session / sub).glob("*.json")):
        data = _load_json(path)
        if not isinstance(data, dict):
            _die(f"convert_ratings: {path.name} is not a JSON object")
        out.append((path, data))
    return out


def _arm_status(arm: dict) -> str:
    if "error" in arm:
        return "error"
    if "skipped" in arm:
        return "skipped"
    return "ok"


def convert(session_dir, out_dir, overwrite: bool = False) -> dict:
    session, out = Path(session_dir), Path(out_dir)
    if not session.is_dir():
        _die(f"convert_ratings: session dir not found: {session}")

    # ------------------------------------------------------------ read + validate
    steps: dict[tuple[str, int], dict] = {}
    for path, data in _read_dir(session, "steps"):
        turn = _check_turn_id(path, data)
        index = _check_index(path, data.get("index"))
        _check_step(path, data)
        if (turn, index) in steps:
            _die(f"convert_ratings: {path.name} duplicates turn {turn} step {index}")
        steps[(turn, index)] = data

    ratings: list[tuple[Path, dict]] = _read_dir(session, "ratings")
    for path, data in ratings:
        _check_turn_id(path, data)
    requests = {}
    for path, data in _read_dir(session, "requests"):
        requests[_check_turn_id(path, data)] = data

    turn8_owner: dict[str, str] = {}
    for turn, _ in steps:
        owner = turn8_owner.setdefault(_turn8(turn), turn)
        if owner != turn:
            _die(f"convert_ratings: turn ids {owner} and {turn} share the 8-char prefix "
                 f"{_turn8(turn)}; query ids would collide")

    def has_arms(step: dict) -> bool:
        return isinstance(step.get("arms"), dict) and bool(step["arms"]) and not step.get("skipped")

    # Latest rating wins per (turnId, index): order by (ts, file name).
    def _order(item):
        ts = _num(item[1].get("ts"))
        return (ts if ts is not None else 0, item[0].name)

    rated: dict[tuple[str, int], dict] = {}
    for path, rating in sorted(ratings, key=_order):
        turn = rating.get("turnId")
        if not isinstance(turn, str) or not isinstance(rating.get("steps"), list):
            _die(f"convert_ratings: {path.name} lacks a string turnId and a steps list")
        for entry in rating["steps"]:
            if not isinstance(entry, dict):
                _die(f"convert_ratings: {path.name} has a step entry that is not an object")
            index = _check_index(path, entry.get("index"))
            rated[(turn, index)] = entry

    pairs_rows: list[dict] = []
    ratings_without_step = 0
    rated_with_arms: dict[tuple[str, int], dict] = {}
    for (turn, index), entry in rated.items():
        step = steps.get((turn, index))
        if step is None or not has_arms(step):
            ratings_without_step += 1  # no step-with-arms to score it against
            continue
        if turn not in requests:
            # The rater only ever saw what the request showed: without it the
            # ratings cannot be read against what was shown.
            _die(f"convert_ratings: turn {turn} has a rating (step {index}) but no "
                 f"requests/{turn}.json; a rated turn must have its request")
        good, noise = list(entry.get("good") or []), list(entry.get("noise") or [])
        both = sorted(set(good) & set(noise))
        if both:
            _die(f"convert_ratings: turn {turn} step {index} rates {both} as both good and noise")
        blind = step.get("armBlind") or {}
        for letter, verdict in (entry.get("pair") or {}).items():
            if letter not in blind:
                _die(f"convert_ratings: turn {turn} step {index} pair letter {letter!r} "
                     f"is not in that step's armBlind {sorted(blind)}")
            if verdict not in VERDICTS:
                _die(f"convert_ratings: turn {turn} step {index} pair {letter!r} has "
                     f"verdict {verdict!r}, expected one of {VERDICTS}")
            # the request offers only "A vs B": any other key would be compared
            # against A silently, so refuse it
            if letter not in ("A", "B"):
                _die(f"convert_ratings: turn {turn} step {index} pair letter {letter!r} "
                     "is not A or B (the request only offers A vs B)")
            other = "B" if letter == "A" else "A"
            if other not in blind:
                _die(f"convert_ratings: turn {turn} step {index} pair {letter!r} needs "
                     f"letter {other!r} in armBlind, which has {sorted(blind)}")
            pairs_rows.append({"turnId": turn, "index": index, "armA": blind[letter],
                               "armB": blind[other], "verdict": verdict})
        rated_with_arms[(turn, index)] = {"good": good, "noise": noise}

    steps_with_arms = {k: s for k, s in steps.items() if has_arms(s)}
    expected_rrus = sum(len(s["arms"]) for s in steps_with_arms.values())

    _check_out_dir(out)
    stale = [p for p in [out / "pairs.jsonl", *(out / "test").glob("*.json"),
                         *(out / "golden").glob("*.json")] if p.exists()]
    if stale and not overwrite:
        _die(f"convert_ratings: {out} already holds converter output "
             f"({stale[0].name} and {len(stale) - 1} more); pass --overwrite to replace it")

    # ------------------------------------------------------------------- write
    for p in stale:
        p.unlink()
    os.makedirs(out, mode=0o700, exist_ok=True)
    (out / "test").mkdir(mode=0o700, exist_ok=True)
    (out / "golden").mkdir(mode=0o700, exist_ok=True)

    test_written = 0
    for (turn, index), step in sorted(steps_with_arms.items()):
        blind = step.get("armBlind") or {}
        letter_of = {arm: letter for letter, arm in blind.items()}
        qid = _query_id(turn, index)
        shown_by_letter = ((requests.get(turn) or {}).get("shown") or {}).get(str(index))
        for arm_name, arm in step["arms"].items():
            ms = _num(arm.get("ms"))
            ranked = []
            for position, cand in enumerate(arm.get("candidates") or [], start=1):
                signals = {k: v for k, v in cand.items() if k != "id"}
                ranked.append({"id": cand["id"], "rank": position, "grading": None,
                               "signals": signals, "notes": ""})
            rru = {
                "query_id": qid,
                "query_text": arm.get("query") or "",
                "ranked_nodes": ranked,
                "metadata": {
                    "arm": arm_name,
                    "armLetter": letter_of.get(arm_name),
                    "status": _arm_status(arm),
                    "ms": ms,
                    "msOverStep": _ms_over_step(ms, step.get("stepMs")),
                    "stepMs": _num(step.get("stepMs")),
                    "model": step.get("model"),
                    # which material the arm's query was built from, per step: arms
                    # A/B/C/E differ by step, so pooled scores would mix sources
                    "querySource": step.get("querySource"),
                    "visibleChars": step.get("visibleChars"),
                    "envelope": arm.get("envelope"),
                    "sessionId": step.get("sessionId", session.name),
                    # ids the request showed for this step/arm; None = no request
                    # shown-list for the step (unrated, or rated from the files)
                    "shownIds": (shown_by_letter.get(letter_of.get(arm_name), [])
                                 if isinstance(shown_by_letter, dict) else None),
                },
            }
            for key in ("error", "skipped"):
                if key in arm:
                    rru["metadata"][key] = arm[key]
            _write_private(out / "test" / f"{turn}-{index}-{arm_name}.json", json.dumps(rru))
            test_written += 1

    goldens_written = 0
    for (turn, index), marks in sorted(rated_with_arms.items()):
        step = steps_with_arms[(turn, index)]
        surfaced: list[str] = []
        for arm in step["arms"].values():
            for cand in arm.get("candidates") or []:
                if cand["id"] not in surfaced:
                    surfaced.append(cand["id"])
        shown_by_letter = ((requests.get(turn) or {}).get("shown") or {}).get(str(index))
        # None = the request has no shown-list for this step (omitted from the
        # request, rated from the files): the rater could see everything, so
        # nothing is marked.
        shown_ids = (None if not isinstance(shown_by_letter, dict)
                     else {i for ids in shown_by_letter.values() for i in ids})
        extra = [i for i in marks["good"] + marks["noise"] if i not in surfaced]
        rows = []
        for node_id in surfaced + extra:
            if node_id in marks["good"]:
                rel = RELEVANCE["good"]
            elif node_id in marks["noise"]:
                rel = RELEVANCE["noise"]
            else:
                rel = RELEVANCE["neutral"]
            row = {"id": node_id, "grading": {"relevance": rel}}
            if node_id in extra:
                row["notes"] = "unsurfaced"
            elif (shown_ids is not None and node_id not in shown_ids
                  and node_id not in marks["good"] and node_id not in marks["noise"]):
                # surfaced but never shown and never rated: unrated, not neutral.
                # relevance stays 1 so harness semantics are unchanged by default.
                row["notes"] = "unshown"
            rows.append(row)
        order = {r["id"]: n for n, r in enumerate(rows)}
        rows.sort(key=lambda r: (-r["grading"]["relevance"], order[r["id"]]))
        qid = _query_id(turn, index)
        golden = {
            "query_id": qid,
            "query_text": f"probe step {qid} (agent-rated)",
            "primary_category": "probe",
            "source": "probe-agent-rated",
            "context": {"turnId": turn, "shown": (requests.get(turn) or {}).get("shown", {}).get(str(index))},
            "ranked_candidates": rows,
        }
        _write_private(out / "golden" / f"q_{qid}.json", json.dumps(golden))
        goldens_written += 1

    _write_private(out / "pairs.jsonl", "".join(json.dumps(r) + "\n" for r in pairs_rows))

    summary = {
        "steps_total": len(steps),
        "steps_with_arms": len(steps_with_arms),
        "steps_rated": len(rated_with_arms),
        "steps_unrated": len(steps_with_arms) - len(rated_with_arms),
        "ratings_without_step": ratings_without_step,
        "test_rrus_written": test_written,
        "goldens_written": goldens_written,
    }

    # ------------------------------------------------- completeness (never silent)
    problems = []
    if summary["steps_with_arms"] != summary["steps_rated"] + summary["steps_unrated"]:
        problems.append("steps_with_arms != steps_rated + steps_unrated")
    if test_written != expected_rrus:
        problems.append(f"test_rrus_written {test_written} != sum of arms per step-with-arms {expected_rrus}")
    on_disk = len(list((out / "test").glob("*.json")))
    if on_disk != test_written:
        problems.append(f"{on_disk} test RRU files on disk != test_rrus_written {test_written}")
    golden_on_disk = len(list((out / "golden").glob("*.json")))
    if golden_on_disk != goldens_written or goldens_written != summary["steps_rated"]:
        problems.append(f"goldens: {golden_on_disk} on disk, {goldens_written} written, "
                        f"{summary['steps_rated']} steps rated")
    if problems:
        _die("convert_ratings: completeness check FAILED: " + "; ".join(problems))

    _write_private(out / "summary.json", json.dumps(summary, indent=2))
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session_dir", help="<probeDir>/<sessionId>")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--overwrite", action="store_true",
                        help="replace converter output already in --out")
    args = parser.parse_args(argv)
    summary = convert(args.session_dir, args.out, overwrite=args.overwrite)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
