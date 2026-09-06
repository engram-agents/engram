"""Tests for the health-version tag-diff gate (engram-alpha#2563, Gap 3).

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

**Gap 2 (the derived-layer hash) is NOT built in this PR** — so
``required_severity()`` can only ever return ``"MAJOR"`` or ``"NONE"``, never
``"MINOR"``. ``TODO(#2563 gap 2)``: once ``health.measurements`` grows a
``canonical_derived_hash()`` (or equivalent) over curve specs / metric
bindings / importances, wire it into ``required_severity()`` so a
derived-only change can require (and be satisfied by) a MINOR bump. Until
then a MINOR-only change is invisible to ``R`` and can only ever look like
``NONE`` from this gate's point of view — do not fake a MINOR requirement
path here.

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

The L0 hash at ``T`` is read from the ``_PINNED_L0_HASH`` constant in
``git show <T's tag>:tests/test_health_measurements.py`` (regex, not
import) — the pin test at that tag guarantees the constant equals the
computed hash at that commit (it is itself a gate), and tags are cut from
CI-green release PRs, so trusting the constant avoids importing a
historical tree. If a tag carries ``version.py`` but NOT that pin constant,
that is a broken tag (not an inactive gate) and this module fails loudly,
naming the tag.

A tie in T — two tags at the same max version with DIFFERENT pinned
hashes — is a corrupted tag set and also fails loudly, naming both tags;
never picked silently.
"""

from __future__ import annotations

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


def _read_pinned_hash_at_tag(repo_path, tag: str) -> str | None:
    """Regex ``_PINNED_L0_HASH`` out of the pin test at ``tag``. ``None`` if
    the pin test file isn't present at that tag at all (distinct from
    "present but missing the constant", which is the broken-tag case
    resolve_T raises loudly on).

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
    match = _PIN_RE.search(result.stdout)
    if not match:
        raise RuntimeError(
            f"deployable tag {tag!r} carries {VERSION_PATH} but its "
            f"{PIN_TEST_PATH} has no _PINNED_L0_HASH constant — broken tag, "
            f"not an inactive gate."
        )
    return match.group(1)


def resolve_T(repo_path) -> tuple[tuple[int, int], str, str] | None:
    """Resolve T = (max (MAJOR, MINOR), the tag it came from, its pinned L0
    hash) over the deployable tag set. ``None`` if no deployable tag carries
    ``version.py`` at all — the caller must treat that as "gate inactive",
    never as a bug.

    Raises loudly (never silently picks) on a tie in T across tags with
    different pinned hashes, or on a tag that has version.py but no pin
    constant.
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
    for tag in tags_at_max:
        pinned = _read_pinned_hash_at_tag(repo_path, tag)
        if pinned is None:
            raise RuntimeError(
                f"deployable tag {tag!r} carries {VERSION_PATH} (version "
                f"{max_version}) but has no {PIN_TEST_PATH} at all — broken "
                f"tag, not an inactive gate."
            )
        hashes[tag] = pinned

    unique_hashes = set(hashes.values())
    if len(unique_hashes) > 1:
        raise RuntimeError(
            f"deployable tags at max version {max_version} disagree on "
            f"pinned L0 hash — corrupted tag set: "
            f"{sorted(hashes.items())}"
        )

    chosen_tag = tags_at_max[0]
    return max_version, chosen_tag, hashes[chosen_tag]


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


def required_severity(l0_hash_head: str, l0_hash_at_t: str) -> str:
    """R, from hashes only. Gap 2 (derived hash) is not built in this PR, so
    this can only ever return MAJOR or NONE — never MINOR.
    TODO(#2563 gap 2): once a canonical_derived_hash() exists, this needs a
    second hash pair and a MINOR branch (L0 same, derived differs -> MINOR)."""
    if l0_hash_head != l0_hash_at_t:
        return "MAJOR"
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


def verdict(b: str, r: str, t: tuple[int, int], tag: str | None = None) -> tuple[str, str]:
    """The ADEQUACY comparison B vs R, with the exact target version named
    in every DENY message (never just "bump the version"). ``t`` is needed
    (beyond the bare (b, r) pair) purely to render the concrete target
    number — see this module's docstring / handoff notes for why that
    widens the sketch signature from the issue's design comments.

    ``tag`` (the deployable tag T came from) is threaded through so that,
    once a real tag exists, every PASS/DENY message names ``T`` -- see
    ``_name_t``.

    Precondition: b must already be well-formed (NONE/MINOR/MAJOR) — an
    ILLEGAL b must be handled by _illegal_verdict before R is ever computed,
    never routed through here.
    """
    if b not in ("NONE", "MINOR", "MAJOR"):
        raise ValueError(f"verdict() called with a non-well-formed b: {b!r}")
    if r not in ("NONE", "MAJOR"):
        raise ValueError(f"verdict() called with an unsupported r: {r!r}")

    tmaj, tmin = t

    if b == r:
        status, message = "PASS", f"version delta matches required severity ({r})"
    elif r == "MAJOR":
        target = f"{tmaj + 1}.0"
        status = "DENY"
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
    elif b == "MINOR":
        # r == "NONE"
        status = "DENY"
        message = (
            f"version bumped without cause — the L0 hash matches the "
            f"tagged commit. Revert MAJOR.MINOR to {tmaj}.{tmin}. The "
            f"derived-layer hash that would justify a MINOR is not yet "
            f"implemented (#2563 gap 2), so this gate cannot see a "
            f"MINOR-worthy cause. If this IS a derived-layer change, say so "
            f"on the PR rather than reverting."
        )
    else:
        # r == "NONE", b == "MAJOR"
        status = "DENY"
        message = (
            f"over-severity — MAJOR staged but the L0 hash matches the tagged "
            f"commit (no MAJOR-worthy change occurred). This needlessly "
            f"invalidates snapshot history from the previous major version and "
            f"is UNRECOVERABLE once tagged (the only correction would be a "
            f"backwards delta, which this same gate forbids). Revert "
            f"MAJOR.MINOR to {tmaj}.{tmin} (or {tmaj}.{tmin + 1} if a "
            f"MINOR-worthy change is actually intended)."
        )

    return status, _name_t(message, t, tag)


def evaluate_gate(t, s, get_l0_hash_head, get_l0_hash_at_t, tag: str | None = None) -> tuple[str, str]:
    """The full two-step gate: WELL-FORMEDNESS first (never touches hashes
    on an ILLEGAL delta), then ADEQUACY. ``get_l0_hash_head`` /
    ``get_l0_hash_at_t`` are zero-arg callables, so a falsifier test can pass
    a sentinel that raises if called, to prove ILLEGAL never reaches them.
    ``tag`` is the deployable tag T was resolved from (or ``None`` for the
    synthetic-``t``/``s`` unit falsifiers) -- threaded through to name T in
    every produced message once the gate is active."""
    b = classify_delta(t, s)
    if b == "ILLEGAL":
        return _illegal_verdict(t, s, tag=tag)
    r = required_severity(get_l0_hash_head(), get_l0_hash_at_t())
    return verdict(b, r, t, tag=tag)


INACTIVE_MESSAGE = "no tagged health version yet — gate inactive"


def _current_S() -> tuple[int, int]:
    """S at HEAD, imported (not regexed) — per #2563: 'S = (MAJOR, MINOR)
    from src.engram.health.version at HEAD (import it)'."""
    from health import version as health_version  # noqa: E402 (sys.path set at module top)

    return health_version.MAJOR, health_version.MINOR


def _l0_hash_at_head() -> str:
    from health import measurements as hem_l0  # noqa: E402

    return hem_l0.canonical_l0_hash()


def run_live_gate(repo_path=REPO_ROOT) -> tuple[str, str]:
    """The live gate against a real repo checkout: resolve T, and either
    report the gate inactive or run the full evaluate_gate against HEAD's
    real S and L0 hash. This is what both test_gate_on_this_repo and the
    CI step / manual verification run."""
    resolved = resolve_T(repo_path)
    if resolved is None:
        return "PASS", INACTIVE_MESSAGE
    t, tag, pinned_hash = resolved
    s = _current_S()
    return evaluate_gate(t, s, _l0_hash_at_head, lambda: pinned_hash, tag=tag)


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


def _write_pin_test(repo: Path, pinned_hash: str) -> None:
    path = repo / "tests" / "test_health_measurements.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'"""fake pin test for the gate falsifier suite."""\n'
        f'_PINNED_L0_HASH = "{pinned_hash}"\n',
        encoding="utf-8",
    )


def _git_run(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
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

def test_under_bump():
    status, message = evaluate_gate((1, 0), (1, 0), lambda: HASH_B, lambda: HASH_A)
    assert status == "DENY"
    assert "2.0" in message


def test_wrong_new_version():
    status, message = evaluate_gate((1, 0), (1, 1), lambda: HASH_B, lambda: HASH_A)
    assert status == "DENY"
    assert "2.0" in message
    assert "illegal" not in message.lower()


def test_bump_without_cause():
    status, message = evaluate_gate((1, 0), (1, 1), lambda: HASH_A, lambda: HASH_A)
    assert status == "DENY"
    assert "1.0" in message
    # Borges ask 2: while Gap 2 (derived-layer hash) is unbuilt, R can never
    # be MINOR, so this DENY must not claim "no cause" as a flat fact -- it
    # must name the gap and qualify the claim.
    assert "gap 2" in message
    assert "derived-layer" in message.lower()
    assert "without cause" in message.lower()


def test_over_severity_is_a_hard_deny():
    status, message = evaluate_gate((1, 0), (2, 0), lambda: HASH_A, lambda: HASH_A)
    assert status == "DENY"
    msg_lower = message.lower()
    assert "snapshot" in msg_lower
    assert "unrecoverable" in msg_lower
    assert status != "WARN"


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
    status, message = evaluate_gate(t, s, _raising_hash, _raising_hash)
    assert status == "DENY"
    assert expected_reason in message.lower()


def test_passing_none():
    status, message = evaluate_gate((1, 0), (1, 0), lambda: HASH_A, lambda: HASH_A)
    assert status == "PASS"


def test_passing_major():
    status, message = evaluate_gate((1, 0), (2, 0), lambda: HASH_B, lambda: HASH_A)
    assert status == "PASS"


def test_passing_major_batching_no_second_bump():
    """Second PR in the same batch: T is still 1.0 (not yet re-tagged), the
    L0 hash still differs from T, S is already staged at 2.0 from the first
    PR in the batch — PASS again, no further bump required."""
    status, message = evaluate_gate((1, 0), (2, 0), lambda: HASH_B, lambda: HASH_A)
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
    version, tag, pinned_hash = resolved
    assert version == (2, 0)
    assert tag == "deploy/2026-09-05-x"
    assert pinned_hash == HASH_B


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
    version, tag, pinned_hash = resolved
    assert version == (2, 0)
    assert tag == "v2.0.0"
    assert pinned_hash == HASH_B


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
    assert resolved == ((1, 0), "v1.0.0", HASH_A)
    t, tag, pinned_hash_at_t = resolved

    # Commit 2: L0 hash changed at HEAD, version NOT bumped -> under-bump.
    _write_pin_test(repo, HASH_B)  # simulates the NEW measured hash at HEAD
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-q", "-m", "L0 measurement changed, version not bumped yet")

    s_unbumped = (1, 0)
    status, message = evaluate_gate(
        t, s_unbumped, lambda: HASH_B, lambda: pinned_hash_at_t, tag=tag
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
        t, s_bumped, lambda: HASH_B, lambda: pinned_hash_at_t, tag=tag
    )
    assert status == "PASS"
    assert f"tag {tag}" in message
    assert "T=1.0" in message
