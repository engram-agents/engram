#!/usr/bin/env python3
"""check-claude-md-drift — rendered CLAUDE.md drift detector.

Compares the RENDERED CLAUDE.md output between two git refs by reading
template.CLAUDE.md at each ref, then diffing the results.

History: prior to engram-alpha#2067 the render folded a second file,
compact-instructions.md, into template.CLAUDE.md, so this tool diffed the
FOLDED output — catching a compact-instructions.md-only change that a
template-only diff would have missed. #2067 removed the ``## Compact
Instructions`` section from the Claude template entirely (see
docs/compaction-continuity.md); compact-instructions.md is now Codex-only
and no longer contributes to the Claude render, so
``template_render.CLAUDE_RENDER_SOURCES`` is a single-file list. The
render-via-source-list design is kept (rather than hardcoding
template.CLAUDE.md here) so a future extraction of another fragment into
the Claude render is caught by widening CLAUDE_RENDER_SOURCES, without
touching this tool.

Usage
-----
Mode 1 — base-to-head (the original mode):

    python tools/check-claude-md-drift.py --repo <SRC_DIR> --base <BASE_REF> --head <HEAD_REF>

Mode 2 — live-file presence check (engram-alpha#3604):

    python tools/check-claude-md-drift.py --repo <SRC_DIR> --head <REF> --live <FILE> \\
        --user-name N --agent-name N --self-node-id pn_NNNN --engram-home DIR \\
        [--seed-manifest $ENGRAM_HOME/seed-manifest.json] [--seed-id KEY=VALUE ...]

Arguments
---------
--repo    Path to the engram-alpha source tree (used for git show calls).
--base    Mode 1: git ref for the "before" state (e.g. the commit before a PR merge).
--head    Git ref for the "after" state (e.g. origin/dev, HEAD, a branch name).
--live    Mode 2: the live CLAUDE.md to check (read only; never edited).  Mutually
          exclusive with --base.
--user-name / --agent-name / --self-node-id / --engram-home
          Mode 2: values for {{USER_NAME}}, {{AGENT_NAME}}, {{SELF_NODE_ID}},
          {{ENGRAM_HOME}}.
--seed-manifest PATH
          Mode 2: the seed-manifest.json bootstrap wrote (reads only its
          ``seed_ids`` map, e.g. {"{{AX_HONESTY}}": "<the install's ax_ id>", ...}).
--seed-id KEY=VALUE
          Mode 2, repeatable: supply or override one seed id (KEY is the placeholder
          name without braces, e.g. AX_HONESTY_DISCRETION).  An override beats the
          manifest value; a key the template does not use is harmless; for a
          repeated key the last one wins; the value is everything after the first
          ``=``.  An explicit flag (--user-name etc.) beats a --seed-id of the same
          placeholder, so ``--seed-id ENGRAM_HOME=DIR`` also works as a stand-in
          for --engram-home.  Needed when an early seat's manifest lacks an id the
          current template uses.
--multi-agent
          Retired by #4255: accepted, has no effect, prints one stderr line.  The
          "Agent coordination" section lives in template.CLAUDE.md now and is
          checked by the default run.  Not listed in --help.

Exit codes
----------
0   No drift — mode 1: the two refs render identically; mode 2: every rendered
    template paragraph is present in the live file.
1   Drift detected — mode 1: a unified diff on stdout; mode 2: the missing
    paragraphs on stdout.
2   Usage / argument error, OR (mode 2) any input/render error: live file
    missing/unreadable, unknown --head, empty render, unresolved placeholder,
    bad seed manifest.  An error is never reported as "0 missing".
3   (mode 1) unexpected git plumbing failure.

Live mode (``--live``)
----------------------
Question answered: "does my LIVE file carry everything the template carries?"
Mode 1 cannot answer it: once a rebuild crosses a hunk without merging it,
every later base-to-head range starts after that hunk.  Mode 2 checks the live
file against the rendered template at ``--head`` in ONE direction: every
rendered paragraph must be present in the live file; only MISSING ones are
reported; extras and ordering are ignored (the template is the floor).

Home path.  {{ENGRAM_HOME}} comes from --engram-home, but a live file may write
the home as the literal ``~/.engram...`` (ENGRAM_HOME's default) or as the absolute path (an unquoted
``~`` on the command line is expanded by the shell).  A unit that contains the
home is present if the live file has it with the home written EITHER way,
whichever form --engram-home was given in.  Any other home (a different
directory) is still reported missing.  Trailing slashes on --engram-home are
ignored.  Known limit: a single unit that contains the home TWICE, written in
the live file with one ``~`` and one absolute form, reads MISSING (one form is
substituted everywhere in the unit).

Render path.  Bootstrap renders a Claude seat's file by reading
``template.CLAUDE.md`` and running a plain literal ``str.replace`` loop over its
substitutions dict (bootstrap.py ``substitute_template``; the first-session
skill then fills AGENT_NAME / USER_NAME / SELF_NODE_ID).  bootstrap.py cannot
be imported side-effect-free and ``template_render`` substitutes nothing, so
this tool reads the raw template via ``template_render`` and carries its own
literal replace loop.  Equivalence with bootstrap is by reading, NOT by test.
Fillable placeholders: ENGRAM_HOME, USER_NAME, AGENT_NAME, SELF_NODE_ID plus the
seed-ID keys in the manifest (AX_HONESTY, AX_HONESTY_DISCRETION, AX_PROVENANCE,
DF_ENGRAM, DF_EPISTEMIC_IDENTITY, GL_EPISTEMIC_HUMILITY).  Any ``{{X}}`` left
after substitution is exit 2 naming X (a template that gains a placeholder this
tool does not know fails loudly, never as a silent pass).

Unit splitting rule.  The rendered text is split into units: a heading line
is its own unit (a missing heading alone is reportable); an ordinary paragraph
(blank-line separated block) is one unit; each LIST ITEM (a bullet or number
plus its continuation lines, plus a fenced block indented under it) is its own
unit, keyed to the item's first line, and a nested sub-item is a unit of its
own (not folded into its parent); each TABLE ROW is its own unit (the |---|
separator row is not a unit); a fenced code block outside a list is one unit
even if it contains blank lines.  A local edit inside a list or table is thus
not a false "missing", and a real miss names its own line.  Presence = the
unit, whitespace-normalised (runs of whitespace collapse to one space), occurs
in the whitespace-normalised live text bounded by whitespace or text edge, so
an item with text appended at its END still matches (text inserted in the
MIDDLE of a unit does not).  Case, punctuation and words are
significant.  Identical duplicate units (e.g. two "- none" bullets) collapse
into ONE check, reported once at the first occurrence.

Output.  One line per missing unit, ``MISSING <file> line <N>: <first line>``
(N is 1-based in the RENDERED template; first line truncated), then always a
summary line ``N units checked, M missing``.

Limits (what this check does NOT tell you).  It finds ABSENCE, not whether
text is live-effective: a unit is matched ANYWHERE in the live file, including
inside fenced blocks, HTML comments, blockquotes and code spans, so a seat that
commented out or quoted a hunk is NOT reported.  A unit that is a prefix OR the tail of a
longer line (an item extended at its end; ``Do the thing.`` inside
``Please never. Do the thing.``), or a heading with extra words appended,
counts present; a unit extended in the middle does not.
Trailing punctuation appended to an unpunctuated unit counts MISSING
("write it." does not contain the unit "write it" bounded by whitespace).
Blockquotes and HTML comments are ordinary paragraph units.  Locally reworded
units are reported missing; a human judges.  That noise is by design: on a real
seat about 14 of 169 units were reported (some real lag, some deliberate local
rewrites) and the output does not say which kind a line is.  If a seat that
rewords more finds it too noisy, the options are a per-seat accepted-deviations
list or a fuzzy match for lightly reworded units; neither is built.

Agent coordination.  The section that used to live in a second template file
is part of template.CLAUDE.md, so the default run checks it like every other
paragraph.  A seat that hand-merged the old file may have reworded or re-headed
its paragraphs; expect "missing" lines there and let a human judge them.

Source files checked
--------------------
The set of files that make up the render is declared in
``src/engram/template_render.CLAUDE_RENDER_SOURCES`` (canonical SSoT).  A
source file absent at the base ref is treated as empty (so an extraction
commit correctly shows the extracted content as drift rather than an error).

Design constraints
------------------
* stdlib-only: subprocess (git), difflib (diff), tempfile, shutil.
* No import of bootstrap.py (side-effects at module level — reads env vars,
  imports server.py which reads ENGRAM_HOME).  Imports template_render only
  (side-effect-free).
* Follows the verb-first, exit-code-driven style of tools/baton.py and
  tools/forum.py.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — import template_render from src/engram/ (side-effect-free).
#
# This script lives in src/engram/tools/ (with a root-level tools/ symlink).
# __file__ resolves to the canonical src/engram/tools/check-claude-md-drift.py
# path, so:
#   parent     = src/engram/tools/
#   parent.parent = src/engram/          ← where template_render.py lives
# ---------------------------------------------------------------------------

_TOOLS_DIR = Path(__file__).resolve().parent          # src/engram/tools/
_SRC_ENGRAM = _TOOLS_DIR.parent                       # src/engram/

sys.path.insert(0, str(_SRC_ENGRAM))
from template_render import CLAUDE_RENDER_SOURCES, render_identity_surface  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_show(repo: Path, ref: str, rel_path: str) -> str | None:
    """Return the file content at ``ref:src/engram/templates/<rel_path>``.

    Returns None if the file does not exist at that ref (e.g. before the
    compact-instructions.md extraction commit).  Raises SystemExit on
    unexpected git errors.
    """
    git_path = f"src/engram/templates/{rel_path}"
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{git_path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout
    # Exit code 128 with "does not exist" or path-related message = file absent
    # at that ref.  Any other error is unexpected and should surface.
    stderr_lower = result.stderr.lower()
    if (
        "does not exist" in stderr_lower
        or "exists on disk" in stderr_lower
        or "path not found" in stderr_lower
    ):
        return None
    # Unknown git error — surface it on stderr and exit with a DISTINCT code (3),
    # so an upgrading agent / CI never mistakes a git-plumbing failure for a real
    # drift signal (drift is exit 1; no-drift is exit 0).
    print(
        f"git show failed for {ref}:{git_path}\n"
        f"  returncode: {result.returncode}\n"
        f"  stderr: {result.stderr.strip()}",
        file=sys.stderr,
    )
    raise SystemExit(3)


def _render_ref(repo: Path, ref: str) -> str:
    """Render the CLAUDE.md identity surface for a git ref.

    Uses a temp directory populated with the source files from the ref.
    A source file absent at that ref is written as an empty file so
    render_identity_surface() can still be called with a consistent dir.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="engram_drift_"))
    try:
        for src_file in CLAUDE_RENDER_SOURCES:
            content = _git_show(repo, ref, src_file)
            dest = tmpdir / src_file
            dest.write_text(content if content is not None else "", encoding="utf-8")
        return render_identity_surface(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Live mode (engram-alpha#3604)
# ---------------------------------------------------------------------------

#: Placeholders given by explicit CLI flags.
FLAG_PLACEHOLDERS = {
    "{{ENGRAM_HOME}}": "--engram-home",
    "{{USER_NAME}}": "--user-name",
    "{{AGENT_NAME}}": "--agent-name",
    "{{SELF_NODE_ID}}": "--self-node-id",
}
#: Seed-ID placeholders (keys of bootstrap's ``ids`` dict, filled via --seed-manifest).
SEED_PLACEHOLDERS = (
    "{{AX_HONESTY}}", "{{AX_HONESTY_DISCRETION}}", "{{AX_PROVENANCE}}",
    "{{DF_ENGRAM}}", "{{DF_EPISTEMIC_IDENTITY}}", "{{GL_EPISTEMIC_HUMILITY}}",
)
#: Everything this tool can fill.  A drift-guard test asserts the shipped
#: template uses only these.
KNOWN_PLACEHOLDERS = frozenset(FLAG_PLACEHOLDERS) | frozenset(SEED_PLACEHOLDERS)

_PLACEHOLDER_RE = re.compile(r"\{\{[A-Za-z0-9_]+\}\}")
_HEADING_RE = re.compile(r"^#{1,6}(\s|$)")
_LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s")
_FENCE_RE = re.compile(r"^(\s*)(```|~~~)")
_MAIN_FILE = "template.CLAUDE.md"
_TRUNC = 100


class _LiveError(Exception):
    """Input/render error in live mode -> exit 2."""


def _norm(text: str) -> str:
    return " ".join(text.split())


_TABLE_SEP_RE = re.compile(r"[\s|:\-]+")


def split_units(text: str) -> list[tuple[int, list[str], bool]]:
    """Split rendered text into (1-based start line, lines, is_heading) units.

    Unit rule: a heading is its own unit; an ordinary paragraph (blank-line
    separated) is one unit; each LIST ITEM (marker line + its continuation
    lines + an indented fenced block under it) is its own unit, and a nested
    sub-item is a unit of its own (NOT folded into its parent); each TABLE ROW
    is its own unit (the |---| separator row is not a unit); a fenced code
    block outside a list is one unit.  Keys are the unit's first line.
    """
    lines = text.splitlines()
    units: list[tuple[int, list[str], bool]] = []
    cur: list[str] = []
    cur_start = 0
    mode = None          # None | "para" | "item"
    fence_mark = ""
    in_fence = False
    fence_in_item = False

    def flush() -> None:
        nonlocal cur, mode
        while cur and not cur[-1].strip():
            cur.pop()
        if cur:
            units.append((cur_start, cur, False))
        cur = []
        mode = None

    def next_nonblank(i: int) -> str | None:
        for j in range(i + 1, len(lines)):
            if lines[j].strip():
                return lines[j]
        return None

    for i, line in enumerate(lines):
        n = i + 1
        if in_fence:
            cur.append(line)
            if line.strip() == fence_mark:
                in_fence = False
                if not fence_in_item:
                    flush()
            continue
        fm = _FENCE_RE.match(line)
        if fm:
            indented = bool(fm.group(1))
            if mode == "item" and indented:
                fence_in_item = True
            else:
                flush()
                cur_start = n
                fence_in_item = False
                mode = "fence"
            cur.append(line)
            fence_mark = fm.group(2)
            in_fence = True
            continue
        if not line.strip():
            if mode == "item":
                nxt = next_nonblank(i)
                if nxt is not None and nxt[:1] in (" ", "\t") \
                        and not _LIST_RE.match(nxt) and not _HEADING_RE.match(nxt):
                    cur.append(line)          # indented continuation of this item
                    continue
            flush()
            continue
        if _HEADING_RE.match(line):
            flush()
            units.append((n, [line], True))
            continue
        if _LIST_RE.match(line):
            flush()
            cur_start, mode = n, "item"
            cur = [line]
            continue
        if mode == "item":
            cur.append(line)                  # continuation / lazy continuation
            continue
        stripped = line.strip()
        if stripped.startswith("|"):
            flush()
            if not ("-" in stripped and _TABLE_SEP_RE.fullmatch(stripped)):
                units.append((n, [line], False))
            continue
        if mode == "para":
            cur.append(line)
            continue
        cur_start, mode = n, "para"
        cur = [line]
    flush()
    return units


def _present(unit_norm: str, live_norm: str) -> bool:
    start = 0
    while True:
        idx = live_norm.find(unit_norm, start)
        if idx < 0:
            return False
        before_ok = idx == 0 or live_norm[idx - 1].isspace()
        end = idx + len(unit_norm)
        after_ok = end == len(live_norm) or live_norm[end].isspace()
        if before_ok and after_ok:
            return True
        start = idx + 1


def _verify_ref(repo: Path, ref: str) -> None:
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise _LiveError(f"--head {ref!r} is not a known git commit/ref in {repo}")


def _load_seed_ids(path: str) -> dict[str, str]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _LiveError(f"cannot read seed manifest {path}: {exc}")
    ids = data.get("seed_ids") if isinstance(data, dict) else None
    if not isinstance(ids, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in ids.items()
    ):
        raise _LiveError(
            f"seed manifest {path} has no usable 'seed_ids' map of placeholder -> id"
        )
    out = {}
    for k, v in ids.items():
        out[k if k.startswith("{{") else "{{" + k + "}}"] = v
    return out


def _home_forms(home: str | None) -> list[str]:
    """The forms a home path may be written in: as given, plus the other of
    ``~/...`` / absolute (via $HOME).  The given form comes first."""
    if home is None:
        return []
    # A trailing slash would double up against the template's "/..." (a bare
    # "/" is kept as "/": it must not become an empty home).
    home = home.rstrip("/") or "/"
    forms = [home]
    if home == "~" or home.startswith("~/"):
        alt = os.path.expanduser(home)
    else:
        h = os.path.expanduser("~").rstrip("/")
        alt = home
        if h and h != "~" and (home == h or home.startswith(h + "/")):
            alt = "~" + home[len(h):]
    if alt != home:
        forms.append(alt)
    return forms


def _parse_seed_id(raw: str) -> tuple[str, str]:
    key, sep, value = raw.partition("=")
    key = key.strip()
    if key.startswith("{{") and key.endswith("}}"):
        key = key[2:-2].strip()
    if not sep or not key or not value.strip():
        raise _LiveError(
            f"--seed-id {raw!r} is malformed: expected KEY=VALUE with a non-empty key and value"
        )
    return "{{" + key + "}}", value


def _substitute(text: str, subs: dict[str, str]) -> str:
    for placeholder, value in subs.items():
        text = text.replace(placeholder, value)      # literal, like bootstrap
    return text


def _check_set(label: str, rendered: str, live_norm: str, skip_headings: bool,
               homes: list[str] | None = None):
    """Return (checked, [(line, first_line)]) for one rendered file.

    ``rendered`` still carries {{ENGRAM_HOME}} when ``homes`` is given; each
    unit is judged present if it is found with the home written in ANY of the
    ``homes`` forms (the first is the primary form, used for the unresolved
    check and duplicate collapsing)."""
    homes = homes or []
    primary = _substitute(rendered, {"{{ENGRAM_HOME}}": homes[0]}) if homes else rendered
    left = sorted(set(_PLACEHOLDER_RE.findall(primary)))
    if left:
        seeds = [p.strip("{}") for p in left if p in SEED_PLACEHOLDERS]
        others = [p.strip("{}") for p in left if p not in SEED_PLACEHOLDERS]
        parts = []
        if seeds:
            parts.append(
                "seed id(s) missing from the seed manifest: " + ", ".join(seeds)
                + " (pass each with --seed-id KEY=VALUE, e.g. --seed-id "
                + seeds[0] + "=<the install's id>)"
            )
        if others:
            parts.append(
                "unresolved placeholder(s): " + ", ".join(others)
                + " (supply the matching flag, or teach the tool the new placeholder)"
            )
        raise _LiveError(f"in {label} after substitution: " + "; ".join(parts))
    checked, missing = 0, []
    seen: set[str] = set()
    for start, ulines, is_heading in split_units(rendered):
        if is_heading and skip_headings:
            continue
        raw = "\n".join(ulines)
        variants = [_norm(_substitute(raw, {"{{ENGRAM_HOME}}": h})) for h in homes] or [_norm(raw)]
        key = variants[0]
        if key in seen:                  # identical duplicate units: one check
            continue
        seen.add(key)
        checked += 1
        if not any(_present(v, live_norm) for v in variants):
            first = _substitute(ulines[0], {"{{ENGRAM_HOME}}": homes[0]} if homes else {}).strip()
            if len(first) > _TRUNC:
                first = first[: _TRUNC - 3] + "..."
            missing.append((start, first))
    return checked, missing


def run_live(args: argparse.Namespace, repo: Path) -> int:
    try:
        _verify_ref(repo, args.head)
        try:
            live_text = Path(args.live).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise _LiveError(f"cannot read --live file {args.live}: {exc}")
        subs: dict[str, str] = {}
        if args.seed_manifest:
            subs.update(_load_seed_ids(args.seed_manifest))
        for raw in getattr(args, "seed_id", None) or []:
            key, value = _parse_seed_id(raw)     # an override beats the manifest
            subs[key] = value
        for placeholder, flag in FLAG_PLACEHOLDERS.items():
            value = getattr(args, flag[2:].replace("-", "_"))
            if value is not None:
                subs[placeholder] = value
        # {{ENGRAM_HOME}} stays in the text; _check_set tries every home form.
        homes = _home_forms(subs.pop("{{ENGRAM_HOME}}", None))
        live_norm = _norm(live_text)

        rendered = _substitute(_render_ref(repo, args.head), subs)
        if not rendered.strip():
            raise _LiveError(f"empty render of {_MAIN_FILE} at {args.head}")
        sets = [(_MAIN_FILE, rendered, False)]

        results = [(label, *_check_set(label, text, live_norm, skip, homes))
                   for label, text, skip in sets]
    except _LiveError as exc:
        print(f"check-claude-md-drift: error: {exc}", file=sys.stderr)
        return 2
    except SystemExit as exc:
        # _git_show (shared with mode 1, where it exits 3) must not leak its
        # exit code into live mode: every live-mode error is exit 2.
        print(
            f"check-claude-md-drift: error: git failure while reading the template "
            f"(helper exited {exc.code}); see the git message above",
            file=sys.stderr,
        )
        return 2

    total = sum(c for _, c, _ in results)
    total_missing = sum(len(m) for _, _, m in results)
    for label, _, missing in results:
        for line_no, first in missing:
            print(f"MISSING {label} line {line_no}: {first}")
    if len(results) > 1:
        print("  " + "; ".join(f"{l}: {c} checked, {len(m)} missing" for l, c, m in results))
    print(f"{total} units checked, {total_missing} missing")
    return 1 if total_missing else 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Entry point.  Returns an exit code (0 = no drift, 1 = drift)."""
    parser = argparse.ArgumentParser(
        prog="check-claude-md-drift",
        description=(
            "Compare the RENDERED CLAUDE.md between two git refs. "
            "Exits 0 if identical, 1 if drift detected."
        ),
    )
    parser.add_argument("--repo", metavar="SRC_DIR",
                        help="Path to the engram-alpha source tree.")
    parser.add_argument("--base", metavar="BASE_REF",
                        help="Mode 1: git ref for the 'before' state.")
    parser.add_argument("--head", metavar="HEAD_REF",
                        help="Git ref for the 'after' state (mode 1) or the template to render (mode 2).")
    parser.add_argument("--live", metavar="FILE",
                        help="Mode 2: live CLAUDE.md to check against the rendered template at --head. "
                             "Every rendered paragraph must be present in FILE; only missing ones are "
                             "reported, extras ignored. An item or row the seat extended is still found "
                             "when the text was appended at the END (or the unit is the tail of a longer "
                             "line); text inserted in the MIDDLE is reported missing, and so is a reworded "
                             "unit (some noise on a real seat is by design; a human judges each line). Exclusive with --base. Exit 0/1, 2 on any error. "
                             "Finds absence only: text is matched anywhere in FILE, even inside a "
                             "comment, fence or quote, so a commented-out hunk is not reported.")
    parser.add_argument("--user-name", metavar="NAME", help="Mode 2: value for {{USER_NAME}}.")
    parser.add_argument("--agent-name", metavar="NAME", help="Mode 2: value for {{AGENT_NAME}}.")
    parser.add_argument("--self-node-id", metavar="ID", help="Mode 2: value for {{SELF_NODE_ID}} (this install's pn_ self node id).")
    parser.add_argument("--engram-home", metavar="DIR",
                        help="Mode 2: value for {{ENGRAM_HOME}}; a live file that writes the home as ~/... "
                             "or as the absolute path matches either way, whichever form is given here.")
    parser.add_argument("--seed-manifest", metavar="PATH",
                        help="Mode 2: seed-manifest.json written by bootstrap; its 'seed_ids' map fills "
                             "the seed-ID placeholders.")
    parser.add_argument("--seed-id", metavar="KEY=VALUE", action="append", default=[],
                        help="Mode 2, repeatable: supply or override one seed id (KEY = placeholder name "
                             "without braces, e.g. AX_HONESTY_DISCRETION). Beats the manifest value; a key "
                             "the template does not use is ignored; the last repeat of a key wins; "
                             "the value is everything after the first '='; an explicit flag such as "
                             "--user-name beats a --seed-id of the same placeholder. Use it when an early seat's manifest "
                             "lacks an id the current template needs.")
    # Retired by #4255: accepted, no effect. Hidden from --help.
    parser.add_argument("--multi-agent", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.multi_agent:
        print("--multi-agent is retired and has no effect: the Agent coordination section "
              "is checked by default; see #4255", file=sys.stderr)

    if args.live is not None:
        if args.base is not None:
            parser.error("--live and --base are mutually exclusive (pick one mode)")
        missing_args = [f for f, v in (("--repo", args.repo), ("--head", args.head)) if v is None]
        if missing_args:
            parser.error("the following arguments are required: " + ", ".join(missing_args))
    else:
        missing_args = [f for f, v in (("--repo", args.repo), ("--base", args.base),
                                       ("--head", args.head)) if v is None]
        if missing_args:
            parser.error("the following arguments are required: " + ", ".join(missing_args))

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        parser.error(f"--repo {repo} does not look like a git repository (no .git dir)")

    if args.live is not None:
        for flag in FLAG_PLACEHOLDERS.values():
            value = getattr(args, flag[2:].replace("-", "_"))
            if value is not None and not value.strip():
                parser.error(f"{flag} must not be empty or whitespace-only")
        return run_live(args, repo)

    base_ref = args.base
    head_ref = args.head

    # Render both refs.
    base_rendered = _render_ref(repo, base_ref)
    head_rendered = _render_ref(repo, head_ref)

    if base_rendered == head_rendered:
        return 0

    # Produce a unified diff.
    diff_lines = list(
        difflib.unified_diff(
            base_rendered.splitlines(keepends=True),
            head_rendered.splitlines(keepends=True),
            fromfile=f"CLAUDE.md @ {base_ref}",
            tofile=f"CLAUDE.md @ {head_ref}",
        )
    )
    sys.stdout.writelines(diff_lines)
    return 1


if __name__ == "__main__":
    sys.exit(main())
