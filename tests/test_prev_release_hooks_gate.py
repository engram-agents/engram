"""Cross-release hook-availability gate (#3253). Contract: tests-first, Lei 2026-09-19.

Claude Code reads hook registrations at SESSION START, so a session begun on
release N still holds release N's registrations after the plugin is rebuilt on
release N+1.  If a script that release N registered is ABSENT from the rebuilt
plugin, every matching tool call runs python on a missing file, python exits 2,
and Claude Code treats a non-zero PreToolUse hook exit as a BLOCK.  engram-alpha
#3213 did exactly this (engram-toolcall-repair.py) and was caught by a colleague
reading a CHANGELOG draft — not by any gate.  The remedy already in the tree is
a TOMBSTONE: an unregistered no-op file left at the path the old registration
names (see src/engram/hooks/claude/engram-toolcall-repair.py, the canonical
statement of the convention).

This module is the cross-release SIBLING of build.py's check_hooks_consistency:
the same existence test, but the reference set comes from the PREVIOUS
release's hooks.json rather than from the current tree's.

Contract — the report status is one of {"pass", "fail", "unknown"}:

1. The ref is the newest ``v*`` tag that is an ANCESTOR of HEAD.  No such tag
   ⇒ status ``unknown``.
2. The reference set is every hook-file basename referenced by any ``command``
   in ``git show <ref>:src/engram/hooks/hooks.json``, extracted with the
   build's own ``HOOK_REF_RE`` (imported, never re-copied: a second copy of
   that regex is a drift hazard).  Source-tree path at the tag, not a built
   path.
3. Existence is tested against the CURRENT tree's BUILT (flattened) plugin
   layout, ``<plugin_root>/hooks/<basename>``, by reusing the build's own
   ``check_hooks_consistency`` (see ``_missing_in_built``).  A tombstone
   satisfies it by EXISTING — it is deliberately NOT required to be registered
   in the current hooks.json, because being unregistered is what a tombstone IS.
4. A file present in the current build but not referenced by the ref is never
   a finding.
5. ``unknown``, never ``pass``, whenever the ref's hooks.json cannot be READ or
   is not the expected SHAPE.  Unreadable: no ancestor tag, ``git show``
   failing, invalid JSON.  Wrong shape: ANY deviation at ANY of the seven
   levels ``_validated_refs`` checks — ``hooks.json`` an object; ``hooks`` an
   object; each event value a list; each list element an object; each present
   ``hooks`` a list; its elements objects; each present ``command`` a string.
   Every extraction routes through that one validator, so the malformed-shape
   family is handled in one place rather than one shape at a time; a silent
   pass here is the exact failure this guard exists to prevent, so the unknown
   path names its reason.
   By contrast, a structurally VALID document declaring ZERO references is a
   KNOWN empty: validating every level makes a zero a fact rather than a symptom
   of a silent miss, so it is a ``pass`` whose ``referenced`` tuple is empty.
   ``pass`` therefore requires zero missing — NOT at least one reference.
   Nothing may raise out of the gate for any JSON-parseable input: the build
   engine's ``check_hooks_consistency`` call is guarded, and the advisory
   current-refs read can never raise either (see point 7).
6. ``fail`` names every missing path, sorted.  Partial reporting is a defect.
7. The tombstone-window list is ADVISORY ONLY and never changes the status:
   whether a file should now be deleted is a judgement, not a mechanical
   check.  Do not "tighten" it into a gate.
8. Layout safety: the build flattens ``hooks/claude/*`` → ``hooks/*``, so a
   fixed ``parents[N]`` path resolution overshoots in the deployed tree (see
   the project CLAUDE.md section "Plugin build restructures code paths").  This
   guard resolves the way the build itself does — an explicit
   ``<plugin_root>/hooks/<basename>`` — and
   ``test_flattened_layout_is_the_resolution_basis`` pins that against a
   src-shaped decoy.

Tier note (a known limitation, not a defect this guard can close on its own):
the reference set is the tag's *source* hooks.json, which registers hooks at
every tier, while the built plugin is filtered to one tier.  A narrower build
therefore reports a below-the-tier hook as "missing" — so the live acceptance
test builds the CUMULATIVE tier (``--tier dev --multi-agent``), where every
hook ships.  The mismatch cuts the other way too, and this guard does NOT catch
that direction: a hook that stays in the tree but is dropped from a NARROWER
install's build — re-tiered, or moved between tiers — leaves a session on that
tier blocking on a missing file, while this guard, whose live test runs on the
cumulative ``--tier dev --multi-agent`` build, still ships that hook and returns
``pass``.  That per-tier gap is filed as #3689 ("the cross-release hook gate
checks one built tier, but the failure it guards is per-tier").
Reconstructing which subset the previous release actually built at a given
install's tier is not mechanically possible from the tree alone.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Reused, never re-copied: the build's own reference regex, and the build's own
# existence test (see the module docstring, contract points 2 and 3).
from tools.engine.build import HOOK_REF_RE, check_hooks_consistency  # noqa: E402

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_UNKNOWN = "unknown"

# Source-tree path of the registration SOT, at the tag AND in the current tree.
PREV_HOOKS_JSON_SRC = "src/engram/hooks/hooks.json"

# The tombstone convention's deletion note: "delete this file ... in the first
# release AFTER <tag>".  ``\s+`` (not " ") because the note wraps across lines in
# the canonical docstring.
_TOMBSTONE_NOTE_RE = re.compile(r"first\s+release\s+AFTER\s+(\S+)", re.IGNORECASE)

_BUILD_PLUGIN_SH = os.path.join(PROJECT_ROOT, "tools", "build-plugin.sh")


# ─────────────────────────────────────────────────────────────────────────────
# the guard
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PrevReleaseHooksReport:
    """Report from :func:`check_prev_release_hooks`.

    ``status`` is one of ``pass`` / ``fail`` / ``unknown``.  ``passed_window_
    tombstones`` is advisory and never feeds ``status`` (contract point 7).
    """

    status: str
    reason: str = ""
    tag: str | None = None
    referenced: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    passed_window_tombstones: tuple[str, ...] = ()

    def format(self) -> str:
        lines = [f"prev-release hooks gate: {self.status.upper()}"]
        if self.tag:
            lines.append(f"  previous release tag: {self.tag}")
        if self.reason:
            lines.append(f"  reason: {self.reason}")
        if self.referenced:
            lines.append(f"  referenced hook files: {len(self.referenced)}")
        if self.missing:
            lines.append(f"  MISSING from the current build ({len(self.missing)}):")
            lines.extend(f"    hooks/{name}" for name in self.missing)
        if self.passed_window_tombstones:
            lines.append(
                "  advisory — tombstones past their deletion window "
                "(does NOT affect status):"
            )
            lines.extend(f"    hooks/{name}" for name in self.passed_window_tombstones)
        return "\n".join(lines)


def _run_git(repo_root: str, *args: str) -> subprocess.CompletedProcess:
    """Run git, capturing stdout/stderr as BYTES — deliberately NOT ``text=True``.

    ``text=True`` decodes INSIDE ``subprocess.run`` with ``errors="strict"``,
    before that call returns, so a non-UTF-8 byte anywhere in git's output
    escapes as ``UnicodeDecodeError`` past every caller's ``except`` clause.  The
    same file is safe through ``open(..., encoding="utf-8")`` only because there
    the decode happens in the CALLER's own frame — same bytes, opposite outcome
    (see ``_current_registered_refs`` vs ``check_prev_release_hooks``).

    Decoding is therefore each caller's explicit step here, via
    ``_decode_utf8_strict``, so each decides what an undecodable output means:
    for the previous release's hooks.json a loud ``unknown`` (contract point 5),
    for a tag list an unresolvable tag.
    """
    return subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True,
        timeout=60,
    )


def _decode_utf8_strict(raw: bytes) -> tuple[str | None, str]:
    """``(text, "")`` for valid UTF-8, else ``(None, reason)``.

    STRICT on purpose — ``errors="replace"`` / ``"ignore"`` / ``"surrogateescape"``
    are the obvious one-line patch and they are WRONG here: substitution silently
    ALTERS the document, so a hook path carrying a bad byte would decode to U+FFFD,
    ``HOOK_REF_RE`` would not match it, and the guard would report FEWER
    references with no signal at all — the silent-gap shape this guard exists to
    eliminate.  A decode failure is loud-to-``unknown``, never silently mangled
    into a successful parse.  ``reason`` names the byte and its position.
    """
    try:
        return raw.decode("utf-8"), ""
    except UnicodeDecodeError as exc:
        return None, f"the bytes are not valid UTF-8: {exc}"


def _ancestor_release_tags(repo_root: str) -> tuple[list[str] | None, str]:
    """``v*`` ancestor tags newest-creator-date-first, or ``(None, reason)``.

    ``--merged HEAD`` is the load-bearing half: a tag that is newer but not an
    ancestor of HEAD must never be selected (contract point 1).  ``None`` — as
    opposed to an empty list — means the tag list itself could not be READ:
    ``git tag`` failing, or a tag name git emitted that is not valid UTF-8.
    Both are ``unknown`` at the gate (contract point 5), never ``pass``.
    """
    proc = _run_git(
        repo_root, "tag", "--list", "v*", "--merged", "HEAD", "--sort=-creatordate"
    )
    if proc.returncode != 0:
        return None, (
            f"cannot list v* tags in {repo_root} (git tag exited {proc.returncode})"
        )
    text, decode_err = _decode_utf8_strict(proc.stdout)
    if text is None:
        return None, f"cannot read the v* tag list in {repo_root}: {decode_err}"
    return [line.strip() for line in text.splitlines() if line.strip()], ""


def _resolve_prev_release_tag(repo_root: str) -> tuple[str | None, str]:
    """``(tag, "")`` for the newest ancestor release, else ``(None, reason)``."""
    tags, err = _ancestor_release_tags(repo_root)
    if tags is None:
        return None, err
    if not tags:
        return None, (
            f"no v* tag reachable from HEAD in {repo_root} — cannot resolve the "
            "previous release (shallow clone?)"
        )
    return tags[0], ""


def _prev_hooks_json(repo_root: str, tag: str) -> tuple[str | None, str]:
    """The previous release's hooks.json text, or ``(None, reason)``.

    The blob is read as BYTES and decoded HERE, strictly and explicitly, so a
    non-UTF-8 document is a named ``unknown`` — contract point 5's "cannot be
    READ" clause — instead of an exception out of ``subprocess``.
    """
    proc = _run_git(repo_root, "show", f"{tag}:{PREV_HOOKS_JSON_SRC}")
    if proc.returncode != 0:
        return None, (
            f"cannot read {PREV_HOOKS_JSON_SRC} at {tag} "
            f"(git show exited {proc.returncode})"
        )
    text, decode_err = _decode_utf8_strict(proc.stdout)
    if text is None:
        return None, f"cannot read {PREV_HOOKS_JSON_SRC} at {tag}: {decode_err}"
    return text, ""


def _extract_hook_refs(hooks_json: dict) -> set[str]:
    """Every hook-file basename referenced by any ``command`` entry.

    Assumes ``hooks_json`` has already passed ``_validated_refs``: this walk is
    deliberately unguarded, and an unvalidated shape would either raise
    (``AttributeError`` / ``TypeError``) or silently yield an EMPTY set — the
    silent pass contract point 5 forbids.  Validate first, always.
    """
    refs: set[str] = set()
    for groups in hooks_json.get("hooks", {}).values():
        for group in groups:
            for entry in group.get("hooks", []):
                for match in HOOK_REF_RE.finditer(entry.get("command", "")):
                    refs.add(match.group(1))
    return refs


def _validated_refs(data: object) -> tuple[set[str] | None, str]:
    """``(refs, "")`` for a structurally valid hooks.json, else ``(None, reason)``.

    The ONE structural gate for this guard: every extraction routes through it
    so the whole malformed-shape family is handled in a single place rather than
    one shape at a time.  It validates EVERY level ``_extract_hook_refs`` walks:

        hooks.json            an object
          "hooks"             present, and an object
            <event>           a list
              <group>         an object
                "hooks"       if present, a list
                  <entry>     an object
                    "command" if present, a string

    Optional keys are type-checked only when present.  ``reason`` is non-empty
    and names the offending JSON path and the type actually found, e.g.
    ``hooks.PreToolUse is a dict, expected a list``.
    """
    if not isinstance(data, dict):
        return None, f"hooks.json is a {type(data).__name__}, expected an object"
    if "hooks" not in data:
        return None, "hooks.json has no 'hooks' key"
    hooks = data["hooks"]
    if not isinstance(hooks, dict):
        return None, f"hooks is a {type(hooks).__name__}, expected an object"
    for event_name, groups in hooks.items():
        if not isinstance(groups, list):
            return None, (
                f"hooks.{event_name} is a {type(groups).__name__}, expected a list"
            )
        for i, group in enumerate(groups):
            if not isinstance(group, dict):
                return None, (
                    f"hooks.{event_name}[{i}] is a {type(group).__name__}, "
                    "expected an object"
                )
            if "hooks" not in group:
                continue
            entries = group["hooks"]
            if not isinstance(entries, list):
                return None, (
                    f"hooks.{event_name}[{i}].hooks is a {type(entries).__name__}, "
                    "expected a list"
                )
            for j, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    return None, (
                        f"hooks.{event_name}[{i}].hooks[{j}] is a "
                        f"{type(entry).__name__}, expected an object"
                    )
                if "command" not in entry:
                    continue
                command = entry["command"]
                if not isinstance(command, str):
                    return None, (
                        f"hooks.{event_name}[{i}].hooks[{j}].command is a "
                        f"{type(command).__name__}, expected a string"
                    )
    return _extract_hook_refs(data), ""


def _missing_in_built(
    plugin_root: str, prev_hooks_json_text: str
) -> tuple[list[str] | None, str]:
    """Basenames referenced by the PREVIOUS release but absent from the build.

    The existence test is the build's own ``check_hooks_consistency``, not a
    second copy of it: a shadow plugin root mirrors ``plugin_root/hooks/`` and
    carries the previous release's hooks.json, so the build's function is asked
    exactly the question this guard needs answered.  Skipping ``hooks.json``
    while mirroring keeps the reference set coming from the TAG, never from the
    current tree.

    Returns ``(missing, "")`` on success, or ``(None, reason)`` when the build's
    ``check_hooks_consistency`` raises.  That function carries its own
    malformed-shape family (a pre-existing defect, filed separately); guarding
    the call here is what keeps THIS gate correct regardless of it — the failure
    surfaces as ``unknown``, never as a propagated exception and never as a
    silent ``[]``.
    """
    with tempfile.TemporaryDirectory(prefix="prev-release-hooks-shadow-") as shadow:
        shadow_hooks = os.path.join(shadow, "hooks")
        os.makedirs(shadow_hooks)
        real_hooks = os.path.join(plugin_root, "hooks")
        if os.path.isdir(real_hooks):
            for name in os.listdir(real_hooks):
                if name == "hooks.json":
                    continue
                os.symlink(os.path.join(real_hooks, name), os.path.join(shadow_hooks, name))
        with open(os.path.join(shadow_hooks, "hooks.json"), "w", encoding="utf-8") as fh:
            fh.write(prev_hooks_json_text)
        try:
            return check_hooks_consistency(shadow), ""
        except Exception as exc:  # noqa: BLE001 — fail loud to ``unknown``, never propagate
            return None, (
                "the build's check_hooks_consistency raised on the previous "
                f"release's hooks.json: {type(exc).__name__}: {exc}"
            )


def _current_registered_refs(repo_root: str) -> set[str]:
    """Basenames the CURRENT source hooks.json registers (the SOT, unfiltered).

    The source file, not the built one: the built copy is tier-filtered, and a
    tier-dropped registration would masquerade as an unregistered tombstone.
    EVERY failure — unreadable, undecodable, invalid JSON, a document json
    cannot parse at all (nested past the recursion limit), or a structurally
    malformed shape — yields an EMPTY set: the result only widens the ADVISORY
    candidate list and can never change the status (contract point 7).  This
    function never raises, on any shape.
    """
    path = os.path.join(repo_root, *PREV_HOOKS_JSON_SRC.split("/"))
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, RecursionError):
        # OSError: unreadable.  ValueError: invalid JSON — which INCLUDES
        # UnicodeDecodeError, raised here in this frame (not inside subprocess),
        # so a non-UTF-8 file is already covered on this path.  RecursionError (a
        # RuntimeError, NOT a ValueError): json's recursive parser on a document
        # nested past the recursion limit.  Every one yields an EMPTY set: the
        # result only widens the ADVISORY candidate list and can never change the
        # status (contract point 7).
        return set()
    refs, _reason = _validated_refs(data)
    return refs if refs is not None else set()


def _release_has_successor(repo_root: str, named_tag: str) -> bool:
    """Has a release AFTER ``named_tag`` shipped?

    True iff ``named_tag`` is itself an ancestor of HEAD (so it shipped) AND it
    is not the newest ancestor release (so a later release exists).  When the
    named tag is absent — e.g. "v0.5.2" while only ``v0.5.2-rc3`` exists — the
    release has not shipped and the tombstone's window is still ahead.  An
    UNREADABLE tag list (``None``) is also ``False``: this is advisory only and
    can never change the status (contract point 7).
    """
    tags, _err = _ancestor_release_tags(repo_root)
    if tags is None or named_tag not in tags:
        return False
    return tags[0] != named_tag


def _passed_window_tombstones(
    repo_root: str,
    plugin_root: str,
    referenced: tuple[str, ...],
    current_refs: set[str],
) -> tuple[str, ...]:
    """ADVISORY ONLY — never feeds the status (contract point 7).

    A candidate tombstone is a basename the previous release referenced, that
    exists in the current build, and that the current hooks.json no longer
    registers — i.e. an unregistered no-op sitting at an old registration's
    path.  It is listed only when its own deletion note names a release whose
    successor has already shipped.  Being one release early or late in this
    list is harmless; treating it as a gate is not.
    """
    passed: list[str] = []
    for name in sorted(referenced):
        if name in current_refs:
            continue
        path = os.path.join(plugin_root, "hooks", name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        match = _TOMBSTONE_NOTE_RE.search(text)
        if not match:
            continue
        named = match.group(1).strip().rstrip(".,;:)'\"")
        if named and _release_has_successor(repo_root, named):
            passed.append(name)
    return tuple(passed)


def check_prev_release_hooks(repo_root: str, plugin_root: str) -> PrevReleaseHooksReport:
    """Cross-release hook-availability gate (contract above).

    Parameters
    ----------
    repo_root:
        The source checkout, used for tag resolution and for reading the
        previous release's hooks.json out of git.
    plugin_root:
        A BUILT plugin root (flattened: ``<plugin_root>/hooks/<basename>``).
        Pass the cumulative tier (dev + multi-agent) to avoid tier-gating
        false positives — see the module docstring's tier note.
    """
    tag, tag_err = _resolve_prev_release_tag(repo_root)
    if tag is None:
        return PrevReleaseHooksReport(STATUS_UNKNOWN, reason=tag_err)

    raw, err = _prev_hooks_json(repo_root, tag)
    if raw is None:
        return PrevReleaseHooksReport(STATUS_UNKNOWN, reason=err, tag=tag)

    try:
        data = json.loads(raw)
    except ValueError as exc:
        return PrevReleaseHooksReport(
            STATUS_UNKNOWN,
            reason=f"{PREV_HOOKS_JSON_SRC} at {tag} is not valid JSON: {exc}",
            tag=tag,
        )
    except RecursionError as exc:
        # json.loads on a document nested past the recursion limit.  A
        # RuntimeError, NOT a ValueError, so the clause above does not see it —
        # and contract point 5 makes an unparseable document ``unknown``.
        return PrevReleaseHooksReport(
            STATUS_UNKNOWN,
            reason=(
                f"{PREV_HOOKS_JSON_SRC} at {tag} is nested too deeply for json "
                f"to parse: {type(exc).__name__}: {exc}"
            ),
            tag=tag,
        )
    refs, shape_err = _validated_refs(data)
    if refs is None:
        return PrevReleaseHooksReport(
            STATUS_UNKNOWN,
            reason=(
                f"{PREV_HOOKS_JSON_SRC} at {tag} is not the expected shape: "
                f"{shape_err}"
            ),
            tag=tag,
        )
    # A structurally VALID zero is a KNOWN empty, not an unknown: validating
    # every walk level makes "zero references" a fact rather than a silent miss,
    # so it is a ``pass`` whose ``referenced`` tuple is empty (contract point 5).
    referenced = tuple(sorted(refs))

    missing, missing_err = _missing_in_built(plugin_root, raw)
    if missing is None:
        return PrevReleaseHooksReport(
            STATUS_UNKNOWN, reason=missing_err, tag=tag, referenced=referenced
        )
    missing = tuple(missing)
    passed_window = _passed_window_tombstones(
        repo_root, plugin_root, referenced, _current_registered_refs(repo_root)
    )

    if missing:
        status = STATUS_FAIL
        reason = (
            f"{len(missing)} hook file(s) referenced by {tag} are absent from "
            "the current build; a session started on that release would run "
            "python on a missing file (exit 2 == PreToolUse block) — add a "
            "tombstone, or restore the file"
        )
    else:
        status = STATUS_PASS
        reason = ""

    return PrevReleaseHooksReport(
        status=status,
        reason=reason,
        tag=tag,
        referenced=referenced,
        missing=missing,
        passed_window_tombstones=passed_window,
    )


# ─────────────────────────────────────────────────────────────────────────────
# scratch-repo + built-root fixtures (hermetic; the live repo state is never read)
# ─────────────────────────────────────────────────────────────────────────────


def _git(path, *args, env=None):
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=run_env,
    )


def _commit(repo, message: str, when: str | None = None) -> None:
    """Commit everything.  ``when`` fixes the committer date, so a fixture's
    tag ordering is deterministic (two commits in one second would otherwise
    tie on creator-date and fall back to refname order)."""
    _git(repo, "add", "-A")
    env = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when} if when else None
    proc = _git(repo, "commit", "-q", "-m", message, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _new_repo(tmp_path, name: str = "scratch-repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "gate-tests@example.invalid")
    _git(repo, "config", "user.name", "gate tests")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _hooks_json(refs) -> dict:
    """A hooks.json referencing one command per hook basename in ``refs``."""
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                '$HOME/.engram/venv/bin/python3 '
                                '"${CLAUDE_PLUGIN_ROOT}/hooks/' + ref + '"'
                            ),
                        }
                        for ref in refs
                    ]
                }
            ]
        }
    }


def _write_hooks_json(repo, refs) -> None:
    hooks_dir = repo / "src" / "engram" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(_hooks_json(refs)), encoding="utf-8"
    )


def _write_raw_hooks_json(repo, payload: str | bytes) -> None:
    """Write ``src/engram/hooks/hooks.json`` from RAW bytes — a malformed shape.

    ``bytes`` is accepted alongside ``str`` because the READ/DECODE layer's own
    escapes cannot be expressed any other way: a payload that is not valid UTF-8
    has no ``str`` form at all, and ``write_text`` would refuse it.  The
    byte-level rows in ``_BYTE_LEVEL_HOOKS_JSON_PAYLOADS`` are what reach that
    layer — the dict-valued tables above reach only ``_validated_refs``.
    """
    hooks_dir = repo / "src" / "engram" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    path = hooks_dir / "hooks.json"
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")


def _write_built_root(tmp_path, files, name: str = "built", built_refs=()):
    """A flattened built plugin root: ``<root>/hooks/<basename>``.

    ``files`` maps a basename to its text.  ``built_refs`` are the basenames the
    built ``hooks.json`` registers — deliberately independent of the previous
    release's reference set, so a test can prove which one the guard consults.
    """
    root = tmp_path / name
    hooks_dir = root / "hooks"
    hooks_dir.mkdir(parents=True)
    for basename, text in files.items():
        (hooks_dir / basename).write_text(text, encoding="utf-8")
    (hooks_dir / "hooks.json").write_text(
        json.dumps(_hooks_json(built_refs)), encoding="utf-8"
    )
    return root


@pytest.fixture
def repo_with_prev_tag(tmp_path):
    """HEAD = post-release commit; ``v1.0`` (an ancestor) references alpha.py.

    The post-release commit's hooks.json registers NOTHING, so alpha.py is the
    unregistered-after-the-release shape a tombstone has.
    """
    repo = _new_repo(tmp_path)
    _write_hooks_json(repo, ["alpha.py"])
    _commit(repo, "release", when="2026-01-01T00:00:00+00:00")
    _git(repo, "tag", "v1.0")
    _write_hooks_json(repo, [])
    _commit(repo, "post-release", when="2026-02-01T00:00:00+00:00")
    return repo


# ─────────────────────────────────────────────────────────────────────────────
# (1) ref resolution — newest v* tag that is an ANCESTOR of HEAD
# ─────────────────────────────────────────────────────────────────────────────


class TestRefResolution:
    def test_newest_ancestor_tag_is_selected(self, repo_with_prev_tag, tmp_path):
        built = _write_built_root(tmp_path, {"alpha.py": ""})
        report = check_prev_release_hooks(str(repo_with_prev_tag), str(built))
        assert report.tag == "v1.0", report.format()

    def test_newer_non_ancestor_tag_is_ignored(self, tmp_path):
        """A newer v* tag that is NOT an ancestor of HEAD must not be selected.

        Discriminating case for the ``--merged HEAD`` filter: the candidate tag
        list is non-empty in both orderings, so a resolver that dropped the
        ancestry filter (or took the head of the full, unfiltered tag list)
        would answer ``v2.0`` here — and ``v2.0``'s hooks.json (written before
        the park) does not reference alpha.py, so the guard would also flip to
        ``pass`` for the wrong reason.
        """
        repo = _new_repo(tmp_path)
        _write_hooks_json(repo, ["alpha.py"])
        _commit(repo, "release", when="2026-01-01T00:00:00+00:00")
        _git(repo, "tag", "v1.0")
        release_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        _write_hooks_json(repo, [])
        _commit(repo, "newer branch work", when="2027-01-01T00:00:00+00:00")
        _git(repo, "tag", "v2.0")

        # Park HEAD back on the v1.0 commit: v2.0 now EXISTS but is not an ancestor.
        checkout = _git(repo, "checkout", "-q", release_sha)
        assert checkout.returncode == 0, checkout.stderr

        built = _write_built_root(tmp_path, {"alpha.py": ""})
        report = check_prev_release_hooks(str(repo), str(built))
        assert report.tag == "v1.0", report.format()

    def test_no_ancestor_tag_is_unknown(self, tmp_path):
        repo = _new_repo(tmp_path)
        _write_hooks_json(repo, ["alpha.py"])
        _commit(repo, "commit with no tag")
        built = _write_built_root(tmp_path, {"alpha.py": ""})

        report = check_prev_release_hooks(str(repo), str(built))
        assert report.status == STATUS_UNKNOWN, report.format()
        assert report.status != STATUS_PASS
        assert report.missing == ()
        assert "tag" in report.reason.lower(), report.reason


# ─────────────────────────────────────────────────────────────────────────────
# (2)(3)(4)(8) reference extraction, build-layout existence, never-registered
# ─────────────────────────────────────────────────────────────────────────────


class TestReferenceExtractionAndExistence:
    def test_script_missing_from_build_is_fail_and_names_it(
        self, repo_with_prev_tag, tmp_path
    ):
        """(a) referenced by the previous tag, absent from the current build ⇒ fail."""
        built = _write_built_root(tmp_path, {"other.py": ""})
        report = check_prev_release_hooks(str(repo_with_prev_tag), str(built))

        assert report.status == STATUS_FAIL, report.format()
        assert report.missing == ("alpha.py",), report.format()
        assert "hooks/alpha.py" in report.format(), report.format()

    def test_every_missing_path_is_named_and_sorted(self, tmp_path):
        """(6) partial reporting is a defect: every missing path, sorted."""
        repo = _new_repo(tmp_path)
        _write_hooks_json(repo, ["charlie.py", "alpha.py", "bravo.sh"])
        _commit(repo, "release")
        _git(repo, "tag", "v1.0")
        _write_hooks_json(repo, [])
        _commit(repo, "post-release")

        built = _write_built_root(tmp_path, {})
        report = check_prev_release_hooks(str(repo), str(built))

        assert report.status == STATUS_FAIL, report.format()
        assert report.missing == ("alpha.py", "bravo.sh", "charlie.py"), report.format()
        rendered = report.format()
        for name in ("alpha.py", "bravo.sh", "charlie.py"):
            assert f"hooks/{name}" in rendered, rendered

    def test_tombstone_present_but_unregistered_is_pass(
        self, repo_with_prev_tag, tmp_path
    ):
        """(b) a file at the old path that is NOT registered today satisfies it."""
        built = _write_built_root(
            tmp_path, {"alpha.py": ""}, built_refs=["something-else.py"]
        )
        report = check_prev_release_hooks(str(repo_with_prev_tag), str(built))

        assert report.status == STATUS_PASS, report.format()
        assert report.missing == ()

    def test_build_only_file_not_referenced_by_tag_is_ignored(
        self, repo_with_prev_tag, tmp_path
    ):
        """(4)(c) a file in the build the previous release never referenced is not a finding."""
        built = _write_built_root(
            tmp_path,
            {"alpha.py": "", "brand-new.py": ""},
            built_refs=["brand-new.py"],
        )
        report = check_prev_release_hooks(str(repo_with_prev_tag), str(built))

        assert report.status == STATUS_PASS, report.format()
        assert "brand-new.py" not in report.missing
        assert "brand-new.py" not in report.referenced
        assert "brand-new.py" not in report.format()

    def test_flattened_layout_is_the_resolution_basis(
        self, repo_with_prev_tag, tmp_path
    ):
        """(8) resolve like the build: ``<plugin_root>/hooks/<name>``, flattened.

        A src-shaped decoy (``hooks/claude/alpha.py``) is present in the second
        root but the flattened path is not — a resolver that walked parents
        looking for the src nesting would wrongly report the file present.
        """
        flattened = _write_built_root(tmp_path, {"alpha.py": ""}, name="flat")
        nested = tmp_path / "nested"
        (nested / "hooks" / "claude").mkdir(parents=True)
        (nested / "hooks" / "claude" / "alpha.py").write_text("", encoding="utf-8")
        (nested / "hooks" / "hooks.json").write_text(
            json.dumps(_hooks_json([])), encoding="utf-8"
        )

        assert (
            check_prev_release_hooks(str(repo_with_prev_tag), str(flattened)).status
            == STATUS_PASS
        )
        nested_report = check_prev_release_hooks(str(repo_with_prev_tag), str(nested))
        assert nested_report.status == STATUS_FAIL, nested_report.format()
        assert nested_report.missing == ("alpha.py",), nested_report.format()


# ─────────────────────────────────────────────────────────────────────────────
# (5) unknown, never pass, when the previous hooks.json cannot be read
# ─────────────────────────────────────────────────────────────────────────────


class TestUnknownNeverPass:
    def test_hooks_json_absent_at_tag_is_unknown(self, tmp_path):
        repo = _new_repo(tmp_path)
        (repo / "README.md").write_text("no hooks.json here\n", encoding="utf-8")
        _commit(repo, "release without hooks.json")
        _git(repo, "tag", "v1.0")
        (repo / "README.md").write_text("still none\n", encoding="utf-8")
        _commit(repo, "post-release")

        built = _write_built_root(tmp_path, {"alpha.py": ""})
        report = check_prev_release_hooks(str(repo), str(built))
        assert report.status == STATUS_UNKNOWN, report.format()
        assert report.status != STATUS_PASS

    def test_invalid_json_is_unknown(self, tmp_path):
        repo = _new_repo(tmp_path)
        hooks_dir = repo / "src" / "engram" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text("{ not json", encoding="utf-8")
        _commit(repo, "release")
        _git(repo, "tag", "v1.0")
        (repo / "README.md").write_text("later\n", encoding="utf-8")
        _commit(repo, "post-release")

        built = _write_built_root(tmp_path, {"alpha.py": ""})
        report = check_prev_release_hooks(str(repo), str(built))
        assert report.status == STATUS_UNKNOWN, report.format()
        assert report.status != STATUS_PASS

    def test_missing_hooks_key_is_unknown(self, tmp_path):
        """A structurally-wrong hooks.json must not read as "zero references"."""
        repo = _new_repo(tmp_path)
        hooks_dir = repo / "src" / "engram" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )
        _commit(repo, "release")
        _git(repo, "tag", "v1.0")
        (repo / "README.md").write_text("later\n", encoding="utf-8")
        _commit(repo, "post-release")

        built = _write_built_root(tmp_path, {"alpha.py": ""})
        report = check_prev_release_hooks(str(repo), str(built))
        assert report.status == STATUS_UNKNOWN, report.format()
        assert report.status != STATUS_PASS

    def test_hooks_value_not_an_object_is_unknown(self, tmp_path):
        """(5) a valid-JSON ``hooks`` value of the wrong TYPE must read unknown.

        ``{"hooks": []}`` passes a bare ``"hooks" in data`` membership test, then
        ``_extract_hook_refs`` calls ``.values()`` on a list and raises
        ``AttributeError: 'list' object has no attribute 'values'``.  Loud, but a
        wrongly-shaped hooks.json *cannot be read* for references, so contract
        point 5 makes it ``unknown`` — with the reason naming the shape problem,
        not a bare crash.
        """
        repo = _new_repo(tmp_path)
        hooks_dir = repo / "src" / "engram" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps({"hooks": []}), encoding="utf-8"
        )
        _commit(repo, "release")
        _git(repo, "tag", "v1.0")
        (repo / "README.md").write_text("later\n", encoding="utf-8")
        _commit(repo, "post-release")

        built = _write_built_root(tmp_path, {"alpha.py": ""})
        report = check_prev_release_hooks(str(repo), str(built))
        assert report.status == STATUS_UNKNOWN, report.format()
        assert report.status != STATUS_PASS
        assert "list" in report.reason, report.reason


# ─────────────────────────────────────────────────────────────────────────────
# (5) the WHOLE malformed-shape family: ``unknown``, never a silent ``pass``
#
# Table-driven ON PURPOSE.  The class got in one member at a time — a
# one-off test per shape is what let the next sibling through — so every
# shape shares one parametrized assertion AND the success control shares the
# test class.  Without the control a guard that answered ``unknown``
# unconditionally would satisfy the whole table.
# ─────────────────────────────────────────────────────────────────────────────


def _repo_with_tagged_raw_hooks_json(
    tmp_path, payload: str | bytes, name: str = "raw-shape-repo"
):
    """A repo whose PREVIOUS release tag (``v1.0``) carries ``payload`` as hooks.json.

    HEAD sits one commit past the tag, so ``v1.0`` is the newest ancestor release.
    ``payload`` may be ``bytes`` so a row can pin a document that is not valid
    UTF-8 — see ``_write_raw_hooks_json``.
    """
    repo = _new_repo(tmp_path, name)
    _write_raw_hooks_json(repo, payload)
    _commit(repo, "release", when="2026-01-01T00:00:00+00:00")
    _git(repo, "tag", "v1.0")
    (repo / "README.md").write_text("post-release\n", encoding="utf-8")
    _commit(repo, "post-release", when="2026-02-01T00:00:00+00:00")
    return repo


# One row per shape that DEVIATES from the seven-level schema.  Each is
# ``unknown`` — the extractor's walk must never see these.  A structurally
# valid document that merely declares ZERO references is a DIFFERENT state;
# see ``_VALID_EMPTY_HOOKS_JSON_SHAPES`` below.
_MALFORMED_HOOKS_JSON_SHAPES = [
    pytest.param({"hooks": {"PreToolUse": {}}}, id="event-maps-to-object"),
    pytest.param({"hooks": {"PreToolUse": [{"hooks": {}}]}}, id="inner-hooks-is-object"),
    pytest.param(
        {"hooks": {"PreToolUse": {"hooks": []}}}, id="event-object-instead-of-list"
    ),
    pytest.param(
        {"hooks": {"PreToolUse": [{"hooks": [{"command": None}]}]}},
        id="command-is-null",
    ),
    pytest.param({"hooks": {"PreToolUse": ["nope"]}}, id="group-is-a-string"),
    pytest.param(
        {"hooks": {"PreToolUse": [{"hooks": ["cmd"]}]}}, id="entry-is-a-string"
    ),
    pytest.param(
        {"hooks": {"PreToolUse": [{"hooks": [{"command": {}}]}]}},
        id="command-is-an-object",
    ),
    pytest.param({"hooks": []}, id="hooks-is-a-list"),
]

# Structurally VALID documents that declare no references at all.  Malformed
# and valid-empty are different states: the validator above every walk level
# makes a zero a FACT rather than a silent miss, so these read ``pass`` with an
# empty ``referenced`` tuple.  Pinned explicitly so the distinction is asserted
# rather than assumed — a guard that collapsed them would pass the table above
# by answering ``unknown`` unconditionally.
_VALID_EMPTY_HOOKS_JSON_SHAPES = [
    pytest.param({"hooks": {}}, id="hooks-empty-object"),
    pytest.param({"hooks": {"PreToolUse": [{}]}}, id="group-without-hooks-key"),
]

# ── the READ/DECODE layer IN FRONT of ``_validated_refs`` ────────────────────
#
# Every row above is a Python dict that has ALREADY been decoded, so that table
# structurally cannot reach this layer: these escapes happen BEFORE
# ``_validated_refs`` is ever called — inside ``subprocess.run``'s text-mode
# decode (``_run_git``) and inside ``json``'s recursive parser (``json.loads``).
# Bytes, not ``json.dumps(...)``, because a payload that is not valid UTF-8 has
# no ``str`` form.
#
# The asymmetry is the point and the reason both escapes live in one table:
# ``_current_registered_refs`` reads via ``open(..., encoding="utf-8")``, whose
# ``UnicodeDecodeError`` IS a ``ValueError`` and is caught by its
# ``except (OSError, ValueError)`` — so the invalid-UTF-8 file is already safe
# THERE and fatal in ``check_prev_release_hooks``.  Same file, one path safe and
# one fatal, which is exactly why an end-to-end row per path is required.
_INVALID_UTF8_HOOKS_JSON = (
    b'{"hooks": {"PreToolUse": [{"hooks": [{"command": "x \xe9\xff\xfe"}]}]}}'
)
# Deep enough that ``json``'s recursive parser hits ``RecursionError`` (a
# RuntimeError, NOT a ValueError) on a document that is otherwise well-formed.
_DEEP_NESTING_HOOKS_JSON = b"[" * 100000 + b"]" * 100000
_WELLFORMED_HOOKS_JSON_BYTES = (
    b'{"hooks": {"PreToolUse": [{"hooks": '
    b'[{"command": "python3 $X/hooks/alpha.py"}]}]}}'
)

# The pass control is IN this table on purpose: without it, a guard that
# answered ``unknown`` unconditionally — or a decode change that broke the happy
# path — would satisfy every malformed byte row.
_BYTE_LEVEL_HOOKS_JSON_PAYLOADS = [
    pytest.param(
        _INVALID_UTF8_HOOKS_JSON,
        STATUS_UNKNOWN,
        None,
        id="invalid-utf-8-is-unknown",
    ),
    pytest.param(
        _DEEP_NESTING_HOOKS_JSON,
        STATUS_UNKNOWN,
        None,
        id="deep-nesting-is-unknown",
    ),
    pytest.param(
        _WELLFORMED_HOOKS_JSON_BYTES,
        STATUS_PASS,
        ("alpha.py",),
        id="wellformed-bytes-is-the-pass-control",
    ),
]

# The advisory current-refs read must be ``set()`` on these — never a raise.
_BYTE_LEVEL_CURRENT_REFS_PAYLOADS = [
    pytest.param(_INVALID_UTF8_HOOKS_JSON, id="invalid-utf-8-is-empty"),
    pytest.param(_DEEP_NESTING_HOOKS_JSON, id="deep-nesting-is-empty"),
]


class TestMalformedShapeFamilyIsUnknown:
    @pytest.mark.parametrize("shape", _MALFORMED_HOOKS_JSON_SHAPES)
    def test_malformed_prior_hooks_json_is_unknown_with_reason(self, tmp_path, shape):
        """Every malformed shape ⇒ ``unknown`` + a reason, and NO exception.

        All three halves are asserted per row, so the table cannot be satisfied
        by a guard that only stopped crashing, or only stopped silently passing.
        """
        repo = _repo_with_tagged_raw_hooks_json(tmp_path, json.dumps(shape))
        built = _write_built_root(tmp_path, {"alpha.py": ""})
        try:
            report = check_prev_release_hooks(str(repo), str(built))
        except Exception as exc:  # noqa: BLE001 — the contract forbids any escape
            pytest.fail(
                f"{shape!r} raised out of the guard: {type(exc).__name__}: {exc}"
            )
        assert report.status == STATUS_UNKNOWN, report.format()
        assert report.status != STATUS_PASS
        assert report.reason.strip(), report.format()

    def test_wellformed_prior_hooks_json_is_the_pass_control(self, tmp_path):
        """The control: a well-formed prior hooks.json whose ref EXISTS ⇒ ``pass``.

        This is what makes the table above mean something — a guard returning
        ``unknown`` unconditionally would pass every malformed row and fail here.
        """
        shape = {
            "hooks": {
                "PreToolUse": [{"hooks": [{"command": "python3 $X/hooks/alpha.py"}]}]
            }
        }
        repo = _repo_with_tagged_raw_hooks_json(tmp_path, json.dumps(shape))
        built = _write_built_root(tmp_path, {"alpha.py": ""})

        report = check_prev_release_hooks(str(repo), str(built))

        assert report.status == STATUS_PASS, report.format()
        assert report.referenced == ("alpha.py",), report.format()
        assert report.missing == ()

    @pytest.mark.parametrize("shape", _VALID_EMPTY_HOOKS_JSON_SHAPES)
    def test_structurally_valid_zero_reference_prior_is_the_valid_empty_control(
        self, tmp_path, shape
    ):
        """A VALID document declaring no references is KNOWN EMPTY ⇒ ``pass``.

        Malformed and valid-empty are distinct states.  Because every walk
        level is validated, a zero can no longer be *produced* by a silent miss,
        so a zero is a fact — and ``referenced == ()`` is what surfaces it.  The
        report must NOT grow a new field for this (see the amendment).  A guard
        returning ``unknown`` here would be asserting "cannot determine" about
        something the validator just determined.
        """
        repo = _repo_with_tagged_raw_hooks_json(tmp_path, json.dumps(shape))
        built = _write_built_root(tmp_path, {"alpha.py": ""})

        report = check_prev_release_hooks(str(repo), str(built))

        assert report.status == STATUS_PASS, report.format()
        assert report.missing == ()
        assert report.referenced == (), report.format()

    @pytest.mark.parametrize("shape", _MALFORMED_HOOKS_JSON_SHAPES)
    def test_current_registered_refs_never_raises_on_a_malformed_shape(
        self, tmp_path, shape
    ):
        """The advisory current-refs read is ``set()`` on ANY shape, never a raise.

        It feeds the tombstone-window list, which can never change the status
        (contract point 7) — a crash here would contradict that outright.
        """
        repo = _new_repo(tmp_path)
        _write_raw_hooks_json(repo, json.dumps(shape))
        try:
            refs = _current_registered_refs(str(repo))
        except Exception as exc:  # noqa: BLE001 — must never raise
            pytest.fail(
                f"{shape!r} raised out of _current_registered_refs: "
                f"{type(exc).__name__}: {exc}"
            )
        assert refs == set(), refs

    @pytest.mark.parametrize(
        "payload,expected_status,expected_referenced",
        _BYTE_LEVEL_HOOKS_JSON_PAYLOADS,
    )
    def test_byte_level_payloads_reach_the_read_layer(
        self, tmp_path, payload, expected_status, expected_referenced
    ):
        """Raw BYTES at the tag, end to end through a scratch git repo.

        The table above feeds already-decoded dicts, so it cannot reach the
        layer these two escapes live in: a document that is not valid UTF-8
        (``UnicodeDecodeError`` out of ``subprocess.run``'s text-mode decode) and
        a document ``json`` cannot parse for depth (``RecursionError``).  Both
        are contract point 5's "cannot be READ" clause — ``unknown``, never
        ``pass``, and never an exception out of the gate.

        The third row is the pass CONTROL, in this same table so a guard that
        refused everything (or a decode change that broke the happy path) cannot
        satisfy the two above.
        """
        repo = _repo_with_tagged_raw_hooks_json(tmp_path, payload)
        built = _write_built_root(tmp_path, {"alpha.py": ""})
        try:
            report = check_prev_release_hooks(str(repo), str(built))
        except Exception as exc:  # noqa: BLE001 — the contract forbids any escape
            pytest.fail(
                f"{payload[:48]!r}… raised out of the guard: "
                f"{type(exc).__name__}: {exc}"
            )
        assert report.status == expected_status, report.format()
        if expected_status == STATUS_UNKNOWN:
            assert report.status != STATUS_PASS
            assert report.reason.strip(), report.format()
        else:
            assert report.referenced == expected_referenced, report.format()
            assert report.missing == ()

    @pytest.mark.parametrize("payload", _BYTE_LEVEL_CURRENT_REFS_PAYLOADS)
    def test_current_registered_refs_never_raises_on_a_byte_level_payload(
        self, tmp_path, payload
    ):
        """``set()`` for a payload ``json`` cannot decode OR cannot parse.

        The invalid-UTF-8 row is the asymmetry pinned: this read is ALREADY safe
        there (``UnicodeDecodeError`` is a ``ValueError``, caught by the caller's
        own ``except``), while the same file escapes the gate on
        ``check_prev_release_hooks``.  The deep-nesting row is the one that was
        NOT safe on either path.
        """
        repo = _new_repo(tmp_path)
        _write_raw_hooks_json(repo, payload)
        try:
            refs = _current_registered_refs(str(repo))
        except Exception as exc:  # noqa: BLE001 — must never raise
            pytest.fail(
                f"{payload[:48]!r}… raised out of _current_registered_refs: "
                f"{type(exc).__name__}: {exc}"
            )
        assert refs == set(), refs


# ─────────────────────────────────────────────────────────────────────────────
# (7) the tombstone-window list is advisory and never changes the status
# ─────────────────────────────────────────────────────────────────────────────


_TOMBSTONE_NOTE = (
    "TOMBSTONE: tomb.py is a no-op kept for one release.\n"
    "delete this file in the first release AFTER v1.0.\n"
)


class TestTombstoneWindowAdvisory:
    def _repo_with_two_releases(self, tmp_path):
        repo = _new_repo(tmp_path)
        _write_hooks_json(repo, ["tomb.py"])
        _commit(repo, "release v1.0", when="2026-01-01T00:00:00+00:00")
        _git(repo, "tag", "v1.0")
        (repo / "README.md").write_text("v1.1\n", encoding="utf-8")
        _commit(repo, "release v1.1", when="2026-03-01T00:00:00+00:00")
        _git(repo, "tag", "v1.1")
        _write_hooks_json(repo, [])  # the tombstone is unregistered at HEAD
        _commit(repo, "post-release", when="2026-04-01T00:00:00+00:00")
        return repo

    def test_passed_window_tombstone_listed_without_changing_status(self, tmp_path):
        """(f) a release AFTER the named one has shipped ⇒ listed; status untouched."""
        repo = self._repo_with_two_releases(tmp_path)
        built = _write_built_root(tmp_path, {"tomb.py": _TOMBSTONE_NOTE})

        report = check_prev_release_hooks(str(repo), str(built))

        assert report.status == STATUS_PASS, report.format()
        assert report.missing == ()
        assert report.passed_window_tombstones == ("tomb.py",), report.format()
        assert "hooks/tomb.py" in report.format()

    def test_unshipped_window_tombstone_not_listed(self, tmp_path):
        """v1.0 is the NEWEST ancestor release ⇒ its successor has not shipped."""
        repo = _new_repo(tmp_path)
        _write_hooks_json(repo, ["tomb.py"])
        _commit(repo, "release v1.0", when="2026-01-01T00:00:00+00:00")
        _git(repo, "tag", "v1.0")
        _write_hooks_json(repo, [])
        _commit(repo, "post-release", when="2026-04-01T00:00:00+00:00")
        built = _write_built_root(tmp_path, {"tomb.py": _TOMBSTONE_NOTE})

        report = check_prev_release_hooks(str(repo), str(built))

        assert report.status == STATUS_PASS, report.format()
        assert report.passed_window_tombstones == (), report.format()


# ─────────────────────────────────────────────────────────────────────────────
# live end-to-end acceptance — the one test that reads the real repo state
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def live_prev_tag():
    """The real repo's previous-release tag, or a clean skip in a shallow checkout."""
    proc = subprocess.run(
        ["git", "-C", PROJECT_ROOT, "tag", "--list", "v*", "--merged", "HEAD",
         "--sort=-creatordate"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    tags = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if proc.returncode != 0 or not tags:
        pytest.skip(
            "no v* tag reachable in this checkout (shallow clone / depth-1): the "
            "cross-release guard cannot resolve a previous release here"
        )
    return tags[0]


@pytest.fixture(scope="module")
def live_built_plugin(tmp_path_factory, live_prev_tag):
    """A dev+multi-agent build — the CUMULATIVE tier, so tier-gating cannot
    produce a false 'missing' for a hook the previous release registered but
    that ships below the essential tier.

    Depends on ``live_prev_tag`` so a shallow checkout skips BEFORE paying for
    a build, rather than relying on pytest's fixture-argument ordering.
    """
    out = str(tmp_path_factory.mktemp("live-built") / "plugin")
    proc = subprocess.run(
        ["bash", _BUILD_PLUGIN_SH, "--allow-branch", "--allow-untagged",
         "--tier", "dev", "--multi-agent", "--output", out],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return out


class TestLiveRepositoryAcceptance:
    def test_live_repo_passes(self, live_prev_tag, live_built_plugin):
        report = check_prev_release_hooks(PROJECT_ROOT, live_built_plugin)
        if report.status == STATUS_UNKNOWN:
            # In this deep-checkout pr-checks job the newest ancestor tag
            # resolves readably, so ``unknown`` here is an ANOMALY (e.g. a
            # reorganisation moving src/engram/hooks/hooks.json), not the
            # shallow case — the shallow case already skipped in live_prev_tag.
            # A skip would read green while the gate does nothing, so FAIL.
            pytest.fail(
                "the live guard returned unknown although the previous-release "
                f"tag {live_prev_tag!r} resolved (the shallow-checkout case is "
                "handled by the live_prev_tag fixture skip): "
                f"{report.reason}"
            )

        assert report.status == STATUS_PASS, report.format()
        assert report.missing == (), report.format()
        assert report.tag == live_prev_tag

        # NOTE: the #3213 incident's specific reference is deliberately NOT
        # asserted here.  This test runs against whatever tag is NEWEST; a later
        # release that legitimately stops registering the renamed hook would
        # make a GREEN report fail here.  The historical record is pinned at its
        # own release in TestLiveHistoricalIncident below.

    def test_live_unknown_with_resolved_tag_fails_not_skips(
        self, monkeypatch, tmp_path
    ):
        """The live guard's ``unknown`` branch FAILS (never skips) once a tag resolved.

        ``test_live_repo_passes`` receives ``live_prev_tag`` only after that
        fixture has resolved a real ancestor tag — a shallow checkout skips in
        the fixture, so the shallow case never reaches the test body.  An
        ``unknown`` from the guard at that point is therefore the deep-checkout
        ANOMALY (e.g. a reorganisation moving ``src/engram/hooks/hooks.json``),
        and the live assertion must fail on it rather than read green as a skip.

        This test monkeypatches the guard to return ``unknown`` with the tag
        resolved, then drives the real test body.  It reddens if that branch is
        changed back to ``pytest.skip``: a skip raises the DISTINCT
        ``pytest.skip.Exception``, caught below and turned into a failure.
        """
        module = sys.modules[__name__]
        monkeypatch.setattr(
            module,
            "check_prev_release_hooks",
            lambda repo_root, plugin_root: PrevReleaseHooksReport(
                STATUS_UNKNOWN, reason="simulated anomaly: hooks.json relocated"
            ),
        )
        built = _write_built_root(tmp_path, {"alpha.py": ""})
        try:
            TestLiveRepositoryAcceptance().test_live_repo_passes(
                live_prev_tag="v1.0", live_built_plugin=str(built)
            )
        except pytest.fail.Exception as exc:  # the branch we require
            assert "unknown" in str(exc).lower(), str(exc)
            assert "v1.0" in str(exc), str(exc)
        except pytest.skip.Exception as exc:
            pytest.fail(
                "test_live_repo_passes SKIPPED on a resolved tag + unknown guard; "
                f"it must FAIL instead: {exc}"
            )
        else:
            pytest.fail("test_live_repo_passes neither failed nor skipped")


# ─────────────────────────────────────────────────────────────────────────────
# live historical-incident record — pinned at ITS OWN tag, never at "newest"
# ─────────────────────────────────────────────────────────────────────────────


# #3213's incident: a hook was renamed away while the previous release still
# registered it.  That release is v0.5.2-rc3 (measured: its hooks.json names
# this basename; the newer v0.5.2-rc4 does not).
_LIVE_INCIDENT_TAG = "v0.5.2-rc3"
_LIVE_INCIDENT_REF = "engram-toolcall-repair.py"


def _refs_at_tag(repo_root: str, tag: str) -> tuple[set[str] | None, str]:
    """Refs in ``tag``'s hooks.json, or ``(None, reason)`` if unreadable.

    Names the tag EXPLICITLY, so a test can pin a specific PAST release rather
    than whatever tag happens to be the newest ancestor of HEAD.
    """
    raw, err = _prev_hooks_json(repo_root, tag)
    if raw is None:
        return None, err
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"{PREV_HOOKS_JSON_SRC} at {tag} is not valid JSON: {exc}"
    refs, reason = _validated_refs(data)
    return (refs, "") if refs is not None else (None, reason)


class TestLiveHistoricalIncident:
    def test_3213_incident_reference_is_pinned_at_its_own_release(self):
        """#3213 pinned at the release where it ACTUALLY lives.

        The generic newest-tag acceptance test asserts the CONTRACT on whatever
        tag is newest; requiring this one retired reference there forever would
        fail a legitimately green report as soon as a later release stops
        registering the renamed hook.  The incident record is stable — but only
        at its own tag — so it belongs here.
        """
        refs, reason = _refs_at_tag(PROJECT_ROOT, _LIVE_INCIDENT_TAG)
        if refs is None:
            pytest.skip(
                f"{_LIVE_INCIDENT_TAG} is not readable in this checkout: {reason}"
            )
        assert _LIVE_INCIDENT_REF in refs, (
            f"{_LIVE_INCIDENT_TAG}'s hooks.json no longer references "
            f"{_LIVE_INCIDENT_REF}; the #3213 record moved: {sorted(refs)}"
        )
