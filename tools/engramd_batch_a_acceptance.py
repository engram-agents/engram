#!/usr/bin/env python3
"""tools/engramd_batch_a_acceptance.py -- the Batch A acceptance script (part of #2652).

Batch A is the set of read-side tool flags (`engramd.<tool>_reads_via_owner`, and the one write-side
`regen_embeddings_via_owner`) that move a tool's store access to the engramd owner. A seat may turn a Batch A
flag ON only after this script has run on that seat's own store and every tool it could run said PASS.

For ONE seat the script
  1. takes ONE snapshot of the seat's store through the SQLite backup API (read side only) into a scratch
     directory it created,
  2. runs every Batch A tool twice, flag OFF and flag ON, each run on its OWN fresh copy of that one
     snapshot in a scratch ENGRAM_HOME (so a tool that writes in its OFF run cannot change what its ON run
     reads),
  3. compares the two runs, and counts the direct opens of the store copy made by the ON run,
  4. writes one report (stdout, and `--report <path>` as JSON).

The contract (the design seat's ruling on #2652):
  C1  Never the live store. Both paths are resolved with realpath; the script REFUSES (exit 2, named reason)
      before any tool runs when the scratch directory, or anything a tool's argv names, resolves to the seat's
      live home or live knowledge.db. Some Batch A tools write (`regen_embeddings`, `backup`). `--report` is
      held to the same rule: a report path (realpath) that is the live store or one of its -wal/-shm/-journal
      siblings, lies anywhere inside the live home, is an existing non-regular file, is a hard link (same
      device and inode) to the store, a sibling or a top-level file of the live home, has a parent directory
      that does not exist, or lies inside a scratch directory this run deletes is refused (exit 2) before the
      scratch directory is made or any tool runs.
  C2  Per tool: OFF and ON outputs are equal (stdout and exit code byte for byte after the tool's NAMED
      normalisations; a tool that writes artifacts also has them compared, and every declared artifact must
      EXIST and be non-empty in BOTH runs: one missing, zero-byte or an empty directory on either side is FAIL
      naming the artifact and the run) AND the ON run makes 0 direct opens of the store copy.
  C3  Positive verdicts only: PASS / FAIL / CONTROL-SILENT / SKIPPED-with-reason. A flag absent from the
      tree under test is SKIPPED, never PASS. A crash is FAIL with its stderr tail. Exit 0 only if every
      non-skipped tool is PASS (and at least one was). CONTROL-SILENT is not a PASS: it is a tool whose
      outputs are equal and whose ON run made 0 direct opens, but whose OFF run (the instrument's positive
      control) also made 0, so this run cannot show the counter would have seen an open. Check that tool by
      hand. Every FAIL condition outranks it.
  C4  One report: seat, SQLite build, deployed tag, the flag list read from the live seat config, and one
      line per tool (verdict, equality basis, open count).

THE INSTRUMENT. Opens are counted by the `sqlite3.connect` audit event -- the instrument the earlier
per-flag checks use (tools/engramd_a6_latency.py: `_audit_open_hook` / `_counting_store_opens`, realpath
match of the connect argument against the store copy). A tool runs as a child process, so the hook is
installed INSIDE each child: a `sitecustomize` module in a scratch directory on the child's PYTHONPATH
installs it at interpreter start (so it also reaches a Python process a shell tool starts) and appends one
line per matching open to a file the parent reads. `_store_path_of` below is a copy of the a6 function of the
same name (copied, not imported: this script must not break if that private helper is renamed).

An instrument that cannot say no is not evidence, so two controls ride on every tool:
  * the hook writes an `armed` line when it installs; a run with no `armed` line was not watched, and the
    verdict is FAIL "opens not countable by the instrument" -- never a PASS on a count of zero. That is also
    what a non-Python tool (a shell script that starts no Python) gets. A shell tool that reads the store
    with the `sqlite3` command-line binary is invisible to the hook even when it starts Python elsewhere:
    it is FAIL before it runs.
  * the OFF run is the positive control: a tool whose OFF run the instrument never saw open the store is
    FAIL ("ON == 0 proves nothing").

THE OWNER. A tool that needs the owner (`needs_owner`) gets a scratch owner (the tree's `engramd_owner.py`)
on the scratch ENGRAM_HOME for its ON run, started after the fresh copy is in place and stopped (SIGTERM,
wait, kill) when the run ends: replacing the store file under a running owner would displace it. The seat's
own owner is never contacted (the socket lives under the scratch home). If it cannot serve, the tool is FAIL
"scratch owner did not serve".

STATED LIMITS (what a PASS does not cover):
  * stderr is never compared. It carries timing and paths that differ between any two runs; it is kept only
    as the tail shown for a FAIL.
  * The open counter arms once per Python process. Each tool runs with PYTHONPATH set to a scratch directory
    holding a `sitecustomize` module, so the interpreter imports it at start-up and the hook installs; a
    Python the tool starts inherits that environment and arms too. A Python child escapes the counter only
    when it is started with -I (isolated mode: ignores PYTHONPATH, so the hook is never found), with -S
    (no `site`, so no sitecustomize import), or with PYTHONPATH or the environment cleared. Opens made in such
    a child are not counted; one armed process anywhere in a run is enough for the "armed" control. Checked
    by grep in the fourteen shipped tool files (anchoring_report and quote_quality_survey included) and the shell scripts under tools/engram-pkg and
    engram-fix-git-backup.sh: none starts Python with -I/-S or a cleared environment (the only
    subprocess in them that sets an environment is the backup tool's git call, which extends os.environ and
    starts no Python). Modules the tools import were not searched.
  * For the tools that write the store (`regen_embeddings`, `backup`) the resulting store state is not
    compared, only the output they print and the artifacts they declare.

SIGNALS. SIGTERM and SIGINT stop the run through the same cleanup as any other exit: the running tool's
process group is killed, the scratch owner is stopped and the scratch directory is removed (exit 143 / 130).
A further SIGTERM / SIGINT while that cleanup runs is ignored, so it cannot cut the cleanup short.

    python tools/engramd_batch_a_acceptance.py [--tree ROOT] [--live-home DIR] [--report PATH]
                                               [--only FLAG[,FLAG...]] [--python PATH] [--timeout S]
                                               [--scratch-base DIR] [--keep-scratch]

  --tree          the plugin/tree root under test (default: <live-home>/marketplace/plugins/engram)
  --live-home     the seat's ENGRAM_HOME (default: the shared resolver's value)
  --table         TEST ONLY: a JSON file that REPLACES the shipped table (see TOOLS for the entry shape)

Exit codes: 0 every non-skipped tool PASS (and one ran); 1 a tool FAILed; 2 refused (C1, a --report path included) or bad input;
3 nothing was verified (every tool SKIPPED); 4 the harness itself failed (snapshot, scratch setup, an OSError or
other unexpected exception outside a tool run; one line, no traceback; a tool that crashes, or whose declared
artifacts cannot be read after it ran, is a FAIL, not a 4);
5 no tool FAILed but at least one is CONTROL-SILENT (not a PASS: check those tools by hand).
When a FAIL and a CONTROL-SILENT both occur, the exit is 1.
"""
from __future__ import annotations

import argparse
import difflib
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional, Tuple

PROG = "engramd_batch_a_acceptance"
REPO_ROOT = Path(__file__).resolve().parent.parent

EXIT_PASS, EXIT_FAIL, EXIT_REFUSED, EXIT_NOTHING, EXIT_HARNESS, EXIT_CONTROL_SILENT = 0, 1, 2, 3, 4, 5

# --- the shipped table ---------------------------------------------------------------------------
#
# One entry per Batch A flag. Fields (all but flag/tool optional):
#   flag          the `engramd.<key>` config key; SKIPPED unless it appears in the tool's file
#   label         optional: the row's name in the report and in --only (default: the flag). Needed only
#                 when two rows share one flag.
#   artifact_normalise  names from NORMALISERS applied to each artifact FILE's text before it is hashed
#   tool          candidate paths relative to the tree root (first that exists wins; the deployed plugin
#                 flattens hooks/claude/ to hooks/)
#   runner        python (default) | bash | exec
#   argv          arguments after the tool path; ${home} ${db} ${work} ${tree} ${python} ${node_id}
#                 ${person_name} (JSON-escaped) are filled in; ${live_db} exists only so a test can prove
#                 the live-store refusal
#   stdin         text fed to the tool (same placeholders)
#   needs_owner   the ON run needs a scratch owner (default true)
#   seed          scratch setup before BOTH runs: "ledger" | "transcripts"
#   artifacts     files/directories (placeholders allowed) hashed after each run and compared
#   compare_stdout  default true; false for a tool whose log text differs ON/OFF by design (see
#                 `stdout_excluded_why`, which must then say why)
#   normalise     names from NORMALISERS (or inline {name, pattern, replacement, why}) applied to stdout
#   also_on       other flags turned ON together with this one in the ON run, for a tool that calls another
#                 flagged tool (study_queue calls the weakest-belief ranking, which follows ITS OWN flag)
#   ok_exits      exit codes that count as "ran" (default [0])
#   off_opens_required  default true (the positive control above)
#
# Invocations were chosen from each tool's own argparse/usage: the read-only deterministic mode where one
# exists (a fixed `--now`, a single node id), otherwise the tool's default run on the scratch copy.
TOOLS: List[Dict[str, Any]] = [
    # backup: the default run writes the dated .sql archive (+ binary copy) into the scratch home's
    # db-backup/. The dry run is NOT used: its ON path pings the owner and prints different text by design.
    {"flag": "backup_reads_via_owner", "tool": ["tools/backup_knowledge_db.py"], "argv": [],
     "normalise": ["backup_binary_stamp"]},
    # prepare reads the named node and writes payload files; the written directory is compared too.
    {"flag": "cohort_dispatch_reads_via_owner", "tool": ["tools/cohort_dispatch.py"],
     "argv": ["prepare", "--ids", "${node_id}", "--out", "${work}/cohort"], "artifacts": ["${work}/cohort"]},
    {"flag": "dream_safety_reads_via_owner", "tool": ["tools/dream_master_batch.py"],
     "argv": ["safety-row", "${node_id}"]},
    # regen loads the embedding model before it looks at the store and has no dry/limit mode: it needs the
    # model cached on this host (HF_HUB_OFFLINE=1 is set for it). Its --json object is the compared output.
    {"flag": "regen_embeddings_via_owner", "tool": ["tools/engram-regenerate-embeddings.py"], "argv": ["--json"]},
    # read-only; with an empty ledger the tool never opens the store, so a scratch ledger naming real
    # lessons of the copy is seeded (both runs) and --now is fixed.
    {"flag": "reconcile_demotions_reads_via_owner", "tool": ["tools/reconcile_lesson_demotions.py"],
     "argv": ["--now", "1800000000"], "seed": ["ledger"]},
    {"flag": "weakest_load_bearing_reads_via_owner", "tool": ["tools/weakest_load_bearing.py"],
     "argv": ["--json", "--n", "5"]},
    # a hook: a UserPromptSubmit payload whose prompt starts with a person node's name (or an unknown one).
    {"flag": "user_identity_reads_via_owner",
     "tool": ["hooks/engram-user-identity-hook.py", "hooks/claude/engram-user-identity-hook.py"],
     "argv": [], "stdin": '{"prompt": "${person_name}: hello"}'},
    # --now fixes the only clock value in the output.
    {"flag": "study_queue_reads_via_owner", "tool": ["tools/study_queue.py"],
     "argv": ["--json", "--n", "3", "--home", "${home}", "--now", "2026-01-01T00:00:00Z"],
     "also_on": ["weakest_load_bearing_reads_via_owner"]},
    # the audit needs transcripts; a scratch one is seeded instead of reading ~/.claude/projects.
    {"flag": "lesson_pattern_audit_reads_via_owner", "tool": ["tools/lesson-pattern-audit.py"],
     "argv": ["--transcript-glob", "${work}/transcripts/*.jsonl", "--transcripts", "3"], "seed": ["transcripts"]},
    {"flag": "inspect_raw_reads_via_owner", "tool": ["tools/inspect_raw.py"], "argv": ["${node_id}"]},
    # the tool is a shell script that starts Python for every store access, so the hook reaches those
    # processes (it uses no sqlite3 binary: checked statically before it runs). Its log text differs ON/OFF
    # by design ("verified: N nodes match" vs "quick_check ok, N nodes"), so the artifacts it writes are
    # the compared output.
    {"flag": "fix_git_backup_reads_via_owner", "tool": ["tools/engram-fix-git-backup.sh"], "runner": "bash",
     "argv": [], "artifacts": ["${home}/knowledge.sql", "${home}/.gitignore"], "compare_stdout": False,
     "stdout_excluded_why": "its progress text differs ON/OFF by design; the dump and .gitignore it writes are compared"},
    {"flag": "scope_export_reads_via_owner", "tool": ["tools/engram-pkg/engram-pkg"],
     "argv": ["scope-export", "--nodes", "${node_id}", "--out", "${work}/pkg", "--name", "batch-a-pkg"],
     "artifacts": ["${work}/pkg/knowledge.sql"], "normalise": ["scope_export_closure_line"]},
    # anchoring_report and quote_quality_survey ride ONE flag through two call sites (the survey loads
    # anchoring_report by path and rebinds three names), so a survey-only fault is caught only by the
    # survey's own comparison: two rows, one flag, told apart by `label`. The json report carries the only
    # clock value (generated_at).
    {"flag": "anchoring_report_reads_via_owner", "tool": ["tools/anchoring_report.py"],
     "argv": ["--format", "json"], "normalise": ["generated_at_field"]},
    # The survey WRITES a judging sheet and key: both go under the scratch work directory (never a live
    # path). The draw is seeded, the same seed both runs (4050 is the tool's own default). The key carries
    # generated_at too, so the artifacts are masked the same way. Exit 3 is ambiguous for this tool (a read
    # failure, or --score-sheet with unread answers): this invocation never scores, so a 3 is a read failure
    # and the FAIL shows the stderr tail.
    {"flag": "anchoring_report_reads_via_owner", "label": "anchoring_report_reads_via_owner:quote_quality_survey",
     "tool": ["tools/quote_quality_survey.py"],
     "argv": ["--format", "json", "--seed", "4050", "--judging-sheet", "${work}/survey-sheet.md",
              "--judging-key", "${work}/survey-key.json"],
     "artifacts": ["${work}/survey-sheet.md", "${work}/survey-key.json"],
     "normalise": ["generated_at_field"], "artifact_normalise": ["generated_at_field"]},
]

# Named normalisations. Each is named per tool in the table AND in the report, with why.
NORMALISERS: Dict[str, Dict[str, str]] = {
    "backup_binary_stamp": {
        "pattern": r"knowledge-\d{4}-\d{2}-\d{2}T\d{6}Z\.db",
        "replacement": "knowledge-<UTC-second>.db",
        "why": "the rollback binary copy's file name carries the wall-clock second of the run; the rest of "
               "the line (directory, ordering) and the archive line before it (byte and statement counts) "
               "must match",
    },
    "generated_at_field": {
        "pattern": r'("generated_at": )"[^"]*"',
        "replacement": r'\1"<generated_at>"',
        "why": "the report/key carries the wall-clock second of the run; every other field (counts, strata, "
               "the drawn items and their scores) must match byte for byte",
    },
    "scope_export_closure_line": {
        "pattern": r"^scope-export: computing closure from .*$",
        "replacement": "scope-export: computing closure from <source>",
        "why": "the first progress line names the source: the store file OFF, 'an engramd owner snapshot of' "
               "the store ON; the closure and everything after it must match",
    },
}

# --- the instrument ------------------------------------------------------------------------------

# Installed as `sitecustomize` on the children's PYTHONPATH. `_store_path_of` is a copy of
# tools/engramd_a6_latency._store_path_of (see the module docstring).
INSTRUMENT_SRC = '''\
import os, sys
from urllib.parse import unquote_to_bytes

_LOG = os.environ.get("BATCHA_OPEN_LOG")
_TARGET = os.environ.get("BATCHA_TARGET")


def _store_path_of(arg):
    try:
        text = os.fspath(arg)
    except TypeError:
        return None
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    if text == ":memory:":
        return None
    if text.startswith("file:"):
        text = text[len("file:"):]
        for sep in ("?", "#"):
            text = text.split(sep, 1)[0]
        if text.startswith("//"):
            text = text[2:]
            if text.startswith("localhost/"):
                text = text[len("localhost"):]
        text = os.fsdecode(unquote_to_bytes(text))
        if text in ("", ":memory:"):
            return None
    return os.path.realpath(text)


def _append(line):
    try:
        fd = os.open(_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except Exception:
        pass


def _hook(event, args):
    if event != "sqlite3.connect":
        return
    try:  # an audit hook that raises aborts the open it observes: never let this one
        if _store_path_of(args[0]) == _TARGET:
            _append("open %d %s\\n" % (os.getpid(), args[0]))
    except Exception:
        return


if _LOG and _TARGET:
    sys.addaudithook(_hook)
    _append("armed %d\\n" % os.getpid())
'''

# --- small helpers -------------------------------------------------------------------------------


class Refused(Exception):
    """A C1 refusal: the message names the reason."""


class HarnessError(Exception):
    """The script itself could not do its job (snapshot, scratch setup)."""


def _real(path) -> str:
    return os.path.realpath(os.fspath(path))


def _under(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _overlap(a: str, b: str) -> bool:
    return _under(a, b) or _under(b, a)


def _tail(text: str, limit: int = 1500) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def _load_owner_read():
    """The shared owner-read helper (tools/_owner_read.py): the ONE ENGRAM_HOME definition (#3863). The repo
    root is appended (never inserted, #2565) so `tools` resolves as it does under pytest."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.append(str(REPO_ROOT))
    from tools import _owner_read as owner_read
    return owner_read


# --- "absent" versus "could not be checked" ------------------------------------------------------
#
# `Path.is_file()` / `Path.is_dir()` answered False for a path that is MISSING and raised for one that could not
# be CHECKED (EACCES under an unsearchable directory) up to Python 3.12; 3.14 answers False for both, so a
# harness built on them reports an unreadable tree as a missing tool (SKIPPED, exit 3) instead of a harness
# failure (exit 4). The helpers below classify with `os.stat` and branch on the errno only, so every interpreter
# gives the 3.12 answer. The boundary is the one `engramd_a6_latency._classify_real_store` uses (copied, not
# imported: that is a private helper of another tool): the errnos pathlib used to swallow mean "absent"; any
# other OSError propagates to the caller.

_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP})


def _stat_or_absent(path: Path) -> Optional[os.stat_result]:
    """os.stat(path) (follows symlinks), or None when the path is absent. Any other OSError propagates."""
    try:
        return os.stat(path)
    except OSError as exc:
        if exc.errno in _ABSENT_ERRNOS:
            return None
        raise


def _is_file(path: Path) -> bool:
    st = _stat_or_absent(path)
    return st is not None and stat.S_ISREG(st.st_mode)


def _is_dir(path: Path) -> bool:
    st = _stat_or_absent(path)
    return st is not None and stat.S_ISDIR(st.st_mode)


def _deployed_tag(live_home: Path) -> str:
    try:
        for line in (live_home / ".deployed-version").read_text(errors="replace").splitlines():
            if line.startswith("alpha_tag="):
                return line.split("=", 1)[1].strip() or "unknown"
    except OSError:
        pass
    return "unknown"


def _flags_on(live_home: Path, tree: Path) -> Optional[List[str]]:
    """The ON owner flags of the LIVE seat config, from the same function `engramd_owner_flags.py --names`
    prints (`owner_flags_on`). Reported only; nothing here is flipped. None when no copy of the module exists."""
    import importlib.util
    for cand in (tree / "engramd_owner_flags.py", REPO_ROOT / "src" / "engram" / "engramd_owner_flags.py"):
        try:
            if not cand.is_file():
                continue
            spec = importlib.util.spec_from_file_location("_batcha_owner_flags", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return list(mod.owner_flags_on(live_home / "config.json"))
        except Exception:
            continue
    return None


def _sqlite_version(python: str) -> str:
    if os.path.realpath(python) == os.path.realpath(sys.executable):
        return sqlite3.sqlite_version
    try:
        out = subprocess.run([python, "-c", "import sqlite3; print(sqlite3.sqlite_version)"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# --- the table, the tool, the skip decision ------------------------------------------------------


def _load_table(path: Optional[str]) -> List[Dict[str, Any]]:
    if path is None:
        return [dict(e) for e in TOOLS]
    data = json.loads(Path(path).read_text())
    return data["tools"] if isinstance(data, dict) else data


def _candidates(entry: Dict[str, Any]) -> List[str]:
    tool = entry["tool"]
    return [tool] if isinstance(tool, str) else list(tool)


def _label(entry: Dict[str, Any]) -> str:
    """The name a row goes by in the report and in --only: the flag, unless two rows share one flag."""
    return entry.get("label") or entry["flag"]


def _resolve_tool(tree: Path, entry: Dict[str, Any]) -> Optional[Path]:
    for rel in _candidates(entry):
        path = tree / rel
        if _is_file(path):  # an uncheckable path raises: the harness failed (exit 4), the tool is not 'missing'
            return path
    return None


_SQLITE_CLI = re.compile(r"(?:^|[;&|(`]|\$\(|\bexec\s|\bxargs\s)\s*sqlite3(?:\s|$)")


def _uses_sqlite_cli(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if _SQLITE_CLI.search(stripped):
            return True
    return False


def _normalisers(entry: Dict[str, Any], field: str = "normalise") -> List[Dict[str, str]]:
    out = []
    for item in entry.get(field, []):
        if isinstance(item, str):
            spec = dict(NORMALISERS[item])
            spec["name"] = item
        else:
            spec = dict(item)
            spec.setdefault("name", "unnamed")
        out.append(spec)
    return out


def _apply_normalisers(text: str, specs: List[Dict[str, str]]) -> str:
    for spec in specs:
        text = re.sub(spec["pattern"], spec["replacement"], text, flags=re.MULTILINE)
    return text


def _basis(entry: Dict[str, Any], specs: List[Dict[str, str]]) -> str:
    parts = ["stdout+exit"] if entry.get("compare_stdout", True) else ["exit"]
    if entry.get("artifacts"):
        parts.append(f"artifacts[{len(entry['artifacts'])}]")
    text = "+".join(parts)
    if not entry.get("compare_stdout", True):
        text += f" (stdout excluded: {entry.get('stdout_excluded_why', 'by design')})"
    if specs:
        text += " (normalised: " + ", ".join(s["name"] for s in specs) + ")"
    return text


# --- scratch layout, snapshot, ids ---------------------------------------------------------------


class Scratch:
    """<root>/snapshot.db (the ONE snapshot), <root>/instr/sitecustomize.py, and the per-run tree
    <root>/run/{h,u,w}: h = ENGRAM_HOME, u = HOME, w = work. OFF and ON runs use the SAME paths, wiped
    between runs, so no path normalisation is ever needed."""

    def __init__(self, root: Path):
        self.root = root
        self.snapshot = root / "snapshot.db"
        self.instr = root / "instr"
        self.run = root / "run"
        self.home = self.run / "h"
        self.userhome = self.run / "u"
        self.work = self.run / "w"
        self.log = root / "opens.log"


def take_snapshot(live_db: Path, dest: Path) -> None:
    """The SQLite backup API, source opened read-only: a consistent copy, never a file copy of a live store."""
    try:
        src = sqlite3.connect(live_db.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise HarnessError(f"cannot open the live store read-only for the snapshot ({exc})") from exc
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    except sqlite3.Error as exc:
        raise HarnessError(f"the backup-API snapshot failed ({exc})") from exc
    finally:
        src.close()


def pick_ids(snapshot: Path) -> Dict[str, Any]:
    """Ids read from the SNAPSHOT (never the live store), the same for every OFF and ON run."""
    ids: Dict[str, Any] = {"node_id": "none", "person_name": "Nobody", "lesson_ids": []}
    try:
        conn = sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return ids
    try:
        def first(sql: str):
            try:
                return conn.execute(sql).fetchone()
            except sqlite3.Error:
                return None
        row = (first("select id from nodes where is_current=1 and type like 'observation%' order by id limit 1")
               or first("select id from nodes where is_current=1 order by id limit 1"))
        if row:
            ids["node_id"] = row[0]
        row = first("select metadata from nodes where type='person' and is_current=1 order by id limit 1")
        if row and row[0]:
            try:
                name = json.loads(row[0]).get("name")
                if isinstance(name, str) and name:
                    ids["person_name"] = name
            except (ValueError, AttributeError):
                pass
        try:
            ids["lesson_ids"] = [r[0] for r in conn.execute(
                "select id from nodes where type='lesson' and is_current=1 order by id limit 3")]
        except sqlite3.Error:
            pass
    finally:
        conn.close()
    return ids


def _seed(names: List[str], sc: Scratch, ids: Dict[str, Any]) -> None:
    for name in names:
        if name == "ledger":
            # The ledger the reconcile tool reads; without ledgered lessons it never opens the store.
            lessons = ids["lesson_ids"] or ["ls_none"]
            lines = [json.dumps({"mode": "auto_demoted_lease", "lesson_id": lid}) for lid in lessons]
            (sc.home / "lesson-prevention-ledger.jsonl").write_text("\n".join(lines) + "\n")
        elif name == "transcripts":
            tdir = sc.work / "transcripts"
            tdir.mkdir(parents=True, exist_ok=True)
            calls = [{"message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}
                     for cmd in ("ls -la", "python3 -m pytest -q", "cat notes.txt")]
            (tdir / "session-a.jsonl").write_text("\n".join(json.dumps(c) for c in calls) + "\n")
        else:
            raise HarnessError(f"unknown seed {name!r}")


def fresh_run(sc: Scratch, flag: str, flag_on: bool, entry: Dict[str, Any], ids: Dict[str, Any]) -> None:
    """Wipe the run tree and rebuild it from the ONE snapshot: store copy, a config carrying only this flag,
    the seeds. The open log is truncated, so each run's count is its own."""
    shutil.rmtree(sc.run, ignore_errors=True)
    for d in (sc.home, sc.userhome, sc.work, sc.work / "tmp"):
        d.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sc.snapshot, sc.home / "knowledge.db")
    flags = {flag: bool(flag_on)}
    flags.update({other: bool(flag_on) for other in entry.get("also_on", [])})
    (sc.home / "config.json").write_text(json.dumps({"engramd": flags}))
    _seed(entry.get("seed", []), sc, ids)
    sc.log.write_text("")


# --- running a tool ------------------------------------------------------------------------------


def _mapping(sc: Scratch, tree: Path, python: str, ids: Dict[str, Any], live_db: Path) -> Dict[str, str]:
    return {
        "home": str(sc.home), "db": str(sc.home / "knowledge.db"), "work": str(sc.work), "tree": str(tree),
        "python": python, "node_id": str(ids["node_id"]), "person_name": json.dumps(ids["person_name"])[1:-1],
        "live_db": str(live_db),
    }


def _subst(text: str, mapping: Dict[str, str]) -> str:
    return Template(text).safe_substitute(mapping)


def _child_env(sc: Scratch, tree: Path, python: str, with_instrument: bool) -> Dict[str, str]:
    # A minimal environment: nothing of the seat's session leaks in (CLAUDE_PLUGIN_ROOT in particular would
    # point a tool at the live plugin). HOME is scratch, so a tool that writes under ~ writes there.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(sc.userhome), "ENGRAM_HOME": str(sc.home), "TMPDIR": str(sc.work / "tmp"),
        "CLAUDE_PLUGIN_ROOT": str(tree), "PYTHON_BIN": python,
        "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8",
        # the embedding model is read from its cache, never downloaded
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
    }
    hf_home = os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "huggingface")
    if _is_dir(Path(hf_home)):
        env["HF_HOME"] = hf_home
    for key in ("SENTENCE_TRANSFORMERS_HOME", "TRANSFORMERS_CACHE", "HF_HUB_CACHE"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if with_instrument:
        env["PYTHONPATH"] = str(sc.instr)
        env["BATCHA_OPEN_LOG"] = str(sc.log)
        env["BATCHA_TARGET"] = _real(sc.home / "knowledge.db")
    return env


class RunResult:
    def __init__(self):
        self.exit: Optional[int] = None
        self.stdout = ""
        self.stderr = ""
        self.timed_out = False
        self.armed = 0
        self.opens = 0
        self.artifacts: Dict[str, str] = {}
        self.eval_error = ""  # set when the tool's outputs could not be evaluated after it ran


ABSENT = "<absent>"
EMPTY_DIR = "<empty-dir>"
EMPTY_FILE = "<empty-file>"


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _digest_masked(path: Path, specs: List[Dict[str, str]]) -> str:
    """The digest of a file's text after the entry's named artifact normalisations (none: the raw bytes)."""
    if not specs:
        return _digest(path)
    text = _apply_normalisers(path.read_bytes().decode(errors="replace"), specs)
    return hashlib.sha256(text.encode()).hexdigest()


def _raise(exc: OSError) -> None:
    raise exc


def _files_under(root: Path) -> List[Path]:
    """Every regular file under a directory, sorted as `sorted(root.rglob("*"))` was. `rglob` skips a
    subdirectory it cannot list, so its files would vanish from the digest without a word; `os.walk` with an
    `onerror` that raises turns that into the OSError instead. Symlinked directories are not descended into
    (as with rglob); a symlink to a file is digested; a dangling one is skipped."""
    found = []
    for dirpath, _dirs, names in os.walk(root, onerror=_raise):
        for name in names:
            sub = Path(dirpath) / name
            if _is_file(sub):
                found.append(sub)
    return sorted(found)


def _artifact_digests(paths: List[str], specs: Optional[List[Dict[str, str]]] = None) -> Dict[str, str]:
    out: Dict[str, str] = {}
    specs = specs or []
    for text in paths:
        p = Path(text)
        st = _stat_or_absent(p)  # one stat decides: absent, a file, a directory; an uncheckable path raises
        if st is not None and stat.S_ISREG(st.st_mode):
            # a declared file of size 0 gives nothing to compare: counted as missing in judge(), like an empty dir
            out[text] = EMPTY_FILE if st.st_size == 0 else _digest_masked(p, specs)
        elif st is not None and stat.S_ISDIR(st.st_mode):
            files = _files_under(p)
            for sub in files:
                out[f"{text}::{sub.relative_to(p)}"] = _digest_masked(sub, specs)
            if not files:
                out[text] = EMPTY_DIR  # exists but holds nothing to compare: counted as missing in judge()
        else:
            out[text] = ABSENT
    return out


def run_tool(entry: Dict[str, Any], tool: Path, sc: Scratch, tree: Path, python: str, ids: Dict[str, Any],
             live_db: Path, timeout: float) -> RunResult:
    mapping = _mapping(sc, tree, python, ids, live_db)
    argv = [_subst(a, mapping) for a in entry.get("argv", [])]
    runner = entry.get("runner", "python")
    cmd = {"python": [python, str(tool)], "bash": ["bash", str(tool)], "exec": [str(tool)]}[runner] + argv
    stdin = _subst(entry["stdin"], mapping) if entry.get("stdin") is not None else ""
    env = _child_env(sc, tree, python, with_instrument=True)
    for k, v in entry.get("env", {}).items():
        env[k] = _subst(v, mapping)
    res = RunResult()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=str(sc.work), env=env, start_new_session=True)
    try:
        out, err = proc.communicate(stdin.encode(), timeout=timeout)
    except BaseException as exc:
        if not isinstance(exc, subprocess.TimeoutExpired):
            # a signal (SystemExit / KeyboardInterrupt) or a harness fault: do not leave the tool running
            # against a scratch directory that is about to be deleted
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            raise
        res.timed_out = True
        try:  # the group this call started (start_new_session): its pid is its pgid
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = proc.communicate()
    res.exit = proc.returncode
    res.stdout = out.decode(errors="replace")
    res.stderr = err.decode(errors="replace")
    try:
        lines = sc.log.read_text().splitlines()
    except OSError:
        lines = []
    res.armed = sum(1 for ln in lines if ln.startswith("armed "))
    res.opens = sum(1 for ln in lines if ln.startswith("open "))
    try:
        res.artifacts = _artifact_digests([_subst(a, mapping) for a in entry.get("artifacts", [])],
                                          _normalisers(entry, "artifact_normalise"))
    except Exception as exc:  # this tool's outputs could not be read after it ran: its FAIL, not the run's
        res.eval_error = f"{type(exc).__name__}: {' '.join(str(exc).split())}"
    return res


# --- the scratch owner ---------------------------------------------------------------------------

_PING_PY = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from pathlib import Path\n"
    "import engramd_client as ec\n"
    "r = ec.read('ping', {}, timeout=1.5, socket_path=ec.socket_path_for_home(Path(sys.argv[2])))\n"
    "sys.exit(0 if isinstance(r, ec.Ok) else 1)\n"
)


class ScratchOwner:
    """The tree's engramd_owner.py, detached in its own session, on the scratch ENGRAM_HOME."""

    def __init__(self, sc: Scratch, tree: Path, python: str, serve_timeout: float = 30.0):
        self.sc, self.tree, self.python, self.serve_timeout = sc, tree, python, serve_timeout
        self.proc: Optional[subprocess.Popen] = None
        self.reason = ""

    def start(self) -> bool:
        owner_py = self.tree / "engramd_owner.py"
        if not _is_file(owner_py):  # an uncheckable path raises (the harness failed); only an absent one is 'no file'
            self.reason = f"the tree has no engramd_owner.py ({owner_py})"
            return False
        env = _child_env(self.sc, self.tree, self.python, with_instrument=False)
        logf = open(self.sc.root / "owner.log", "ab")
        try:
            self.proc = subprocess.Popen([self.python, str(owner_py)], stdin=subprocess.DEVNULL, stdout=logf,
                                         stderr=logf, env=env, start_new_session=True, close_fds=True)
        except OSError as exc:
            self.reason = f"the owner could not be started ({exc})"
            return False
        finally:
            logf.close()
        deadline = time.monotonic() + self.serve_timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.reason = f"the owner exited with {self.proc.returncode}: {self._log_tail()}"
                return False
            try:
                ping = subprocess.run([self.python, "-c", _PING_PY, str(self.tree), str(self.sc.home)],
                                      env=env, capture_output=True, timeout=15)
                if ping.returncode == 0:
                    return True
            except subprocess.TimeoutExpired:
                pass
            time.sleep(0.3)
        self.reason = f"no ping answer within {self.serve_timeout:g}s: {self._log_tail()}"
        return False

    def _log_tail(self) -> str:
        try:
            return _tail((self.sc.root / "owner.log").read_text(errors="replace"), 400)
        except OSError:
            return "(no owner log)"

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


# --- the verdict ---------------------------------------------------------------------------------


def _diff_note(a: str, b: str) -> str:
    diff = list(difflib.unified_diff(a.splitlines(), b.splitlines(), "OFF", "ON", lineterm="", n=0))
    return _tail(" | ".join(ln for ln in diff[2:8]), 300) or "(whitespace only)"


def judge(entry: Dict[str, Any], off: RunResult, on: RunResult, specs: List[Dict[str, str]]) -> Tuple[str, str]:
    """-> (verdict, reason): PASS, FAIL or CONTROL-SILENT. SKIPPED is decided before a tool runs."""
    ok_exits = entry.get("ok_exits", [0])
    for label, res in (("OFF", off), ("ON", on)):
        if res.eval_error:
            return "FAIL", f"the {label} run's outputs could not be evaluated ({res.eval_error})"
        if res.timed_out:
            return "FAIL", f"the {label} run timed out"
        if res.exit not in ok_exits:
            return "FAIL", f"the {label} run exited {res.exit} (expected {ok_exits})"
    for label, res in (("OFF", off), ("ON", on)):
        if res.armed == 0:
            return "FAIL", (f"opens not countable by the instrument: no Python process armed the open counter "
                            f"in the {label} run")
    problems: List[str] = []
    control_silent = False
    if entry.get("off_opens_required", True) and off.opens == 0:
        control_silent = True  # a verdict only if nothing below is a problem: every FAIL outranks it
    if entry.get("compare_stdout", True):
        a, b = _apply_normalisers(off.stdout, specs), _apply_normalisers(on.stdout, specs)
        if a != b:
            problems.append("OFF and ON stdout differ: " + _diff_note(a, b))
    if off.exit != on.exit:
        problems.append(f"exit codes differ (OFF {off.exit}, ON {on.exit})")
    for label, res in (("OFF", off), ("ON", on)):
        if entry.get("artifacts"):
            for key in sorted(res.artifacts):
                if res.artifacts[key] in (ABSENT, EMPTY_DIR, EMPTY_FILE):
                    kind = " (a zero-byte file)" if res.artifacts[key] == EMPTY_FILE else ""
                    problems.append(f"declared artifact {key} is missing in the {label} run{kind}")
    if off.artifacts != on.artifacts:
        bad = sorted(k for k in set(off.artifacts) | set(on.artifacts) if off.artifacts.get(k) != on.artifacts.get(k))
        problems.append("OFF and ON artifacts differ: " + ", ".join(bad[:4]))
    if on.opens > 0:
        problems.append(f"the ON run opened the store copy {on.opens} time(s) directly")
    if problems:
        return "FAIL", "; ".join(problems)
    if control_silent:
        return "CONTROL-SILENT", ("the OFF run made no direct open, so this run cannot show the counter would "
                                  "have seen one; check this tool by hand")
    return "PASS", f"outputs equal; ON run made 0 direct opens (OFF run: {off.opens})"


# --- one tool, end to end ------------------------------------------------------------------------


def evaluate(entry: Dict[str, Any], tree: Path, sc: Scratch, python: str, ids: Dict[str, Any], live_db: Path,
             timeout: float, owner_ready: Optional[Tuple[bool, str]], verbose: bool = False) -> Dict[str, Any]:
    flag = entry["flag"]
    specs = _normalisers(entry)
    result: Dict[str, Any] = {
        "flag": _label(entry), "tool": None, "verdict": "", "reason": "", "basis": _basis(entry, specs),
        "normalisations": [{"name": s["name"], "why": s.get("why", "")} for s in specs],
        "off_opens": None, "on_opens": None, "exit_off": None, "exit_on": None, "stderr_tail": "",
        "needs_owner": bool(entry.get("needs_owner", True)), "also_on": list(entry.get("also_on", [])),
    }
    tool = _resolve_tool(tree, entry)
    if tool is None:
        result.update(verdict="SKIPPED", reason=f"tool file not found in the tree: {', '.join(_candidates(entry))}")
        return result
    result["tool"] = str(tool.relative_to(tree))
    text = tool.read_text(errors="replace")
    if flag not in text:
        result.update(verdict="SKIPPED",
                      reason=f"the flag key {flag} does not appear in {result['tool']}: this tree predates the flag")
        return result
    if entry.get("runner", "python") != "python" and _uses_sqlite_cli(text):
        result.update(verdict="FAIL", reason="opens not countable by the instrument: the tool invokes the "
                                             "sqlite3 command-line binary, which the audit hook cannot see")
        return result
    if result["needs_owner"] and owner_ready is not None and not owner_ready[0]:
        result.update(verdict="FAIL", reason=f"scratch owner did not serve: {owner_ready[1]}")
        return result

    fresh_run(sc, flag, False, entry, ids)
    off = run_tool(entry, tool, sc, tree, python, ids, live_db, timeout)

    fresh_run(sc, flag, True, entry, ids)
    owner: Optional[ScratchOwner] = None
    try:
        if result["needs_owner"]:
            owner = ScratchOwner(sc, tree, python)
            if not owner.start():
                result.update(verdict="FAIL", reason=f"scratch owner did not serve: {owner.reason}",
                              off_opens=off.opens, exit_off=off.exit)
                return result
        on = run_tool(entry, tool, sc, tree, python, ids, live_db, timeout)
    finally:
        if owner is not None:
            owner.stop()

    verdict, reason = judge(entry, off, on, specs)
    result.update(verdict=verdict, reason=reason, off_opens=off.opens, on_opens=on.opens,
                  exit_off=off.exit, exit_on=on.exit)
    if verbose:
        result["stdout_head"], result["stderr_head"] = on.stdout[:200], on.stderr[:200]
    if verdict == "FAIL":
        failing = off if (off.exit not in entry.get("ok_exits", [0]) or off.timed_out) else on
        result["stderr_tail"] = _tail(failing.stderr)
    return result


# --- the C1 pre-flight ---------------------------------------------------------------------------


def _names_live_store(tokens: List[str], live_home_real: str, live_db_real: str) -> Optional[str]:
    for token in tokens:
        for piece in {token, token.split("=", 1)[-1]}:
            if not piece or "\n" in piece:
                continue
            candidate = _real(piece) if os.path.isabs(piece) else None
            if candidate and (candidate == live_db_real or _under(candidate, live_home_real)):
                return piece
    return None


STORE_SIBLINGS = ("", "-wal", "-shm", "-journal")


def _refuse_same_file_as_live(report: str, real: str, live_home: Path, live_db_real: str) -> None:
    """A hard link is another NAME for the same inode, so realpath cannot see it. An existing report file that
    shares (st_dev, st_ino) with the store, a -wal/-shm/-journal sibling, or any file at the top level of the
    live home is that file: writing the report would overwrite it. Raises Refused."""
    try:
        rst = os.stat(real)
    except OSError:
        return
    candidates = [live_db_real + s for s in STORE_SIBLINGS]
    try:
        candidates += [str(p) for p in live_home.iterdir()]
    except OSError:
        pass
    for cand in candidates:
        try:
            cst = os.stat(cand)
        except OSError:
            continue
        if (cst.st_dev, cst.st_ino) == (rst.st_dev, rst.st_ino):
            raise Refused(f"--report {report} is the same file as {cand} (a hard link: {rst.st_nlink} names for "
                          "one inode); a report must never overwrite a file of the live store or home")


REPORT_HINT = "use a path from `mktemp -d`"


def check_report_path(report: str, live_home: Path, base: Path, scratch_root: Optional[Path] = None) -> None:
    """The --report guard (below), with the usage hint appended to every refusal. Raises Refused."""
    try:
        _check_report_path(report, live_home, base, scratch_root)
    except Refused as exc:
        raise Refused(f"{exc}; {REPORT_HINT}") from None


def _check_report_path(report: str, live_home: Path, base: Path, scratch_root: Optional[Path] = None) -> None:
    """C1 for --report. The report is written after the tools have run, so a path that is (or leads to) the
    store would overwrite it. Resolved with realpath (a symlink to the store is the store), then compared by
    inode (a hard link to the store, a sibling or a top-level file of the live home is that file); a path whose
    parent directory does not exist is refused up front. Raises Refused."""
    real = _real(Path(report).expanduser())
    live_home_real = _real(live_home)
    live_db_real = _real(live_home / "knowledge.db")
    for suffix in STORE_SIBLINGS:
        if real == live_db_real + suffix:
            raise Refused(f"--report {report} is the live store ({live_db_real + suffix}); a report must never "
                          "overwrite it")
    if _under(real, live_home_real):
        raise Refused(f"--report {report} lies inside the live home {live_home_real}; write the report "
                      "somewhere else")
    if os.path.lexists(real) and not os.path.isfile(real):
        raise Refused(f"--report {report} is an existing path that is not a regular file")
    if scratch_root is not None:
        if _under(real, _real(scratch_root)):
            raise Refused(f"--report {report} lies inside this run's scratch directory, which the run deletes")
    else:
        base_real = _real(base)
        if _under(real, base_real) and os.path.relpath(real, base_real).split(os.sep)[0].startswith("bA-"):
            raise Refused(f"--report {report} lies inside a scratch directory under {base_real} of the kind "
                          "this run deletes")
    if os.path.isfile(real):
        _refuse_same_file_as_live(report, real, live_home, live_db_real)
    else:
        parent = os.path.dirname(real) or "."
        if not os.path.isdir(parent):
            raise Refused(f"--report {report}: its parent directory {parent} does not exist or cannot be "
                          "checked; create it first (the report is written after the tools ran)")


def preflight(live_home: Path, base: Path, table: List[Dict[str, Any]], tree: Path, python: str,
              report: Optional[str] = None) -> None:
    """Every C1 refusal that can be decided without creating anything. Raises Refused."""
    live_home_real = _real(live_home)
    live_db_real = _real(live_home / "knowledge.db")
    if report:
        check_report_path(report, live_home, base)
    if not _is_file(live_home / "knowledge.db"):  # an uncheckable store raises, naming the OSError
        raise HarnessError(f"no live store at {live_home / 'knowledge.db'}")
    base_real = _real(base)
    if _under(base_real, live_home_real):
        raise Refused(f"the scratch base {base_real} is inside the live home {live_home_real}; a scratch "
                      "directory must never be created in the seat's live home")
    # a throwaway mapping: the run paths are not made yet, so they stand in as unresolvable strings
    mapping = {"home": "<run-home>", "db": "<run-home>/knowledge.db", "work": "<run-work>", "tree": str(tree),
               "python": python, "node_id": "x", "person_name": "x", "live_db": str(live_home / "knowledge.db")}
    for entry in table:
        tokens = [_subst(a, mapping) for a in entry.get("argv", [])]
        tokens += [_subst(v, mapping) for v in entry.get("env", {}).values()]
        bad = _names_live_store(tokens, live_home_real, live_db_real)
        if bad:
            raise Refused(f"{entry['flag']}: an argument names the live store ({bad}); no tool ran")


def check_layout(sc: Scratch, live_home: Path, report: Optional[str] = None) -> None:
    if report:
        check_report_path(report, live_home, sc.root.parent, scratch_root=sc.root)
    live_home_real = _real(live_home)
    live_db_real = _real(live_home / "knowledge.db")
    for label, path in (("scratch root", sc.root), ("scratch ENGRAM_HOME", sc.home), ("scratch HOME", sc.userhome)):
        real = _real(path)
        if _overlap(real, live_home_real):
            raise Refused(f"the {label} {real} resolves into the live home {live_home_real}")
    if _real(sc.home / "knowledge.db") == live_db_real:
        raise Refused("the scratch store resolves to the live knowledge.db")


# --- the report ----------------------------------------------------------------------------------


def format_report(rep: Dict[str, Any]) -> str:
    lines = [
        "Batch A acceptance (part of #2652)",
        f"seat:          {rep['seat']}",
        f"sqlite:        {rep['sqlite_version']}   python: {rep['python']}",
        f"deployed tag:  {rep['deployed_tag']}",
        f"tree:          {rep['tree']}",
        "flags ON here: " + ("(unavailable: no engramd_owner_flags.py found)" if rep["flags_on"] is None
                             else (", ".join(rep["flags_on"]) or "(none)")),
        "",
    ]
    for t in rep["tools"]:
        opens = "" if t["on_opens"] is None else f"  opens(on={t['on_opens']} off={t['off_opens']})"
        tail = t["reason"] if t["verdict"] != "PASS" else ""
        also = f"  (also ON: {', '.join(t['also_on'])})" if t.get("also_on") else ""
        lines.append(f"{t['verdict']:<8} {t['flag']}  basis={t['basis']}{opens}{also}" + (f"  -- {tail}" if tail else ""))
        if t["verdict"] == "FAIL" and t["stderr_tail"]:
            lines.append("         stderr tail: " + t["stderr_tail"].replace("\n", " | ")[-400:])
    s = rep["summary"]
    lines += ["", f"summary: {s['PASS']} PASS, {s['FAIL']} FAIL, {s['CONTROL-SILENT']} CONTROL-SILENT, "
              f"{s['SKIPPED']} SKIPPED -> exit {rep['exit']}"]
    return "\n".join(lines)


def _home_forms() -> List[str]:
    """The operator's home as written (HOME) and as resolved, longest first; [] for no usable home."""
    home = os.path.expanduser("~")
    forms = {home.rstrip("/"), _real(Path(home)).rstrip("/")} if home and home != "~" else set()
    return sorted((f for f in forms if f), key=len, reverse=True)


def tilde_home(value: Any) -> Any:
    """Every string in `value` (a report dict, recursively) with the operator's home path written as `~`, so a
    report can be posted without the account name. A path outside the home stays absolute. Only a whole path
    component matches: a sibling folder whose name merely starts with the account name is not under it."""
    forms = _home_forms()
    if not forms:
        return value
    pattern = re.compile(r"(?<![\w.~/-])(?:" + "|".join(re.escape(f) for f in forms) + r")(?![\w.-])")

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return pattern.sub("~", v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    return walk(value)


def write_report_atomically(report: str, text: str) -> None:
    """Write `text` to the --report path through a temp file in the SAME directory (one filesystem), fsync,
    then os.replace onto the path. A rename replaces the directory entry, so a hard link or symlink created
    after check_report_path leads nowhere: the store's inode is never opened for writing. The temp file is
    removed on any error. Raises OSError."""
    target = os.path.abspath(os.path.expanduser(report))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".bA-report-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)  # mkstemp makes 0600; a plain write would have made 0666 & ~umask
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- main ----------------------------------------------------------------------------------------


_EXIT_EPILOG = """exit codes:
  0  every non-skipped tool PASS (and at least one ran)
  1  a tool FAILed (outranks CONTROL-SILENT)
  2  refused (C1, a --report path included) or bad input
  3  nothing was verified (every tool SKIPPED)
  4  the harness itself failed (snapshot, scratch setup, an OSError or other unexpected exception outside
     a tool run: one line, no traceback; a tool that crashes, or whose declared artifacts cannot be read
     after it ran, is a FAIL, not a 4)
  5  no FAIL, but at least one CONTROL-SILENT: not a PASS, check those tools by hand
  SIGTERM / SIGINT run the cleanup (tool killed, scratch owner stopped, scratch removed), then exit 143 / 130.

stated limits (what a PASS does not cover):
  - stderr is never compared (it carries timing and paths); it is only shown, as a tail, for a FAIL.
  - the open counter arms per Python process: each tool runs with PYTHONPATH pointing at a scratch directory
    that holds a sitecustomize module. A Python child escapes it only when started with -I/-S (-I ignores
    PYTHONPATH, -S skips site and so sitecustomize) or with PYTHONPATH/the environment cleared; its opens
    are then not counted. Not found in the fourteen shipped tool files or their shell helpers (imported modules
    were not searched).
  - for the tools that write the store (regen_embeddings, backup) the resulting store state is not compared,
    only the output they print and the artifacts they declare."""


def _ignore_signals() -> Dict[int, Any]:
    """Ignore SIGTERM and SIGINT; -> the handlers they had (for _restore_signals). Not the main thread: no-op."""
    previous: Dict[int, Any] = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, signal.SIG_IGN)
        except ValueError:
            pass
    return previous


def _restore_signals(previous: Dict[int, Any]) -> None:
    for sig, handler in previous.items():
        signal.signal(sig, handler)


def _raise_on_signal(signum, _frame):
    """SIGTERM / SIGINT become SystemExit so every `finally` below runs (scratch owner, tool group, scratch dir).
    The first signal also switches both to ignored: a second one (a supervisor that retries `kill`) must not
    interrupt the cleanup that the first started."""
    _ignore_signals()
    raise SystemExit(128 + signum)


def main(argv: Optional[List[str]] = None) -> int:
    """Install the SIGTERM/SIGINT handlers for the run, restore the previous ones on the way out."""
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, _raise_on_signal)
        except ValueError:  # not the main thread: no handler can be installed, run without
            pass
    try:
        return _main(argv)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _main(argv: Optional[List[str]]) -> int:
    p = argparse.ArgumentParser(prog=PROG, description=__doc__.split("\n\n")[0], epilog=_EXIT_EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tree", help="the tree root under test (default: <live-home>/marketplace/plugins/engram)")
    p.add_argument("--live-home", help="the seat's ENGRAM_HOME (default: the shared resolver's value)")
    p.add_argument("--report", help="write the JSON report here (a path that is not in the live home or a scratch "
                                    "directory this run deletes: use a path from `mktemp -d`)")
    p.add_argument("--only", help="comma-separated flag keys to run (default: all)")
    p.add_argument("--python", default=sys.executable, help="the interpreter the tools run under")
    p.add_argument("--timeout", type=float, default=600.0, help="per-run limit in seconds (default 600)")
    p.add_argument("--scratch-base", help="where the scratch directory is created (default: the system temp dir)")
    p.add_argument("--keep-scratch", action="store_true", help="do not delete the scratch directory (a store copy)")
    p.add_argument("--table", help="TEST ONLY: a JSON table that replaces the shipped one")
    p.add_argument("--verbose", action="store_true",
                   help="add the first 200 characters of each tool's ON stdout and stderr to the report (the "
                        "output can carry node text from the store copy: leave it off for a shared report)")
    args = p.parse_args(argv)

    try:
        live_home = Path(args.live_home).expanduser() if args.live_home else _load_owner_read().engram_home()
    except ValueError as exc:
        print(f"{PROG}: error -- {' '.join(str(exc).split())}", file=sys.stderr)
        return EXIT_REFUSED
    if not live_home.is_absolute():
        print(f"{PROG}: error -- --live-home must be an absolute path, got {str(live_home)!r}", file=sys.stderr)
        return EXIT_REFUSED
    tree = Path(args.tree).expanduser() if args.tree else live_home / "marketplace" / "plugins" / "engram"
    python = args.python
    try:
        table = _load_table(args.table)
    except (OSError, ValueError, KeyError) as exc:
        print(f"{PROG}: error -- cannot read the table ({exc})", file=sys.stderr)
        return EXIT_REFUSED
    if args.only:
        wanted = {f.strip() for f in args.only.split(",") if f.strip()}
        unknown = wanted - {_label(e) for e in table}
        if unknown:
            print(f"{PROG}: error -- --only names flags that are not in the table: {sorted(unknown)}", file=sys.stderr)
            return EXIT_REFUSED
        table = [e for e in table if _label(e) in wanted]
    base = Path(args.scratch_base).expanduser() if args.scratch_base else Path(tempfile.gettempdir())

    sc: Optional[Scratch] = None
    try:
        preflight(live_home, base, table, tree, python, args.report)
        base.mkdir(parents=True, exist_ok=True)
        sc = Scratch(Path(tempfile.mkdtemp(prefix="bA-", dir=str(base))))
        check_layout(sc, live_home, args.report)
        sc.instr.mkdir()
        (sc.instr / "sitecustomize.py").write_text(INSTRUMENT_SRC)
        take_snapshot(live_home / "knowledge.db", sc.snapshot)
        ids = pick_ids(sc.snapshot)

        # a tool that needs the owner is only run once a scratch owner has been seen to serve
        owner_ready: Optional[Tuple[bool, str]] = None
        if any(e.get("needs_owner", True) and _resolve_tool(tree, e) is not None for e in table):
            fresh_run(sc, "owner_probe", True, {}, ids)
            probe = ScratchOwner(sc, tree, python)
            try:
                owner_ready = (probe.start(), probe.reason)
            finally:
                probe.stop()

        results = [evaluate(e, tree, sc, python, ids, live_home / "knowledge.db", args.timeout, owner_ready,
                            args.verbose)
                   for e in table]
    except Refused as exc:
        print(f"{PROG}: refused -- {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except HarnessError as exc:
        print(f"{PROG}: error -- {exc}", file=sys.stderr)
        return EXIT_HARNESS
    except Exception as exc:  # OSError and anything else unexpected OUTSIDE a tool run: the harness failed
        print(f"{PROG}: error -- harness failure ({type(exc).__name__}: {' '.join(str(exc).split())})",
              file=sys.stderr)
        return EXIT_HARNESS
    finally:
        # no signal may cut the cleanup short; the handlers come back when it is done
        held = _ignore_signals()
        try:
            if sc is not None and not args.keep_scratch:
                shutil.rmtree(sc.root, ignore_errors=True)
        finally:
            _restore_signals(held)

    summary = {k: sum(1 for r in results if r["verdict"] == k) for k in ("PASS", "FAIL", "CONTROL-SILENT", "SKIPPED")}
    if summary["FAIL"]:
        code = EXIT_FAIL
    elif summary["CONTROL-SILENT"]:
        code = EXIT_CONTROL_SILENT
    elif summary["PASS"] == 0:
        code = EXIT_NOTHING
    else:
        code = EXIT_PASS
    report = {
        "seat": socket.gethostname(), "sqlite_version": _sqlite_version(python), "python": sys.version.split()[0],
        "deployed_tag": _deployed_tag(live_home), "tree": str(tree), "flags_on": _flags_on(live_home, tree),
        "tools": results, "summary": summary, "exit": code,
    }
    report = tilde_home(report)
    print(format_report(report))
    if args.report:
        try:
            write_report_atomically(args.report, json.dumps(report, indent=2) + "\n")
        except OSError as exc:
            print(f"{PROG}: error -- cannot write the report ({exc})", file=sys.stderr)
            return EXIT_HARNESS
    return code


if __name__ == "__main__":
    sys.exit(main())
