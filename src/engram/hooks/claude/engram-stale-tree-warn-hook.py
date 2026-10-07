#!/usr/bin/env python3
"""
engram-stale-tree-warn-hook.py — PreToolUse behind-count warning (Layer 2: CATCH).

Part of #1203's stale-tree tripwire, the catch half. See
engram-stale-tree-pull-hook.py's module docstring for the full two-layer
design and the shared `stale_tree_watch` config field. Both hooks read the
SAME field from ~/.engram/config.json via the shared _stale_tree_hooklib.py
module (SSoT) — never hardcode repo paths here.

Fires on PreToolUse for Read, Grep, Glob, and Bash (matcher wired in
hooks.json). For a Read/Grep/Glob call, the target path is taken directly
from tool_input (`file_path` or `path`). For Bash, this is best-effort: the
command string is scanned for any watched repo-root path as a
boundary-aware substring (the root must be followed by a path separator,
end-of-string, or a shell/path terminator — not be a bare prefix of a
longer path segment). False negatives are still acceptable here — Layer 1
(the SessionStart auto-pull) is the load-bearing prevention; this hook is a
backstop for mid-session drift after Layer 1 already ran. Do NOT
over-engineer Bash command parsing.

When the target resolves to a watched repo root, this hook runs a CHEAP,
LOCAL, NO-NETWORK check:

    git -C <root> rev-list --count HEAD..origin/<branch>

This deliberately reuses the refs Layer 1 already fetched at session start —
it never runs `git fetch` itself, since a per-tool-call fetch is exactly the
latency this hook exists to avoid (a behind-count, not a pull). Under normal
operation this is silent every time — Layer 1 already fast-forwarded, so the
count is 0 — and only fires on genuine drift landing mid-session (someone
else pushed to the watched branch since this session started).

WARN ONLY — this hook never blocks the tool call (Lei's ruling: "warn is
fine, you just need the nudge at the right time").

OUT OF SCOPE (do not build here): the behind-count catches stale
*checkouts* (a clone behind its origin ref). It does NOT catch git-invisible
stale *file-copies* inside an otherwise-current checkout — a different class
of staleness that no git command can see.
"""
import json
import os
import re
import subprocess
import sys

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or os.path.expanduser("~/.engram")
)

# Cheap + local only — no network. This is a `rev-list --count` against
# already-fetched refs, not a fetch; the cap is a hang guard, not a
# network-latency budget.
REV_LIST_TIMEOUT = 3

# Shared config-read (SSoT): stale_tree_watch parsing lives in
# _stale_tree_hooklib.py, which also serves engram-stale-tree-pull-hook.py
# (Layer 1). See that hook's matching comment for the sys.path bootstrap
# rationale — identical here. Best-effort: any import failure degrades to a
# silent no-op (the watch list reads as empty) rather than crashing.
_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
try:
    from _stale_tree_hooklib import load_watch_list as _load_watch_list_impl
    _STALE_TREE_HOOKLIB_OK = True
except Exception:
    _STALE_TREE_HOOKLIB_OK = False

# #3521: shared advisory-probe `unknown` rendering (sanitize + stderr line + the
# one injected line + the stamp-file rate limit). Same sys.path bootstrap as
# above; an import failure degrades to a plain, unlimited line (never silent).
try:
    from _hooklib import (
        probe_unknown_line, write_probe_unknown_stderr,
        probe_notice_due, mark_probe_notice,
    )
    _PROBE_LIB_OK = True
except Exception:
    _PROBE_LIB_OK = False

# Path shown in the unknown line is cut to this many characters (tail kept):
# the whole injected line stays <= 200 chars, well inside the 300 budget.
_UNKNOWN_PATH_CHARS = 60


def _load_watch_list() -> list[dict]:
    """Read stale_tree_watch from config.json. Malformed/missing -> [].

    Thin wrapper: delegates to the shared _stale_tree_hooklib implementation,
    passing this hook's own ENGRAM_HOME (a module global read at call time,
    not import time, so test monkeypatching of `mod.ENGRAM_HOME` still
    works exactly as before this was extracted). Never raises — any
    read/parse/shape problem, or the shared module being unimportable,
    degrades to an empty list, which the caller treats as a silent no-op.
    """
    if not _STALE_TREE_HOOKLIB_OK:
        return []
    return _load_watch_list_impl(ENGRAM_HOME)


def _find_watched_root(target_path: str, watch_list: list[dict]) -> dict | None:
    """Return the watch entry whose repo root contains target_path, or None.

    Picks the longest-matching root when watch entries are nested (defensive;
    not an expected config shape but cheap to handle correctly).
    """
    if not target_path:
        return None
    try:
        abs_target = os.path.abspath(os.path.expanduser(target_path))
    except (OSError, ValueError, TypeError):
        return None

    best = None
    for entry in watch_list:
        try:
            abs_root = os.path.abspath(entry["path"])
        except (OSError, ValueError, TypeError):
            continue
        if abs_target == abs_root or abs_target.startswith(abs_root + os.sep):
            if best is None or len(abs_root) > len(os.path.abspath(best["path"])):
                best = entry
    return best


# Boundary-aware Bash matching (round-2 fix): a watched root must be
# followed by a path separator, end-of-string, or a common shell/path
# terminator — never be a bare prefix of a longer path segment. Without
# this, watched root `/home/x/engram-alpha` would false-positive-match a
# command referencing the unrelated sibling `/home/x/engram-alphaXYZ`.
# Still best-effort (per module docstring): no left-edge boundary check, no
# exotic-quoting awareness.
_ROOT_BOUNDARY_CHARS = r'/\s\'"|;&<>:,)\]}'


def _find_watched_root_in_command(command: str, watch_list: list[dict]) -> dict | None:
    """Best-effort: return the watch entry whose repo-root path appears as a
    boundary-aware substring of the Bash command string. False negatives
    acceptable (see module docstring) — this is a backstop, not the
    load-bearing prevention.
    """
    if not command:
        return None
    best = None
    for entry in watch_list:
        root = entry["path"]
        if not root:
            continue
        pattern = re.escape(root) + r'(?:$|[' + _ROOT_BOUNDARY_CHARS + r'])'
        if re.search(pattern, command):
            if best is None or len(root) > len(best["path"]):
                best = entry
    return best


def _behind_count(root: str, branch: str) -> "tuple[int | None, str | None]":
    """Return `(count, None)` -- how many commits `root` is behind
    origin/<branch>, 0 meaning current -- or `(None, "<class>: <message>")`
    when the probe could not tell (#3521): UNKNOWN, never the same value as
    0 behind. Reason classes: `git-missing` (git not installed),
    `timeout`, `nonzero` (git exited non-zero: not a repo, no such ref,
    typo'd branch), `unparsable` (stdout was not an integer), `os-error`
    (any other OSError launching git). Never raises for those."""
    try:
        result = subprocess.run(
            ["git", "-C", root, "rev-list", "--count", f"HEAD..origin/{branch}"],
            capture_output=True,
            text=True,
            timeout=REV_LIST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, f"timeout: git rev-list exceeded {REV_LIST_TIMEOUT}s"
    except FileNotFoundError as exc:
        return None, f"git-missing: {exc}"
    except OSError as exc:
        return None, f"os-error: {type(exc).__name__}: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        return None, f"nonzero: git exited {result.returncode}: {detail[0] if detail else ''}"
    try:
        return int(result.stdout.strip()), None
    except ValueError:
        return None, f"unparsable: {result.stdout.strip()[:80]!r}"


def _report_unknown(entry: dict, reason: str) -> None:
    """Render an UNKNOWN behind-count (#3521): always one sanitized stderr line;
    ONE bounded injected line at most once per window per checkout path
    (stamp under ENGRAM_HOME, written only after the line was printed). The
    hook still exits 0 and never blocks. Never raises.

    Rendering choice: this hook prints its own JSON (as its not-ok warning
    does) rather than going through emit_context -- the line is a single
    <= 200-char string, far under any harness budget, and emit_context would
    add a trailer to a block that has none today."""
    try:
        path = str(entry.get("path", ""))
        probe = "stale-tree-behind-count"
        if not _PROBE_LIB_OK:
            clean = "".join(" " if (ord(c) < 32 or 127 <= ord(c) <= 159) else c for c in str(reason))
            sys.stderr.write(f"engram-stale-tree-warn-hook: {probe} unknown: {clean}"[:300] + "\n")
            line = "[probe] stale-tree behind-count state UNKNOWN; checkout may be behind origin"
            key = None
        else:
            write_probe_unknown_stderr("engram-stale-tree-warn-hook", probe, reason)
            key = f"stale-tree:{path}"
            if not probe_notice_due(ENGRAM_HOME, key):
                return
            shown = path if len(path) <= _UNKNOWN_PATH_CHARS else "…" + path[-(_UNKNOWN_PATH_CHARS - 1):]
            line = probe_unknown_line(
                f"stale-tree behind-count ({shown})", reason, "checkout may be behind origin")
        print(json.dumps({
            "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": line}
        }))
        if key is not None:
            sys.stdout.flush()
            mark_probe_notice(ENGRAM_HOME, key)
    except Exception:
        pass


def _extract_read_glob_grep_target(tool_input: dict) -> str | None:
    """Pull the target path out of a Read/Grep/Glob tool_input dict.

    Read uses `file_path`; Grep/Glob use `path`. Try both, in that order,
    since a tool-specific dispatch isn't needed for a simple key lookup.
    """
    target = tool_input.get("file_path") or tool_input.get("path")
    return target if isinstance(target, str) else None


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        sys.exit(0)  # never block on parse errors

    try:
        watch_list = _load_watch_list()
        if not watch_list:
            sys.exit(0)  # silent no-op — no install opt-in

        tool_name = hook_input.get("tool_name", "")
        tool_input = hook_input.get("tool_input") or hook_input.get("input") or {}
        if not isinstance(tool_input, dict):
            sys.exit(0)

        entry = None
        if tool_name == "Bash":
            command = tool_input.get("command", "")
            if isinstance(command, str) and command:
                entry = _find_watched_root_in_command(command, watch_list)
        elif tool_name in ("Read", "Grep", "Glob"):
            target = _extract_read_glob_grep_target(tool_input)
            if target:
                entry = _find_watched_root(target, watch_list)
        else:
            sys.exit(0)  # only these four tools are wired in hooks.json

        if entry is None:
            sys.exit(0)

        count, reason = _behind_count(entry["path"], entry["branch"])
        if count is None:  # #3521: probe could not tell — NOT the same as current
            _report_unknown(entry, reason)
            sys.exit(0)
        if not count:  # 0 (current) — stay silent
            sys.exit(0)

        response = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": (
                    f"⚠ {entry['path']} is {count} commit(s) behind "
                    f"origin/{entry['branch']} — your read may be stale; pull first "
                    f"(`git -C {entry['path']} pull --ff-only`)."
                ),
            }
        }
        print(json.dumps(response))
        sys.exit(0)

    except Exception:
        sys.exit(0)  # hook failures must never block a tool call


if __name__ == "__main__":
    main()
