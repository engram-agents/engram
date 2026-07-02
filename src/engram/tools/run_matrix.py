#!/usr/bin/env python3
"""run_matrix.py — multi-environment test harness driver (#864 P0).

Composes the existing import-graph selection (`run_touched_tests.py --json`,
computed ONCE on the host) with a small set of prebuilt Docker images, runs the
selected test suite inside each image, and aggregates a per-environment result
table. Exits nonzero if ANY environment fails OR any image is excluded for
staleness — silent-skip is the exact failure class this harness exists to kill
(design: docs/864-multi-env-harness-design.md §3/§4).

It does NOT invent test selection (that is run_touched_tests.py's job) and it
does NOT replace the full-suite convergence gate — it makes the *fast* path
honest across environments the host can't otherwise represent.

Usage:
    python3 tools/run_matrix.py --envs p0 [--base origin/dev]
    python3 tools/run_matrix.py --envs p0 --full           # whole suite per env
    python3 tools/run_matrix.py --image lean-essential      # one image
    python3 tools/run_matrix.py --envs p0 --dry-run         # plan only, no docker
    python3 tools/run_matrix.py --envs p0 --build           # force (re)build stale images

Exit codes:
    0   every selected environment passed
    1   at least one environment failed
    2   at least one image was excluded (missing/stale and not built) — loud
    3   usage / environment error (e.g. docker absent and not --dry-run)

Selection is computed once on the host; the repo is mounted read-only into each
container, so there is no per-env git. Exit status is read directly from each
container's pytest process — no pipeline stages between check and gate (the
piped-exit-code-swallow class is structural here, not remembered).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT_DEFAULT = str(Path(__file__).resolve().parents[3])
IMAGE_TAG_PREFIX = "engram-test"
STAMP_LABEL = "engram.test.stamp"


# ---------------------------------------------------------------------------
# Environment registry (#864 §2 — the P0 four-image set)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EnvSpec:
    """One matrix environment: a Docker image + the axis values it pins.

    `axes` is purely descriptive (it shows in the report so a failure reads as
    the axis + the layer in one line); the actual axis construction lives in the
    image's Dockerfile (docker/Dockerfile.<name>).
    """

    name: str
    dockerfile: str
    axes: dict[str, str]

    @property
    def tag(self) -> str:
        return f"{IMAGE_TAG_PREFIX}:{self.name}"


# P0 set — every 1a axis with a real incident history gets one ON and one OFF
# cell; the two incident-bearing interaction pairs (vec×install-mode,
# locale×platform) get dedicated cells. (docs/864-multi-env-harness-design.md §2)
P0_ENVS: list[EnvSpec] = [
    EnvSpec(
        name="full-default",
        dockerfile="docker/Dockerfile.full-default",
        axes={"sqlite_vec": "present", "locale": "C.UTF-8", "tier": "dev",
              "topology": "multi", "install_mode": "plugin", "graph": "seeded"},
    ),
    EnvSpec(
        name="lean-essential",
        dockerfile="docker/Dockerfile.lean-essential",
        axes={"sqlite_vec": "absent", "locale": "C.UTF-8", "tier": "essential",
              "topology": "single", "install_mode": "plugin", "graph": "fresh"},
    ),
    EnvSpec(
        name="hostile-locale",
        dockerfile="docker/Dockerfile.hostile-locale",
        axes={"sqlite_vec": "present", "locale": "C (coercion off)", "tier": "dev",
              "topology": "multi", "install_mode": "source-clone", "graph": "seeded"},
    ),
    EnvSpec(
        name="codex-target",
        dockerfile="docker/Dockerfile.codex-target",
        axes={"sqlite_vec": "present", "locale": "C.UTF-8", "tier": "convenience",
              "topology": "multi", "install_mode": "plugin", "target": "codex"},
    ),
]

ENV_SETS: dict[str, list[EnvSpec]] = {"p0": P0_ENVS}


def _envs_by_name() -> dict[str, EnvSpec]:
    out: dict[str, EnvSpec] = {}
    for envs in ENV_SETS.values():
        for e in envs:
            out[e.name] = e
    return out


# ---------------------------------------------------------------------------
# Selection — compose run_touched_tests.py --json (once, on the host)
# ---------------------------------------------------------------------------
def compute_selection(base: str, full: bool, repo_root: str,
                      runner=subprocess.run) -> dict:
    """Run `run_touched_tests.py --json` on the host and return the parsed dict.

    Raises RuntimeError if the emitter fails or emits non-JSON — we never scrape
    prose, and a corrupt selection must fail loudly rather than silently run the
    wrong suite.
    """
    cmd = [sys.executable, os.path.join(repo_root, "tools", "run_touched_tests.py"),
           "--json", "--base", base, "--repo-root", repo_root]
    if full:
        cmd.append("--full")
    proc = runner(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"selection emitter exited {proc.returncode}: {proc.stderr.strip()}"
        )
    line = (proc.stdout or "").strip()
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"selection emitter did not emit valid JSON: {exc}\n--- stdout ---\n{line[:500]}"
        ) from exc


def pytest_targets(selection: dict) -> list[str]:
    """The pytest path args for a given selection.

    full=true → the whole suite (`tests/`); otherwise the selected file list.
    An empty non-full selection means nothing was touched → empty list (the
    caller treats that as a no-op pass, not a full run).
    """
    if selection.get("full"):
        return ["tests/"]
    return list(selection.get("selected", []))


# ---------------------------------------------------------------------------
# Image freshness (#864 §4 — the staleness trap)
# ---------------------------------------------------------------------------
def _freshness_inputs(env: EnvSpec, repo_root: str) -> list[str]:
    """Files whose change should invalidate a prebuilt image, in stable order."""
    root = Path(repo_root)
    inputs = [env.dockerfile, "src/build/packaging/tiers.json"]
    inputs += sorted(str(p.relative_to(root)) for p in root.glob("requirements/requirements*.txt"))
    return inputs


def compute_image_hash(env: EnvSpec, repo_root: str) -> str:
    """sha256 over the freshness inputs' contents (missing files contribute their
    absence, so deleting an input also invalidates). Deterministic + pure."""
    import warnings
    h = hashlib.sha256()
    for rel in _freshness_inputs(env, repo_root):
        p = Path(repo_root) / rel
        h.update(rel.encode())
        h.update(b"\0")
        if p.is_file():
            h.update(p.read_bytes())
        else:
            warnings.warn(
                f"run_matrix: freshness input missing — {rel!r} not found under {repo_root!r}; "
                "image-staleness detection for this input is degraded",
                stacklevel=2,
            )
            h.update(b"<absent>")
        h.update(b"\0")
    return h.hexdigest()


def docker_available(which=shutil.which) -> bool:
    return which("docker") is not None


def image_stamp(tag: str, runner=subprocess.run) -> str | None:
    """Return the image's baked-in freshness stamp label, or None if the image
    does not exist."""
    proc = runner(
        ["docker", "image", "inspect", "--format",
         "{{ index .Config.Labels \"" + STAMP_LABEL + "\" }}", tag],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None  # image absent
    return (proc.stdout or "").strip()


def build_image(env: EnvSpec, repo_root: str, image_hash: str,
                runner=subprocess.run) -> bool:
    """Build the image, baking the freshness stamp as a label. Returns success."""
    proc = runner(
        ["docker", "build", "-f", os.path.join(repo_root, env.dockerfile),
         "-t", env.tag, "--label", f"{STAMP_LABEL}={image_hash}", repo_root],
        text=True,
    )
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Per-environment run
# ---------------------------------------------------------------------------
def parse_pytest_counts(output: str) -> tuple[int, int, int]:
    """Best-effort (passed, failed, errors) from pytest's summary line.

    The authoritative pass/fail signal is the process return code; these counts
    are for the human report only, so a parse miss degrades to zeros, never to a
    wrong gate decision.
    """
    passed = failed = errors = 0
    for line in reversed(output.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            for n, kind in re.findall(r"(\d+)\s+(passed|failed|error|errors)", line):
                if kind == "passed":
                    passed = int(n)
                elif kind == "failed":
                    failed = int(n)
                else:
                    errors = int(n)
            if passed or failed or errors:
                break
    return passed, failed, errors


@dataclass
class EnvResult:
    name: str
    status: str  # "pass" | "fail" | "excluded" | "skip"
    returncode: int | None = None
    passed: int = 0
    failed: int = 0
    errors: int = 0
    detail: str = ""
    axes: dict[str, str] = field(default_factory=dict)


def run_in_env(env: EnvSpec, targets: list[str], repo_root: str,
               runner=subprocess.run) -> EnvResult:
    """Run the selected pytest targets inside one image. Reads the container's
    pytest return code directly (no pipe between check and gate)."""
    if not targets:
        return EnvResult(env.name, "skip", returncode=0,
                         detail="nothing selected (no touched tests)", axes=env.axes)
    cmd = ["docker", "run", "--rm",
           "-v", f"{repo_root}:/repo:ro", "-w", "/repo",
           env.tag,
           "python", "-m", "pytest", *targets, "-q", "--tb=short"]
    proc = runner(cmd, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    passed, failed, errors = parse_pytest_counts(out)
    status = "pass" if proc.returncode == 0 else "fail"
    detail = "" if status == "pass" else _tail(out, 12)
    return EnvResult(env.name, status, returncode=proc.returncode,
                     passed=passed, failed=failed, errors=errors,
                     detail=detail, axes=env.axes)


def _tail(text: str, n: int) -> str:
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-n:])


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def format_report(selection: dict, results: list[EnvResult]) -> str:
    lines: list[str] = []
    buckets = selection.get("buckets", {})
    if selection.get("full"):
        sel_desc = "FULL suite"
    else:
        n = len(selection.get("selected", []))
        sel_desc = (f"{n} selected "
                    f"(internals {buckets.get('internals', 0)} · "
                    f"contract {buckets.get('contract', 0)} · "
                    f"map_residue {buckets.get('map_residue', 0)})")
    lines.append(f"[run_matrix] selection: {sel_desc}  (base {selection.get('base')})")
    lines.append("")
    lines.append(f"  {'ENV':18s} {'STATUS':9s} {'P/F/E':12s} AXES")
    lines.append("  " + "-" * 76)
    for r in results:
        pfe = f"{r.passed}/{r.failed}/{r.errors}"
        axes = " ".join(f"{k}={v}" for k, v in r.axes.items())
        lines.append(f"  {r.name:18s} {r.status.upper():9s} {pfe:12s} {axes[:60]}")
        if r.detail and r.status in ("fail", "excluded"):
            for dl in r.detail.splitlines():
                lines.append(f"      | {dl}")
    return "\n".join(lines)


def aggregate_exit(results: list[EnvResult]) -> int:
    if any(r.status == "excluded" for r in results):
        return 2
    if any(r.status == "fail" for r in results):
        return 1
    return 0


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def resolve_envs(args) -> list[EnvSpec]:
    if args.image:
        by_name = _envs_by_name()
        missing = [n for n in args.image if n not in by_name]
        if missing:
            raise SystemExit(f"unknown image(s): {', '.join(missing)}  "
                             f"(known: {', '.join(sorted(by_name))})")
        return [by_name[n] for n in args.image]
    if args.envs not in ENV_SETS:
        raise SystemExit(f"unknown env set: {args.envs}  (known: {', '.join(ENV_SETS)})")
    return ENV_SETS[args.envs]


def ensure_image(env: EnvSpec, repo_root: str, do_build: bool,
                 runner=subprocess.run) -> tuple[bool, str]:
    """Resolve image freshness. Returns (usable, reason).

    usable=False means the image is missing or stale and was not (re)built —
    the caller EXCLUDES it loudly (never silently skipped)."""
    want = compute_image_hash(env, repo_root)
    have = image_stamp(env.tag, runner=runner)
    if have == want:
        return True, "fresh"
    stale_reason = "absent" if have is None else "stale (inputs changed)"
    if not do_build:
        return False, f"{stale_reason}; rerun with --build to (re)build"
    ok = build_image(env, repo_root, want, runner=runner)
    if not ok:
        return False, f"{stale_reason}; build FAILED"
    return True, "rebuilt"


def run_matrix(args, runner=subprocess.run) -> int:
    repo_root = os.path.abspath(args.repo_root) if args.repo_root else REPO_ROOT_DEFAULT
    envs = resolve_envs(args)

    selection = compute_selection(args.base, args.full, repo_root, runner=runner)
    targets = pytest_targets(selection)

    if args.dry_run:
        print(format_report(selection, [
            EnvResult(e.name, "skip", detail="(dry-run: not executed)", axes=e.axes)
            for e in envs
        ]))
        print("\n[run_matrix] dry-run: would run "
              f"{'FULL suite' if selection.get('full') else str(len(targets)) + ' file(s)'} "
              f"in {len(envs)} image(s): {', '.join(e.name for e in envs)}")
        return 0

    if not docker_available():
        print("[run_matrix] ERROR: docker not found on PATH. Install docker, or "
              "use --dry-run to see the plan.", file=sys.stderr)
        return 3

    results: list[EnvResult] = []
    for env in envs:
        usable, reason = ensure_image(env, repo_root, args.build, runner=runner)
        if not usable:
            print(f"[run_matrix] EXCLUDING {env.name}: {reason}", file=sys.stderr)
            results.append(EnvResult(env.name, "excluded", detail=reason, axes=env.axes))
            continue
        results.append(run_in_env(env, targets, repo_root, runner=runner))

    print(format_report(selection, results))
    code = aggregate_exit(results)
    if code == 2:
        print("\n[run_matrix] FAILED: one or more images excluded (stale/missing). "
              "These are NOT passes — rerun with --build.", file=sys.stderr)
    elif code == 1:
        print("\n[run_matrix] FAILED: one or more environments had test failures.",
              file=sys.stderr)
    else:
        print("\n[run_matrix] OK: all environments passed.")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Multi-environment test harness driver (#864). Composes "
                    "run_touched_tests.py selection with prebuilt Docker images.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--envs", default="p0", metavar="SET",
                        help="named env set to run (default: p0)")
    parser.add_argument("--image", action="append", metavar="NAME",
                        help="restrict to specific image(s); repeatable")
    parser.add_argument("--base", default="origin/dev", metavar="REF",
                        help="base git ref for selection diff (default: origin/dev)")
    parser.add_argument("--full", action="store_true",
                        help="run the whole suite per env (override selection)")
    parser.add_argument("--build", action="store_true",
                        help="(re)build images that are missing or stale before running")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan (selection + images) without docker")
    parser.add_argument("--repo-root", default=None, metavar="PATH",
                        help="repository root (default: parent dir of this script)")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return run_matrix(args)
    except RuntimeError as exc:
        print(f"[run_matrix] ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
