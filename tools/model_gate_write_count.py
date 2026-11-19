#!/usr/bin/env python3
"""Count ENGRAM write calls per dream-master run on this seat (#4596 latency line).

The model-family gate adds a wait to every ENGRAM WRITE tool call (not per node: a batch tool that
writes many nodes is one call). A sleep cycle's added time is therefore (write calls in the cycle) x
(added time per write). This script reads this seat's Claude Code transcripts, finds every sub-agent
run of the dream master (``subagents/agent-<id>.meta.json`` whose agentType names
``engram-dream-master``), and counts that run's tool calls whose name is in the gate's generated
write list (both MCP prefixes; exact names, so ``engram_focus_sets`` is never ``engram_focus``).

    python3 tools/model_gate_write_count.py [--projects ~/.claude/projects] [--per-write-s 0.2]

Read-only. Prints one row per run (date, write calls, models seen), then n, median and max, and the
estimated added time per cycle at ``--per-write-s``. The number is this seat's; say so when quoting it.
"""
import argparse
import datetime as dt
import glob
import json
import os
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_LIST = REPO / "src" / "engram" / "model_gate_write_tools.json"


def write_names(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    names = set()
    for item in data.get("write_tools", []):
        if isinstance(item, str) and item:
            names.add(item.split("__")[-1])
    if not names:
        sys.exit(f"model_gate_write_count: no write_tools in {path}")
    return names


def runs(projects, names):
    out = []
    for meta in glob.glob(os.path.join(projects, "*", "*", "subagents", "*.meta.json")):
        try:
            kind = json.loads(Path(meta).read_text(encoding="utf-8")).get("agentType") or ""
        except (OSError, ValueError):
            continue
        if "engram-dream-master" not in kind:
            continue
        transcript = meta[: -len(".meta.json")] + ".jsonl"
        if not os.path.isfile(transcript):
            continue
        calls, models = 0, set()
        with open(transcript, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") != "assistant":
                    continue
                msg = row.get("message") or {}
                if msg.get("model"):
                    models.add(msg["model"])
                for block in msg.get("content") or []:
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and str(block.get("name", "")).split("__")[-1] in names):
                        calls += 1
        out.append((os.path.getmtime(transcript), calls, sorted(models)))
    return sorted(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--write-list", default=str(DEFAULT_LIST))
    ap.add_argument("--per-write-s", type=float, default=0.2)
    a = ap.parse_args(argv)
    rows = runs(a.projects, write_names(a.write_list))
    if not rows:
        print("no dream-master runs found under", a.projects)
        return 0
    for mtime, calls, models in rows:
        when = dt.datetime.fromtimestamp(mtime, dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")
        print(f"{when}  write calls {calls:3d}  models {','.join(models) or '-'}")
    counts = [c for _, c, _ in rows]
    med = statistics.median(counts)
    print(f"runs {len(counts)}; write calls per run: median {med}, max {max(counts)}")
    print(f"added time per cycle at {a.per_write_s} s per write: about {med * a.per_write_s:.1f} s "
          f"(median run), at most about {max(counts) * a.per_write_s:.1f} s, on this seat only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
