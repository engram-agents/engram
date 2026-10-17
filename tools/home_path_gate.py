#!/usr/bin/env python3
"""Home-path gate: fail on a NEW hard-coded ENGRAM home path [part of #3863].

Lei's ruling (2026-10-17): the deployment must use ENGRAM_HOME wherever
possible, because the install allows a custom engram folder. A shipped file
that reaches the store (or anything else under the ENGRAM home) through a
hard-coded `~/.engram` is a defect, even on a seat where `~/.engram` happens to
be right today.

WHAT IS A VIOLATION. A tracked file under `src/engram/` (excluding tests and
docs) that contains at least one hard-coded ENGRAM-home spelling (the
SPELLINGS table below) and zero occurrences of the substring `ENGRAM_HOME`.
Scope: every tracked `src/engram/` file except a `tests` or `docs` path
component and `test_*` basenames. That is wider than "what ships at some tier":
three of the issue's 13 files (the `migrate_*` one-shots) are manifest-excluded
and never ship, but the issue names them as positive controls, so the gate
reads the source tree, not the build manifest.

KNOWN BOUNDS (the instrument is the issue's floor, made wider, not a proof):
  * File-level. A file that reads ENGRAM_HOME in one place and hard-codes the
    home in another counts clean.
  * The test is the substring `ENGRAM_HOME`, so a comment or a longer name
    containing it also counts as reading it.
  * Line-level matching. A home built across lines (a variable holding
    `Path.home()` joined with `".engram"` three lines later) is invisible
    unless the quoted-segment row catches the `".engram"` literal.
  * A home built from a variable-held HOME with no `.engram` literal is
    invisible.
  * Only the default folder name `.engram` is matched. A sibling such as
    `~/.engram-gemini` is a different home, not this defect (the lookahead
    `(?![\\w-])` after each `.engram` is what excludes it, along with
    `.engram_home`, `.engram_query` and the like).

THE BASELINE (tools/home_path_baseline.json). Today's violators, checked in.
It may only SHRINK:
  1. a violator not in the baseline            -> RED
  2. a baseline entry that no longer violates  -> RED "stale", remove it
  3. a baseline/allowlist entry naming a file that is not a scanned tracked
     file (missing, or outside the scope above) -> RED "dangling"
  4. with --base-sha: a head baseline entry absent from the baseline at the
     base commit -> RED "grew". The only exception is the SEED: the base
     commit has no baseline file at all (`git ls-tree` finds nothing at the
     path). An unresolvable base ref or an unreadable base baseline is RED, it
     is never read as a seed.
  5. the `allowlist` key exists and is EMPTY today. It is for the single future
     resolver; the PR that creates the resolver adds its entry in the same
     commit (a pre-written entry would bless whatever file later lands there).

Rule 4 compares against the base commit CI passes. On a pull_request run HEAD
is GitHub's merge commit, so `git merge-base origin/<base> HEAD` is the base
branch's tip as of that merge ref, not the PR's branch point: a stricter, more
current comparison. Moving BASELINE_REL to a new path
would read as a seed; that edit is to this file and is visible in review.

Usage:
  home_path_gate.py [--root DIR] [--base-sha SHA]   check (exit 1 on problems)
  home_path_gate.py --report                        list violators + matches
  home_path_gate.py --seed                          write the baseline from the
                                                    current tree (refuses if one
                                                    exists)

stdlib only: it runs in the pr-checks stdlib carve-out, wall time well under a
second.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

BASELINE_REL = "tools/home_path_baseline.json"
SCAN_PREFIX = "src/engram/"
HOME_TOKEN = "ENGRAM_HOME"
_SCHEMA = 1

# `.engram` as the whole folder name: `.engram-gemini`, `.engram_home` and
# `.engram_query` do not match (the lookahead); a launchd label such as
# `com.engram.viz-server` has no home-ref context in front of it.
_E = r"\.engram(?![\w-])"
_Q = r"""["']"""

# (name, regex). One row per spelling; each has a planted case in
# tests/test_home_path_gate.py. A match is per line.
SPELLINGS = [
    # Path.home() / ".engram"
    ("path_home_div", re.compile(r"Path\.home\(\)\s*/\s*" + _Q + _E)),
    # Path.home().joinpath(".engram", ...)
    ("path_home_joinpath", re.compile(r"Path\.home\(\)\s*\.joinpath\(\s*" + _Q + _E)),
    # Path("~/.engram").expanduser()
    ("path_tilde_literal", re.compile(r"Path\(\s*[rRfF]?" + _Q + r"~/" + _E)),
    # Path("~").expanduser() / ".engram"
    (
        "path_tilde_div",
        re.compile(
            r"Path\(\s*" + _Q + r"~" + _Q + r"\s*\)(?:\s*\.expanduser\(\))?\s*/\s*" + _Q + _E
        ),
    ),
    # os.path.expanduser("~/.engram...")
    ("os_expanduser_tilde_engram", re.compile(r"expanduser\(\s*[rRfF]?" + _Q + r"~/" + _E)),
    # os.path.join(os.path.expanduser("~"), ".engram") and
    # os.path.expanduser("~") + "/.engram" (a bare "~" + "/.engram" is the
    # engram_after_slash_or_quote row at the end of the table)
    (
        "expanduser_tilde_then_engram",
        re.compile(r"expanduser\(\s*" + _Q + r"~" + _Q + r"\s*\)[^\n]{0,80}?" + _E),
    ),
    # os.path.join(str(Path.home()), ".engram")
    ("path_home_join_arg", re.compile(r"Path\.home\(\)\)?\s*,\s*" + _Q + _E)),
    # os.environ["HOME"] ... .engram
    (
        "environ_home_index",
        re.compile(r"environ\[\s*" + _Q + r"HOME" + _Q + r"\s*\][^\n]{0,80}?" + _E),
    ),
    # os.environ.get("HOME"...) ... .engram
    (
        "environ_home_get",
        re.compile(r"environ\.get\(\s*" + _Q + r"HOME" + _Q + r"[^\n]{0,80}?" + _E),
    ),
    # os.getenv("HOME") ... .engram
    (
        "getenv_home",
        re.compile(r"getenv\(\s*" + _Q + r"HOME" + _Q + r"[^\n]{0,80}?" + _E),
    ),
    # ~/.engram in strings and prose a user would paste (any file under it)
    ("tilde_slash", re.compile(r"~/" + _E)),
    # $HOME/.engram, ${HOME}/.engram, "$HOME"/.engram
    ("shell_home_var", re.compile(r"\$\{?HOME\}?\"?/" + _E)),
    # /home/<user>/.engram, /Users/<user>/.engram, /root/.engram
    ("abs_user_home", re.compile(r"(?:/home/[\w.-]+|/Users/[\w.-]+|/root)/" + _E)),
    # catch-all: a quoted ".engram" path segment (join(seat_home, ".engram"), tuples)
    ("quoted_segment", re.compile(_Q + r"\.engram" + _Q)),
    # catch-all: `.engram` as a folder name directly after a slash or a
    # quote/backtick, however the home in front of it was built (f-strings,
    # `h + '/.engram'`, `$(echo ~)/.engram`, `/home/$USER/.engram`, JS
    # `process.env.HOME + '/.engram'`). `.engram-gemini` / `.engram_home` stay
    # out via _E's lookahead; `com.engram.*` has no slash or quote before it.
    ("engram_after_slash_or_quote", re.compile(r"(?<=[/\"'`])" + _E)),
]


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True
    )


def in_scope(rel):
    if not rel.startswith(SCAN_PREFIX):
        return False
    parts = rel.split("/")
    if "tests" in parts[:-1] or "docs" in parts[:-1]:
        return False
    return not parts[-1].startswith("test_")


def scanned_files(root):
    """Tracked files in scope that exist on disk. Raises RuntimeError outside
    a git checkout."""
    r = _git(root, "ls-files", "-z", "--", SCAN_PREFIX)
    if r.returncode != 0:
        raise RuntimeError(f"not a git checkout: {root} ({r.stderr.strip()})")
    root = Path(root)
    return sorted(
        rel for rel in r.stdout.split("\0") if rel and in_scope(rel) and (root / rel).is_file()
    )


def violations(root):
    """{relpath: {spelling_name: first matching line}} for every scanned file
    that has a spelling and does not contain ENGRAM_HOME."""
    root = Path(root)
    out = {}
    for rel in scanned_files(root):
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        if HOME_TOKEN in text:
            continue
        hits = {}
        for line in text.splitlines():
            for name, rx in SPELLINGS:
                m = rx.search(line) if name not in hits else None
                if m:
                    hits[name] = line[max(0, m.start() - 40) : m.end() + 40].strip()
        if hits:
            out[rel] = hits
    return out


def _parse_baseline(text):
    """-> (baseline set, allowlist set). Raises ValueError on a bad shape."""
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("schema") != _SCHEMA:
        raise ValueError(f"expected an object with schema {_SCHEMA}")
    for key in ("baseline", "allowlist"):
        v = data.get(key)
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(f"'{key}' must be a list of strings")
    return set(data["baseline"]), set(data["allowlist"])


def load_baseline(root):
    """-> (baseline set, allowlist set) from the working tree."""
    return _parse_baseline((Path(root) / BASELINE_REL).read_text(encoding="utf-8"))


def _base_baseline(root, base_sha):
    """-> (baseline set | None, problem | None). None set with no problem is
    the seed (base commit has no baseline file)."""
    if _git(root, "rev-parse", "--verify", "--quiet", f"{base_sha}^{{commit}}").returncode != 0:
        return None, f"cannot resolve base ref {base_sha!r}; the baseline-growth rule did not run"
    ls = _git(root, "ls-tree", base_sha, "--", BASELINE_REL)
    if ls.returncode != 0:
        return None, f"cannot list base {base_sha!r}: {ls.stderr.strip()}"
    if not ls.stdout.strip():
        return None, None  # seed
    shown = _git(root, "show", f"{base_sha}:{BASELINE_REL}")
    if shown.returncode != 0:
        return None, f"cannot read {BASELINE_REL} at base {base_sha!r}: {shown.stderr.strip()}"
    try:
        return _parse_baseline(shown.stdout)[0], None
    except ValueError as e:  # includes json.JSONDecodeError
        return None, f"{BASELINE_REL} at base {base_sha!r} is unreadable: {e}"


def check(root, base_sha=None):
    """Return a list of problem strings; empty means green."""
    root = Path(root)
    try:
        files = set(scanned_files(root))
    except RuntimeError as e:
        return [str(e)]
    try:
        baseline, allow = load_baseline(root)
    except FileNotFoundError:
        return [f"baseline file missing: {BASELINE_REL} (create it with --seed)"]
    except (ValueError, OSError) as e:
        return [f"{BASELINE_REL} is unreadable: {e}"]

    problems = []
    for label, entries in (("baseline", baseline), ("allowlist", allow)):
        for rel in sorted(entries - files):
            problems.append(
                f"dangling {label} entry (not a scanned tracked file under {SCAN_PREFIX}): {rel} "
                f"(a renamed baselined file reads as growth: fix the file in the same PR)"
            )

    effective = {f: h for f, h in violations(root).items() if f not in allow}
    for rel in sorted(set(effective) - baseline):
        spell = ", ".join(sorted(effective[rel]))
        sample = next(iter(effective[rel].values()))
        problems.append(
            f"new hard-coded ENGRAM home path in {rel} [{spell}]: {sample!r} "
            f"-- read ENGRAM_HOME (env, else the default) instead of spelling the folder"
        )
    for rel in sorted((baseline & files) - set(effective)):
        problems.append(f"stale baseline entry, remove it from {BASELINE_REL}: {rel}")

    if base_sha:
        base_entries, prob = _base_baseline(root, base_sha)
        if prob:
            problems.append(prob)
        elif base_entries is not None:
            for rel in sorted(baseline - base_entries):
                problems.append(
                    f"baseline grew: {rel} is not in {BASELINE_REL} at base {base_sha[:12]}; "
                    f"the baseline may only shrink (fix the file instead; a renamed "
                    f"baselined file reads as growth, so fix it in the same PR)"
                )
    return problems


def _seed(root):
    path = Path(root) / BASELINE_REL
    if path.exists():
        print(f"refusing to overwrite {BASELINE_REL}", file=sys.stderr)
        return 1
    path.write_text(
        json.dumps(
            {"schema": _SCHEMA, "baseline": sorted(violations(root)), "allowlist": []},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--base-sha", default=None, help="enable the baseline-growth rule")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--seed", action="store_true")
    args = ap.parse_args(argv)

    if args.seed:
        return _seed(args.root)
    if args.report:
        for rel, hits in sorted(violations(args.root).items()):
            name, line = next(iter(hits.items()))
            print(f"{rel}\t[{','.join(sorted(hits))}]\t{line}")
        return 0
    problems = check(args.root, base_sha=args.base_sha)
    for p in problems:
        print(p)
    if problems:
        print(
            "home-path gate: FAILED. Fix the file (read ENGRAM_HOME) rather than baselining it; "
            f"{BASELINE_REL} may only shrink."
        )
        return 1
    print("home-path gate: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
