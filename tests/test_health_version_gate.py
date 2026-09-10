"""Tests for the health-version tag-diff gate (engram-alpha#2563, Gaps 2+3).

## What this gate protects

``health/version.py``'s ``(MAJOR, MINOR)`` must always describe how HEAD's L0
measurement set differs from the **last deployable version** — the highest
health version carried by any deployable tag (see "Deployable tag set"
below). Two independent checks, in order (Borges's design on #2563, amended
by his 20:01Z comment and Lei's ruling recorded there):

1. **WELL-FORMEDNESS** of the staged delta ``B = T vs S`` — pure version
   arithmetic, no hashes consulted:

   - ``MAJOR``: ``S == (T.major + 1, 0)``
   - ``MINOR``: ``S == (T.major, T.minor + 1)``
   - ``NONE``:  ``S == T``
   - ``ILLEGAL``: everything else (the complement) — skip-major, skip-minor,
     major-without-minor-reset, backwards, and any shape nobody anticipated.
     ILLEGAL denies WITHOUT ever computing a hash.

2. **ADEQUACY** ``B vs R``, where ``R`` is the required severity computed
   from hashes at HEAD vs at T:

   - ``B == R`` -> PASS
   - ``B <  R`` -> DENY ("legal bump, insufficient — set <exact value>")
   - ``B >  R`` -> DENY ("over-severity" — Lei's 2026-09-04 20:01Z ruling on
     #2563: an unnecessary MAJOR needlessly invalidates snapshot history from
     the previous major version and is UNRECOVERABLE once tagged, because
     the only "fix" is a backwards delta, which this same gate forbids. This
     is a hard DENY, not a warning — an earlier draft of the design called it
     a warning; that was superseded.)

**Gap 2 (the derived-layer hash) is built** (engram-alpha#2563 Gap 2):
``required_severity()`` takes TWO hash pairs — L0 (``health.measurements.
canonical_l0_hash``) and derived (``health.measurements.
canonical_derived_hash``, over ``aspects``/``metrics``/``CurveSpec``-incl-
``knots``/``SIGNAL_DEFS``) — and can now return ``"MAJOR"`` (L0 differs,
wins over a derived difference), ``"MINOR"`` (L0 same, derived differs), or
``"NONE"`` (both same). A derived-only change (a curve reshaped, an
importance reweighted, a metric reassigned to a different aspect, a
signal's arithmetic changed) is now visible to ``R`` and correctly requires
(and is satisfied by) a MINOR bump — before this gap landed it was
invisible and could only ever look like ``NONE``.

## Deployable tag set

``refs/tags/v*`` UNION ``refs/tags/deploy/*`` — ruled by Lei on 2026-09-05,
recorded verbatim on #2563 (Kepler's "Lei's rulings of 2026-09-05" comment,
2026-09-05T15:09:14Z, id 5552711060). Release tooling and changelog scanning
read only ``v*``, so a ``deploy/<date>-<label>`` tag is invisible to release
machinery by construction — but it IS something a seat can be upgraded to
(per Lei's same-day ruling on issue-comment id for the temporary deploy-tag
policy), so ``T`` must see it too.

Enumerated with ``git for-each-ref`` — never ``git describe``, which walks
ancestry from HEAD and misses tags cut on other branches (#2563 measured
``v0.5.0-rc3`` invisible from ``dev`` via ``git describe``).

## T, precisely

``T`` = the maximum ``(MAJOR, MINOR)`` over all deployable tags that carry
``src/engram/health/version.py``, read by regexing
``git show <tag>:src/engram/health/version.py`` for the module-level
``^MAJOR = (\\d+)$`` / ``^MINOR = (\\d+)$`` lines — never by importing the
historical module (a historical tree's import graph may not load cleanly
under the current environment). A tag that doesn't carry that file
contributes nothing to ``T``. But a tag that DOES carry the file, where the
``MAJOR``/``MINOR`` regexes don't both match, is a broken tag (a deployable
tag whose health version cannot be read) — this fails loudly, naming the tag
and the file, never silently skipped as if the file were absent. Likewise
any OTHER ``git show`` failure at that tag (bad ref, git error) — anything
other than the path being absent — fails loudly, naming the tag and stderr;
a git error is not "tag has no version.py".

If NO deployable tag carries ``version.py`` (today's real state — every
``v0.5.0-rc*`` tag predates ``health/``), the gate PASSES with a stated
reason: the message contains the literal substring
``"no tagged health version yet — gate inactive"``, so a green result here
is never mistaken for a checked one.

The L0 hash at ``T`` is read from the ``_PINNED_L0_HASH`` constant, and the
derived hash at ``T`` from the sibling ``_PINNED_DERIVED_HASH`` constant
(Gap 2), both in ``git show <T's tag>:tests/test_health_measurements.py``
(regex, not import) — the pin tests at that tag guarantee each constant
equals its computed hash at that commit (each is itself a gate), and tags
are cut from CI-green release PRs, so trusting the constants avoids
importing a historical tree. If a tag carries ``version.py`` but NOT the L0
pin constant, that is a broken tag (not an inactive gate) and this module
fails loudly, naming the tag; same for the derived pin constant.

A tie in T — two tags at the same max version with DIFFERENT pinned
hashes — is a corrupted tag set and also fails loudly, naming both tags;
never picked silently.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT))
sys.path.append(str(REPO_ROOT / "src" / "engram"))  # #2565: append, not insert(0)

VERSION_PATH = "src/engram/health/version.py"
PIN_TEST_PATH = "tests/test_health_measurements.py"

_MAJOR_RE = re.compile(r"^MAJOR = (\d+)$", re.MULTILINE)
_MINOR_RE = re.compile(r"^MINOR = (\d+)$", re.MULTILINE)
_PIN_RE = re.compile(r'_PINNED_L0_HASH\s*=\s*"([0-9a-fA-F]+)"')
_DERIVED_PIN_RE = re.compile(r'_PINNED_DERIVED_HASH\s*=\s*"([0-9a-fA-F]+)"')


# ---------------------------------------------------------------------------
# git plumbing — subprocess only, never shell=True, never a piped string.
# ---------------------------------------------------------------------------

def _git(repo_path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
    )


def deployable_tags(repo_path) -> list[str]:
    """The deployable tag set: refs/tags/v* UNION refs/tags/deploy/*.

    Uses ``git for-each-ref`` — never ``git describe`` (an ancestry walk from
    HEAD misses tags cut on other branches; #2563 measured this live with
    ``v0.5.0-rc3``, visible only from ``origin/main``).
    """
    result = _git(
        repo_path,
        [
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/tags/v*",
            "refs/tags/deploy/*",
        ],
    )
    result.check_returncode()
    return [line for line in result.stdout.splitlines() if line.strip()]


def _verify_tag_resolves(repo_path, tag: str) -> None:
    """Raise loudly if ``tag`` does not resolve to a commit -- a bad ref
    must never be silently treated as "path absent at this tag"."""
    result = _git(repo_path, ["rev-parse", "--verify", f"{tag}^{{commit}}"])
    if result.returncode != 0:
        raise RuntimeError(
            f"tag {tag!r} does not resolve to a commit -- bad ref, not a "
            f"missing-file case -- stderr: {result.stderr.strip()!r}"
        )


def _path_exists_at_tag(repo_path, tag: str, path: str) -> bool:
    """``git cat-file -e <tag>:<path>`` -- exit-code only, no stderr string
    matching. Exits non-zero both for a missing path AND for a bad ref, so
    callers must verify the ref resolves first (``_verify_tag_resolves``)."""
    result = _git(repo_path, ["cat-file", "-e", f"{tag}:{path}"])
    return result.returncode == 0


def _read_version_at_tag(repo_path, tag: str) -> tuple[int, int] | None:
    """Regex ``MAJOR``/``MINOR`` out of ``version.py`` at ``tag``. ``None``
    if the tag doesn't carry that file at all (contributes nothing to T).

    Existence is decided by ``git cat-file -e``'s exit code, never by
    matching stderr text (locale- and git-version-dependent). A bad ref is
    ruled out first via ``_verify_tag_resolves`` -- ``cat-file -e`` exits
    non-zero for both a missing path AND a bad ref, so the two must not be
    conflated. Once existence is confirmed, ``git show`` must succeed; any
    failure raises loudly, naming the tag and stderr.

    And if the file IS present but the ``MAJOR``/``MINOR`` regexes don't both
    match, that is a broken tag (a deployable tag whose health version cannot
    be read) -- raise loudly, never silently return ``None`` and let T
    resolve around it.
    """
    _verify_tag_resolves(repo_path, tag)
    if not _path_exists_at_tag(repo_path, tag, VERSION_PATH):
        return None
    result = _git(repo_path, ["show", f"{tag}:{VERSION_PATH}"])
    if result.returncode != 0:
        raise RuntimeError(
            f"git show failed for {tag!r}:{VERSION_PATH} after cat-file -e "
            f"confirmed the path exists -- stderr: {result.stderr.strip()!r}"
        )
    text = result.stdout
    major_m = _MAJOR_RE.search(text)
    minor_m = _MINOR_RE.search(text)
    if not major_m or not minor_m:
        raise RuntimeError(
            f"deployable tag {tag!r} carries {VERSION_PATH} but its MAJOR/"
            f"MINOR lines don't both match ``^MAJOR = (\\d+)$`` / "
            f"``^MINOR = (\\d+)$`` -- broken tag, not an inactive gate."
        )
    return int(major_m.group(1)), int(minor_m.group(1))


def _read_pinned_constant_at_tag(
    repo_path,
    tag: str,
    pattern: "re.Pattern[str]",
    constant_name: str,
    *,
    required: bool,
) -> str | None:
    """Shared plumbing for ``_read_pinned_hash_at_tag`` (L0) and
    ``_read_pinned_derived_hash_at_tag`` (Gap 2, derived) — regex
    ``constant_name`` out of the pin test at ``tag`` via ``pattern``.

    ``None`` if the pin test file isn't present at that tag at all. When
    the file IS present but the constant isn't: ``required=True`` (L0 —
    the constant has existed since before this gate did, so its absence
    from an otherwise-readable pin test is a broken tag) raises loudly;
    ``required=False`` (the derived pin, Gap 2 — a tag cut before Gap 2
    landed legitimately has no such constant, which is "Gap 2 wasn't
    active yet at this tag", not a broken tag) returns ``None`` instead.
    A genuine git error (bad ref, a ``git show`` failure after ``cat-file
    -e`` confirmed the path exists) raises regardless of ``required`` —
    that is never "constant absent", it's an operational failure.

    Same ``git cat-file -e`` existence check as ``_read_version_at_tag`` --
    exit-code only, a bad ref ruled out first via ``_verify_tag_resolves``."""
    _verify_tag_resolves(repo_path, tag)
    if not _path_exists_at_tag(repo_path, tag, PIN_TEST_PATH):
        return None
    result = _git(repo_path, ["show", f"{tag}:{PIN_TEST_PATH}"])
    if result.returncode != 0:
        raise RuntimeError(
            f"git show failed for {tag!r}:{PIN_TEST_PATH} after cat-file -e "
            f"confirmed the path exists -- stderr: {result.stderr.strip()!r}"
        )
    match = pattern.search(result.stdout)
    if not match:
        if required:
            raise RuntimeError(
                f"deployable tag {tag!r} carries {VERSION_PATH} but its "
                f"{PIN_TEST_PATH} has no {constant_name} constant — broken "
                f"tag, not an inactive gate."
            )
        return None
    return match.group(1)


def _read_pinned_hash_at_tag(repo_path, tag: str) -> str | None:
    """Regex ``_PINNED_L0_HASH`` out of the pin test at ``tag``. A tag
    whose pin test lacks this constant is a BROKEN tag (``required=True``)
    -- see ``_read_pinned_constant_at_tag`` for the full None/broken-tag
    semantics."""
    return _read_pinned_constant_at_tag(
        repo_path, tag, _PIN_RE, "_PINNED_L0_HASH", required=True
    )


def _read_pinned_derived_hash_at_tag(repo_path, tag: str) -> str | None:
    """Regex ``_PINNED_DERIVED_HASH`` out of the pin test at ``tag`` (Gap 2
    sibling of ``_read_pinned_hash_at_tag``). ``required=False``: a tag cut
    before Gap 2 landed legitimately has no such constant -- that is "Gap 2
    inactive relative to this T", not a broken tag, so it returns ``None``
    rather than raising. See ``_read_pinned_constant_at_tag`` for the full
    semantics and the L0/derived asymmetry."""
    return _read_pinned_constant_at_tag(
        repo_path, tag, _DERIVED_PIN_RE, "_PINNED_DERIVED_HASH", required=False
    )


def _read_tag_creation_info(repo_path, tag: str) -> tuple[str, str]:
    """Read ``(objecttype, taggerdate_unix)`` for ``tag`` via ``git
    for-each-ref`` (engram-alpha#2796, Lei's ruling 2026-09-10 13:17Z --
    exact command form: ``git for-each-ref --format='%(objecttype)
    %(taggerdate:unix) %(refname:short)'``). Never ``git log`` or the
    underlying commit's own date -- the tag object's tagger date is a
    DIFFERENT clock from the commit it points at (measured on this repo's
    real tags: ``v0.3.2``'s tagger date is 16 days after its commit date,
    which is exactly why the *tagger* date is the tag's creation time, not
    the commit date).

    ``objecttype`` is ``"tag"`` for an ANNOTATED tag (a real tag object
    exists, hence a tagger date) and ``"commit"`` for a LIGHTWEIGHT tag
    (the ref points straight at the commit -- no tag object, no tagger
    date at all; ``taggerdate_unix`` is then the empty string).

    Exact-ref lookup (``refs/tags/<tag>``), not a glob pattern --
    ``deployable_tags`` has already confirmed the ref exists, so an empty
    result here is a plumbing failure, not a legitimate "tag absent" case,
    and raises loudly rather than being read as such."""
    result = _git(
        repo_path,
        [
            "for-each-ref",
            "--format=%(objecttype) %(taggerdate:unix)",
            f"refs/tags/{tag}",
        ],
    )
    result.check_returncode()
    line = result.stdout.strip()
    if not line:
        raise RuntimeError(
            f"git for-each-ref returned nothing for refs/tags/{tag!r} -- "
            f"expected an existing ref (deployable_tags already enumerated "
            f"it as one)"
        )
    objecttype, _, taggerdate = line.partition(" ")
    return objecttype, taggerdate


def resolve_T(repo_path) -> tuple[tuple[int, int], str, str, str | None] | None:
    """Resolve T = (max (MAJOR, MINOR), the tag it came from, its pinned L0
    hash, its pinned derived hash [Gap 2]) over the deployable tag set.
    ``None`` if no deployable tag carries ``version.py`` at all — the
    caller must treat that as "gate inactive", never as a bug.

    The derived hash is ``None`` exactly when T's tag was cut before Gap 2
    landed (no ``_PINNED_DERIVED_HASH`` constant at that tag) — a bootstrap
    state, not a broken tag (``_read_pinned_derived_hash_at_tag``'s own
    ``required=False``). ``required_severity`` degrades to L0-only
    (MAJOR-or-NONE, same behavior as before Gap 2 existed) whenever T's
    derived hash is ``None`` — see that function's own docstring.

    Raises loudly (never silently picks) on a tie in T across tags with
    different pinned L0 hashes, different pinned NON-``None`` derived
    hashes, or on a tag that has version.py but no L0 pin constant (the
    derived pin constant is exempt from this raise — see above).

    Tie-break (engram-alpha#2796, Lei's ruling 2026-09-10 13:17Z --
    SUPERSEDES an earlier pin-preference tie-break tried on this same
    issue): when multiple tags survive the agreement checks above at the
    max version (so every non-``None`` derived hash among them, and every
    L0 hash among them, is already guaranteed identical), the tag CHOSEN
    TO REPORT is the one with the LATEST tag CREATION time -- its own
    ``%(taggerdate:unix)`` -- never lexical order, and never a preference
    for whichever tag happens to carry a derived pin. Lei's own words,
    ruling out both of those: "Lexical is arbitrary, don't use that. Can
    we get the tag creation time? Can we use the most recent one?" The
    tagger date is the tag OBJECT's own creation timestamp, a clock
    distinct from the commit it points at -- measured on this repo's real
    tags, ``v0.3.2``'s tagger date is 16 days after its commit date.

    This selection only ever runs when a genuine tie exists (more than one
    tag survives to this point) -- a single tag at the max version is
    trivially T, whether or not it happens to be a lightweight tag; there
    is nothing to disambiguate, so nothing about it is inspected. Once a
    tie genuinely exists, two states are refused loudly rather than
    silently resolved (Lei's ruling, via Kepler: "Both are operational
    defects in the tag set, not a bootstrap."):

    - a candidate is a LIGHTWEIGHT tag (no tag object, hence no tagger
      date to compare it by) -- the tie cannot be broken by creation time
      at all;
    - two or more candidates share an IDENTICAL tagger timestamp -- the
      tie cannot be broken by creation time either, since there is no
      unique latest.

    The raises above do NOT guarantee this choice is inert on the derived
    hash (Ariadne's colleague-review catch on this PR): they guarantee
    every candidate's L0 hash agrees, and that any two candidates who both
    carry a non-``None`` derived pin agree with EACH OTHER -- but a
    candidate with no pin at all (``None``) is excluded from that
    agreement check entirely, so a ``None`` candidate can tie against a
    pinned one without raising. Picking between them is exactly the
    difference that matters: it is what determines whether
    ``required_severity`` has a derived baseline to compare against at
    all. On today's live tag set the most-recent tag by tagger date
    happens to be the pinned one, so the choice is inert THERE -- but that
    is a fact about today's tags, not a guarantee this function enforces.
    Which is the actual argument for "most recent wins": it is the one
    rule among the ties that keeps pace with which tag is newest, so a
    freshly pin-carrying tag is picked the moment it exists rather than
    depending on luck.

    This tie-break is still the missing link Ariadne's colleague review
    diagnosed on #2796, restated for the corrected mechanism: before this
    fix, ``required_severity``'s own docstring described a self-clearing
    bootstrap ("a MINOR-worthy change is invisible until a new tag lands
    carrying the derived pin"), but nothing connected a new tag's ARRIVAL
    to WHICH tag this function selected -- a same-version tag set could
    accumulate a pin-carrying tag and this function would still never
    report it, because a fixed rule (lexical order, or a pin-preference
    that ignores recency) does not track which tag is actually the
    newest. Selecting by tag creation time closes that loop directly:
    whichever tag was cut MOST RECENTLY at a given version is always the
    one reported as T, so the bootstrap clears itself the moment any new
    tag is cut at that version -- with a derived pin or without one,
    since T is now simply "the current state at this version," not a
    guess about which tag looks more authoritative.
    """
    tags = deployable_tags(repo_path)
    versions: dict[str, tuple[int, int]] = {}
    for tag in tags:
        version = _read_version_at_tag(repo_path, tag)
        if version is not None:
            versions[tag] = version

    if not versions:
        return None

    max_version = max(versions.values())
    tags_at_max = sorted(tag for tag, v in versions.items() if v == max_version)

    hashes: dict[str, str] = {}
    derived_hashes: dict[str, str | None] = {}
    for tag in tags_at_max:
        pinned = _read_pinned_hash_at_tag(repo_path, tag)
        if pinned is None:
            raise RuntimeError(
                f"deployable tag {tag!r} carries {VERSION_PATH} (version "
                f"{max_version}) but has no {PIN_TEST_PATH} at all — broken "
                f"tag, not an inactive gate."
            )
        hashes[tag] = pinned
        derived_hashes[tag] = _read_pinned_derived_hash_at_tag(repo_path, tag)

    unique_hashes = set(hashes.values())
    if len(unique_hashes) > 1:
        raise RuntimeError(
            f"deployable tags at max version {max_version} disagree on "
            f"pinned L0 hash — corrupted tag set: "
            f"{sorted(hashes.items())}"
        )

    # This raise -- and the L0-agreement raise above it -- must keep seeing
    # EVERY tag at max version, pinned or not: both loop over the full,
    # unfiltered tags_at_max built above, never a subset pre-filtered to
    # pinned tags. Two pinned tags at max is a state this repo will
    # genuinely enter at the next tag cut, and filtering tags_at_max before
    # this point (the "obvious" but wrong way to implement the tie-break
    # below) silently drops this check -- and the broken-tag raise above --
    # for whatever gets filtered out (#2796, Ariadne's colleague-review
    # finding; see the two-armed mutation control in the falsifier suite).
    unique_derived = {v for v in derived_hashes.values() if v is not None}
    if len(unique_derived) > 1:
        raise RuntimeError(
            f"deployable tags at max version {max_version} disagree on "
            f"pinned derived hash — corrupted tag set: "
            f"{sorted(derived_hashes.items())}"
        )

    # Tie-break (see the docstring's "Tie-break" paragraph): most-recent
    # tag by CREATION time wins, never lexical order, never pin
    # preference. Only exercised when a genuine tie exists -- a single
    # candidate is trivially chosen, whether or not it is lightweight;
    # there is nothing to disambiguate.
    if len(tags_at_max) == 1:
        chosen_tag = tags_at_max[0]
    else:
        tagger_dates: dict[str, int] = {}
        for tag in tags_at_max:
            objecttype, taggerdate_str = _read_tag_creation_info(repo_path, tag)
            if objecttype != "tag":
                raise RuntimeError(
                    f"deployable tag {tag!r} is one of {len(tags_at_max)} "
                    f"tags tied at max version {max_version} but is a "
                    f"LIGHTWEIGHT tag (objecttype {objecttype!r}, no tagger "
                    f"date) — the tie cannot be broken by tag creation "
                    f"time; corrupted tag set, not a bootstrap: "
                    f"{sorted(tags_at_max)}"
                )
            tagger_dates[tag] = int(taggerdate_str)

        dates_seen: dict[int, list[str]] = {}
        for tag, date in tagger_dates.items():
            dates_seen.setdefault(date, []).append(tag)
        for date, tags_sharing_date in dates_seen.items():
            if len(tags_sharing_date) > 1:
                raise RuntimeError(
                    f"deployable tags at max version {max_version} share "
                    f"an identical tagger timestamp ({date}) — the tie "
                    f"cannot be broken by tag creation time; corrupted "
                    f"tag set: {sorted(tags_sharing_date)}"
                )

        chosen_tag = max(tags_at_max, key=lambda t: tagger_dates[t])

    return max_version, chosen_tag, hashes[chosen_tag], derived_hashes[chosen_tag]


# ---------------------------------------------------------------------------
# Pure version arithmetic — no git, no hashes. Unit-testable in isolation.
# ---------------------------------------------------------------------------

def classify_delta(t: tuple[int, int], s: tuple[int, int]) -> str:
    """Classify the staged delta T -> S. Pure arithmetic; ILLEGAL is the
    complement of the three well-formed shapes, not an enumeration — a shape
    nobody anticipated is denied, never silently accepted."""
    tmaj, tmin = t
    smaj, smin = s
    if (smaj, smin) == (tmaj, tmin):
        return "NONE"
    if (smaj, smin) == (tmaj + 1, 0):
        return "MAJOR"
    if (smaj, smin) == (tmaj, tmin + 1):
        return "MINOR"
    return "ILLEGAL"


def required_severity(
    l0_hash_head: str,
    l0_hash_at_t: str,
    derived_hash_head: str,
    derived_hash_at_t: str | None,
) -> str:
    """R, from hashes only (engram-alpha#2563, Gap 2). L0 wins: a
    MAJOR-worthy change (the measured L0 set itself changed) is reported as
    MAJOR regardless of whether the derived layer also changed — MAJOR
    already subsumes MINOR's snapshot-history guarantee, so a derived-layer
    delta alongside an L0 delta needs no separate signal. Only when L0 is
    unchanged does a derived-layer difference surface, as MINOR.

    ``derived_hash_at_t`` is ``None`` exactly when T's tag predates Gap 2
    (``resolve_T``'s own bootstrap state, not a broken tag) — in that case
    this degrades to L0-only (MAJOR-or-NONE, the exact pre-Gap-2 behavior),
    since there is no derived-layer baseline at T to compare HEAD against;
    a MINOR-worthy change is invisible until a new tag lands carrying the
    derived pin, same as the original Gap-3-only gate's own bootstrap for
    T itself (no deployable tag -> gate inactive)."""
    if l0_hash_head != l0_hash_at_t:
        return "MAJOR"
    if derived_hash_at_t is not None and derived_hash_head != derived_hash_at_t:
        return "MINOR"
    return "NONE"


def _name_t(message: str, t: tuple[int, int], tag: str | None) -> str:
    """Prefix ``message`` with ``T=<major>.<minor> (tag <name>)`` when
    ``tag`` is given -- so once the gate is ACTIVE (T resolved from a real
    deployable tag), the CI log line is visibly different from the inactive
    line. ``tag`` is ``None`` for the direct-``evaluate_gate`` unit falsifiers
    that pass synthetic ``t``/``s`` with no real tag behind them; those
    messages are left unprefixed."""
    if tag is None:
        return message
    tmaj, tmin = t
    return f"T={tmaj}.{tmin} (tag {tag}) — {message}"


def _illegal_verdict(t: tuple[int, int], s: tuple[int, int], tag: str | None = None) -> tuple[str, str]:
    """Build the DENY verdict for an ILLEGAL delta. Never consults hashes —
    callers must not call this after computing R; classify_delta's ILLEGAL
    result alone is sufficient and hashes are never needed."""
    tmaj, tmin = t
    smaj, smin = s
    legal_minor = f"{tmaj}.{tmin + 1}"
    legal_major = f"{tmaj + 1}.0"
    if (smaj, smin) < (tmaj, tmin):
        reason = "moves backwards"
    elif smaj == tmaj + 1 and smin != 0:
        reason = "bumps major without resetting minor to 0"
    elif smaj == tmaj and smin > tmin + 1:
        reason = "skips a minor version"
    elif smaj > tmaj + 1:
        reason = "skips a major version"
    else:
        reason = "does not match any legal delta shape"
    message = (
        f"illegal version delta: {tmaj}.{tmin} -> {smaj}.{smin} ({reason}). "
        f"Legal deltas from {tmaj}.{tmin} are {legal_minor} (MINOR), "
        f"{legal_major} (MAJOR), or no change."
    )
    return "DENY", _name_t(message, t, tag)


def verdict(
    b: str,
    r: str,
    t: tuple[int, int],
    tag: str | None = None,
    derived_available: bool = True,
) -> tuple[str, str]:
    """The ADEQUACY comparison B vs R, with the exact target version named
    in every DENY message (never just "bump the version"). ``t`` is needed
    (beyond the bare (b, r) pair) purely to render the concrete target
    number — see this module's docstring / handoff notes for why that
    widens the sketch signature from the issue's design comments.

    ``tag`` (the deployable tag T came from) is threaded through so that,
    once a real tag exists, every PASS/DENY message names ``T`` -- see
    ``_name_t``.

    ``derived_available`` (engram-alpha#2793) disambiguates the underlying
    THREE states ``required_severity`` can now be in (Ariadne's
    colleague-review framing, folded in from #2796: collapsing the middle
    one into the first is exactly what produced the #2794 detour) --

    (a) **compared and genuinely equal**: T carries a derived pin, HEAD's
        derived hash was compared against it, and they matched
        (``derived_available=True``, the default).
    (b) **no derived pin exists at T's tag**: ``required_severity`` degraded
        to L0-only because T's tag predates Gap 2 and carries no
        ``_PINNED_DERIVED_HASH`` (its L0 pin still exists -- only the
        derived pin is ever absent, per ``_read_pinned_derived_hash_at_tag``'s
        ``required=False``), so the derived hash was never compared at all
        (``derived_available=False``).
    (c) **compared against a pin, and it differs**: this is ``r ==
        "MINOR"``, not ``"NONE"`` -- it is already unambiguous (the MINOR
        message names the derived-layer difference directly) and needs no
        flag, since ``required_severity`` can only ever return ``"MINOR"``
        when a derived pin existed to compare against in the first place.

    So ``r == "NONE"`` alone conflates (a) and (b) -- both parameters
    together (``r``, ``derived_available``) recover all three states. This
    flag disambiguates ONLY the ``r == "NONE"``-reachable DENY messages
    below, which otherwise falsely claim "the derived-layer hash does not
    differ from the tagged commit" in case (b), when in fact it was never
    checked because there was nothing to check it against. It changes ONLY
    the phrasing of those messages in case (b) -- it never changes
    ``status``, and it never touches any PASS-path message or the case-(a)
    wording.

    Precondition: b must already be well-formed (NONE/MINOR/MAJOR) — an
    ILLEGAL b must be handled by _illegal_verdict before R is ever computed,
    never routed through here.
    """
    if b not in ("NONE", "MINOR", "MAJOR"):
        raise ValueError(f"verdict() called with a non-well-formed b: {b!r}")
    if r not in ("NONE", "MINOR", "MAJOR"):
        raise ValueError(f"verdict() called with an unsupported r: {r!r}")

    tmaj, tmin = t

    if b == r:
        status, message = "PASS", f"version delta matches required severity ({r})"
        return status, _name_t(message, t, tag)

    status = "DENY"

    if r == "MAJOR" and b in ("NONE", "MINOR"):
        # Insufficient: an L0-worthy change staged as NONE or MINOR.
        target = f"{tmaj + 1}.0"
        if b == "NONE":
            message = (
                f"legal bump, insufficient — this change requires MAJOR "
                f"(the L0 hash differs from the tagged commit). Set "
                f"MAJOR.MINOR to {target}."
            )
        else:  # b == "MINOR"
            message = (
                f"legal bump, insufficient — this change requires MAJOR "
                f"(the L0 hash differs from the tagged commit); MINOR alone "
                f"is not enough. Set MAJOR.MINOR to {target}."
            )
    elif r == "MINOR" and b == "NONE":
        # Insufficient (Gap 2, new cell): a derived-only change staged as
        # NONE.
        target = f"{tmaj}.{tmin + 1}"
        message = (
            f"legal bump, insufficient — this change requires MINOR (the "
            f"derived-layer hash differs from the tagged commit — a curve, "
            f"metric, aspect, or signal declaration changed — while the L0 "
            f"hash does not). Set MAJOR.MINOR to {target}."
        )
    elif b == "MINOR" and r == "NONE":
        # Bumped without cause. NOT the "over-severity, unrecoverable"
        # shape below — a MINOR bump is retro-applicable to every stored
        # L0 snapshot (health.signals._rate's own docstring), so an
        # unnecessary one costs nothing beyond the revert itself; it does
        # not invalidate snapshot history the way an unnecessary MAJOR
        # does. Gap 2 (derived-layer hash) makes this a genuine, checked
        # "no" -- pre-Gap-2 this branch could only ever say the gate
        # COULDN'T see a cause; now it has actually looked and found none.
        if derived_available:
            message = (
                f"version bumped without cause — neither the L0 hash nor the "
                f"derived-layer hash differs from the tagged commit. Revert "
                f"MAJOR.MINOR to {tmaj}.{tmin}. If this IS a derived-layer "
                f"change, say so on the PR rather than reverting."
            )
        else:
            # #2793: r == "NONE" here because required_severity degraded to
            # L0-only (T's tag predates Gap 2, no derived pin) -- the
            # derived hash was never compared, so it is false to claim it
            # "does not differ". Name the real reason instead.
            message = (
                f"version bumped without cause — the L0 hash does not "
                f"differ from the tagged commit, and the derived-layer hash "
                f"has no baseline to compare against yet (T's tag predates "
                f"Gap 2). Revert MAJOR.MINOR to {tmaj}.{tmin}. If this IS a "
                f"derived-layer change, say so on the PR rather than "
                f"reverting."
            )
    else:
        # b == "MAJOR", r in ("NONE", "MINOR") -- over-severity, the hard
        # DENY (Lei's 2026-09-04 20:01Z ruling on #2563): an unnecessary
        # MAJOR needlessly invalidates snapshot history from the previous
        # major version and is UNRECOVERABLE once tagged (the only
        # correction would be a backwards delta, which this same gate
        # forbids). Extended by Gap 2 to the r == "MINOR" case: staging
        # MAJOR when only a derived-layer change occurred is the same
        # needless invalidation, one severity level over.
        target = f"{tmaj}.{tmin}" if r == "NONE" else f"{tmaj}.{tmin + 1}"
        if r == "NONE" and derived_available:
            cause = (
                "no MAJOR- or MINOR-worthy change occurred (neither the L0 "
                "nor the derived-layer hash differs from the tagged commit)"
            )
        elif r == "NONE":
            # #2793: same conflation as the "bumped without cause" branch
            # above, reachable via the identical r == "NONE"-degraded-from-
            # unavailable path -- do not claim the derived hash "does not
            # differ" when it was never compared.
            cause = (
                "no MAJOR-worthy change occurred (the L0 hash does not "
                "differ from the tagged commit) — the derived-layer hash "
                "has no baseline to compare against yet (T's tag predates "
                "Gap 2)"
            )
        else:  # r == "MINOR" -- always derived_available=True, unambiguous
            cause = (
                "only a derived-layer (MINOR-worthy) change occurred — the L0 "
                "hash matches the tagged commit"
            )
        message = (
            f"over-severity — MAJOR staged but {cause}. This needlessly "
            f"invalidates snapshot history from the previous major version "
            f"and is UNRECOVERABLE once tagged (the only correction would "
            f"be a backwards delta, which this same gate forbids). Revert "
            f"MAJOR.MINOR to {target}."
        )

    return status, _name_t(message, t, tag)


def evaluate_gate(
    t,
    s,
    get_l0_hash_head,
    get_l0_hash_at_t,
    get_derived_hash_head,
    get_derived_hash_at_t,
    tag: str | None = None,
) -> tuple[str, str]:
    """The full two-step gate: WELL-FORMEDNESS first (never touches hashes
    on an ILLEGAL delta), then ADEQUACY. Every ``get_*`` arg is a zero-arg
    callable, so a falsifier test can pass a sentinel that raises if
    called, to prove ILLEGAL never reaches any of them. ``tag`` is the
    deployable tag T was resolved from (or ``None`` for the synthetic-
    ``t``/``s`` unit falsifiers) -- threaded through to name T in every
    produced message once the gate is active.

    ``get_derived_hash_at_t`` may return ``None`` (T's tag predates Gap 2)
    -- ``required_severity`` degrades to L0-only in that case; see its own
    docstring. That same ``None``-ness is captured once here (as
    ``derived_available``, #2793) and threaded into ``verdict`` so its
    ``r == "NONE"``-reachable DENY messages can distinguish "compared and
    genuinely equal" from "no derived baseline existed to compare against"
    -- see ``verdict``'s own docstring for the full three-state framing."""
    b = classify_delta(t, s)
    if b == "ILLEGAL":
        return _illegal_verdict(t, s, tag=tag)
    derived_hash_at_t = get_derived_hash_at_t()
    r = required_severity(
        get_l0_hash_head(),
        get_l0_hash_at_t(),
        get_derived_hash_head(),
        derived_hash_at_t,
    )
    return verdict(b, r, t, tag=tag, derived_available=derived_hash_at_t is not None)


INACTIVE_MESSAGE = "no tagged health version yet — gate inactive"


def _current_S() -> tuple[int, int]:
    """S at HEAD, imported (not regexed) — per #2563: 'S = (MAJOR, MINOR)
    from src.engram.health.version at HEAD (import it)'."""
    from health import version as health_version  # noqa: E402 (sys.path set at module top)

    return health_version.MAJOR, health_version.MINOR


def _l0_hash_at_head() -> str:
    from health import measurements as hem_l0  # noqa: E402

    return hem_l0.canonical_l0_hash()


def _derived_hash_at_head() -> str:
    """Gap 2 sibling of ``_l0_hash_at_head``."""
    from health import measurements as hem_l0  # noqa: E402

    return hem_l0.canonical_derived_hash()


def run_live_gate(repo_path=REPO_ROOT) -> tuple[str, str]:
    """The live gate against a real repo checkout: resolve T, and either
    report the gate inactive or run the full evaluate_gate against HEAD's
    real S, L0 hash, and derived hash. This is what both
    test_gate_on_this_repo and the CI step / manual verification run."""
    resolved = resolve_T(repo_path)
    if resolved is None:
        return "PASS", INACTIVE_MESSAGE
    t, tag, pinned_hash, pinned_derived_hash = resolved
    s = _current_S()
    return evaluate_gate(
        t,
        s,
        _l0_hash_at_head,
        lambda: pinned_hash,
        _derived_hash_at_head,
        lambda: pinned_derived_hash,
        tag=tag,
    )


# ---------------------------------------------------------------------------
# The live gate against THIS repo.
# ---------------------------------------------------------------------------

def test_gate_on_this_repo():
    """Resolve T from the real repo, compute S and the HEAD L0 hash, and
    assert PASS. When no deployable tag carries version.py yet (today's real
    state), assert the inactive message is produced instead."""
    status, message = run_live_gate(REPO_ROOT)
    resolved = resolve_T(REPO_ROOT)
    if resolved is None:
        assert status == "PASS"
        assert INACTIVE_MESSAGE in message
    else:
        assert status == "PASS", f"expected PASS, got {status}: {message}"


# ---------------------------------------------------------------------------
# Falsifier suite — a gate never seen to fail is not a gate (#2563).
# Scratch git repo in tmp_path; the real repo is never touched, no real tag
# is ever created.
# ---------------------------------------------------------------------------


def _write_version_py(repo: Path, major: int, minor: int) -> None:
    path = repo / "src" / "engram" / "health" / "version.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'"""fake health.version for the gate falsifier suite."""\n'
        f"MAJOR = {major}\n"
        f"MINOR = {minor}\n",
        encoding="utf-8",
    )


def _write_pin_test(
    repo: Path, pinned_hash: str, derived_hash: str | None = "c" * 64
) -> None:
    """Write the scratch pin test. ``derived_hash=None`` writes NO
    ``_PINNED_DERIVED_HASH`` constant at all -- simulates a tag cut before
    Gap 2 landed (``_read_pinned_derived_hash_at_tag``'s ``required=False``
    bootstrap case). The default (a fixed, distinct-from-HASH_A/HASH_B
    literal) means every EXISTING call site that only cares about L0
    behavior gets a real, present, non-varying derived pin for free --
    Gap 2 stays inert for those tests without them needing to know it
    exists."""
    path = repo / "tests" / "test_health_measurements.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    derived_line = f'_PINNED_DERIVED_HASH = "{derived_hash}"\n' if derived_hash is not None else ""
    path.write_text(
        f'"""fake pin test for the gate falsifier suite."""\n'
        f'_PINNED_L0_HASH = "{pinned_hash}"\n'
        f'{derived_line}',
        encoding="utf-8",
    )


def _git_run(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_tag_annotated(repo: Path, tag: str, *, committer_date: str) -> None:
    """Create an ANNOTATED tag (``git tag -a``) at HEAD with a CONTROLLED
    tagger date, via ``GIT_COMMITTER_DATE`` -- empirically verified (not
    assumed) as the env var that moves an annotated tag's own tagger date:
    a scratch-repo probe showed ``GIT_COMMITTER_DATE=2020-01-01T00:00:00``
    on ``git tag -a`` produces ``%(taggerdate:unix)`` == 1577854800 (the
    exact instant requested), while ``GIT_AUTHOR_DATE`` alone has NO
    effect on a tag's tagger date at all (a tag object has no author
    field -- only a tagger, populated the same way a commit's COMMITTER
    is, never its author). ``committer_date`` accepts any date string
    ``git`` itself parses (e.g. ``"2026-09-05T00:00:00"``)."""
    env = dict(os.environ)
    env["GIT_COMMITTER_DATE"] = committer_date
    subprocess.run(
        ["git", "-C", str(repo), "tag", "-a", tag, "-m", f"tag {tag}"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _init_scratch_repo(tmp_path: Path, major: int, minor: int, pinned_hash: str, tag: str) -> Path:
    """A scratch git repo with a fake version.py + pin test, committed and
    tagged at (major, minor) / pinned_hash."""
    repo = tmp_path / "scratch-repo"
    repo.mkdir()
    _git_run(repo, "init", "-q")
    _git_run(repo, "config", "user.email", "fairy@example.com")
    _git_run(repo, "config", "user.name", "gate-falsifier")
    _write_version_py(repo, major, minor)
    _write_pin_test(repo, pinned_hash)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", f"v{major}.{minor}")
    _git_run(repo, "tag", tag)
    return repo


HASH_A = "a" * 64
HASH_B = "b" * 64


def _raising_hash():
    raise AssertionError("hash function was called on an ILLEGAL delta — hashes must never be consulted")


# --- direct evaluate_gate() falsifiers (no git needed for these) ----------
#
# Shorthand for these unit falsifiers: `_l0(head, at_t)` / `_derived(head,
# at_t)` build the (get_head, get_at_t) callable pairs `evaluate_gate` needs,
# so each test states its intent as "L0: same/differs, derived: same/
# differs" rather than four positional lambdas.

def _l0(head: str, at_t: str):
    return (lambda: head), (lambda: at_t)


def _derived(head: str, at_t: str | None):
    return (lambda: head), (lambda: at_t)


def test_under_bump():
    l0 = _l0(HASH_B, HASH_A)  # L0 differs -> R = MAJOR
    derived = _derived(HASH_A, HASH_A)
    status, message = evaluate_gate((1, 0), (1, 0), *l0, *derived)
    assert status == "DENY"
    assert "2.0" in message


def test_wrong_new_version():
    l0 = _l0(HASH_B, HASH_A)  # L0 differs -> R = MAJOR
    derived = _derived(HASH_A, HASH_A)
    status, message = evaluate_gate((1, 0), (1, 1), *l0, *derived)
    assert status == "DENY"
    assert "2.0" in message
    assert "illegal" not in message.lower()


def test_bump_without_cause():
    """B = MINOR staged, but NEITHER hash differs from T -> R = NONE ->
    DENY. Gap 2 rewrite (Kepler's #2563 note, 2026-09-05T15:41:35Z item 3):
    before Gap 2, this DENY could only ever say the gate COULDN'T see a
    MINOR-worthy cause (it had no derived hash to check); now it has
    genuinely checked and found none, so the "gap 2"/"cannot see" hedge is
    RETIRED -- its presence would now be a stale, false claim, not a
    qualification. This is exactly the tripwire Kepler's note named: this
    test failing (because the hedge it used to assert is gone) is the
    intended signal that Gap 2 landed correctly."""
    l0 = _l0(HASH_A, HASH_A)
    derived = _derived(HASH_A, HASH_A)  # derived also matches -> R = NONE
    status, message = evaluate_gate((1, 0), (1, 1), *l0, *derived)
    assert status == "DENY"
    assert "1.0" in message
    assert "without cause" in message.lower()
    assert "gap 2" not in message.lower()
    assert "cannot see" not in message.lower()


def test_over_severity_is_a_hard_deny():
    l0 = _l0(HASH_A, HASH_A)
    derived = _derived(HASH_A, HASH_A)  # both match -> R = NONE, B = MAJOR
    status, message = evaluate_gate((1, 0), (2, 0), *l0, *derived)
    assert status == "DENY"
    msg_lower = message.lower()
    assert "snapshot" in msg_lower
    assert "unrecoverable" in msg_lower
    assert status != "WARN"


# --- verdict() derived_available message falsifiers (engram-alpha#2793) ---
# Direct verdict() calls, not evaluate_gate: these exercise the two
# r == "NONE"-reachable DENY branches under BOTH derived_available states,
# to prove (a) the new "unavailable" phrasing lands when it should, and
# (b) the ORIGINAL "compared and equal" phrasing is untouched -- a
# mutation-style control catching an accidental edit to the case-(a) text.

def test_bump_without_cause_message_unchanged_when_derived_genuinely_compared_and_equal():
    """Mutation-style control for #2793's fix: derived_available=True (the
    default, and the genuinely-both-compared-and-equal case) must produce
    EXACTLY the original wording -- if the fix accidentally changed this
    branch too, this test catches it."""
    status, message = verdict("MINOR", "NONE", (1, 0), derived_available=True)
    assert status == "DENY"
    assert (
        "version bumped without cause — neither the L0 hash nor the "
        "derived-layer hash differs from the tagged commit. Revert "
        "MAJOR.MINOR to 1.0. If this IS a derived-layer change, say so on "
        "the PR rather than reverting."
    ) in message


def test_bump_without_cause_message_names_unavailable_baseline_when_derived_pin_missing():
    """Fix for #2793: when required_severity degrades to L0-only because
    T's tag predates Gap 2 (no derived pin), R can still come out NONE --
    but NOT because the derived hash was compared and found equal. The old
    message asserted "neither...differs", which is false when the derived
    hash was never compared at all. derived_available=False must produce
    the accurate 'no baseline to compare against yet' phrasing instead, and
    must NOT repeat the false "differs" claim about the derived hash."""
    status, message = verdict("MINOR", "NONE", (1, 0), derived_available=False)
    assert status == "DENY"
    msg_lower = message.lower()
    assert "neither the l0 hash nor the derived-layer hash differs" not in msg_lower
    assert "no baseline to compare against" in msg_lower
    assert "predates gap 2" in msg_lower
    assert "1.0" in message  # still names the revert target


def test_over_severity_message_unchanged_when_derived_genuinely_compared_and_equal():
    """Mutation-style control mirroring the pair above, for the
    over-severity branch (#2793's diagnosis: this branch is reachable via
    the identical r == "NONE"-degraded-from-unavailable path, so it carries
    the same conflation defect and needed the same fix)."""
    status, message = verdict("MAJOR", "NONE", (1, 0), derived_available=True)
    assert status == "DENY"
    assert (
        "no MAJOR- or MINOR-worthy change occurred (neither the L0 nor the "
        "derived-layer hash differs from the tagged commit)"
    ) in message


def test_over_severity_message_names_unavailable_baseline_when_derived_pin_missing():
    """Fix for #2793, over-severity branch: derived_available=False must
    not claim the derived hash was compared and found equal here either."""
    status, message = verdict("MAJOR", "NONE", (1, 0), derived_available=False)
    assert status == "DENY"
    msg_lower = message.lower()
    assert "neither the l0 nor the derived-layer hash differs" not in msg_lower
    assert "no baseline to compare against" in msg_lower
    assert "predates gap 2" in msg_lower


# --- Gap 2 severity-cell falsifiers (engram-alpha#2563 unit spec item 4) --

def test_derived_only_change_no_bump_is_denied():
    """R = MINOR (derived differs, L0 same), B = NONE -> DENY, insufficient."""
    l0 = _l0(HASH_A, HASH_A)
    derived = _derived(HASH_B, HASH_A)
    status, message = evaluate_gate((1, 0), (1, 0), *l0, *derived)
    assert status == "DENY"
    assert "1.1" in message
    assert "minor" in message.lower()


def test_derived_only_change_with_minor_bump_passes():
    """R = MINOR, B = MINOR -> PASS. The cell Gap 2 exists to make
    reachable at all -- unreachable before this PR (R could never be
    MINOR)."""
    l0 = _l0(HASH_A, HASH_A)
    derived = _derived(HASH_B, HASH_A)
    status, message = evaluate_gate((1, 0), (1, 1), *l0, *derived)
    assert status == "PASS"


def test_derived_only_change_with_major_bump_is_over_severity():
    """R = MINOR, B = MAJOR -> DENY, over-severity -- staging MAJOR when
    only a derived-layer change occurred needlessly invalidates snapshot
    history one severity level early, same hard-DENY shape as the original
    R = NONE / B = MAJOR case."""
    l0 = _l0(HASH_A, HASH_A)
    derived = _derived(HASH_B, HASH_A)
    status, message = evaluate_gate((1, 0), (2, 0), *l0, *derived)
    assert status == "DENY"
    msg_lower = message.lower()
    assert "over-severity" in msg_lower
    assert "unrecoverable" in msg_lower
    assert "1.1" in message  # names the actually-sufficient MINOR target


def test_l0_change_with_minor_bump_is_insufficient():
    """R = MAJOR (L0 differs -- wins even though derived also differs), B =
    MINOR -> DENY, insufficient: MINOR alone is not enough when the L0 set
    itself changed."""
    l0 = _l0(HASH_B, HASH_A)
    derived = _derived(HASH_B, HASH_A)  # derived ALSO differs; L0 must win
    status, message = evaluate_gate((1, 0), (1, 1), *l0, *derived)
    assert status == "DENY"
    assert "2.0" in message
    assert "not enough" in message.lower()


def test_l0_and_derived_both_differ_major_bump_passes():
    """R = MAJOR (L0 wins), B = MAJOR -> PASS. Confirms L0-wins is not just
    a DENY-path rule -- a correctly-sized MAJOR bump passes even when the
    derived hash ALSO changed alongside the L0 change."""
    l0 = _l0(HASH_B, HASH_A)
    derived = _derived(HASH_B, HASH_A)
    status, message = evaluate_gate((1, 0), (2, 0), *l0, *derived)
    assert status == "PASS"


def test_mutation_control_reverting_to_l0_only_reddens_exactly_the_minor_cells():
    """The required mutation control (#2563 unit spec item 4): revert
    ``required_severity`` to its pre-Gap-2, L0-only form and show that
    doing so reddens EXACTLY the new MINOR-shaped cells above, and nothing
    else -- proving those tests actually exercise the derived hash rather
    than passing for an unrelated reason."""

    def pre_gap_2_required_severity(l0_head, l0_at_t, derived_head, derived_at_t):
        if l0_head != l0_at_t:
            return "MAJOR"
        return "NONE"  # the old ceiling: MINOR is never reachable

    l0_same = _l0(HASH_A, HASH_A)
    l0_diff = _l0(HASH_B, HASH_A)
    derived_same = _derived(HASH_A, HASH_A)
    derived_diff = _derived(HASH_B, HASH_A)

    def _b_under_mutation(t, s, l0, derived):
        b = classify_delta(t, s)
        r = pre_gap_2_required_severity(l0[0](), l0[1](), derived[0](), derived[1]())
        status, _ = verdict(b, r, t)
        return status

    # The cell that SHOULD flip from PASS (real R = MINOR) to DENY (mutant
    # R = NONE, since the mutant can never see a MINOR-worthy cause):
    assert _b_under_mutation((1, 0), (1, 1), l0_same, derived_diff) == "DENY", (
        "mutation control failed: reverting required_severity to L0-only "
        "did NOT redden the derived-only/MINOR-bump cell -- Gap 2's own "
        "tests above are not actually discriminating"
    )
    # Sanity: the same cell against the REAL required_severity is PASS
    # (already asserted by test_derived_only_change_with_minor_bump_passes,
    # re-confirmed here so the contrast is visible in one place).
    real_r = required_severity(HASH_A, HASH_A, HASH_B, HASH_A)
    assert real_r == "MINOR"
    real_status, _ = verdict(classify_delta((1, 0), (1, 1)), real_r, (1, 0))
    assert real_status == "PASS"

    # Cells the mutation must NOT change (pure-L0 behavior, unaffected by
    # the derived-layer regression): L0 AND derived both unchanged (real R
    # = NONE, same as the mutant's only non-MAJOR value), and L0 changed
    # (real R = MAJOR, L0-wins, same regardless of what the mutant can or
    # can't see about the derived layer).
    for t, s, l0, derived in [
        ((1, 0), (1, 0), l0_same, derived_same),  # NONE vs NONE either way
        ((1, 0), (2, 0), l0_diff, derived_diff),  # MAJOR vs MAJOR either way
    ]:
        b = classify_delta(t, s)
        real_r = required_severity(l0[0](), l0[1](), derived[0](), derived[1]())
        real_status, _ = verdict(b, real_r, t)
        mutant_status = _b_under_mutation(t, s, l0, derived)
        assert mutant_status == real_status, (
            f"mutation control changed a pure-L0 cell it should not have "
            f"touched: t={t} s={s} real={real_status} mutant={mutant_status}"
        )


@pytest.mark.parametrize(
    "t, s, expected_reason",
    [
        ((1, 0), (3, 0), "skips a major version"),  # skip-major
        ((1, 0), (1, 2), "skips a minor version"),  # skip-minor
        ((1, 3), (2, 3), "bumps major without resetting minor to 0"),  # no-minor-reset
        ((1, 1), (1, 0), "moves backwards"),  # backwards
    ],
    ids=["skip-major", "skip-minor", "no-minor-reset", "backwards"],
)
def test_illegal_delta_never_consults_hashes(t, s, expected_reason):
    status, message = evaluate_gate(
        t, s, _raising_hash, _raising_hash, _raising_hash, _raising_hash
    )
    assert status == "DENY"
    assert expected_reason in message.lower()


def test_illegal_delta_message_names_t_with_the_tag(tmp_path):
    """Deferred item 1 (#2563's 2026-09-05T15:41:35Z note): ILLEGAL
    messages thread T through ``_name_t`` (verified by direct invocation
    when it was written), but no test asserted it end-to-end against a
    REAL resolved tag until now. Scratch repo, one real tag, an ILLEGAL
    delta staged against it."""
    repo = _init_scratch_repo(tmp_path, 1, 0, HASH_A, "v1.0.0")
    resolved = resolve_T(repo)
    assert resolved is not None
    t, tag, pinned_hash, pinned_derived_hash = resolved

    status, message = evaluate_gate(
        t,
        (3, 0),  # skip-major -> ILLEGAL
        _raising_hash,
        _raising_hash,
        _raising_hash,
        _raising_hash,
        tag=tag,
    )
    assert status == "DENY"
    assert "T=" in message
    assert f"tag {tag}" in message


def test_passing_none():
    l0 = _l0(HASH_A, HASH_A)
    derived = _derived(HASH_A, HASH_A)
    status, message = evaluate_gate((1, 0), (1, 0), *l0, *derived)
    assert status == "PASS"


def test_passing_major():
    l0 = _l0(HASH_B, HASH_A)
    derived = _derived(HASH_A, HASH_A)
    status, message = evaluate_gate((1, 0), (2, 0), *l0, *derived)
    assert status == "PASS"


def test_passing_major_batching_no_second_bump():
    """Second PR in the same batch: T is still 1.0 (not yet re-tagged), the
    L0 hash still differs from T, S is already staged at 2.0 from the first
    PR in the batch — PASS again, no further bump required."""
    l0 = _l0(HASH_B, HASH_A)
    derived = _derived(HASH_A, HASH_A)
    status, message = evaluate_gate((1, 0), (2, 0), *l0, *derived)
    assert status == "PASS"


# --- resolve_T() falsifiers, against real scratch git repos ---------------

def test_inactive_when_no_tag_carries_version_py(tmp_path):
    repo = tmp_path / "scratch-no-version"
    repo.mkdir()
    _git_run(repo, "init", "-q")
    _git_run(repo, "config", "user.email", "fairy@example.com")
    _git_run(repo, "config", "user.name", "gate-falsifier")
    (repo / "README.md").write_text("no health here\n", encoding="utf-8")
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "initial")
    _git_run(repo, "tag", "v1.0.0")

    resolved = resolve_T(repo)
    assert resolved is None


def test_broken_tag_carries_version_but_no_pin_fails_loudly(tmp_path):
    repo = tmp_path / "scratch-broken-tag"
    repo.mkdir()
    _git_run(repo, "init", "-q")
    _git_run(repo, "config", "user.email", "fairy@example.com")
    _git_run(repo, "config", "user.name", "gate-falsifier")
    _write_version_py(repo, 1, 0)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "v1.0, no pin test at all")
    _git_run(repo, "tag", "v1.0.0")

    with pytest.raises(RuntimeError, match="v1.0.0"):
        resolve_T(repo)


def test_broken_tag_version_missing_minor_line_fails_loudly(tmp_path):
    """A tag carries version.py, MAJOR matches but MINOR is absent entirely
    -- a broken tag whose health version cannot be read. This must raise,
    never silently return None and let T resolve around it (the bug this
    round's fix addresses: the old code conflated "file absent" with "file
    present but unparseable")."""
    repo = tmp_path / "scratch-broken-no-minor"
    repo.mkdir()
    _git_run(repo, "init", "-q")
    _git_run(repo, "config", "user.email", "fairy@example.com")
    _git_run(repo, "config", "user.name", "gate-falsifier")

    version_path = repo / "src" / "engram" / "health" / "version.py"
    version_path.parent.mkdir(parents=True, exist_ok=True)
    version_path.write_text(
        '"""fake health.version, MINOR line missing entirely."""\nMAJOR = 1\n',
        encoding="utf-8",
    )
    _write_pin_test(repo, HASH_A)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "v1, no MINOR line")
    _git_run(repo, "tag", "v1.0.0")

    with pytest.raises(RuntimeError, match="v1.0.0"):
        resolve_T(repo)


def test_broken_tag_version_no_spaces_fails_loudly(tmp_path):
    """A tag carries version.py written as ``MAJOR=1`` (no spaces around the
    ``=``), which does not match the required ``^MAJOR = (\\d+)$`` shape --
    a broken tag, must raise (not silently return None)."""
    repo = tmp_path / "scratch-broken-no-spaces"
    repo.mkdir()
    _git_run(repo, "init", "-q")
    _git_run(repo, "config", "user.email", "fairy@example.com")
    _git_run(repo, "config", "user.name", "gate-falsifier")

    version_path = repo / "src" / "engram" / "health" / "version.py"
    version_path.parent.mkdir(parents=True, exist_ok=True)
    version_path.write_text(
        '"""fake health.version, no-spaces assignment shape."""\nMAJOR=1\nMINOR=0\n',
        encoding="utf-8",
    )
    _write_pin_test(repo, HASH_A)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "v1, no-spaces MAJOR/MINOR")
    _git_run(repo, "tag", "v1.0.0")

    with pytest.raises(RuntimeError, match="v1.0.0"):
        resolve_T(repo)


def test_deployable_namespace_deploy_tag_wins_over_v_tag(tmp_path):
    repo = tmp_path / "scratch-deploy-namespace"
    repo.mkdir()
    _git_run(repo, "init", "-q")
    _git_run(repo, "config", "user.email", "fairy@example.com")
    _git_run(repo, "config", "user.name", "gate-falsifier")

    _write_version_py(repo, 1, 0)
    _write_pin_test(repo, HASH_A)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "v1.0")
    _git_run(repo, "tag", "v1.0.0")

    _write_version_py(repo, 2, 0)
    _write_pin_test(repo, HASH_B)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "v2.0 deploy tag")
    _git_run(repo, "tag", "deploy/2026-09-05-x")

    # an unrelated tag namespace must never contribute to T
    _git_run(repo, "tag", "foo/bar")

    resolved = resolve_T(repo)
    assert resolved is not None
    version, tag, pinned_hash, pinned_derived_hash = resolved
    assert version == (2, 0)
    assert tag == "deploy/2026-09-05-x"
    assert pinned_hash == HASH_B
    assert pinned_derived_hash == "c" * 64  # _write_pin_test's default


def test_describe_blindness_control_tag_on_unreachable_branch(tmp_path):
    """T must resolve even when the max-version tag sits on a commit NOT
    reachable from HEAD — `git describe` would miss it; `for-each-ref` must
    not (#2563's measured v0.5.0-rc3-from-dev blind spot)."""
    repo = tmp_path / "scratch-describe-blindness"
    repo.mkdir()
    _git_run(repo, "init", "-q")
    _git_run(repo, "config", "user.email", "fairy@example.com")
    _git_run(repo, "config", "user.name", "gate-falsifier")

    _write_version_py(repo, 1, 0)
    _write_pin_test(repo, HASH_A)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "v1.0 on main line")
    _git_run(repo, "tag", "v1.0.0")

    _git_run(repo, "checkout", "-q", "-b", "side-branch")
    _write_version_py(repo, 2, 0)
    _write_pin_test(repo, HASH_B)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "v2.0 on side branch only")
    _git_run(repo, "tag", "v2.0.0")

    # Go back to the main line, where HEAD does NOT contain the v2.0.0 commit.
    # We're currently ON side-branch; switch back to whatever the first
    # commit's branch was (git init default branch name varies by config).
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    main_line = next(b for b in branches if b != "side-branch")
    _git_run(repo, "checkout", "-q", main_line)

    # Sanity: v2.0.0's commit is genuinely unreachable from HEAD now.
    merge_base = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", "v2.0.0", "HEAD"],
        capture_output=True,
        text=True,
    )
    assert merge_base.returncode != 0, "test setup bug: v2.0.0 must NOT be an ancestor of HEAD"

    resolved = resolve_T(repo)
    assert resolved is not None
    version, tag, pinned_hash, pinned_derived_hash = resolved
    assert version == (2, 0)
    assert tag == "v2.0.0"
    assert pinned_hash == HASH_B
    assert pinned_derived_hash == "c" * 64  # _write_pin_test's default


def test_tie_in_T_with_different_pinned_hashes_fails_loudly(tmp_path):
    repo = tmp_path / "scratch-tie"
    repo.mkdir()
    _git_run(repo, "init", "-q")
    _git_run(repo, "config", "user.email", "fairy@example.com")
    _git_run(repo, "config", "user.name", "gate-falsifier")

    _write_version_py(repo, 1, 0)
    _write_pin_test(repo, HASH_A)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "first v1.0")
    _git_run(repo, "tag", "v1.0.0")

    # A second commit, same (1, 0) version, but a DIFFERENT pinned hash —
    # a corrupted tag set (two tags claim the same version with conflicting
    # measured hashes).
    (repo / "unrelated.txt").write_text("noise\n", encoding="utf-8")
    _write_pin_test(repo, HASH_B)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "second v1.0, different hash")
    _git_run(repo, "tag", "v1.0.1-same-version")

    with pytest.raises(RuntimeError, match="corrupted tag set"):
        resolve_T(repo)


def test_tie_in_T_with_different_pinned_derived_hashes_fails_loudly(tmp_path):
    """Gap 2 sibling of the L0 tie test above. Same version, same L0 hash
    (so the L0 uniqueness check upstream doesn't fire first and mask this
    one), but two tags disagree on their pinned DERIVED hash -- also a
    corrupted tag set, and also must raise loudly rather than silently
    picking one (reviewer-fairy suggestion on PR #2725, round 1: this path
    was exercised in code by hand during review but had no direct test)."""
    repo = tmp_path / "scratch-derived-tie"
    repo.mkdir()
    _git_run(repo, "init", "-q")
    _git_run(repo, "config", "user.email", "fairy@example.com")
    _git_run(repo, "config", "user.name", "gate-falsifier")

    _write_version_py(repo, 1, 0)
    _write_pin_test(repo, HASH_A, derived_hash=HASH_A)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "first v1.0")
    _git_run(repo, "tag", "v1.0.0")

    # A second commit, same (1, 0) version, SAME L0 hash (HASH_A), but a
    # DIFFERENT derived hash -- corrupted at the derived layer specifically.
    (repo / "unrelated.txt").write_text("noise\n", encoding="utf-8")
    _write_pin_test(repo, HASH_A, derived_hash=HASH_B)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "second v1.0, same L0 hash, different derived hash")
    _git_run(repo, "tag", "v1.0.1-same-version")

    with pytest.raises(RuntimeError, match="pinned derived hash"):
        resolve_T(repo)


# --- resolve_T() tie-break falsifiers (engram-alpha#2796) -----------------
#
# Lei's ruling, 2026-09-10 13:17Z (relayed by Kepler on the issue thread):
# the tie-break is by TAG CREATION TIME (tagger date), most-recent wins --
# never lexical, and never a preference for whichever tag carries a
# derived pin. This supersedes an earlier round on this same issue that
# implemented (and tested) a pin-preference tie-break; that cluster of
# tests is replaced wholesale below, not reworded in place, because the
# selection RULE itself changed, not just its wording.

def test_tie_break_prefers_latest_tagger_date_over_lexical_order(tmp_path):
    """Falsifier for #2796 (tagger-date mechanism): among tags tied at max
    version, resolve_T must pick the tag with the LATEST tag CREATION
    time (its own ``%(taggerdate:unix)``) -- never the lexically-first (or
    -last) tag, and never a preference for whichever tag happens to carry
    a derived pin.

    Tag NAMES are deliberately disconnected from their tagger dates so a
    lexical pick and a time pick DISAGREE: the tag with the OLDER tagger
    date is named to sort FIRST lexically -- exactly what
    ``sorted(tags_at_max)[0]`` (the OLD, reverted behavior; see the
    mutation control below) would wrongly choose -- while the tag with the
    NEWER tagger date is named to sort LAST. A fixture where the
    temporally-newest tag also happens to sort first (or last) lexically
    can't tell a tagger-date implementation from a lexical-sort one --
    exactly the 'passes for the wrong reason' trap Ariadne's #2796
    colleague review named for the pin-preference falsifier this test
    replaces."""
    repo = tmp_path / "scratch-tiebreak-taggerdate"
    repo.mkdir()
    _git_run(repo, "init", "-q")
    _git_run(repo, "config", "user.email", "fairy@example.com")
    _git_run(repo, "config", "user.name", "gate-falsifier")

    # First commit/tag: sorts lexically FIRST ("aaa..."), but tagged with
    # the OLDER tagger date -- what a lexical-first pick would (wrongly)
    # choose.
    _write_version_py(repo, 1, 0)
    _write_pin_test(repo, HASH_A)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "v1.0, tagged with the OLDER tagger date")
    _git_tag_annotated(
        repo, "deploy/aaa-older-by-tagger-date", committer_date="2020-01-01T00:00:00"
    )

    # Second commit: SAME version, SAME L0 + derived hash (so the
    # agreement checks upstream don't fire), sorts lexically LAST
    # ("zzz..."), but tagged with the NEWER tagger date -- the tag
    # resolve_T must actually choose.
    (repo / "unrelated.txt").write_text("noise\n", encoding="utf-8")
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "v1.0, tagged with the NEWER tagger date")
    _git_tag_annotated(
        repo, "deploy/zzz-newer-by-tagger-date", committer_date="2026-01-01T00:00:00"
    )

    tags_at_max = deployable_tags(repo)
    assert sorted(tags_at_max)[0] == "deploy/aaa-older-by-tagger-date", (
        "test setup bug: the lexically-first tag must be the OLDER one by "
        "tagger date for this fixture to disagree with a lexical pick"
    )

    resolved = resolve_T(repo)
    assert resolved is not None
    version, tag, pinned_hash, pinned_derived_hash = resolved
    assert version == (1, 0)
    assert tag == "deploy/zzz-newer-by-tagger-date", (
        f"tie-break picked {tag!r} -- expected the tag with the LATEST "
        f"tagger date, not the lexically-first one"
    )
    assert pinned_hash == HASH_A
    assert pinned_derived_hash == "c" * 64  # _write_pin_test's default


def test_mutation_control_reverting_tiebreak_to_lexical_reddens_the_tagger_date_case(tmp_path):
    """Mutation control (#2796's own required falsifier convention): revert
    the tie-break to the OLD unconditional ``sorted(tags_at_max)[0]``
    behavior and confirm it would have picked the WRONG (older-by-tagger-
    date) tag on the fixture above -- proving the falsifier actually
    discriminates the tagger-date fix, not something that was already
    true. This is a read of ``tags_at_max`` computed the same way
    ``resolve_T`` computes it (not a monkeypatch of the real function),
    contrasted with what the real, fixed ``resolve_T`` returns on the
    identical repo -- same shape as the pin-preference mutation control
    this test replaces, retargeted at the corrected mechanism."""
    repo = tmp_path / "scratch-tiebreak-mutation-lexical"
    repo.mkdir()
    _git_run(repo, "init", "-q")
    _git_run(repo, "config", "user.email", "fairy@example.com")
    _git_run(repo, "config", "user.name", "gate-falsifier")

    _write_version_py(repo, 1, 0)
    _write_pin_test(repo, HASH_A)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "v1.0, tagged with the OLDER tagger date")
    _git_tag_annotated(
        repo, "deploy/aaa-older-by-tagger-date", committer_date="2020-01-01T00:00:00"
    )

    (repo / "unrelated.txt").write_text("noise\n", encoding="utf-8")
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "v1.0, tagged with the NEWER tagger date")
    _git_tag_annotated(
        repo, "deploy/zzz-newer-by-tagger-date", committer_date="2026-01-01T00:00:00"
    )

    tags_at_max = deployable_tags(repo)  # both tags carry version.py at v1.0
    old_tie_break_choice = sorted(tags_at_max)[0]
    assert old_tie_break_choice == "deploy/aaa-older-by-tagger-date", (
        "test setup bug: the OLD (lexical) tie-break must pick the "
        "older-by-tagger-date tag here for this to be a discriminating "
        "fixture"
    )

    resolved = resolve_T(repo)
    assert resolved is not None
    _, real_chosen_tag, _, _ = resolved
    assert real_chosen_tag == "deploy/zzz-newer-by-tagger-date"

    assert old_tie_break_choice != real_chosen_tag, (
        "mutation control failed: the OLD (lexical) tie-break and the "
        "real, fixed resolve_T agree on this fixture -- it is not "
        "discriminating the fix"
    )


def test_lightweight_tag_among_candidates_fails_loudly(tmp_path):
    """Refusal test 1/2 for #2796's tagger-date tie-break: a LIGHTWEIGHT
    tag (created via plain ``git tag``, no ``-a``) has no tag object and
    hence no tagger date at all. If it is one of several tags tied at max
    version, the tie cannot be broken by tag creation time -- resolve_T
    must raise loudly rather than silently pick (Lei's ruling, via Kepler:
    "Both are operational defects in the tag set, not a bootstrap."),
    never crash unexpectedly or silently fall back to some other order."""
    repo = tmp_path / "scratch-tiebreak-lightweight"
    repo.mkdir()
    _git_run(repo, "init", "-q")
    _git_run(repo, "config", "user.email", "fairy@example.com")
    _git_run(repo, "config", "user.name", "gate-falsifier")

    # First tag: properly annotated.
    _write_version_py(repo, 1, 0)
    _write_pin_test(repo, HASH_A)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "v1.0, annotated tag")
    _git_tag_annotated(repo, "deploy/annotated-tag", committer_date="2020-01-01T00:00:00")

    # Second tag: SAME version, SAME L0 + derived hash (so the agreement
    # checks upstream don't fire) -- but LIGHTWEIGHT (plain `git tag`, no
    # `-a`), so it has no tagger date to break the tie by.
    (repo / "unrelated.txt").write_text("noise\n", encoding="utf-8")
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "v1.0, lightweight tag")
    _git_run(repo, "tag", "deploy/lightweight-tag")

    with pytest.raises(RuntimeError, match="LIGHTWEIGHT"):
        resolve_T(repo)


def test_duplicate_tagger_timestamps_among_candidates_fails_loudly(tmp_path):
    """Refusal test 2/2 for #2796's tagger-date tie-break: two candidates
    at max version with an IDENTICAL tagger timestamp cannot be
    disambiguated by tag creation time either -- there is no unique
    latest. resolve_T must raise loudly rather than silently picking
    whichever one happens to come first in some incidental order."""
    repo = tmp_path / "scratch-tiebreak-duplicate-timestamp"
    repo.mkdir()
    _git_run(repo, "init", "-q")
    _git_run(repo, "config", "user.email", "fairy@example.com")
    _git_run(repo, "config", "user.name", "gate-falsifier")

    _write_version_py(repo, 1, 0)
    _write_pin_test(repo, HASH_A)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "v1.0, first tag")
    _git_tag_annotated(repo, "deploy/tag-one", committer_date="2020-01-01T00:00:00")

    # SAME version, SAME hashes, SAME tagger timestamp as tag-one --
    # exactly the "corrupted tag set" state the raise exists to name.
    (repo / "unrelated.txt").write_text("noise\n", encoding="utf-8")
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "v1.0, second tag, identical tagger date")
    _git_tag_annotated(repo, "deploy/tag-two", committer_date="2020-01-01T00:00:00")

    with pytest.raises(RuntimeError, match="identical tagger timestamp"):
        resolve_T(repo)


def _build_three_tag_broken_guard_fixture(tmp_path, name: str) -> Path:
    """Shared fixture for the test below (Ariadne's colleague-review repro
    on #2796, generalized past the pin-preference mechanism it was
    originally built to catch): three tags at the same max version --

    - ``deploy/aaa-unpinned``: pinned L0, NO derived pin.
    - ``deploy/bbb-pinned``:   pinned L0, WITH a derived pin.
    - ``deploy/ccc-broken``:  version.py present, but the pin test file is
      REMOVED entirely (no L0 pin at all) -- genuinely broken.

    All three tags are LIGHTWEIGHT (plain ``git tag``) here deliberately:
    the property this fixture exercises (below) is that the broken-tag
    guard fires from INSIDE the loop that populates
    ``hashes``/``derived_hashes``, before resolve_T ever reaches tie-break
    selection -- so it is unaffected by whichever selection rule the
    tie-break itself uses, tagger-date or otherwise. The pin/no-pin split
    on aaa/bbb no longer determines anything about selection (#2796's
    tie-break is now by tag creation time, not pin presence); it is kept
    as fixture data for continuity with the original repro. Only ``ccc``'s
    brokenness is load-bearing to the assertion below."""
    repo = tmp_path / name
    repo.mkdir()
    _git_run(repo, "init", "-q")
    _git_run(repo, "config", "user.email", "fairy@example.com")
    _git_run(repo, "config", "user.name", "gate-falsifier")

    _write_version_py(repo, 1, 0)
    _write_pin_test(repo, HASH_A, derived_hash=None)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "aaa: v1.0, no derived pin")
    _git_run(repo, "tag", "deploy/aaa-unpinned")

    (repo / "unrelated-1.txt").write_text("noise\n", encoding="utf-8")
    _write_pin_test(repo, HASH_A, derived_hash=HASH_B)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "bbb: v1.0, with a derived pin")
    _git_run(repo, "tag", "deploy/bbb-pinned")

    (repo / "unrelated-2.txt").write_text("noise\n", encoding="utf-8")
    _git_run(repo, "rm", "-q", PIN_TEST_PATH)
    _git_run(repo, "commit", "-q", "-m", "ccc: v1.0, pin test removed entirely")
    _git_run(repo, "tag", "deploy/ccc-broken")

    return repo


def test_tie_break_does_not_mask_a_genuinely_broken_tag_at_max_version(tmp_path):
    """Ariadne's colleague-review finding on #2796, restated generically
    (orthogonal to which selection rule the tie-break uses -- see
    ``resolve_T``'s own docstring, "This raise -- and the L0-agreement
    raise above it -- must keep seeing EVERY tag at max version"): the
    broken-tag / L0-agreement / derived-agreement guards must run over the
    FULL, unfiltered ``tags_at_max`` before any candidate is filtered out
    for tie-break purposes. A tie-break implementation that filters
    candidates down BEFORE those guards run -- the 'obvious' but wrong
    shape, first caught here for a pin-preference filter -- silently drops
    those guards for whatever gets filtered out. The broken-tag raise must
    still fire even though other, valid candidates exist at the same max
    version.

    NOTE on the tagger-date mechanism's analogous trap (an annotated-tag-
    only filter applied before these guards): this fixture does NOT
    demonstrate coverage of that mutant -- all three of its tags are
    lightweight, so such a filter empties the candidate set here and the
    failure surfaces as an uninformative ``max() iterable argument is
    empty`` rather than this test's own ``match="deploy/ccc-broken"``.
    That mutant IS caught cleanly, just by a different test:
    ``test_lightweight_tag_among_candidates_fails_loudly``, whose fixture
    mixes one lightweight and one annotated candidate and gets the
    informative "DID NOT RAISE" failure. (Verified by reviewer-fairy round
    1 on #2796, hand-built mutation control.)"""
    repo = _build_three_tag_broken_guard_fixture(tmp_path, "scratch-tiebreak-broken-guard")

    with pytest.raises(RuntimeError, match="deploy/ccc-broken"):
        resolve_T(repo)


def test_full_history_scratch_repo_major_pass_and_under_bump(tmp_path):
    """A realistic history in one scratch repo (per this spec's own note:
    a tag's contents can't be rewritten after the fact, so a "changed pin at
    the same tag" scenario is built as two real commits instead):

    commit 1 (tagged v1.0.0): version 1.0, pin HASH_A.
    commit 2 (no tag yet):    version 1.0 unchanged, pin HASH_B (an L0-hash-
                               changing change landed, staged version NOT
                               bumped yet) -> under-bump DENY.
    commit 3 (no tag yet):    version bumped to 2.0, pin still HASH_B ->
                               MAJOR/PASS.
    """
    repo = tmp_path / "scratch-full-history"
    repo.mkdir()
    _git_run(repo, "init", "-q")
    _git_run(repo, "config", "user.email", "fairy@example.com")
    _git_run(repo, "config", "user.name", "gate-falsifier")

    _write_version_py(repo, 1, 0)
    _write_pin_test(repo, HASH_A)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "v1.0.0")
    _git_run(repo, "tag", "v1.0.0")

    resolved = resolve_T(repo)
    assert resolved == ((1, 0), "v1.0.0", HASH_A, "c" * 64)
    t, tag, pinned_hash_at_t, pinned_derived_hash_at_t = resolved

    # Commit 2: L0 hash changed at HEAD, version NOT bumped -> under-bump.
    # Derived hash held constant throughout ("c" * 64, _write_pin_test's
    # default both times) — this scenario is pure-L0, same as before Gap 2.
    _write_pin_test(repo, HASH_B)  # simulates the NEW measured hash at HEAD
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "L0 measurement changed, version not bumped yet")

    s_unbumped = (1, 0)
    status, message = evaluate_gate(
        t,
        s_unbumped,
        lambda: HASH_B,
        lambda: pinned_hash_at_t,
        lambda: "c" * 64,
        lambda: pinned_derived_hash_at_t,
        tag=tag,
    )
    assert status == "DENY"
    assert "2.0" in message
    assert f"tag {tag}" in message

    # Commit 3: version bumped to 2.0 to match -> PASS.
    _write_version_py(repo, 2, 0)
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "bump to 2.0 to match the L0 change")

    s_bumped = (2, 0)
    status, message = evaluate_gate(
        t,
        s_bumped,
        lambda: HASH_B,
        lambda: pinned_hash_at_t,
        lambda: "c" * 64,
        lambda: pinned_derived_hash_at_t,
        tag=tag,
    )
    assert status == "PASS"
    assert f"tag {tag}" in message
    assert "T=1.0" in message
