#!/usr/bin/env python3
"""Request a plugin compaction at the nap boundary (part of #3640).

Half A of agent-decided compaction. The engram-nap skill calls this as its
LAST step, after the checkpoint, the warm-briefing sync and the focus rotation.
It assembles the post-compaction context deterministically and leaves three
files under ``$ENGRAM_HOME/compact/``:

    context.md       the text the compaction will carry
    context.sha256   its sha256 (hex)
    request.json     {requested_at, session_id, loop, context_sha256}; written LAST,
                     because its presence is what the mod (tools/mods/engram-compaction)
                     acts on. A kill before that rename leaves no request.

Usage:
    compact-request.py --clear [--session <id>]     (a declined nap: remove any pending request, exit 0)
    compact-request.py --session <id> --backup-ok true|false
                       [--checkpoint-at <ISO>] [--wake-armed-at HH:MMZ] [--wake-for HH:MMZ]

Refusals (exit 3, one stderr line naming the check, nothing written):
    * --backup-ok false (the checkpoint's backup verdict)
    * the warm briefing fails any of: exactly one ``^## From this session`` anchor,
      a non-empty arc below it, a file mtime at or after the last checkpoint
Exit codes: 0 written, 2 usage, 3 refused, 4 could not assemble or write (nothing written).

Any older request.json is removed first, whatever the outcome, so a stale
request can never fire beside a newer (or refused) one. The context never
carries the loop marker's ``loop_prompt`` or any ``loop-wake`` text: a literal
wake token in a post-compaction message would be misread as a wake.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOK_NAME = "engram-session-start-hook.py"
# One compaction must stay one compaction: a context past this many UTF-8 bytes is refused.
MAX_CONTEXT_BYTES = 60_000

_ANCHOR_RE = re.compile(r"^## From this session", re.MULTILINE)
# The server's own checkpoint-header rule (a nap or a sleep checkpoint).
_CHECKPOINT_HEADER_RE = re.compile(r"^## (?:Nap \(turn \d+\)|Turn \d+) — (\S+)", re.MULTILINE)
_LOOP_WAKE_RE = re.compile(r"loop[-_]?wake", re.IGNORECASE)
_LOOP_FIELDS = ("kind", "pacer", "cadence_seconds", "activated")


class Refused(Exception):
    """A rider check failed (exit 3)."""


class CouldNotAssemble(Exception):
    """Nothing could be built or written (exit 4)."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_hook(tool_dir: Path) -> Path | None:
    """The SessionStart hook file, in the deployed (hooks/) or the source (hooks/claude/) layout."""
    base = Path(tool_dir).parent
    for cand in (base / "hooks" / HOOK_NAME, base / "hooks" / "claude" / HOOK_NAME):
        if cand.is_file():
            return cand
    return None


def load_builders(engram_home: str):
    """Import the SessionStart hook module (its filename has dashes) and return it.

    Importing prints nothing and writes nothing; stdout is captured during the
    import anyway and forwarded to stderr, so a regression there cannot corrupt
    this tool's output.
    """
    hook = find_hook(Path(__file__).resolve().parent)
    if hook is None:
        raise CouldNotAssemble(f"cannot find {HOOK_NAME} beside this tool (hooks/ or hooks/claude/)")
    os.environ["ENGRAM_HOME"] = engram_home
    hook_dir = str(hook.parent)
    if hook_dir not in sys.path:
        sys.path.insert(0, hook_dir)
    spec = importlib.util.spec_from_file_location("engram_session_start_hook_for_compact", hook)
    if spec is None or spec.loader is None:
        raise CouldNotAssemble(f"cannot load {hook}")
    module = importlib.util.module_from_spec(spec)
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - any import failure means no request
        raise CouldNotAssemble(f"cannot import {hook.name}: {exc}") from exc
    if captured.getvalue():
        sys.stderr.write(captured.getvalue())
    return module


def _parse_ts(text: str) -> float | None:
    s = text.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # the log's timestamps are naive but UTC
    return dt.timestamp()


def _checkpoint_epoch(home: Path, override: str | None) -> float | None:
    if override:
        return _parse_ts(override)
    try:
        content = (home / "session_log.md").read_text(encoding="utf-8")
    except OSError:
        return None
    matches = list(_CHECKPOINT_HEADER_RE.finditer(content))
    return _parse_ts(matches[-1].group(1)) if matches else None


def check_warm_briefing(home: Path, checkpoint_override: str | None) -> str:
    """The rider. Returns the arc text, or raises Refused naming the failed check."""
    path = home / "warm-briefing.md"
    try:
        content = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime
    except OSError:
        raise Refused("warm briefing missing or unreadable")
    anchors = list(_ANCHOR_RE.finditer(content))
    if len(anchors) != 1:
        raise Refused(f"warm briefing anchor count is {len(anchors)}, need exactly one '## From this session'")
    arc = content[anchors[0].end():].strip()
    if not arc:
        raise Refused("warm briefing arc below the anchor is empty")
    checkpoint = _checkpoint_epoch(home, checkpoint_override)
    if checkpoint is None:
        raise Refused("last checkpoint time unknown (no nap header in session_log.md and no --checkpoint-at)")
    if mtime < checkpoint:
        raise Refused("warm briefing is older than the checkpoint (step 5b not done since)")
    return arc


def _loop_section(home: Path, armed_at: str | None, armed_for: str | None) -> tuple[str, bool]:
    path = home / "loop-mode.json"
    if not path.exists():
        return "loop marker: none (not in loop mode)", False
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(marker, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        return "loop marker: unreadable", True
    lines = ["loop marker: present"]
    for key in _LOOP_FIELDS:
        lines.append(f"- {key}: {marker.get(key, 'absent')}")
    if armed_at and armed_for:
        lines.append(f"a ScheduleWakeup was armed at {armed_at} for {armed_for}")
    else:
        lines.append("ScheduleWakeup arm time: not reported by the caller")
    return "\n".join(lines), True


def assemble(home: Path, arc: str, session: str, armed_at: str | None, armed_for: str | None) -> tuple[str, bool]:
    builders = load_builders(str(home))

    def block(build) -> str:
        try:
            return build(str(home)) or ""
        except Exception:  # noqa: BLE001 - a builder failure is an absence, named below
            return ""

    focus = block(builders.focus_block).strip("\n")
    history = block(builders.history_arc_block).strip("\n")
    loop_text, in_loop = _loop_section(home, armed_at, armed_for)
    parts = [
        f"# Post-compaction context (assembled by compact-request at {_utc_now_iso()}, session {session})",
        "The nap just persisted everything below; this is the state to resume from.",
        "## Focus list",
        focus or "focus list: none found",
        "## History arc",
        history or "history arc: unavailable",
        "## Warm briefing arc (From this session)",
        arc,
        "## Loop",
        loop_text,
    ]
    text = "\n\n".join(parts) + "\n"
    return _LOOP_WAKE_RE.sub("loop wake", text), in_loop


def _atomic_write(path: Path, text: str, tmps: list[Path]) -> None:
    tmp = path.with_name(f".tmp-{path.name}-{os.getpid()}")
    tmps.append(tmp)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    home = Path(os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram"))
    out_dir = home / "compact"
    request_path = out_dir / "request.json"
    # Any older request goes first, whatever happens next (even a bad invocation).
    try:
        request_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"compact-request: cannot remove the old request: {exc}", file=sys.stderr)
        return 4

    # --clear: a declined nap. Remove every file of a pending compaction and stop; no rider
    # is evaluated, nothing is assembled, no request is written.
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    pre.add_argument("--clear", action="store_true")
    if pre.parse_known_args(argv)[0].clear:
        for name in ("inflight", "context.md", "context.sha256"):
            try:
                (out_dir / name).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                print(f"compact-request: cannot remove {name}: {exc}", file=sys.stderr)
                return 4
        print("compact-request: cleared")
        return 0

    ap = argparse.ArgumentParser(prog="compact-request.py", description=__doc__.split("\n")[0])
    ap.add_argument("--session", required=True)
    ap.add_argument("--backup-ok", required=True, choices=["true", "false"])
    ap.add_argument("--checkpoint-at", default=None)
    ap.add_argument("--wake-armed-at", default=None)
    ap.add_argument("--wake-for", default=None)
    args = ap.parse_args(argv)

    tmps: list[Path] = []
    try:
        if args.backup_ok != "true":
            raise Refused("the checkpoint's backup is not ok")
        arc = check_warm_briefing(home, args.checkpoint_at)
        context, in_loop = assemble(home, arc, args.session, args.wake_armed_at, args.wake_for)
        raw = context.encode("utf-8")
        if len(raw) > MAX_CONTEXT_BYTES:
            raise CouldNotAssemble(f"context is {len(raw)} bytes, over the {MAX_CONTEXT_BYTES} byte cap")
        digest = hashlib.sha256(raw).hexdigest()
        request = {
            "requested_at": _utc_now_iso(),
            "session_id": args.session,
            "loop": in_loop,
            "context_sha256": digest,
        }
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(out_dir / "context.md", context, tmps)
            _atomic_write(out_dir / "context.sha256", digest + "\n", tmps)
            _atomic_write(request_path, json.dumps(request), tmps)  # LAST: its presence is the trigger
        except OSError as exc:
            raise CouldNotAssemble(f"could not write the request files: {exc}") from exc
    except Refused as exc:
        print(f"compact-request: refused: {exc}", file=sys.stderr)
        return 3
    except CouldNotAssemble as exc:
        print(f"compact-request: could not assemble: {exc}", file=sys.stderr)
        return 4
    finally:
        for tmp in tmps:
            try:
                tmp.unlink()
            except OSError:
                pass
    print(f"compact-request: written ({len(raw)} bytes of context)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
