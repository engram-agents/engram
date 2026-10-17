#!/usr/bin/env python3
"""Home-path gate: fail on a hard-coded ENGRAM home path, line by line [part of #3863].

Lei's ruling (2026-10-17): the deployment must use ENGRAM_HOME wherever
possible, because the install allows a custom engram folder. A shipped file
that reaches the store (or anything else under the ENGRAM home) through a
hard-coded `~/.engram` is a defect, even on a seat where `~/.engram` happens to
be right today.

WHAT IS A VIOLATION (R5, line level). A tracked file under `src/engram/` or
`src/build/` (SCAN_PREFIXES; excluding tests and docs) is scanned LINE BY LINE. A line that carries at least
one hard-coded ENGRAM-home spelling (the SPELLINGS table below) is a violating
line unless that same line
  * contains the substring `ENGRAM_HOME`, or
  * matches a resolver token (RESOLVER_TOKENS below, today the call
    `\\bengram_home\\(`; new resolver names are added only there), or
  * lives in a file allowlisted with an exempting class (foreign-home or
    self-contained-no-lib; a `resolver` entry declares a layer and exempts
    nothing).
The unit is the PHYSICAL line (designer's ruling, no parser): `ENGRAM_HOME` on
some OTHER line of the file, or on another line of the same multi-line
statement, does not rescue a bare spelling line. Scope: every tracked `src/engram/` or
`src/build/` file except a `tests` or `docs` path component and `test_*`
basenames. That is wider than "what ships at some tier":
three of the issue's 13 files (the `migrate_*` one-shots) are manifest-excluded
and never ship, but the issue names them as positive controls, so the gate
reads the source tree, not the build manifest.

KNOWN BOUNDS (the instrument is the issue's floor, made wider, not a proof):
  * Only the SCAN_PREFIXES trees are read. src/forum (146 tracked files) and
    every other tree outside both prefixes are NOT scanned; it is not measured
    whether they carry violations.
  * The test is the substring `ENGRAM_HOME`, so a comment on the line, or a
    longer name containing it, also counts the line clean.
  * The resolver clause is the text of a RESOLVER_TOKENS match on the line; it
    does not check that the call is the one in tools/_owner_read.py.
  * A spelling is matched within one line. A home built across lines (a
    variable holding `Path.home()` joined with `".engram"` three lines later) is
    invisible unless the quoted-segment row catches the `".engram"` literal.
  * A per-file COUNT baseline cannot see a swap: fixing one violating line and
    adding a new bare-spelling line elsewhere in the SAME file leaves the count
    unchanged and the gate green (measured on viz/version_info.py: 4 lines
    before and after). The bound disappears when the baseline is empty (every
    violating line is then red), which is why the follow-up PR (6c) takes the
    counts to zero. Pinned by test_known_bound_a_swap_inside_one_file_keeps_the_count_green.
  * A home built from a variable-held HOME with no `.engram` literal is
    invisible.
  * Only the default folder name `.engram` is matched. A sibling such as
    `~/.engram-gemini` is a different home, not this defect (the lookahead
    `(?![\\w-])` after each `.engram` is what excludes it, along with
    `.engram_home`, `.engram_query` and the like).

THE BASELINE (tools/home_path_baseline.json, schema 2):
  {"schema": 2,
   "scan_prefixes": ["src/engram/", "src/build/"],   (must equal SCAN_PREFIXES,
                                       else RED "scope declaration out of sync")
   "baseline":  {"<relpath>": <number of violating LINES in that file>},
   "allowlist": {"<relpath>": {"class": "resolver"|"foreign-home"|
                                        "self-contained-no-lib",
                               "reason": "<non-empty>"}}}
The three allowlist classes (CLASSES) and when each applies:
  resolver              one per declared self-contained layer, listed in
                        RESOLVER_LAYERS (the file defines the layer's engram_home()).
                        A DECLARATION, not an exemption: the file's other violating
                        lines stay in the baseline with exact counts, and the file
                        may be in both lists.
  foreign-home          the file finds ANOTHER seat's home, so the caller's
                        ENGRAM_HOME is meaningless for it. Exempts the file.
  self-contained-no-lib a file that by contract cannot import a resolver (a hook
                        that must keep running with no sibling lib). Needs a
                        reason; no cap and no declared layer. Exempts the file.
The baseline is an exact, shrink-only ratchet:
  a. a file with violating lines that is absent from the baseline -> RED
  b. a file whose violating-line count is GREATER than its baselined count -> RED
  c. a file whose count is LOWER than baselined -> RED "stale: lower it to N";
     a count of 0 that is still listed -> RED "stale: remove it" (never
     silently loose)
  d. a baseline or allowlist entry naming a file that is not a scanned tracked
     file (missing, or outside the scope above) -> RED "dangling"
  e. with --base-sha: a head count greater than the base's, or a head entry
     absent from the base baseline -> RED "grew". An allowlist entry added since
     the base may be of either class if it is valid (rule f); a resolver
     entry is bounded by the declared RESOLVER_LAYERS, so a layer's resolver
     PR is not red by construction once it declares the layer.
     Files that ENTER SCOPE are seeded at the count measured at entry,
     shrink-only from then on; this is not a transitional exception, it is the
     same rule 6a used to seed the baseline: an entry whose path starts with a
     prefix that is in the head's scan_prefixes and not in the base's (a base
     baseline without the key is read as ["src/engram/"]) is exempt from the
     "grew" comparison in that one PR, while the exactness rules a-c still
     bind its count (a padded or lower count is RED). Once the base carries the
     prefix, growth there is RED again; a new violator under an old prefix is
     never exempt.
     The SEED is the only other exception: the base commit has no baseline file at all
     (`git ls-tree` finds nothing at the path). An unresolvable base ref or an
     unreadable base baseline is RED, it is never read as a seed. A base
     baseline in schema 1 (the retired file-level list of names) is the
     TRANSITION: the line-level baseline names files the file-level rule never
     saw, so no count or membership comparison is possible against it and (e)
     is skipped; rules a-d and f-g still run on the head. That leniency exists
     for the one PR that migrates the file and ends with it. The growth
     comparison is skipped ONLY when the BASE baseline is schema 1 (the one
     format-transition step), never for a schema-2 base. The bound on inflating
     the baseline during that step is the exactness rule: every baselined count
     must equal the true violating-line count of the tree (rules a-c still run),
     so an entry cannot be padded.
  f. the allowlist (R4): class is one of the three; reason is non-empty after
     strip (every class); only a `resolver` entry is tied to RESOLVER_LAYERS: a
     `resolver` entry only for a path declared in RESOLVER_LAYERS (r1), no more
     entries than layers (r2), every layer declared with path, layer and a
     truthy parity_pinned (r3), layer paths unique (r4); a
     file of an exempting class (foreign-home, self-contained-no-lib) is excluded
     from the baseline checks and so may not also be in the baseline (a
     `resolver` file is not excluded and may be in both); and an exempting
     entry whose file has no violating lines is RED "exempts nothing, remove
     it" (a `resolver` entry is a declaration and is not subject to this).
     The gate checks an entry's SHAPE, not whether it is justified: the real
     allowlist is pinned by tests/test_home_path_gate.py, so adding or removing
     an entry edits that test in the same commit. A new entry is checked by
     these same rules, with or without --base-sha.
  g. a baseline in schema 1 on the head -> RED "migrate to schema 2".

Rule e compares against the base commit CI passes. On a pull_request run HEAD
is GitHub's merge commit, so `git merge-base origin/<base> HEAD` is the base
branch's tip as of that merge ref, not the PR's branch point: a stricter, more
current comparison. Moving BASELINE_REL to a new path would read as a seed; that
edit is to this file and is visible in review.

Usage:
  home_path_gate.py [--root DIR] [--base-sha SHA]   check (exit 1 on problems)
  home_path_gate.py --report                        list violating lines, per
                                                    file, with line numbers and
                                                    counts
  home_path_gate.py --seed                          write a schema-2 baseline of
                                                    per-file counts from the
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
# The trees the gate reads. The baseline records this list as `scan_prefixes`
# and the two must agree (a baseline written for another scope is RED). A prefix
# that is new relative to the base baseline ENTERS SCOPE in that PR: its files
# are seeded at the count measured at entry (see rule e).
SCAN_PREFIXES = ("src/engram/", "src/build/")
# The scope when schema 2 began; a base baseline without `scan_prefixes` is read as this.
_LEGACY_PREFIXES = ["src/engram/"]
HOME_TOKEN = "ENGRAM_HOME"
_SCHEMA = 2
CLASSES = ("resolver", "foreign-home", "self-contained-no-lib")
# Only these classes EXEMPT their file from the violation/ratchet checks. A
# `resolver` entry DECLARES a layer (see RESOLVER_LAYERS): the file's other
# violating lines stay counted in the baseline like any other file's.
EXEMPTING_CLASSES = ("foreign-home", "self-contained-no-lib")
# The declared resolver layers (R4, amended: one resolver per self-contained
# layer, each pinned AST-identical to the canonical tools/_owner_read.engram_home()
# by a parity test). A `resolver` allowlist entry is valid only for a path listed
# here. Today the canonical layer only; PR 6b adds the _hooklib, _prompthooklib and
# forum_api layers together with their parity test. The parity test is the real
# guard; this list only makes a new resolver copy a deliberate act. A layer
# without `parity_pinned` truthy is refused (check() reports it).
RESOLVER_LAYERS = [
    {"path": "src/engram/tools/_owner_read.py", "layer": "tools", "parity_pinned": True},
]
# The shared-resolver names the gate treats as a clean token on a line. NEW
# RESOLVER NAMES ARE ADDED ONLY HERE (a tuple of regexes); the docstring and the
# tests refer to this constant. Today exactly one: a call to engram_home() (in
# tools/_owner_read.py). The paren is load-bearing: a bare `engram_home` name (a
# variable, `args.engram_home`) is not a call, and `\b` keeps `my_engram_home(` out.
RESOLVER_TOKENS = (r"\bengram_home\(",)
_RESOLVER_RES = tuple(re.compile(t) for t in RESOLVER_TOKENS)

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


def _line_is_clean(line):
    """C1: a spelling line is clean when it names ENGRAM_HOME or calls the
    shared resolver."""
    return HOME_TOKEN in line or any(rx.search(line) for rx in _RESOLVER_RES)


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True
    )


def in_scope(rel):
    if not rel.startswith(SCAN_PREFIXES):
        return False
    parts = rel.split("/")
    if "tests" in parts[:-1] or "docs" in parts[:-1]:
        return False
    return not parts[-1].startswith("test_")


def scanned_files(root):
    """Tracked files in scope that exist on disk. Raises RuntimeError outside
    a git checkout."""
    r = _git(root, "ls-files", "-z", "--", *SCAN_PREFIXES)
    if r.returncode != 0:
        raise RuntimeError(f"not a git checkout: {root} ({r.stderr.strip()})")
    root = Path(root)
    return sorted(
        rel for rel in r.stdout.split("\0") if rel and in_scope(rel) and (root / rel).is_file()
    )


def violating_lines(root):
    """{relpath: [(lineno, line, [spelling names])]} for every scanned file's
    violating lines: a line with a SPELLINGS match that does not itself contain
    ENGRAM_HOME or a call to the shared resolver. Allowlisting is NOT applied
    here (check() does that); this is what the tree says."""
    root = Path(root)
    out = {}
    for rel in scanned_files(root):
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        rows = []
        for lineno, line in enumerate(text.split("\n"), 1):
            if _line_is_clean(line):
                continue
            names = [name for name, rx in SPELLINGS if rx.search(line)]
            if names:
                rows.append((lineno, line, names))
        if rows:
            out[rel] = rows
    return out


def violation_counts(root):
    """{relpath: number of violating LINES} (a line matching two rows is one)."""
    return {rel: len(rows) for rel, rows in violating_lines(root).items()}


def violations(root):
    """{relpath: {spelling_name: first matching snippet}} for every scanned
    file that has at least one violating line."""
    rx_by_name = dict(SPELLINGS)
    out = {}
    for rel, rows in violating_lines(root).items():
        hits = {}
        for _, line, names in rows:
            for name in names:
                if name not in hits:
                    m = rx_by_name[name].search(line)
                    hits[name] = line[max(0, m.start() - 40) : m.end() + 40].strip()
        out[rel] = hits
    return out


def _parse_baseline(text):
    """-> (schema, baseline, allowlist, scan_prefixes). scan_prefixes is the
    recorded list, or None when the key is absent (always None for schema 1). Schema 1: baseline is a set of file
    names (the retired file-level format), allowlist a set. Schema 2: baseline
    is {file: int >= 0}, allowlist is {file: entry dict}. Raises ValueError on a
    bad shape; the allowlist entries' class/reason are judged by check(), which
    reports them as problems rather than parse errors."""
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("schema") not in (1, _SCHEMA):
        raise ValueError(f"expected an object with schema 1 or {_SCHEMA}")
    if data["schema"] == 1:
        for key in ("baseline", "allowlist"):
            v = data.get(key)
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ValueError(f"'{key}' must be a list of strings (schema 1)")
        return 1, set(data["baseline"]), set(data["allowlist"]), None
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
    if prefixes is not None and (
        not isinstance(prefixes, list) or not all(isinstance(x, str) for x in prefixes)
    ):
        raise ValueError("'scan_prefixes' must be a list of strings")
    return _SCHEMA, dict(base), dict(allow), prefixes


def load_baseline(root):
    """-> (schema, baseline, allowlist, scan_prefixes) from the working tree."""
    return _parse_baseline((Path(root) / BASELINE_REL).read_text(encoding="utf-8"))


def _base_baseline(root, base_sha):
    """-> ((schema, baseline, allowlist) | None, problem | None). None with no
    problem is the seed (base commit has no baseline file)."""
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
        return _parse_baseline(shown.stdout), None
    except ValueError as e:  # includes json.JSONDecodeError
        return None, f"{BASELINE_REL} at base {base_sha!r} is unreadable: {e}"


def _layer_problems():
    """Rules r3 and r4: every declared layer has path, layer and a truthy
    parity_pinned; the paths are unique."""
    problems = []
    seen = set()
    for i, layer in enumerate(RESOLVER_LAYERS):
        if not isinstance(layer, dict):
            problems.append(f"RESOLVER_LAYERS[{i}] must be an object with path, layer, parity_pinned")
            continue
        path, name = layer.get("path"), layer.get("layer")
        for key in ("path", "layer"):
            v = layer.get(key)
            if not isinstance(v, str) or not v.strip():
                problems.append(f"RESOLVER_LAYERS[{i}] ({path or name or '?'}): missing or empty {key!r}")
        if not layer.get("parity_pinned"):
            problems.append(
                f"RESOLVER_LAYERS[{i}] ({path or name or '?'}): a layer declared without "
                f"parity_pinned is refused; pin it with a parity test against the canonical "
                f"tools/_owner_read.engram_home() first"
            )
        if isinstance(path, str) and path:
            if path in seen:
                problems.append(f"RESOLVER_LAYERS: duplicate layer path {path}")
            seen.add(path)
    return problems


def _allowlist_problems(allow, baseline):
    """Rule f: the shape of the allowlist (R4), and no file in both maps."""
    problems = []
    resolvers = []
    for rel in sorted(allow):
        entry = allow[rel]
        cls = entry.get("class")
        if cls not in CLASSES:
            problems.append(
                f"allowlist entry {rel}: class {cls!r} must be one of {', '.join(CLASSES)}"
            )
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"allowlist entry {rel}: reason must be a non-empty string")
        if cls == "resolver":
            resolvers.append(rel)
        if rel in baseline and cls in EXEMPTING_CLASSES:
            problems.append(
                f"{rel} is listed in both the baseline and the allowlist; a {cls} file "
                f"is excluded from the baseline checks, so keep it in one"
            )
    declared = {l.get("path") for l in RESOLVER_LAYERS if isinstance(l, dict)}
    for rel in resolvers:
        if rel not in declared:
            problems.append(
                f"resolver entry names no declared layer: {rel} (add the layer to "
                f"RESOLVER_LAYERS in {Path(__file__).name} together with its parity test)"
            )
    if len(resolvers) > len(RESOLVER_LAYERS):
        problems.append(
            f"{len(resolvers)} resolver allowlist entries but only {len(RESOLVER_LAYERS)} "
            f"declared layer(s) (RESOLVER_LAYERS): one resolver per self-contained layer: "
            + ", ".join(resolvers)
        )
    return problems


def check(root, base_sha=None):
    """Return a list of problem strings; empty means green."""
    root = Path(root)
    try:
        files = set(scanned_files(root))
    except RuntimeError as e:
        return [str(e)]
    try:
        schema, baseline, allow, prefixes = load_baseline(root)
    except FileNotFoundError:
        return [f"baseline file missing: {BASELINE_REL} (create it with --seed)"]
    except (ValueError, OSError) as e:
        return [f"{BASELINE_REL} is unreadable: {e}"]
    if schema == 1:
        return [
            f"{BASELINE_REL} is schema 1 (a list of file names, the retired file-level "
            f"format): migrate to schema 2 (per-file violating-line counts and a classed "
            f"allowlist)"
        ]

    problems = _layer_problems()
    if prefixes != list(SCAN_PREFIXES):
        problems.append(
            f"scope declaration out of sync with the gate: {BASELINE_REL} scan_prefixes is "
            f"{prefixes!r}, the gate scans {list(SCAN_PREFIXES)!r}; set scan_prefixes to the "
            f"gate's list (and seed any entering prefix, see rule e)"
        )
    for label, entries in (("baseline", baseline), ("allowlist", allow)):
        for rel in sorted(set(entries) - files):
            problems.append(
                f"dangling {label} entry (not a scanned tracked file under {', '.join(SCAN_PREFIXES)}): {rel} "
                f"(a renamed baselined file reads as growth: fix the file in the same PR)"
            )
    problems += _allowlist_problems(allow, baseline)

    exempt = {rel for rel, e in allow.items() if e.get("class") in EXEMPTING_CLASSES}
    all_rows = violating_lines(root)
    for rel in sorted(exempt & files):
        if rel not in all_rows:
            problems.append(
                f"allowlist entry exempts nothing, remove it from {BASELINE_REL}: {rel} "
                f"(it has no violating lines)"
            )
    effective = {rel: rows for rel, rows in all_rows.items() if rel not in exempt}
    for rel in sorted(effective):
        rows = effective[rel]
        n = len(rows)
        if rel not in baseline:
            lineno, line, names = rows[0]
            problems.append(
                f"new hard-coded ENGRAM home path in {rel} ({n} line(s)) [{', '.join(sorted(names))}], "
                f"first at line {lineno}: {line.strip()[:100]!r} -- name ENGRAM_HOME on that line "
                f"or call engram_home() instead of spelling the folder"
            )
        elif n > baseline[rel]:
            problems.append(
                f"{rel} has {n} violating lines, the baseline allows {baseline[rel]} "
                f"(first at line {rows[0][0]}): name ENGRAM_HOME on the new line(s) "
                f"or call engram_home()"
            )
        elif n < baseline[rel]:
            problems.append(
                f"stale baseline count for {rel}: now {n} violating line(s), "
                f"lower it to {n} in {BASELINE_REL}"
            )
    for rel in sorted((set(baseline) & files) - set(effective) - exempt):
        problems.append(
            f"stale baseline entry, no violating lines left: remove it from {BASELINE_REL}: {rel}"
        )

    if base_sha:
        base, prob = _base_baseline(root, base_sha)
        if prob:
            problems.append(prob)
        elif base is not None and base[0] == _SCHEMA:
            _, base_counts, _, base_prefixes = base
            if base_prefixes is None:
                base_prefixes = _LEGACY_PREFIXES
            # A prefix in the gate's scope but not in the base's ENTERS SCOPE in
            # this PR: its files are seeded at the count measured at entry. The
            # exactness rules (a-c) above still bind those counts.
            entering = tuple(x for x in SCAN_PREFIXES if x not in base_prefixes)
            for rel in sorted(baseline):
                if entering and rel.startswith(entering):
                    continue
                if rel not in base_counts:
                    problems.append(
                        f"baseline grew: {rel} is not in {BASELINE_REL} at base {base_sha[:12]}; "
                        f"the baseline may only shrink (fix the file instead; a renamed "
                        f"baselined file reads as growth, so fix it in the same PR)"
                    )
                elif baseline[rel] > base_counts[rel]:
                    problems.append(
                        f"baseline grew: {rel} is {baseline[rel]} in {BASELINE_REL} but "
                        f"{base_counts[rel]} at base {base_sha[:12]}; counts may only shrink"
                    )
            # The allowlist may grow with an entry of either class: the shape
            # rules (class, reason, declared resolver layers) already ran on the
            # head, and the growth is visible in review.
        # base is None (seed): nothing to compare. base schema 1: the retired
        # file-level list. The line-level baseline names files the file-level
        # rule never saw, so no count or membership comparison is possible; the
        # head was still checked against the tree above.
    return problems


def _seed(root):
    path = Path(root) / BASELINE_REL
    if path.exists():
        print(f"refusing to overwrite {BASELINE_REL}", file=sys.stderr)
        return 1
    counts = violation_counts(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": _SCHEMA,
                "scan_prefixes": list(SCAN_PREFIXES),
                "baseline": dict(sorted(counts.items())),
                "allowlist": {},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


def _report(root):
    rows = violating_lines(root)
    for rel, lines in sorted(rows.items()):
        print(f"{rel}\t{len(lines)}")
        for lineno, line, names in lines:
            print(f"  {lineno}\t[{','.join(sorted(names))}]\t{line.strip()[:120]}")
    print(f"# {len(rows)} file(s), {sum(len(v) for v in rows.values())} violating line(s)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument(
        "--base-sha", default=None, help="enable the baseline-growth rules against this commit"
    )
    ap.add_argument(
        "--report", action="store_true", help="list every violating line with its number, per file"
    )
    ap.add_argument(
        "--seed",
        action="store_true",
        help="write a schema-2 baseline of per-file violating-line counts (refuses if one exists)",
    )
    args = ap.parse_args(argv)

    if args.seed:
        return _seed(args.root)
    if args.report:
        return _report(args.root)
    problems = check(args.root, base_sha=args.base_sha)
    for p in problems:
        print(p)
    if problems:
        print(
            "home-path gate: FAILED. Fix the line (name ENGRAM_HOME on it, or call engram_home()) "
            f"rather than baselining it; the counts in {BASELINE_REL} may only shrink."
        )
        return 1
    print("home-path gate: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
