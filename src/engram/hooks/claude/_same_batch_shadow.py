"""Same-batch report detector, SHADOW phase (engram-alpha#4390 phase 1). Logs; prints NOTHING.

A "same-batch report" is a tool-call block in which one call REPORTED another call of the same
block (a crossing or history line saying "sent", "pushed", ...) before that call's result existed.
This module decides, once a block is complete, whether the block matches rule A, and appends one
row to `$ENGRAM_HOME/same-batch-shadow-ledger.jsonl`. It changes no behaviour: no stdout, no
hook-context, no block, no deny, no wait. Two already-registered hooks call `evaluate`, each
through one fail-open call: the lesson tripwire (PreToolUse, `source="pretooluse"`) and the loop-arm
Stop hook (Stop, `source="stop"`, called after its stop_hook_active return and before its loop-marker
gate, so on every Stop). Both are convenience tier, so the module ranks with them. hooks.json is
untouched.

Definitions. A BLOCK is all tool_use items sharing one `message.id` (they may sit on separate
transcript lines). A block is COMPLETE when every tool_use in it has a tool_result in the
transcript. Only complete blocks are judged: a hook can see its own block partly flushed.

Rule A (hash-fixed, `RULE_SHA`; the constants below are verbatim from the replay that fixed it,
quirks included -- do not tidy them): a block is a candidate when it holds a REPORT call and an
EFFECT call at a different index; it is flagged when a candidate report text matches `A`.

Exactly once, and never a wait: a lock file serialises concurrent evaluators, taken LOCK_EX|LOCK_NB
(a fire that finds it held returns at once and writes nothing; its blocks stay unseen and a later
fire retries them); under the lock the per-session seen
file is read, the row appended to the ledger, and the message id appended to the seen file. A
failed ledger write leaves the id unseen, so a later fire retries. (A crash between the two
appends could, rarely, duplicate a row on retry; the other order could lose one.)

Unread gap (#4390, Kepler's ruling): the window is the last 256 KiB, so traffic between two fires can
pass wholly unread. Per session and transcript one state file `same-batch-shadow/pos-<24-hex>.json` (the hash is of
session id, NUL, transcript path; mode 0600,
replaced whole through a temp file) holds {"size", "truncated_reads", "result_first_reads"}: the
transcript size at the last COMPLETED evaluation, the reads with a window start past 0, and the subset
whose first newline-delimited line is a user record holding a tool_result item (a counter, not a loss claim; `_blocks` also splits on U+0085, U+2028 and U+2029, so on such a first line the two can differ, and only this counter is affected).
On EVERY enabled fire, under the same lock and in this order, the state is loaded, flagged rows are
written, then at most one `{"event":"unread_gap","ts","session_id","source","unread_bytes","from_size","to_offset"}` row, then the
state: `unread_bytes` is the first kept line's offset minus the stored size when that is positive, and
the string "unknown" (never 0) on a session's first evaluation of a file over the window or when the
file is smaller than the stored size. No row when the window is off (`tail_bytes=None`). A fire that
finds the lock held, a failed row write or a cap stop leaves the state where it was, so the next fire
reports the gap from the older size. A link, FIFO or hard link at the state path is refused: no gap row,
no write. (A crash between the gap row and the state write could, rarely, repeat the row.) Pos and skips files,
like seen files, are not pruned. Since every enabled fire now takes the lock, a concurrent sibling fire
that finds it held returns having written no row, so its flagged blocks wait for a later fire and are
lost only if they leave the window first. Lock-held fires are counted, one line each, in
`same-batch-shadow/skips-<24-hex>.txt`; that count says how often a fire deferred, not how many blocks.

A sub-agent's fire carries the parent's transcript and session (measured on Claude Code 2.1.319, one fire), so it is judged as a parent fire with source "pretooluse-subagent"; a row's source says which fire saw the block first, not who wrote it; a sub-agent's own blocks live in its own transcript and are never judged.

The row names its interval: `from_size` is the stored size (null when there was no usable state),
`to_offset` the first kept offset, and in the integer case unread_bytes == to_offset - from_size.

More bounds on the gap row: the size advances on every completed fire even when the window held incomplete blocks, so a block incomplete at one fire that scrolls out before the next is unjudged, and a gap row claims nothing about it; a gap row carries the time and source of the fire that found the gap, not of the traffic; "unknown" also fires for a transcript rewritten smaller than its stored size, where nothing need be lost, and after a deleted or corrupt state file, while a transcript rewritten larger gives no row or a false integer gap; a file shrunk during the read leaves text short of size, which the next fire reports as "unknown"; a failed state write repeats and grows the row, each row naming its interval, so read the union, not the sum.

Stated bound on the gap: a gap row counts bytes, not blocks, and claims no loss. A block whose tool_use lay before the stored size and whose results lie in the gap is also unjudged, though its first bytes were read. The replay remains the truth.

Privacy: a row holds no report text and no tool input -- fields only (see `_row`).

Never raises: `evaluate` swallows every Exception. All file I/O goes through the strict
regular-file helpers of `_hooklib` (#3781/#4380): no follow through a link, no plain open on a
state path, no blocking open of a FIFO. The transcript read is bounded to the last
`SHADOW_TAIL_BYTES` (plus, in one branch only, a back-read of at most `SHADOW_BACKREAD_BYTES`; see
`_blocks`).

Two stated bounds on what a row means:
- A block judged while a later sibling's tool_use line is not yet flushed (earlier calls have
  results, a later one is not written yet) is flagged on the partial set and its message id is then
  marked seen, so the row can hold fewer `calls`/`tools` than the finished block and a missing
  `any_error`. The flag itself is right: rule A is existential, adding calls only adds.
- Per-session seen files under `same-batch-shadow/` are not pruned; at the ledger cap the ledger
  stops growing, the directory does not. They are small.

Layout-independent: imports `_hooklib` and `_harness` from this file's own directory, which is
`hooks/claude/` in source and `hooks/` once the build flattens it. No fixed-depth parent walk anywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _hooklib import (  # noqa: E402
    _open_single_regular, engram_home, open_regular_text,
)
from _harness import detect_harness  # noqa: E402

SHADOW_TAIL_BYTES = 256 * 1024
SHADOW_BACKREAD_BYTES = 1024 * 1024  # how far back to look for the line before the window's first line
RULE = "A"
RULE_SHA = "d10d33aa8babd0f2"
SHADOW_LOG_CAP_BYTES = 5 * 1024 * 1024
LEDGER_NAME = "same-batch-shadow-ledger.jsonl"
STATE_DIR_NAME = "same-batch-shadow"

# ---- rule A, constants verbatim from the replay ------------------------------------------------
EFFECT = ('send_crossing', 'say', 'wake', 'reply', 'add_observation', 'add_observation_batch', 'derive', 'supersede', 'retract', 'add_lesson', 'add_edge', 'ask', 'add_task', 'update_task', 'lesson_register_incident', 'register_exemplar', 'checklist', 'act')
REPORT = ('send_crossing', 'say', 'reply', 'wake')
BASHFX = re.compile(r'git push|git commit|gh pr|gh issue|gh api -X|baton |>>')
HIST = re.compile(r'>>\s*\S*history/|history/\S*\s*<<|>>.*history/')
A = re.compile(r'\b(sent|filed|told|pushed|flipped|added|posted|merged|committed|recorded|registered|asked|dispatched|replied|updated|wrote|created|superseded|retracted)\b', re.I)


def short(n):
    return n.split('__')[-1]


def isfx(n, i):
    if n == 'Bash':
        return bool(BASHFX.search(i.get('command', '')))
    if n in ('Edit', 'Write'):
        return True
    return n.startswith('mcp__') and any(short(n).endswith(e) for e in EFFECT) and short(n).split('engram_')[-1] in EFFECT


def report_text(n, i):
    if n == 'Bash':
        c = i.get('command', '')
        return c if HIST.search(c) else None
    if n.startswith('mcp__') and short(n) in REPORT:
        p = i.get('payload_json', '')
        try:
            d = json.loads(p)
        except Exception:
            return p
        if not isinstance(d, dict):
            return p
        return d.get('body') or d.get('text') or d.get('message') or p
    return None


# ---- config / harness gate ----------------------------------------------------------------------

def _enabled(home) -> bool:
    """DEFAULT OFF (design owner, 11-06): on only when `same_batch_shadow.enabled` in config.json is
    the boolean true. Absent, false, non-boolean, unreadable or malformed = off."""
    try:
        with open_regular_text(os.path.join(str(home), "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        sec = cfg.get("same_batch_shadow")
        return isinstance(sec, dict) and sec.get("enabled") is True
    except Exception:
        return False


def _harness_ok() -> bool:
    """The transcript schema is Claude Code's: a detected non-Claude harness is a no-op.
    None (no platform.json: source tree, or unreadable) proceeds."""
    h = detect_harness()
    return h is None or h == "claude-code"


# ---- bounded transcript read ---------------------------------------------------------------------

def _first_line_assistant_mid(text):
    """The message.id of the first non-empty line of `text` when it parses to an assistant record,
    else None."""
    first = next((l for l in text.splitlines() if l.strip()), None)
    if first is None:
        return None
    try:
        fr = json.loads(first)
    except Exception:
        return None
    fm = fr.get("message") if isinstance(fr, dict) else None
    if isinstance(fr, dict) and fr.get("type") == "assistant" and isinstance(fm, dict) \
            and isinstance(fm.get("id"), str) and fm.get("id"):
        return fm["id"]
    return None


_PREV_UNKNOWN = "unknown"


def _read_prev_line(fd, line_start):
    """The complete line just before the line that starts at `line_start`, read back through the
    SAME fd (pread), at most SHADOW_BACKREAD_BYTES. Returns its text; `_PREV_UNKNOWN` when its start
    is not found within the bound; None when there is no previous line (line_start is 0)."""
    if line_start <= 0:
        return None
    end = line_start - 1                     # the "\n" that ends the previous line
    low = max(0, end - SHADOW_BACKREAD_BYTES)
    pos = end
    buf = b""
    while pos > low:
        step = min(64 * 1024, pos - low)
        pos -= step
        chunk = os.pread(fd, step, pos)
        buf = chunk + buf
        nl = buf.rfind(b"\n")
        if nl >= 0:
            return buf[nl + 1:].decode("utf-8", "replace")
    if low == 0:
        return buf.decode("utf-8", "replace")  # the previous line starts at the file's start
    return _PREV_UNKNOWN


def _read_tail(path, tail_bytes):
    """(text, truncated, prev, size, first_pos). Strict regular-file open (no link, no FIFO). With
    tail_bytes, only the last that many bytes; when that starts mid-file the first partial line is
    discarded. `size` is the file size read; `first_pos` is the byte offset of the first line actually
    kept: 0 when not truncated, and `size` when truncated and no newline was found (nothing kept).

    `prev` is None except in ONE branch: the read was truncated AND the window's first complete line
    is an assistant record. Only then is the complete line just before it read back, through the same
    fd, at most SHADOW_BACKREAD_BYTES further back: its text, or `_PREV_UNKNOWN` when its start is not
    found within the bound. A fire whose first complete line is anything else reads nothing extra."""
    fd = _open_single_regular(path, os.O_RDONLY)
    prev = None
    try:
        size = os.fstat(fd).st_size
        if tail_bytes is None or size <= tail_bytes:
            start = 0
        else:
            start = size - tail_bytes
        # read one byte early when truncating, to learn whether the window starts on a line boundary
        off = max(start - 1, 0)
        os.lseek(fd, off, os.SEEK_SET)
        chunks = []
        remaining = size - off          # never past the size read: text and size describe the same bytes
        while remaining > 0:
            b = os.read(fd, min(1 << 20, remaining))
            if not b:
                break
            chunks.append(b)
            remaining -= len(b)
        data = b"".join(chunks)
        truncated = start > 0
        first_pos = 0
        if truncated:
            if data[:1] == b"\n":
                data = data[1:]
                first_pos = off + 1
            else:
                nl = data.find(b"\n")
                if nl < 0:
                    data = b""
                    first_pos = size
                else:
                    data = data[nl + 1:]
                    first_pos = off + nl + 1
        text = data.decode("utf-8", "replace")
        if truncated and data[:1] not in (b"", b"\n") \
                and _first_line_assistant_mid(text) is not None:
            prev = _read_prev_line(fd, first_pos)
            if prev is None:
                prev = _PREV_UNKNOWN
    finally:
        os.close(fd)
    return text, truncated, prev, size, first_pos


def _first_line_is_result(text):
    """True when the first non-empty line of `text` parses to a user record whose content holds a
    tool_result item. A line that does not parse, or any other record, is False."""
    pos = 0
    while True:                         # first non-empty line by finding newlines, no pass over the window
        nl = text.find("\n", pos)
        line = text[pos:] if nl < 0 else text[pos:nl]
        if line.strip():
            first = line
            break
        if nl < 0:
            return False
        pos = nl + 1
    try:
        fr = json.loads(first)
    except Exception:
        return False
    fm = fr.get("message") if isinstance(fr, dict) else None
    content = fm.get("content") if isinstance(fm, dict) else None
    return fr.get("type") == "user" and isinstance(content, list) \
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _blocks(text, truncated, prev=None):
    """[(message_id, [(name, input)], complete, any_error)] in order of first appearance.
    When the window cut the file (`truncated`), one block may have lines before the window, and it
    is skipped (never half-read). Assistant lines of different messages do not interleave, so the
    only block that can have been cut is the one whose message.id is on the window's FIRST complete
    line, if that line is an assistant record. That line is parsed regardless of the prefilter. If
    it is any other record (a user or tool_result line, or an assistant record of another message),
    the first-seen block started inside the window and is kept; a window whose newest block is its
    only block must still be judged. If that first line cannot be parsed, the first-seen block is
    skipped (conservative). When that first line IS an assistant record it is also the true first
    line of a whole block whenever the cut fell inside the line before it, so `prev` (the complete
    line just before it, read back by `_read_tail`) decides: the block is skipped only when `prev`
    parses as an assistant record with the SAME message.id; any other parsed record means the block
    started inside the window and is kept; `prev` unparseable, or its start not found within the
    back-read bound, keeps the conservative skip. Residual limit: a cut block whose first complete line is a user result
    line (serial blocks: use, result, use) is not detected and is judged on its in-window part."""
    cut_mid = None       # the message id the window may have cut
    skip_first_seen = False
    if truncated:
        first = next((l for l in text.splitlines() if l.strip()), None)
        if first is not None:
            try:
                fr = json.loads(first)
            except Exception:
                skip_first_seen = True
            else:
                fm = fr.get("message") if isinstance(fr, dict) else None
                if isinstance(fr, dict) and fr.get("type") == "assistant" and isinstance(fm, dict) \
                        and isinstance(fm.get("id"), str) and fm.get("id"):
                    cut_mid = fm["id"]
                    if prev is not None and prev != _PREV_UNKNOWN:
                        try:
                            pr = json.loads(prev)
                        except Exception:
                            pr = None
                        pm = pr.get("message") if isinstance(pr, dict) else None
                        if isinstance(pr, dict):
                            if not (pr.get("type") == "assistant" and isinstance(pm, dict)
                                    and pm.get("id") == cut_mid):
                                cut_mid = None   # the line before is another record: the block began in the window
                elif not isinstance(fr, dict):
                    skip_first_seen = True
    order = []
    uses = {}      # mid -> [(tool_use_id, name, input)]
    results = {}   # tool_use_id -> is_error
    for line in text.splitlines():
        if '"tool_use"' not in line and '"tool_result"' not in line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not isinstance(r, dict):
            continue
        m = r.get("message")
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        if r.get("type") == "assistant":
            mid = m.get("id")
            if not isinstance(mid, str) or not mid:
                continue
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("name"), str):
                    inp = b.get("input")
                    uses.setdefault(mid, []).append((b.get("id"), b["name"], inp if isinstance(inp, dict) else {}))
                    if mid not in order:
                        order.append(mid)
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if isinstance(tid, str):
                    results[tid] = results.get(tid, False) or (b.get("is_error") is True)
    if skip_first_seen and order:
        order = order[1:]
    elif cut_mid is not None:
        order = [m for m in order if m != cut_mid]
    out = []
    for mid in order:
        calls = uses[mid]
        complete = all(isinstance(tid, str) and tid in results for tid, _, _ in calls)
        err = any(results.get(tid, False) for tid, _, _ in calls)
        out.append((mid, [(n, i) for _, n, i in calls], complete, err))
    return out


def _flagged(calls) -> bool:
    if len(calls) < 2:
        return False
    rep = [(i, report_text(n, inp)) for i, (n, inp) in enumerate(calls)]
    rep = [(i, t) for i, t in rep if t]
    fx = [i for i, (n, inp) in enumerate(calls) if isfx(n, inp)]
    cand = [(i, t) for i, t in rep if any(j != i for j in fx)]
    return any(A.search(t) for _, t in cand if isinstance(t, str))


# ---- the log and the exactly-once state ------------------------------------------------------------

def _row(session_id, mid, source, calls, any_error):
    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_id": session_id,
        "message_id": mid,
        "source": source,
        "rule": RULE,
        "rule_sha": RULE_SHA,
        "calls": len(calls),
        "tools": [short(n) for n, _ in calls],
        "any_error": bool(any_error),
    }


def _state_dir(home):
    """The dedupe dir, created if absent (never the home itself); None when it is a link or not a dir."""
    d = os.path.join(str(home), STATE_DIR_NAME)
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        os.mkdir(d, 0o700)
        st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        return None
    return d


def _append_line(path, line):
    """Append one line through the strict regular-file open, created mode 0600 (C13). The mode applies
    at creation only (an existing file is never chmod-ed, so a wider-mode log is not widened further)."""
    fd = _open_single_regular(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        data = (line + "\n").encode("utf-8")
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def _ledger_size(home):
    """Byte size of the ledger: 0 when absent. A non-regular ledger raises OSError (nothing is written)."""
    try:
        st = os.lstat(os.path.join(str(home), LEDGER_NAME))
    except FileNotFoundError:
        return 0
    if not stat.S_ISREG(st.st_mode):
        raise OSError("ledger is not a regular file")
    return st.st_size


def _write_cap_row_once(home, d):
    """Under the caller's lock: the one cap_reached row, then the marker that says it was written."""
    marker = os.path.join(d, "cap-reached")
    try:
        os.lstat(marker)
        return
    except FileNotFoundError:
        pass
    row = {"event": "cap_reached", "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    _append_line(os.path.join(str(home), LEDGER_NAME), json.dumps(row, separators=(",", ":")))
    _append_line(marker, "1")


def _capped_fast(home) -> bool:
    """True when the log is at the cap: the cap row is written once if it is not there yet, and the
    caller then reads no transcript. A log back under the cap clears a stale marker so a later cap
    can say so again."""
    import fcntl
    size = _ledger_size(home)
    marker = os.path.join(str(home), STATE_DIR_NAME, "cap-reached")
    if size < SHADOW_LOG_CAP_BYTES:
        try:
            os.lstat(marker)
        except OSError:
            return False
        try:
            os.unlink(marker)
        except OSError:
            pass
        return False
    try:
        os.lstat(marker)
        return True
    except FileNotFoundError:
        pass
    d = _state_dir(home)
    if d is None:
        return True
    lock_fd = _open_single_regular(os.path.join(d, "lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True  # another fire holds the lock: never wait; the cap row waits for a later fire
        _write_cap_row_once(home, d)
    finally:
        os.close(lock_fd)
    return True


_GAP_UNKNOWN = "unknown"
_POS_KEYS = ("size", "truncated_reads", "result_first_reads")


def _is_count(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _load_pos(path):
    """(state, refused). state is the stored dict, or None for absent / empty / corrupt / wrong-shape
    (treated as no state). refused is True when the path is a link, FIFO, hard-linked or otherwise
    unreadable through the strict open: nothing is read or written there."""
    try:
        fd = _open_single_regular(path, os.O_RDONLY)
    except FileNotFoundError:
        return None, False
    except OSError:
        return None, True
    try:
        with os.fdopen(fd, "rb") as f:
            raw = f.read(4096)
        st = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, False
    if not isinstance(st, dict) or not all(_is_count(st.get(k)) for k in _POS_KEYS):
        return None, False
    return {k: st[k] for k in _POS_KEYS}, False


def _write_pos(path, state):
    """Whole-file replace: a temp file in the same dir (made fresh through the strict open, mode 0600,
    never through a link) then os.replace, so a half-written file is never read as state."""
    tmp = path + ".tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    data = json.dumps({k: state[k] for k in _POS_KEYS}, separators=(",", ":")).encode("utf-8")
    fd = _open_single_regular(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _gap_value(prev_state, obs):
    """The unread_bytes for this fire: an int, `_GAP_UNKNOWN`, or None for no row."""
    if not obs["windowed"]:
        return None
    if prev_state is None:
        return _GAP_UNKNOWN if obs["truncated"] else None
    if obs["size"] < prev_state["size"]:
        return _GAP_UNKNOWN
    if obs["start"] > prev_state["size"]:
        return obs["start"] - prev_state["size"]
    return None


def _record(home, session_id, rows, obs):
    """Under the lock, taken on every enabled fire and never waited for: (1) load this session's
    position state; (2) append each (mid, row) not yet seen, once; (3) append the unread_gap row if any;
    (4) write the new state. A failed write or a cap stop skips the later steps, so state never advances
    past a row that was not written. `obs` = size, start (first_pos), truncated, result_first, windowed."""
    import fcntl
    d = _state_dir(home)
    if d is None:
        return
    h = hashlib.sha256(str(session_id).encode("utf-8", "replace")).hexdigest()[:24]
    seen_path = os.path.join(d, "seen-" + h + ".txt")
    # the position is per (session, transcript): a missing session id ("unknown") or one id seen with a
    # second transcript path must not share a stored size across files
    hp = hashlib.sha256((str(session_id) + "\0" + str(obs["path"])).encode("utf-8", "replace")).hexdigest()[:24]
    pos_path = os.path.join(d, "pos-" + hp + ".json")
    ledger = os.path.join(str(home), LEDGER_NAME)
    lock_fd = _open_single_regular(os.path.join(d, "lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # another fire holds the lock: never wait; the blocks stay unseen, a later fire retries.
            # Count the deferral outside the lock: one "1" line, O_APPEND keeps it whole; swallowed.
            try:
                _append_line(os.path.join(d, "skips-" + hp + ".txt"), "1")
            except Exception:
                pass
            return
        prev_state, refused = _load_pos(pos_path)
        try:
            sfd = _open_single_regular(seen_path, os.O_RDONLY)  # strict: a link here is refused, not read
        except FileNotFoundError:
            seen = set()
        else:
            with os.fdopen(sfd, "r", encoding="utf-8") as f:
                seen = set(f.read().split())
        for mid, row in rows:
            if mid in seen:
                continue
            line = json.dumps(row, separators=(",", ":"))
            if _ledger_size(home) + len(line) + 1 > SHADOW_LOG_CAP_BYTES:
                _write_cap_row_once(home, d)
                return
            _append_line(ledger, line)
            _append_line(seen_path, mid)
            seen.add(mid)
        if refused:
            return  # a link / FIFO at the pos path: no gap row (it could only say "unknown"), no write
        gap = _gap_value(prev_state, obs)
        if gap is not None:
            grow = {"event": "unread_gap",
                    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "session_id": session_id, "source": obs["source"], "unread_bytes": gap,
                    "from_size": prev_state["size"] if prev_state is not None else None,
                    "to_offset": obs["start"]}
            line = json.dumps(grow, separators=(",", ":"))
            if _ledger_size(home) + len(line) + 1 > SHADOW_LOG_CAP_BYTES:
                _write_cap_row_once(home, d)
                return
            _append_line(ledger, line)
        base = prev_state or {"truncated_reads": 0, "result_first_reads": 0}
        _write_pos(pos_path, {
            "size": obs["size"],
            "truncated_reads": base["truncated_reads"] + (1 if obs["truncated"] else 0),
            "result_first_reads": base["result_first_reads"] + (1 if obs["result_first"] else 0),
        })
    finally:
        os.close(lock_fd)  # closing drops the flock


def _evaluate(transcript_path, session_id, source, tail_bytes):
    if not transcript_path or not isinstance(transcript_path, str):
        return
    home = engram_home()
    if not os.path.isdir(str(home)):
        return
    if not _enabled(home) or not _harness_ok():
        return
    if _capped_fast(home):
        return
    text, truncated, prev, size, first_pos = _read_tail(transcript_path, tail_bytes)
    sid = session_id if session_id else "unknown"
    rows = []
    for mid, calls, complete, err in _blocks(text, truncated, prev):
        if complete and _flagged(calls):
            rows.append((mid, _row(sid, mid, source, calls, err)))
    obs = {"path": transcript_path, "size": size, "start": first_pos, "truncated": truncated, "source": source,
           "windowed": tail_bytes is not None,
           "result_first": truncated and _first_line_is_result(text)}
    _record(home, sid, rows, obs)


def evaluate(transcript_path, session_id, source, tail_bytes=SHADOW_TAIL_BYTES):
    """Judge the complete blocks in the transcript's tail and log the flagged ones once.
    Returns None, always, and prints nothing. `tail_bytes=None` reads the whole file (the
    acceptance script only; no hook passes it)."""
    try:
        _evaluate(transcript_path, session_id, source, tail_bytes)
    except Exception:
        pass
    return None
