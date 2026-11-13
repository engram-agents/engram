#!/usr/bin/env python3
"""study_queue -- read-only list of this seat's top study targets
(engram-alpha#3919, item M3; the M2 Stop-hook nudge names this tool as the next act).

Study mode is one scoped unit: ONE target, ONE method, ONE written ENGRAM result.  This
tool picks the targets.  Each target is a node id or file reference, a one-line reason
(<= 160 chars) and ONE deterministic method string.  No network, no LLM, no writes.

Sources, in priority order (a candidate list; the first N after de-duplication by ref win)
==========================================================================================
1. dream     the latest dream report ($ENGRAM_HOME/history/dream/YYYY-MM-DD*.md, by name without .md,
             last): the numbered items of ``## For the awake seat`` and the bullets of
             ``## Open for tomorrow`` that the ``## Sleep-end triage (parent)`` section has
             not HANDLED.  An item is handled when its triage disposition starts ``do now`` or
             ``file``, OR when the record's last ``## Morning pickup`` section (written by
             tools/dream_pickup.py) carries ``pN. picked up ...`` for awake-seat item N (hides it),
             or ``pN. skipped (...)`` (surfaces it with reason annotated in ``why``).  ``defer``,
             or neither source naming the
             item, keeps it.  Open-for-tomorrow bullets have no pickup lines.
2. weakest-belief   the top entry of ``weakest_load_bearing.read_ranking(db, n=1)`` (imported,
             not reimplemented; ITS owner-read flag governs that read).
3. question  current, non-retracted ``question`` nodes whose status is ``open`` or
             ``partially_resolved`` (evidence has landed but the question is not settled), fewest
             ``recall_turn`` first, then id.  Up to 3.
4. conjecture  current, non-retracted ``conjecture`` nodes whose status is ``active``, lowest
             confidence first, then id.  Up to 3.
5. cascade   current, non-retracted nodes with a ``cascade_markers`` row whose status is
             ``pending``, oldest marker first.  Up to 2.

Status filters (read from the code, see the PR): ``question`` -> ``open`` and
``partially_resolved`` are the two statuses ``engram_lifecycle`` / ``engram_stats`` count as
open (``partially_resolved`` is set when the best resolver's confidence is below threshold:
evidence landed, the question is still worth studying); only ``resolved`` is settled.
``conjecture`` -> ``active`` is what ``engram_lifecycle`` lists as an open conjecture;
``_resolve_impl`` moves it to ``supported`` / ``refuted`` / ``inconclusive``.

Sources not wired in this version (the footer says so): goal tension, syllabus/paper queue,
unrun measurements.

Entry layer
===========
``build_queue`` and the CLI read the store ONLY through the shared owner-read helper
(tools/_owner_read.py), behind ``engramd.study_queue_reads_via_owner`` (default OFF): flag ON
reads go to the engramd owner, flag OFF opens the store ``mode=ro``.  With the flag ON and the
owner down, refusing or answering malformed the entry RAISES ``StudyQueueOwnerReadError``:
never an empty queue, never a direct fallback.  This module has no ``connect`` call of its
own.  The CLI prints the queue and exits 0, or prints NOTHING on stdout, one
``<ClassName>: <message>`` line on stderr, and exits 1.  An empty queue is a valid answer
(exit 0).  A pre-3.10 interpreter with no usable venv interpreter is refused BEFORE any
work (the interpreter guard below): one line on stderr, exit 3.

Every statement is a module-level literal (the read-template scanner resolves literals, not
f-strings); they are registered byte for byte in src/engram/engramd_read_templates.json
under the sites ``tools/study_queue.py:<function>`` and the drift gate is
tests/test_engramd_read_scan.py.
"""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# Interpreter guard (#4642).
#
# A stock macOS `python3` is 3.9.6, and the sibling load below
# (weakest_load_bearing -> engram_core) evaluates `str | Path` at module scope, which
# raises TypeError on 3.10-.  So the decision is made BEFORE any import that can pull
# in engram_core -- only os/sys are imported above this point.  It either re-execs
# under the plugin venv interpreter or exits 3 with one line and does nothing at all.
#
# The venv is an INSTALLATION ARTIFACT at ~/.engram/venv, independent of ENGRAM_HOME
# (CLAUDE.md "Infrastructure Locations"; install.sh VENV_DIR="$HOME/.engram/venv";
# README-AGENT.md "must still end up at ~/.engram/venv").  Building it from
# engram_home() would be wrong on a seat whose ENGRAM_HOME is elsewhere AND is its own
# violation class in tools/home_path_gate.py ('engram_home_derived_venv', #3863 6c).
# ---------------------------------------------------------------------------

def _reexec_plan(version_info: tuple[int, ...],
                 venv_python: str,
                 executable: str | None = None,
                 already_reexeced: bool = False) -> tuple[str, str | None]:
    """('run', None) to continue in-process; ('exec', path) to re-exec;
       ('fail', message) when a re-exec is needed and impossible.

    `executable` is the running interpreter (default: sys.executable), a parameter so
    the realpath comparison is testable without touching the process.  `already_reexeced`
    is the loop sentinel: a re-exec has already been attempted and the interpreter is
    STILL too old (a wrapper script that re-execs an under-powered python, or a venv
    that is itself <3.10) — that is "needed and impossible", never "not needed".

    Three distinct states, never a boolean: "not needed" and "needed but impossible"
    are different outcomes and the second says so, in one line, and exits 3."""
    if executable is None:
        executable = sys.executable
    if version_info >= (3, 10):
        return ("run", None)
    version = ".".join(str(part) for part in version_info[:3])
    if already_reexeced:
        return ("fail",
                f"study_queue: this python is {version} (needs 3.10+); a re-exec under "
                f"{venv_python} did not reach a 3.10+ interpreter — run the installer "
                f"with a 3.10+ python3")
    if executable and os.path.realpath(venv_python) == os.path.realpath(executable):
        return ("run", None)  # never loop: the venv interpreter IS this process
    if not (os.path.isfile(venv_python) and os.access(venv_python, os.X_OK)):
        return ("fail",
                f"study_queue: this python is {version} (needs 3.10+); the venv interpreter "
                f"{venv_python} is missing or not executable — run the installer")
    return ("exec", venv_python)


if sys.version_info < (3, 10):
    _VENV_PYTHON = os.path.expanduser("~/.engram/venv/bin/python3")
    _action, _detail = _reexec_plan(
        tuple(sys.version_info[:3]), _VENV_PYTHON,
        already_reexeced=bool(os.environ.get("ENGRAM_STUDY_QUEUE_REEXEC")))
    if _action == "exec":
        # Loop sentinel, set immediately before the exec: a wrapper script whose
        # realpath never equals the child's sys.executable, or a venv that is itself
        # <3.10, would otherwise retry forever or fall through to the raw TypeError.
        os.environ["ENGRAM_STUDY_QUEUE_REEXEC"] = "1"
        try:
            os.execv(_detail, [_detail, __file__, *sys.argv[1:]])
        except OSError as _exc:  # the plan said usable; a race is still one line, never a traceback
            sys.stderr.write(
                "study_queue: could not re-exec under %s (%s); this python is %s (needs 3.10+)\n"
                % (_detail, _exc, ".".join(str(p) for p in sys.version_info[:3])))
            sys.exit(3)
    elif _action == "fail":
        sys.stderr.write(_detail + "\n")
        sys.exit(3)


import argparse  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

# ---------------------------------------------------------------------------
# Locate SSoT modules -- support both "run as module" and direct invocation
# (same pattern as tools/weakest_load_bearing.py).
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import _owner_read as owner_read  # noqa: E402


def _load_sibling(name: str):
    """Load a sibling tool by path: the repo-root tools/ mirror carries symlinks only for the
    modules that need one, so `import tools.<name>` is not reliable across layouts."""
    spec = importlib.util.spec_from_file_location(f"_study_queue_{name}", str(_HERE / f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wlb = _load_sibling("weakest_load_bearing")
dream_pickup = _load_sibling("dream_pickup")  # the writer's own section scan: one definition of "the Morning pickup section"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OWNER_FLAG_KEY = "study_queue_reads_via_owner"

WHY_CHARS = 160
QUESTION_LIMIT = 3
CONJECTURE_LIMIT = 3
CASCADE_LIMIT = 2

# One deterministic method per source.
METHODS = {
    "dream": "do the item as written, then write one ENGRAM observation of the result "
             "(what you ran, what it returned)",
    "weakest-belief": "re-verify its weakest premise at the source, then supersede, retract or "
                      "confirm it with an observation",
    "question": "search the graph, then one external source; write an observation that answers "
                "or narrows it, then engram_resolve or leave it open with what you learned",
    "conjecture": "design one check that could falsify it, run it, then resolve, promote or "
                  "refute with the result as evidence",
    "cascade": "run the engram-resolve-cascade skill on this node",
}

SOURCES = ("dream", "weakest-belief", "question", "conjecture", "cascade")

NOT_WIRED = ["goal tension", "syllabus/paper queue", "unrun measurements"]

STEP_ZERO = ("Step 0 (do this yourself, not automated): call whats_new in the world first; "
             "a missed notice is not study material.")

_QUESTION_STATUSES = frozenset({"open", "partially_resolved"})  # engram_lifecycle: evidence landed, not settled
_CONJECTURE_STATUS = "active"
_STUDY_TYPES_JSON = json.dumps(["question", "conjecture"])

# A node id as the dream reports write them (a prefix, an underscore, digits) or as fixtures do.
_ID_PREFIXES = frozenset("ax cj cs ct df dv ev fl gl ls ob pn qu tk ts".split())  # the store's real node-id prefixes
_DATED_REPORT_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NODE_ID_RE = re.compile(r"(?<![\w/.-])([a-z]{2,4})[_-](\d{3,6})(?![\w/-]|\.\w)")


def _node_ids(text: str) -> list:
    """Node ids named in `text`, in order, deduplicated: a real prefix, not inside a path or URL."""
    out = []
    for m in _NODE_ID_RE.finditer(text):
        if m.group(1) in _ID_PREFIXES and m.group(0) not in out:
            out.append(m.group(0))
    return out

# ---------------------------------------------------------------------------
# The statements.  Plain literals on purpose (see the module docstring).
# ---------------------------------------------------------------------------

_STUDY_NODES_SQL = (
    "SELECT id, type, claim, confidence, recall_turn, status FROM nodes "
    "WHERE is_current = 1 AND COALESCE(status, '') != 'retracted' "
    "AND type IN (SELECT value FROM json_each(?))"
)
_PENDING_NODES_SQL = (
    "SELECT node_id FROM cascade_markers WHERE status = 'pending' ORDER BY id"
)
_LIVE_BRIEF_SQL = (
    "SELECT claim FROM nodes WHERE id = ? AND is_current = 1 AND COALESCE(status, '') != 'retracted'"
)


# ---------------------------------------------------------------------------
# Row checks (the owner's rows are only checked as scalars by the client): reuse
# weakest_load_bearing's shape checker, never a second copy.
# ---------------------------------------------------------------------------

MalformedAnswer = wlb.MalformedAnswer
_checked = wlb._checked
_is_str = wlb._is_str
_is_str_or_null = wlb._is_str_or_null
_is_number_or_null = wlb._is_number_or_null
_is_int_or_null = wlb._is_int_or_null


def _clip(text, limit: int = WHY_CHARS) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= limit else one[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# Source 1: the latest dream report (pure file reads).
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^##\s+(.*?)\s*$")
_NUMBERED_RE = re.compile(r"^(\d+)\.\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
_TRIAGE_RE = re.compile(r"^(o?)(\d+)\.\s*(.*)$")

def _sections(text: str) -> dict:
    """heading prefix key -> list of lines.  A heading is matched by PREFIX, so a parenthetical
    suffix ('## Sleep-end triage (parent)', '## Open for tomorrow (carried)') still lands."""
    wanted = {
        "for the awake seat": "awake",
        "open for tomorrow": "open",
        "sleep-end triage": "triage",
    }
    out: dict = {}
    current = None
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence  # a fenced block is quoted text: no headings, no items
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            current = None
            head = m.group(1).strip().lower()
            for prefix, key in wanted.items():
                if head.startswith(prefix):
                    if key in out:
                        break  # a duplicated heading: the FIRST one wins, the repeat is ignored
                    current = key
                    out[key] = []
                    break
            continue
        if line.startswith("#"):  # a deeper or top-level heading ends the section
            current = None
            continue
        if current is not None:
            out[current].append(line)
    return out


def _items(lines: list, pattern) -> list:
    """(number-or-None, text) for each item; indented lines continue the previous item."""
    items: list = []
    for line in lines:
        m = pattern.match(line)
        if m:
            items.append([m.group(1) if pattern is _NUMBERED_RE else None, m.groups()[-1].strip()])
        elif line.strip() and line[:1].isspace() and items:
            items[-1][1] += " " + line.strip()
    return [(n, t) for n, t in items]


def _triage(lines: list) -> dict:
    """('', N) / ('o', N) -> lowercased disposition text (everything after the number)."""
    out: dict = {}
    for line in lines:
        m = _TRIAGE_RE.match(line.strip())
        if m:
            out.setdefault((m.group(1), int(m.group(2))), m.group(3).strip().lower())
    return out


def _handled(disposition) -> bool:
    return disposition is not None and (disposition.startswith("do now") or disposition.startswith("file"))



def _dream_ref(text: str, report: str, tag: str) -> str:
    ids = _node_ids(text)
    return ",".join(ids) if ids else f"history/dream/{report}#{tag}"


def parse_dream_report(text: str, report_name: str) -> list:
    """The unhandled items of one dream report as candidates, in report order: the numbered
    awake-seat items first, then the open-for-tomorrow bullets.  Pure."""
    date = report_name[:-3] if report_name.endswith(".md") else report_name
    sections = _sections(text)
    triage = _triage(sections.get("triage", []))
    pickup = dream_pickup.read_pickup_dispositions(text)
    out = []
    numbered = _items(sections.get("awake", []), _NUMBERED_RE)
    for pos, (num, body) in enumerate(numbered, 1):
        n = int(num) if num is not None else pos
        if _handled(triage.get(("", n))):
            continue
        disposition = pickup.get(n)
        if disposition and disposition[0] == "picked_up":
            continue          # already handled — drop from queue
        if disposition and disposition[0] == "skipped":
            why_text = f"dream report {date} [skipped: {disposition[1]}]: {body}"
        else:
            why_text = f"dream report {date}: {body}"
        out.append({
            "source": "dream",
            "ref": _dream_ref(body, report_name, str(n)),
            "why": _clip(why_text),
        })
    bullets = _items(sections.get("open", []), _BULLET_RE)
    for pos, (_n, body) in enumerate(bullets, 1):
        if _handled(triage.get(("o", pos))):
            continue
        out.append({
            "source": "dream",
            "ref": _dream_ref(body, report_name, f"o{pos}"),
            "why": _clip(f"dream report {date} (open for tomorrow): {body}"),
        })
    return out


def latest_dream_report(home: Path):
    """The last date-prefixed dream record in <home>/history/dream, sorted by name WITHOUT the
    `.md` (a second dream on a day, `2026-10-02-turn110.md`, is LATER than `2026-10-02.md`, which
    plain filename order gets wrong because '-' sorts before '.'); a stray README.md or notes.md
    is never a report.  None when there is none."""
    d = Path(home) / "history" / "dream"
    try:
        names = sorted(
            (p.name for p in d.iterdir()
             if p.is_file() and p.name.endswith(".md") and _DATED_REPORT_RE.match(p.name)),
            key=lambda n: n[:-3],
        )
    except (FileNotFoundError, NotADirectoryError):
        return None
    return d / names[-1] if names else None


def dream_candidates(home: Path) -> list:
    path = latest_dream_report(home)
    if path is None:
        return []
    return parse_dream_report(path.read_text(encoding="utf-8", errors="replace"), path.name)


# ---------------------------------------------------------------------------
# Sources 3-5: the store (CORE: a pure read over an open connection).
# ---------------------------------------------------------------------------

def store_candidates(conn, skip_ids=frozenset()) -> dict:
    """{'question': [...], 'conjecture': [...], 'cascade': [...]} candidates, each in its own
    priority order and capped.  `skip_ids`: node ids an earlier source already names; they are dropped
    BEFORE the cap, so the cap counts only new targets.  Only SELECTs are issued."""
    rows = _checked(
        conn.execute(_STUDY_NODES_SQL, (_STUDY_TYPES_JSON,)).fetchall(),
        (_is_str, _is_str, _is_str_or_null, _is_number_or_null, _is_int_or_null, _is_str_or_null),
        "study nodes",
    )
    questions = sorted(
        (r for r in rows if r[1] == "question" and r[5] in _QUESTION_STATUSES and r[0] not in skip_ids),
        key=lambda r: (r[4] or 0, r[0]),
    )[:QUESTION_LIMIT]
    conjectures = sorted(
        (r for r in rows if r[1] == "conjecture" and r[5] == _CONJECTURE_STATUS and r[0] not in skip_ids),
        key=lambda r: (r[3] if r[3] is not None else 0.0, r[0]),
    )[:CONJECTURE_LIMIT]

    pending = _checked(
        conn.execute(_PENDING_NODES_SQL).fetchall(),
        (_is_str,),
        "pending markers",
    )
    cascade = []
    seen = set()
    for (nid,) in pending:
        if nid in seen or nid in skip_ids:
            continue
        seen.add(nid)
        brief = _checked(
            conn.execute(_LIVE_BRIEF_SQL, (nid,)).fetchall(), (_is_str_or_null,), "node brief"
        )
        if not brief:  # not current, or retracted: nothing to study
            continue
        cascade.append({
            "source": "cascade",
            "ref": nid,
            "why": _clip(f"pending cascade marker: {brief[0][0]}"),
        })
        if len(cascade) >= CASCADE_LIMIT:
            break

    return {
        "question": [{
            "source": "question",
            "ref": r[0],
            "why": _clip(f"open question, last recalled at turn {r[4] or 0}: {r[2]}"),
        } for r in questions],
        "conjecture": [{
            "source": "conjecture",
            "ref": r[0],
            "why": _clip("live conjecture, confidence "
                         + ("none" if r[3] is None else f"{r[3]:.2f}") + f": {r[2]}"),
        } for r in conjectures],
        "cascade": cascade,
    }


# ---------------------------------------------------------------------------
# Entry layer: the owner-read flag, the typed error, the one read path.
# ---------------------------------------------------------------------------

class StudyQueueOwnerReadError(RuntimeError):
    """The owner could not give the queue's rows (flag ON), or gave rows that do not fit. Raised
    instead of returning an empty or shorter queue: an empty queue would read downstream as
    "nothing is worth studying", a clean bill the store never gave."""


def _owner_spec(home: "Path | None" = None) -> "owner_read.OwnerReadSpec":
    return owner_read.OwnerReadSpec(
        flag_key=OWNER_FLAG_KEY,
        error_cls=StudyQueueOwnerReadError,
        home=Path(home) if home is not None else owner_read.engram_home(),
        never_empty="Not answering from an empty queue: that would read as 'nothing is worth studying'.",
    )


def read_store_candidates(db_path, home: "Path | None" = None, use_owner: "bool | None" = None,
                          skip_ids=frozenset()) -> dict:
    """The store candidates of `db_path`, read through the shared owner-read helper only.

    Behind engramd.study_queue_reads_via_owner (default OFF) the reads go to the engramd owner when
    `db_path` is this home's store.  Flag ON and the owner down, refusing or answering malformed
    RAISES StudyQueueOwnerReadError: never an empty queue, never a direct fallback.  Flag OFF opens
    the store `mode=ro`."""
    db_path = Path(db_path)
    spec = _owner_spec(home)
    if use_owner is None:
        use_owner = owner_read.flag_applies(spec, db_path)
    if use_owner:
        owner_read.ping(spec)
    # Flag OFF: `open_reader` runs `check_store_path` first (#3960); nothing happens in between.
    conn = owner_read.open_reader(spec, db_path, use_owner)
    try:
        return store_candidates(conn, skip_ids)
    except MalformedAnswer as exc:
        via = "the engramd owner answered" if use_owner else "the store answered"
        remedy = (f"retry once the owner is healthy, or set engramd.{spec.flag_key} to false to read the "
                  "store directly" if use_owner else "check the store")
        raise StudyQueueOwnerReadError(
            f"{via} a row that does not match the node schema ({exc}); {remedy}. {spec.never_empty}"
        ) from exc
    finally:
        conn.close()


def _weakest_candidates(db_path: Path) -> list:
    rows = wlb.read_ranking(db_path, n=1)
    out = []
    for r in rows:
        reason = f" [{r['weakness_reason']}]" if r["weakness_reason"] else ""
        out.append({
            "source": "weakest-belief",
            "ref": r["id"],
            "why": _clip(f"weakest load-bearing belief, score {r['score']:.2f} = load {r['load']} x "
                         f"weakness {r['weakness']:.2f}{reason}: {r['claim']}"),
        })
    return out


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_queue(home, db_path, n: int = 3, now: "str | None" = None) -> dict:
    """The queue as a dict: generated_at, targets [{rank, source, ref, why, method}], candidates
    (count after de-duplication by ref and by node id), not_wired.  Raises on any read error."""
    if n < 1:
        raise ValueError(f"n must be at least 1, got {n}")
    home, db_path = Path(home), Path(db_path)
    dream = dream_candidates(home)
    named = {i for c in dream for i in _node_ids(c["ref"])}
    # the store sources skip ids a dream item already names BEFORE their caps, so an absorbed
    # question does not use up one of the three question slots. The store read comes first on
    # purpose: with the owner flag ON and the owner down it must raise before anything else opens
    # the store (the weakest-belief read follows its own flag).
    store = read_store_candidates(db_path, home=home, skip_ids=frozenset(named))
    candidates = (
        dream
        + _weakest_candidates(db_path)
        + store["question"]
        + store["conjecture"]
        + store["cascade"]
    )
    unique, seen, seen_ids = [], set(), set()
    for c in candidates:
        if c["ref"] in seen:
            continue
        ids = set(_node_ids(c["ref"]))
        # a dream bullet's ref joins several ids ("qu_a,qu_b"): a later single-node candidate whose
        # id is already named by an earlier one is the same target, not a new one
        if ids and ids <= seen_ids:
            continue
        seen.add(c["ref"])
        seen_ids |= ids
        unique.append(c)
    targets = [
        {"rank": i, "source": c["source"], "ref": c["ref"], "why": c["why"],
         "method": METHODS[c["source"]]}
        for i, c in enumerate(unique[:n], 1)
    ]
    return {
        "generated_at": now or _iso_now(),
        "targets": targets,
        "candidates": len(unique),
        "not_wired": list(NOT_WIRED),
    }


def to_json(queue: dict) -> str:
    return json.dumps(queue, indent=2)


def format_text(queue: dict) -> str:
    targets = queue["targets"]
    if not targets:
        return f"study queue: nothing queued (empty: {', '.join(SOURCES)})"
    lines = [
        f"study queue ({len(targets)} of {queue['candidates']} candidates) - {queue['generated_at']}",
        STEP_ZERO,
    ]
    for t in targets:
        lines.append(f"{t['rank']}. [{t['source']}] {t['ref']} - {t['why']}")
        lines.append(f"   method: {t['method']}")
    lines.append("sources not wired in this version: " + ", ".join(queue["not_wired"]))
    return "\n".join(lines)


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--n must be an integer, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"--n must be at least 1, got {value}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="study_queue.py",
        description="This seat's top study targets: one id or file reference, why, and one method each. "
                    "Read-only. The queue on stdout and exit 0, or NOTHING on stdout and exit 1 with the "
                    "error class on stderr. A pre-3.10 interpreter with no usable venv refuses before "
                    "any work: one stderr line, exit 3.",
    )
    ap.add_argument("--n", type=_positive_int, default=3, help="how many targets, at least 1 (default 3)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--db", default=None, help="the store to read (default: <home>/knowledge.db)")
    ap.add_argument("--home", default=None, help="the ENGRAM home (default: $ENGRAM_HOME, else ~/.engram): the dream report, the owner flag and the socket follow it; the weakest-belief read's current turn follows the launch home")
    ap.add_argument("--now", default=None,
                    help="the generated_at stamp to print (for reproducible output; default: the UTC clock)")
    return ap


def main(argv: "list[str] | None" = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.home:
            home = Path(args.home).expanduser()
            if not home.is_absolute():
                home = home.resolve()
            # the weakest-belief read resolves ITS owner flag and socket from the environment, so they
            # follow --home; engram_core reads its config path at IMPORT, so the weakness read's
            # current_turn still comes from the launch home (low impact: it only reports recency)
            os.environ["ENGRAM_HOME"] = str(home)
        else:
            home = owner_read.engram_home()
        db = Path(args.db).expanduser() if args.db else home / "knowledge.db"
        queue = build_queue(home, db, n=args.n, now=args.now)
        text = to_json(queue) if args.json else format_text(queue)
    except Exception as exc:  # every failure is loud and empty on stdout; KeyboardInterrupt propagates
        sys.stderr.write(f"{type(exc).__name__}: {' '.join(str(exc).split())}\n")
        return 1
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
