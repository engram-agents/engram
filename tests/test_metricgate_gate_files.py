"""Contract tests for the five pre-registered A6 gate files tools/gates/a6-<hook>.json (A3 of the metricgate
adoption PR). Written BEFORE the files existed.

Each file is the OUTPUT of metricgate's `preregister` (so it carries registered_at, metricgate_version and
gate_sha256) with the values the spec fixes. The hash is recomputed here by re-implementing the six-line rule
(canonical JSON of the gate without its gate_sha256, sha256), so CI can run these cells without the library.
The one cell that asks the library to re-validate a committed file uses importorskip.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATES_DIR = REPO_ROOT / "tools" / "gates"
HOOKS = ("user-identity", "stop", "tripwire", "session-start", "surface")

FIXED = {
    "metric": "sequential p50 per run (ms), owner path vs baseline path",
    "unit": "ms",
    "population": ("installed layout, real-store copy, one (SQLite build, exclusion setting) cell per JSONL "
                   "pair; the cell is named in the JSONL file name"),
    "design": "paired interleaved baseline/owner per iteration inside one run; runs are the unit",
    "unit_of_analysis": "run",
    "n_planned": 6,
    "margin": 0.03,
    "block_bound": 0.10,
    "scale": "log_ratio",
    "direction": "lower_is_better",
    "paired": True,
    "alpha": 0.05,
    "deciders": {"trade_off": "the lane's design owner (Borges for EngramManager); Lei informed"},
}


def _recompute_hash(gate: dict) -> str:
    body = {k: v for k, v in gate.items() if k != "gate_sha256"}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _load(hook: str) -> dict:
    return json.loads((GATES_DIR / f"a6-{hook}.json").read_text(encoding="utf-8"))


def test_exactly_the_five_gate_files_exist():
    """Mutation: drop a hook's file, or add a stray one -> red."""
    assert sorted(p.name for p in GATES_DIR.glob("*.json")) == sorted(f"a6-{h}.json" for h in HOOKS)


@pytest.mark.parametrize("hook", HOOKS)
def test_the_fixed_fields_hold_their_registered_values(hook):
    """Mutation: change the margin, the block bound, n_planned, the scale, pairing or the unit of analysis in
    any file -> red."""
    gate = _load(hook)
    for key, want in FIXED.items():
        assert gate[key] == want, (hook, key, gate[key])
    assert gate["instrument"] == f"tools/engramd_a6_latency.py --hook {hook} --n 300"


@pytest.mark.parametrize("hook", HOOKS)
def test_registration_stamps_are_present_and_there_is_no_look_plan(hook):
    """A fixed-n gate: no look_plan. Mutation: add a look_plan -> red."""
    gate = _load(hook)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", gate["registered_at"])
    assert gate["metricgate_version"] == "0.1.0"
    assert re.fullmatch(r"[0-9a-f]{7,40}", gate["instrument_version"]), gate["instrument_version"]
    assert "look_plan" not in gate


@pytest.mark.parametrize("hook", HOOKS)
def test_verdict_rule_states_the_b1_bands_and_the_absolute_rider(hook):
    """Mutation: drop the 0.5 ms guardrail sentence, or restate the bands with other numbers -> red."""
    rule = _load(hook)["verdict_rule"]
    assert "PASS" in rule and "1.03" in rule and "90%" in rule
    assert "TRADE_OFF" in rule and "10%" in rule and "BLOCK" in rule
    assert "0.5 ms" in rule and "design owner" in rule


@pytest.mark.parametrize("hook", HOOKS)
def test_gate_sha256_equals_the_recomputation(hook):
    """Mutation: edit any field after registration without re-registering -> the stored hash no longer
    matches -> red."""
    gate = _load(hook)
    assert gate["gate_sha256"] == _recompute_hash(gate)


def test_the_hash_check_discriminates():
    """The recomputation is sensitive: one edited field changes it (guards a vacuous always-equal helper)."""
    gate = _load("stop")
    edited = dict(gate, margin=0.05)
    assert _recompute_hash(edited) != gate["gate_sha256"]


def test_the_five_gates_have_distinct_hashes():
    hashes = {_load(h)["gate_sha256"] for h in HOOKS}
    assert len(hashes) == 5


@pytest.mark.parametrize("hook", HOOKS)
def test_the_library_accepts_the_committed_file_as_a_spec(hook):
    """Re-registering the committed gate through the library validates every R1 field (a file that would be
    refused is not a gate)."""
    mg = pytest.importorskip("metricgate")
    gate = _load(hook)
    spec = {k: v for k, v in gate.items() if k not in ("registered_at", "metricgate_version", "gate_sha256")}
    out = mg.preregister(spec)
    assert out["metricgate_version"] == gate["metricgate_version"]
