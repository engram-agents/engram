"""The ONE place engram-alpha imports the `metricgate` library (dev tier, T3).

metricgate (engram-agents/metricgate, tag v0.1.0) is a separate library that judges a metric gate as an
interval against a pre-registered band. engram-alpha's dev-tier measurement tools (the A6 latency tool, the
lock-wait report) reach it only through this module, so the library stays an optional dev dependency: it is
NOT in requirements/requirements.txt and NOT in the plugin venv. A tool that needs a verdict calls
`require_metricgate()` (or `judge_cell`, which does), and a seat without the library gets a one-line message
naming the install line instead of a traceback.

Contract (pinned by tests/test_metricgate_adapter.py):

  * have_metricgate() -> bool                 the import succeeds (checked at call time, never cached)
  * require_metricgate() -> module            or raises MetricgateMissing(RuntimeError)
  * append_run_record(path, *, run_id, ts, stat, host, build, load)
                                              one JSON object per line in metricgate's run-record shape;
                                              pure file I/O, no metricgate import; refuses a non-finite stat
  * append_attempt(path, *, run_id, ts, emitted, reason, host, build)
                                              one line of the cell's attempts ledger (pure file I/O)
  * read_attempts(path) -> list[dict]         the ledger; a malformed line is a ValueError naming the line
  * judge_cell(gate_path, a_path, b_path, attempts_path=None) -> (verdict_name, markdown_report)
                                              through read_runs_jsonl, judge and report; a gate missing a
                                              required key is a ValueError naming the path and the key. With
                                              an attempts ledger: any `contract_violation` attempt makes the
                                              cell NO_VERDICT (`contract_violation_in_cell`), and every other
                                              non-emitted attempt is one tagged line under the report

No other file under src/ or tools/ may import `metricgate` directly (a test greps for it).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional, Tuple

INSTALL_LINE = 'pip install "metricgate @ git+ssh://git@github.com/engram-agents/metricgate@v0.1.0"'
MISSING_MESSAGE = (f"metricgate is not installed, so the tool's gate verdict is unavailable; install it with: "
                   f"{INSTALL_LINE}")


class MetricgateMissing(RuntimeError):
    """The metricgate library is not importable here, so a gate verdict is unavailable."""


def have_metricgate() -> bool:
    """True when `import metricgate` succeeds. Checked on every call: a seat can install the library
    between runs, and a test can hide it."""
    try:
        import metricgate  # noqa: F401
    except ImportError:
        return False
    return True


def require_metricgate():
    """The metricgate module, or MetricgateMissing naming the install line (one line, no traceback)."""
    try:
        import metricgate
    except ImportError:
        raise MetricgateMissing(MISSING_MESSAGE) from None
    return metricgate


def append_run_record(path: Path, *, run_id: str, ts: str, stat: float, host: str, build: str,
                      load: str) -> None:
    """Append one run record (one JSON object on one line) to `path`, creating the file.

    `ts` is an ISO-8601 UTC string; `stat` is the run's statistic (here a sequential p50 in ms) and must be
    a finite real number: a NaN or an infinity is refused (ValueError) before the file is touched, because
    json would write it as a token no reader accepts. Pure file I/O; this does not import metricgate."""
    if isinstance(stat, bool) or not isinstance(stat, (int, float)) or not math.isfinite(stat):
        raise ValueError(f"stat must be a finite number, got {stat!r}")
    record = {"run_id": run_id, "ts": ts, "stat": stat, "host": host, "build": build, "load": load}
    line = json.dumps(record, ensure_ascii=True, allow_nan=False)
    with open(Path(path), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


ATTEMPT_KEYS = ("run_id", "ts", "emitted", "reason", "host", "build")
CONTRACT_VIOLATION = "contract_violation"
NO_VERDICT_REASON = "contract_violation_in_cell"


def append_attempt(path: Path, *, run_id: str, ts: str, emitted: bool, reason: Optional[str], host: str,
                   build: str) -> None:
    """Append one line to a cell's attempts ledger: every invocation that reached the epilogue (or was killed by
    the layout timeout) is one line, whether or not its run records were emitted. `reason` is None for an
    emitted run, else why not (`contract_violation: <first violation>`, `source_layout`, ...). Pure file I/O;
    an OSError (an unwritable ledger) propagates so the caller can refuse to emit."""
    record = {"run_id": run_id, "ts": ts, "emitted": bool(emitted), "reason": reason, "host": host,
              "build": build}
    line = json.dumps(record, ensure_ascii=True, allow_nan=False)
    with open(Path(path), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_attempts(path: Path) -> list:
    """The attempts ledger as a list of dicts, in file order. A line that is not a JSON object carrying the six
    keys is a ValueError naming the line (a skipped line would be a dropped attempt); blank lines are ignored."""
    out = []
    text = Path(path).read_text(encoding="utf-8")
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            raise ValueError(f"attempts ledger {path}: line {number} is not JSON") from None
        if not isinstance(rec, dict) or any(k not in rec for k in ATTEMPT_KEYS):
            raise ValueError(f"attempts ledger {path}: line {number} is not an object with "
                             f"{', '.join(ATTEMPT_KEYS)}")
        out.append(rec)
    return out


def contract_violations(attempts: list) -> list:
    """The attempts whose reason starts with `contract_violation`."""
    return [a for a in attempts if str(a.get("reason") or "").startswith(CONTRACT_VIOLATION)]


def _no_verdict_report(gate_path: Path, bad: list) -> str:
    """The one-row markdown table of a cell that cannot be judged because a run on it broke its contract."""
    gate = json.loads(Path(gate_path).read_text(encoding="utf-8"))
    ids = ", ".join(str(a["run_id"]) for a in bad)
    verdict = f"no verdict ({NO_VERDICT_REASON}; run_id: {ids})"
    cells = [("Metric", gate.get("metric", "n/a")), ("Population", gate.get("population", "n/a")),
             ("Verdict", verdict), ("Gate hash", gate.get("gate_sha256", "n/a"))]
    return "\n".join(["| " + " | ".join(k for k, _ in cells) + " |",
                      "|" + "|".join("---" for _ in cells) + "|",
                      "| " + " | ".join(str(v) for _, v in cells) + " |"])


def judge_cell(gate_path: Path, a_path: Path, b_path: Path,
               attempts_path: Optional[Path] = None) -> Tuple[str, str]:
    """Read one cell against its pre-registered gate: -> (verdict_name, the R12 table as markdown).

    `a_path` and `b_path` are the two arms' run-record files (here baseline and owner); the gate's `paired`
    field decides how the library pairs them. Raises MetricgateMissing when the library is absent, and
    ValueError("gate <path> is missing <key>") when the gate lacks a required key.

    `attempts_path` is the cell's attempts ledger. A contract break is the cell's verdict, not a missing
    sample: when ANY attempt's reason starts with `contract_violation` the answer is ("NO_VERDICT", a table
    whose Verdict column says `contract_violation_in_cell` and lists the run_ids), decided from the ledger alone
    (no record file and no library needed). Otherwise the cell is judged as before and one line per
    non-emitted attempt is appended: `attempt <run_id> at <ts>: not emitted (<reason>)`."""
    attempts = read_attempts(attempts_path) if attempts_path is not None else []
    bad = contract_violations(attempts)
    if bad:
        return "NO_VERDICT", _no_verdict_report(gate_path, bad)
    mg = require_metricgate()
    gate = json.loads(Path(gate_path).read_text(encoding="utf-8"))
    runs_a, runs_b = mg.read_runs_jsonl(a_path), mg.read_runs_jsonl(b_path)
    try:
        judgement = mg.judge(gate, runs_a, runs_b)
        markdown, _table = mg.report(gate, judgement)
    except KeyError as exc:   # a gate dict without a required key: name it, so the caller prints one line
        raise ValueError(f"gate {gate_path} is missing {exc.args[0]}") from None
    tagged = [f"attempt {a['run_id']} at {a['ts']}: not emitted ({a['reason']})"
              for a in attempts if not a.get("emitted")]
    if tagged:
        markdown = markdown + "\n\n" + "\n".join(tagged)
    return judgement.verdict.name, markdown
