#!/usr/bin/env python3
"""Raw-read gate: fail on a raw ENGRAM_HOME environment read outside the pinned resolvers [part of #3863].

Ruling: Borges's ruling on #3863, 2026-10-21.

A relative ENGRAM_HOME resolves against the CALLING process's cwd, so a hook, the owner daemon and the
MCP server would each land in a different home. The ONE resolution that refuses a relative value lives in
five pinned files (PINNED_RESOLVERS: the tools, hooks, prompt-hooks and shared-bin layers, plus the build
engine's flows.py), each AST-identical to tools/_owner_read.engram_home() (tests/test_engram_home_parity_3863.py).
Every OTHER module that reads the variable raw (the get, the subscript or getenv forms, see RAW_READ_RE)
takes the value as given and can silently use a relative one. This gate stops the number of those sites
growing.

WHAT IS A SITE. A tracked `.py` file under SCAN_PREFIXES (`src/` and `tools/`), excluding any `tests` or
`docs` path component, `test_*` basenames and symlinks (every `tools/<x>` is a tracked symlink to
`src/engram/tools/<x>`; counting both would double each site), is scanned LINE BY LINE (the unit is the
physical line, no parser). A line is a site when it matches RAW_READ_RE: a `environ[...]`, `environ.get(...)`
or `getenv(...)` whose first argument is the literal ENGRAM_HOME. Not sites: a subscript that is only
assigned to (a write), `setdefault`, `pop`, `dict(os.environ, ENGRAM_HOME=...)`, and pure comment lines.
A line that abspaths, expands or resolves the raw value is STILL a site: that is the silent version of the
defect (it turns a relative value into a cwd-dependent absolute one), never an exemption.

EXEMPT. The five PINNED_RESOLVERS files, as whole files. A pinned file is never in the baseline.

KNOWN BOUNDS (an instrument with edges, not a proof):
  * One physical line: a call split so the literal sits on another line is invisible.
  * A name held in a variable (`os.environ.get(KEY)`) or a loop (`for k in ("ENGRAM_HOME", ...)`) is invisible.
  * A pinned file is exempt whole, so a second raw read inside one of them is not counted. The parity test
    pins the resolver body, not the rest of the file.
  * A docstring that quotes a raw read verbatim counts like code.
  * Only src/ and tools/ are read; other trees are not measured.
  * Spellings the line pattern does not see (measured by the colleague pass on #4034): an alias read
    (`env = os.environ; env.get("ENGRAM_HOME")`, `from os import getenv as ge; ge("ENGRAM_HOME")`),
    whitespace before the paren (`os.environ.get ("ENGRAM_HOME")`), `os.environ.copy().get("ENGRAM_HOME")`,
    `dict(os.environ)["ENGRAM_HOME"]`, a keyword argument (`os.getenv(key="ENGRAM_HOME")`), a literal built
    by concatenation (`"ENGRAM_" "HOME"`, `"ENGRAM_" + "HOME"`) and `os.environb`.
  * Only tracked `.py` files are scanned, so a Python tool with no extension is invisible. Real case:
    src/engram/tools/engram-pkg/engram-pkg (line ~1199 reads ENGRAM_HOME raw) is an unbaselined read the
    gate cannot see.
  * So "stops the number of sites growing" holds for tracked non-test `.py` files and the spellings the
    pattern matches. It does not hold for the spellings above or for extensionless files.

THE BASELINE (tools/raw_home_read_baseline.json, schema 1):
  {"schema": 1,
   "scan_prefixes": ["src/", "tools/"],              (must equal SCAN_PREFIXES, else RED)
   "baseline":  {"<relpath>": <number of site LINES in that file>},
   "allowlist": {"<relpath>": {"class": "resolver", "reason": "<non-empty>"}}}   (exactly PINNED_RESOLVERS)
It is a shrink-only ratchet:
  a. a file with sites that is absent from the baseline, or whose count is above its baselined count -> RED,
     naming every `file:line` of that file;
  b. a baselined count ABOVE the tree's (a site was fixed, or the file is gone) is reported as SHRINKABLE
     and the check PASSES: say what to lower it to. Only growth fails the build;
  c. with --base-sha: a baseline entry absent from the base's baseline, or a count above the base's,
     -> RED "grew". A site added while another is removed in a different file therefore fails (the new
     file's entry is new). An unresolvable base or an unreadable base baseline is RED, never read as a
     seed; the seed is only a base with no baseline file at all;
  d. the allowlist is exactly the PINNED_RESOLVERS set, each with class `resolver` and a non-empty reason, so
     adding a pinned file is an edit to this file (visible in review); a pinned file may not be baselined.

Usage:
  raw_home_read_gate.py [--root DIR] [--base-sha SHA]   check (exit 1 on problems)
  raw_home_read_gate.py --report                        list every site per file with line numbers
  raw_home_read_gate.py --seed                          write a baseline of exact per-file counts (refuses if
                                                        one exists)

stdlib only: one `git ls-files` plus a regex pass; it runs in pr-checks' stdlib carve-out, well under a second.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

BASELINE_REL = "tools/raw_home_read_baseline.json"
SCAN_PREFIXES = ("src/", "tools/")
_SCHEMA = 1
# The five files that carry the ONE resolver body (four layers + the build engine's flows.py). They are
# exempt whole. Adding one is a deliberate act here and in tests/test_engram_home_parity_3863.py.
PINNED_RESOLVERS = (
    "src/engram/tools/_owner_read.py",
    "src/engram/hooks/claude/_hooklib.py",
    "src/engram/hooks/claude/_prompthooklib.py",
    "src/engram/tools/forum_api.py",
    "src/build/engine/flows.py",
)
_Q = r"""["']"""
# environ.get( / getenv( with the literal as the first argument, or the subscript form unless it is only
# assigned to (`environ["ENGRAM_HOME"] = x` is a write; `== x` is still a read).
RAW_READ_RE = re.compile(
    r"(?:\benviron\.get\(\s*|\bgetenv\(\s*)" + _Q + r"ENGRAM_HOME" + _Q
    + r"|\benviron\s*\[\s*" + _Q + r"ENGRAM_HOME" + _Q + r"\s*\](?!\s*=(?!=))"
)


def line_is_raw_read(line):
    """True for ONE physical line that is a raw ENGRAM_HOME read (a pure comment line is not)."""
    if line.lstrip().startswith("#"):
        return False
    return bool(RAW_READ_RE.search(line))


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def in_scope(rel):
    if not rel.startswith(SCAN_PREFIXES) or not rel.endswith(".py"):
        return False
    parts = rel.split("/")
    if "tests" in parts[:-1] or "docs" in parts[:-1]:
        return False
    return not parts[-1].startswith("test_")


def scanned_files(root):
    """Tracked, in-scope, non-symlink `.py` files that exist on disk. RuntimeError outside a git checkout."""
    r = _git(root, "ls-files", "-s", "-z", "--", *SCAN_PREFIXES)
    if r.returncode != 0:
        raise RuntimeError(f"not a git checkout: {root} ({r.stderr.strip()})")
    root = Path(root)
    out = []
    for rec in r.stdout.split("\0"):
        if not rec or "\t" not in rec:
            continue
        meta, rel = rec.split("\t", 1)
        if meta.split()[0] == "120000" or not in_scope(rel):
            continue
        if (root / rel).is_file():
            out.append(rel)
    return sorted(out)


def sites(root):
    """{relpath: [(lineno, line)]} for every NON-pinned scanned file with at least one site."""
    root = Path(root)
    out = {}
    for rel in scanned_files(root):
        if rel in PINNED_RESOLVERS:
            continue
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        rows = [(n, l) for n, l in enumerate(text.split("\n"), 1) if line_is_raw_read(l)]
        if rows:
            out[rel] = rows
    return out


def violation_counts(root):
    return {rel: len(rows) for rel, rows in sites(root).items()}


def _parse_baseline(text):
    """-> (baseline, allowlist, scan_prefixes). ValueError on a bad shape."""
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("schema") != _SCHEMA:
        raise ValueError(f"expected an object with schema {_SCHEMA}")
    base = data.get("baseline")
    if not isinstance(base, dict):
        raise ValueError("'baseline' must be an object {file: count}")
    for rel, n in base.items():
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ValueError(f"baseline count for {rel!r} must be an integer >= 0")
    allow = data.get("allowlist")
    if not isinstance(allow, dict) or not all(isinstance(v, dict) for v in allow.values()):
        raise ValueError("'allowlist' must be an object {file: {class, reason}}")
    prefixes = data.get("scan_prefixes")
    if prefixes is not None and (not isinstance(prefixes, list) or not all(isinstance(x, str) for x in prefixes)):
        raise ValueError("'scan_prefixes' must be a list of strings")
    return dict(base), dict(allow), prefixes


def _base_baseline(root, base_sha):
    """-> (baseline dict | None, problem | None). (None, None) is the seed."""
    if _git(root, "rev-parse", "--verify", "--quiet", f"{base_sha}^{{commit}}").returncode != 0:
        return None, f"cannot resolve base ref {base_sha!r}; the baseline-growth rule did not run"
    ls = _git(root, "ls-tree", base_sha, "--", BASELINE_REL)
    if ls.returncode != 0:
        return None, f"cannot list base {base_sha!r}: {ls.stderr.strip()}"
    if not ls.stdout.strip():
        return None, None
    shown = _git(root, "show", f"{base_sha}:{BASELINE_REL}")
    if shown.returncode != 0:
        return None, f"cannot read {BASELINE_REL} at base {base_sha!r}: {shown.stderr.strip()}"
    try:
        return _parse_baseline(shown.stdout)[0], None
    except ValueError as e:
        return None, f"{BASELINE_REL} at base {base_sha!r} is unreadable: {e}"


def _allowlist_problems(allow):
    problems = []
    for rel in sorted(set(allow) - set(PINNED_RESOLVERS)):
        problems.append(
            f"allowlist entry {rel} is not a pinned resolver: the allowlist is exactly PINNED_RESOLVERS "
            f"in {Path(__file__).name} (a new pinned file is added there, with its parity pin)"
        )
    for rel in sorted(set(PINNED_RESOLVERS) - set(allow)):
        problems.append(f"allowlist is missing the pinned resolver {rel}")
    for rel in sorted(set(allow) & set(PINNED_RESOLVERS)):
        entry = allow[rel]
        if entry.get("class") != "resolver":
            problems.append(f"allowlist entry {rel}: class {entry.get('class')!r} must be 'resolver'")
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"allowlist entry {rel}: reason must be a non-empty string")
    return problems


def analyse(root, base_sha=None):
    """-> (problems, shrinkable): both lists of strings. Empty `problems` means green."""
    root = Path(root)
    try:
        files = set(scanned_files(root))
    except RuntimeError as e:
        return [str(e)], []
    try:
        baseline, allow, prefixes = _parse_baseline((root / BASELINE_REL).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"baseline file missing: {BASELINE_REL} (create it with --seed)"], []
    except (ValueError, OSError) as e:
        return [f"{BASELINE_REL} is unreadable: {e}"], []

    problems, shrinkable = [], []
    if prefixes != list(SCAN_PREFIXES):
        problems.append(
            f"scope declaration out of sync with the gate: {BASELINE_REL} scan_prefixes is {prefixes!r}, "
            f"the gate scans {list(SCAN_PREFIXES)!r}"
        )
    problems += _allowlist_problems(allow)
    for rel in sorted(set(baseline) & set(PINNED_RESOLVERS)):
        problems.append(f"{rel} is a pinned resolver (exempt whole) and must not be in the baseline")

    found = sites(root)
    for rel in sorted(found):
        rows, n, allowed = found[rel], len(found[rel]), baseline.get(rel, 0)
        if n > allowed:
            where = ", ".join(f"{rel}:{ln}" for ln, _ in rows)
            what = "a new raw ENGRAM_HOME read" if rel not in baseline else f"{n} raw reads, the baseline allows {allowed}"
            problems.append(
                f"{what} in {rel} [{where}]: first {rows[0][1].strip()[:100]!r} -- call the layer's "
                f"engram_home() (it refuses a relative value) instead of reading the variable raw"
            )
    for rel in sorted(baseline):
        if rel in PINNED_RESOLVERS:
            continue
        n = len(found.get(rel, ()))
        if n < baseline[rel]:
            gone = "" if rel in files else " (file is gone or out of scope)"
            shrinkable.append(
                f"shrinkable: {rel} has {n} site(s) now, baselined at {baseline[rel]}{gone}: "
                f"lower it to {n}" + (" (remove the entry)" if n == 0 else "") + f" in {BASELINE_REL}"
            )

    if base_sha:
        base, prob = _base_baseline(root, base_sha)
        if prob:
            problems.append(prob)
        elif base is not None:
            for rel in sorted(baseline):
                if rel not in base:
                    problems.append(
                        f"baseline grew: {rel} is not in {BASELINE_REL} at base {base_sha[:12]}; the baseline "
                        f"may only shrink (fix the file instead)"
                    )
                elif baseline[rel] > base[rel]:
                    problems.append(
                        f"baseline grew: {rel} is {baseline[rel]} in {BASELINE_REL} but {base[rel]} at base "
                        f"{base_sha[:12]}; counts may only shrink"
                    )
    return problems, shrinkable


def check(root, base_sha=None):
    """The problem strings only; empty means green (shrinkable entries do not fail)."""
    return analyse(root, base_sha)[0]


def _seed(root):
    path = Path(root) / BASELINE_REL
    if path.exists():
        print(f"refusing to overwrite {BASELINE_REL}", file=sys.stderr)
        return 1
    doc = {
        "schema": _SCHEMA,
        "scan_prefixes": list(SCAN_PREFIXES),
        "baseline": dict(sorted(violation_counts(root).items())),
        "allowlist": {
            rel: {"class": "resolver", "reason": "one of the pinned resolver files: carries the ONE engram_home() body, AST-pinned by tests/test_engram_home_parity_3863.py"}
            for rel in PINNED_RESOLVERS
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return 0


def _report(root):
    found = sites(root)
    for rel, rows in sorted(found.items()):
        print(f"{rel}\t{len(rows)}")
        for ln, line in rows:
            print(f"  {ln}\t{line.strip()[:120]}")
    print(f"# {len(found)} file(s), {sum(len(v) for v in found.values())} site(s)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--base-sha", default=None, help="enable the baseline-growth rule against this commit")
    ap.add_argument("--report", action="store_true", help="list every site with its line number, per file")
    ap.add_argument("--seed", action="store_true", help="write a baseline of exact per-file counts (refuses if one exists)")
    args = ap.parse_args(argv)

    if args.seed:
        return _seed(args.root)
    if args.report:
        return _report(args.root)
    problems, shrinkable = analyse(args.root, base_sha=args.base_sha)
    for p in problems:
        print(p)
    for s in shrinkable:
        print(s)
    if problems:
        print(
            "raw-home-read gate: FAILED. Call the layer's engram_home() instead of reading ENGRAM_HOME raw; "
            f"the counts in {BASELINE_REL} may only shrink."
        )
        return 1
    try:
        baseline = _parse_baseline((Path(args.root) / BASELINE_REL).read_text(encoding="utf-8"))[0]
    except (OSError, ValueError):
        baseline = {}
    print(f"raw-home-read gate: ok ({sum(baseline.values())} baselined site(s) in {len(baseline)} file(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
