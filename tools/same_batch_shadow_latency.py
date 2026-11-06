#!/usr/bin/env python3
"""#4390 phase 1 latency measurement: one lesson-tripwire hook fire, detector ON vs OFF.

A measurement owed in the PR body, not a CI gate. Gate files: docs/internal/gates/same-batch-shadow-{p50,p99}.json.
Times the hook as a subprocess, 100 fires per phase, ON and OFF alternating, order flipped each run; the run
is the unit (METRIC-GATE-POLICY R7). --aa runs OFF against OFF, the instrument's control.

The transcript is a frozen COPY with one synthetic flagged block appended, so every ON fire takes the
worst path (tail read, flagged block, lock, seen read). A ledger row afterwards is the arm's positive proof;
without one the run exits 2 and there is no verdict. Nothing here touches a live home.
"""
import argparse, json, os, platform, shutil, statistics, subprocess, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tools.metricgate_adapter import append_run_record  # noqa: E402

HOOK = REPO / "src/engram/hooks/claude/engram-lesson-tripwire-hook.py"
FLAG_MID = "msg_latency_fixture"


def fixture_lines():
    uses = [{"type": "tool_use", "id": "toolu_fx1", "name": "mcp__plugin_umwelt_umwelt__send_crossing",
             "input": {"payload_json": json.dumps({"body": "the node is filed"})}},
            {"type": "tool_use", "id": "toolu_fx2", "name": "mcp__plugin_engram_engram__engram_add_observation",
             "input": {"payload_json": "{}"}}]
    res = [{"type": "tool_result", "tool_use_id": u["id"], "content": "ok"} for u in uses]
    return [json.dumps({"type": "assistant", "message": {"id": FLAG_MID, "role": "assistant", "content": uses}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": res}})]


def set_config(home, enabled):
    (home / "config.json").write_text(json.dumps({"same_batch_shadow": {"enabled": enabled}}))


def fire(home, payload):
    env = dict(os.environ, ENGRAM_HOME=str(home))
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    t0 = time.perf_counter()
    p = subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True, text=True, env=env)
    dt = (time.perf_counter() - t0) * 1000
    if p.returncode != 0:
        sys.exit(f"hook rc {p.returncode}: {p.stderr[:200]}")
    return dt


def pct(xs, q):
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--db", required=True, help="a knowledge.db to copy into the scratch home")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cell", required=True)
    ap.add_argument("--runs", type=int, default=8)
    ap.add_argument("--fires", type=int, default=100)
    ap.add_argument("--aa", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sbs-lat-") as td:
        home = Path(td) / "home"
        home.mkdir()
        shutil.copy(a.db, home / "knowledge.db")
        tr = Path(td) / "transcript.jsonl"
        shutil.copy(a.transcript, tr)
        with open(tr, "a", encoding="utf-8") as f:
            f.write("\n".join(fixture_lines()) + "\n")
        payload = json.dumps({"session_id": "latency", "transcript_path": str(tr), "tool_name": "Bash",
                              "tool_use_id": "toolu_x", "tool_input": {"command": "echo latency"},
                              "hook_event_name": "PreToolUse", "cwd": td})
        arms = ("off1", "off2") if a.aa else ("on", "off")
        kind = "aa" if a.aa else "cell"
        for r in range(a.runs):
            order = arms if r % 2 == 0 else arms[::-1]
            times = {}
            for arm in order:
                set_config(home, arm == "on")
                times[arm] = [fire(home, payload) for _ in range(a.fires)]
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            load = f"{os.getloadavg()[0]:.2f}"
            for arm in arms:
                for name, stat in (("p50", pct(times[arm], .5)), ("p99", pct(times[arm], .99))):
                    append_run_record(out / f"{a.cell}-{kind}-{arm}-{name}.jsonl", run_id=f"{a.cell}-{kind}-r{r}",
                                      ts=ts, stat=stat, host=platform.node(), build=HOOK.name, load=load)
            print(r, {k: (round(statistics.median(v), 2), round(pct(v, .99), 2)) for k, v in times.items()}, load, flush=True)
        led = home / "same-batch-shadow-ledger.jsonl"
        rows = led.read_text().splitlines() if led.exists() else []
        print("ledger rows", len(rows), flush=True)
        if not a.aa and not any(FLAG_MID in x for x in rows):
            sys.exit("positive arm failed: the ON arm wrote no fixture row")
        if a.aa and rows:
            sys.exit("A/A wrote rows: the OFF arm is not off")


if __name__ == "__main__":
    main()
