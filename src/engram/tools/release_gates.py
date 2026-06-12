#!/usr/bin/env python3
"""release_gates.py — mechanical release-readiness scorecard for ENGRAM v0.1.

Converts the EPIC #689 acceptance bar (fresh install / upgrade / core MCP /
docs accurate, per the maintainer's 2026-05-30 ruling) from a prose checklist
into a one-command mechanical scorecard.

Usage:
    python3 tools/release_gates.py [options]

Options:
    --json          Emit JSON instead of human-readable table
    --markdown      Emit a scorecard block for posting to EPIC #689
    --gate ID       Run only the named gate (G3a, G3b, G4a, G4b, G5, G1, G2, G6)
    --skip-slow     Skip G3a (the ~3 min pytest suite) and mark it SKIP(--skip-slow)
    --repo-root P   Repo root override (default: parent of this file's directory)
    --python P      Python interpreter for G3a/G3b/G1/G2 (default: venv python if
                    present at ~/.engram/venv/bin/python3, else sys.executable)

To post the scorecard to EPIC #689:
    python3 tools/release_gates.py --markdown | gh issue comment 689 --body-file -

Config: packaging/release-gates.json (thresholds; edit config not code).

Exit code: 0 if RELEASABLE (all non-SKIP gates PASS), 1 otherwise.
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

STATUS_PASS = "PASS"
STATUS_DEGRADED = "DEGRADED"
STATUS_FAIL = "FAIL"
STATUS_SKIP = "SKIP"

# Ordered for display
ALL_GATE_IDS = ["G3a", "G3b", "G4a", "G4b", "G5", "G1", "G2", "G6"]


def make_result(
    gate: str,
    dimension: str,
    status: str,
    metric: str,
    detail: str,
) -> dict[str, Any]:
    """Return a gate result dict."""
    return {
        "gate": gate,
        "dimension": dimension,
        "status": status,
        "metric": metric,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(repo_root: str) -> dict[str, Any]:
    """Load packaging/release-gates.json from repo_root."""
    config_path = os.path.join(repo_root, "src", "engram", "packaging", "release-gates.json")
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Python interpreter resolution
# ---------------------------------------------------------------------------

def resolve_python(repo_root: str, override: str | None = None) -> str:
    """Return the Python interpreter to use for suite/schema runs.

    Priority:
      1. --python CLI override
      2. ~/.engram/venv/bin/python3 if it exists
      3. sys.executable (fallback)
    """
    if override:
        return override
    venv_py = os.path.expanduser("~/.engram/venv/bin/python3")
    if os.path.isfile(venv_py):
        return venv_py
    return sys.executable


# ---------------------------------------------------------------------------
# Pure gate-logic functions (testable without side effects)
# ---------------------------------------------------------------------------

def compare_suite_residual(
    failures: list[str],
    expected_residual: list[str],
) -> tuple[str, str, str]:
    """Compare actual failures against expected residual set.

    Returns (status, metric, detail).

    Logic per spec:
      - PASS if failures is empty and expected_residual is empty
      - DEGRADED if failures ⊆ expected_residual (and expected_residual non-empty)
      - FAIL if any failure is NOT in expected_residual
    """
    expected_set = set(expected_residual)
    actual_set = set(failures)

    unexpected = actual_set - expected_set
    if unexpected:
        unexpected_sorted = sorted(unexpected)
        return (
            STATUS_FAIL,
            f"{len(unexpected)} unexpected failure(s)",
            "Unexpected failures: " + "; ".join(unexpected_sorted),
        )
    if actual_set:
        # All failures are in expected set → DEGRADED (known residual)
        return (
            STATUS_DEGRADED,
            f"{len(actual_set)} known residual failure(s)",
            "Expected residual: " + "; ".join(sorted(actual_set)),
        )
    # No failures at all
    return (STATUS_PASS, "0 failures", "")


# sha component is \w+ (not [0-9a-f]+): the producer (tools/engine/build.py
# compute_build_version) emits two non-hex fallback shapes that still carry a
# valid per-rebuild stamp — "unknown" (git unavailable) and "g<digits>"
# (pure-numeric sha7 g-prefixed per semver §9 / git-describe convention).
_STAMP_RE = re.compile(r"-dev\.\d{14}\.\w+")


def check_stamp_pattern(version: str) -> tuple[str, str, str]:
    """Check a build version string for the per-rebuild stamp pattern (G2 upgrade-smoke).

    POST-#897: plugin.json carries a per-rebuild stamped version of the form
    ``0.1.0-dev.<YYYYMMDDHHMMSS>.<sha>`` (14-digit timestamp + hex sha).

    Property under test: the version string carries a per-build stamp.
    Two consecutive same-second builds produce identical stamps — that collision
    is not a regression; stamp presence is the invariant that matters.

    Returns (status, metric, detail).
      PASS — version matches the stamped pattern (#897 landed correctly)
      FAIL — version does not match (static/unstamped version is a regression
             after #897 merged)
    """
    if _STAMP_RE.search(version):
        return (
            STATUS_PASS,
            "stamp present",
            f"version={version!r} matches per-rebuild stamp pattern",
        )
    return (
        STATUS_FAIL,
        "stamp absent",
        (
            f"version={version!r} — no per-rebuild stamp "
            "(expected -dev.<YYYYMMDDHHMMSS>.<sha> format; regression after #897?)"
        ),
    )


def check_forbidden_teachings_in_content(
    content: str,
    regex: str,
    source_path: str,
) -> list[tuple[int, str]]:
    """Find all lines matching the forbidden regex in content.

    Returns list of (lineno, line_text) tuples for matches.
    """
    pattern = re.compile(regex)
    hits = []
    for lineno, line in enumerate(content.splitlines(), 1):
        if pattern.search(line):
            hits.append((lineno, line.rstrip()))
    return hits


# ---------------------------------------------------------------------------
# Gate runners
# ---------------------------------------------------------------------------

def run_G3a(
    repo_root: str,
    python: str,
    expected_residual: list[str],
    skip_slow: bool,
) -> dict[str, Any]:
    """G3a — core-MCP suite gate."""
    if skip_slow:
        return make_result(
            "G3a", "core-MCP",
            STATUS_SKIP, "skipped", "--skip-slow",
        )

    tests_dir = os.path.join(repo_root, "tests")
    env = os.environ.copy()
    env["ENGRAM_NO_EMBEDDINGS"] = "1"

    try:
        proc = subprocess.run(
            [python, "-m", "pytest", tests_dir, "-q", "--tb=no", "--no-header"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            env=env,
            timeout=360,
        )
        output = proc.stdout + proc.stderr
    except FileNotFoundError:
        return make_result(
            "G3a", "core-MCP",
            STATUS_SKIP, "skipped", f"Python not found: {python}",
        )
    except subprocess.TimeoutExpired:
        return make_result(
            "G3a", "core-MCP",
            STATUS_FAIL, "timeout", "Suite timed out after 360s",
        )

    # Parse failures from pytest -q output.
    # pytest -q prints:
    #   "FAILED tests/foo.py::Bar::test_baz - AssertionError"  (assertion failures)
    #   "ERROR tests/foo.py::test_x - RuntimeError: ..."       (fixture/collection errors)
    # Both count as failures for the residual comparison.
    failures: list[str] = []
    for line in output.splitlines():
        line_s = line.strip()
        if line_s.startswith("FAILED ") or line_s.startswith("ERROR "):
            prefix_len = len("FAILED ") if line_s.startswith("FAILED ") else len("ERROR ")
            rest = line_s[prefix_len:]
            # Strip the " - <reason>" suffix using rsplit so that parametrize
            # values containing " - " (e.g. test_x[a - b]) survive intact.
            test_id = rest.rsplit(" - ", 1)[0].strip()
            failures.append(test_id)

    # Returncode backstop: if pytest exited nonzero but we parsed no failures,
    # the output format may have drifted (e.g. collection error with no test IDs).
    # Treat this as FAIL rather than silently PASS.
    if proc.returncode not in (0, 1) or (proc.returncode != 0 and not failures):
        return make_result(
            "G3a", "core-MCP",
            STATUS_FAIL,
            f"exit={proc.returncode}",
            (
                f"suite exited {proc.returncode} with no parsed FAILED/ERROR lines — "
                "possible collection error or output format drift; inspect output"
            ),
        )

    status, metric, detail = compare_suite_residual(failures, expected_residual)
    return make_result("G3a", "core-MCP", status, metric, detail)


def run_G3b(
    repo_root: str,
    python: str,
    expected_tool_count: int,
) -> dict[str, Any]:
    """G3b — MCP schema tool-count gate."""
    # We need to count the @mcp.tool() declarations by running the server
    # in a tmp ENGRAM_HOME without launching the full stdio server.
    # Strategy: run a subprocess that imports server and counts tools.
    count_script = r"""
import sys, os, json
# No ENGRAM_HOME / env setup needed: the AST parse below never imports
# server.py, so nothing touches a data dir (review catch — the tmpdir
# setup was vestigial from a pre-AST design of this gate).
sys.path.insert(0, os.getcwd())
try:
    import ast, pathlib
    src = pathlib.Path('src', 'engram', 'server.py').read_text()
    tree = ast.parse(src)
    # Count top-level function defs decorated with @mcp.tool(...)
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for deco in node.decorator_list:
                # @mcp.tool( or @mcp.tool()
                if isinstance(deco, ast.Call):
                    func = deco.func
                    if (isinstance(func, ast.Attribute) and func.attr == 'tool'):
                        count += 1
                        break
    print(json.dumps({'tool_count': count}))
except Exception as e:
    print(json.dumps({'error': str(e)}), file=sys.stderr)
    sys.exit(1)
"""
    try:
        proc = subprocess.run(
            [python, "-c", count_script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return make_result(
            "G3b", "core-MCP",
            STATUS_SKIP, "skipped", f"Python not found: {python}",
        )
    except subprocess.TimeoutExpired:
        return make_result(
            "G3b", "core-MCP",
            STATUS_FAIL, "timeout", "Schema count timed out after 30s",
        )

    if proc.returncode != 0:
        return make_result(
            "G3b", "core-MCP",
            STATUS_FAIL, "error", f"Schema count script failed: {proc.stderr.strip()[:200]}",
        )

    try:
        data = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        return make_result(
            "G3b", "core-MCP",
            STATUS_FAIL, "parse-error", f"Could not parse output: {proc.stdout.strip()[:100]}",
        )

    # Defensive: the current inner script never emits {"error": ...} with
    # returncode 0, so this branch is unreachable today — kept as a guard
    # against future inner-script changes (an error MUST never read as PASS).
    if "error" in data:
        return make_result(
            "G3b", "core-MCP",
            STATUS_FAIL, "error", data["error"][:200],
        )

    actual = data["tool_count"]
    if actual == expected_tool_count:
        return make_result(
            "G3b", "core-MCP",
            STATUS_PASS,
            f"tool_count={actual}",
            f"Matches expected ({expected_tool_count})",
        )
    return make_result(
        "G3b", "core-MCP",
        STATUS_FAIL,
        f"tool_count={actual}",
        f"Expected {expected_tool_count}, got {actual}",
    )


def run_G4a(repo_root: str) -> dict[str, Any]:
    """G4a — node-ID burn-down metric (docs dimension).

    Counts concrete ENGRAM node IDs (regex: \\b[a-z]{2}_[0-9]{4,}\\b) in
    shipped surfaces, applying the same exclude-set as
    .github/workflows/check-no-new-shipped-node-ids.yml.

    This is a DEGRADED metric (grandfathered corpus); never FAIL.
    The diff-gate owns regressions.
    """
    pattern = re.compile(r'\b[a-z]{2}_[0-9]{4,}\b')

    # Exclude dirs/files matching the CI workflow's EXCLUDES
    exclude_dirs = {"tests", ".git", "__pycache__", "node_modules", "paper_draft", "active-work"}
    exclude_files = {"README.md", "CHANGELOG.md", "check-no-new-shipped-node-ids.yml"}

    total_count = 0
    hit_files: list[str] = []

    for dirpath, dirnames, filenames in os.walk(repo_root):
        rel_dir = os.path.relpath(dirpath, repo_root)
        # Prune excluded top-level dirs
        # Any-depth pruning: matches the CI excludes' intent (tests/ AND
        # forum/tests/ are both excluded there; membership pruning covers
        # nested test dirs uniformly).
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]

        for fname in filenames:
            if fname in exclude_files:
                continue
            fpath = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(fpath, repo_root)

            # Skip .github/workflows/check-no-new-shipped-node-ids.yml
            if rel_path.endswith("check-no-new-shipped-node-ids.yml"):
                continue
            # Only scan text files with relevant extensions
            ext = Path(fpath).suffix.lower()
            if ext not in {".py", ".md", ".sh", ".json", ".template", ".txt", ".yml", ".yaml"}:
                continue
            try:
                content = Path(fpath).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            file_count = len(pattern.findall(content))
            if file_count > 0:
                total_count += file_count
                hit_files.append(f"{rel_path}:{file_count}")

    if total_count == 0:
        return make_result(
            "G4a", "docs",
            STATUS_PASS,
            "0 node IDs",
            "No concrete node IDs in shipped surfaces",
        )
    return make_result(
        "G4a", "docs",
        STATUS_DEGRADED,
        f"{total_count} node IDs",
        f"Burn-down metric — grandfathered corpus ({len(hit_files)} files); "
        "diff-gate owns regressions",
    )


def run_G4b(repo_root: str, forbidden_teachings: list[dict]) -> dict[str, Any]:
    """G4b — forbidden teachings gate (docs dimension).

    Each entry: {surface_glob, regex, reason}.
    PASS if zero matches across all entries; FAIL with file:line list.
    """
    all_hits: list[str] = []

    for entry in forbidden_teachings:
        surface_glob = entry["surface_glob"]
        regex = entry["regex"]
        reason = entry.get("reason", "")

        # Expand glob relative to repo root
        matched_files = glob.glob(os.path.join(repo_root, surface_glob))
        for fpath in sorted(matched_files):
            if not os.path.isfile(fpath):
                continue
            try:
                content = Path(fpath).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            rel_path = os.path.relpath(fpath, repo_root)
            hits = check_forbidden_teachings_in_content(content, regex, rel_path)
            for lineno, _line in hits:
                all_hits.append(f"{rel_path}:{lineno} [{reason}]")

    if not all_hits:
        return make_result(
            "G4b", "docs",
            STATUS_PASS,
            "0 matches",
            "No forbidden teachings found",
        )
    return make_result(
        "G4b", "docs",
        STATUS_FAIL,
        f"{len(all_hits)} match(es)",
        "; ".join(all_hits),
    )


def run_G5(repo_root: str) -> dict[str, Any]:
    """G5 — de-personalisation scan (public-readiness dimension).

    Runs tools/scan-leaks.py --root . and reports hit count.
    DEGRADED with count > 0; PASS at 0.
    If roster absent → SKIP with advisory (spec language for future
    #665 roster extension; current scan-leaks.py has no roster concept,
    so we always run it and SKIP only if the script itself is missing).
    """
    scan_leaks = os.path.join(repo_root, "tools", "scan-leaks.py")
    if not os.path.isfile(scan_leaks):
        return make_result(
            "G5", "public-readiness",
            STATUS_SKIP, "skipped",
            "tools/scan-leaks.py not found",
        )

    try:
        proc = subprocess.run(
            [sys.executable, scan_leaks, "--root", repo_root],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return make_result(
            "G5", "public-readiness",
            STATUS_FAIL, "timeout", "scan-leaks timed out after 60s",
        )

    if proc.returncode == 0:
        return make_result(
            "G5", "public-readiness",
            STATUS_PASS, "0 hits", "No personal-info leaks detected",
        )

    # Count hits from stderr output.
    # scan-leaks.py writes individual hit lines as "  path:lineno [label] snippet"
    # (two leading spaces); the summary line is "scan-leaks: N potential..." with no indent.
    # hit_count = number of hit lines (one per match, not unique files).
    lines = proc.stderr.splitlines()
    hit_lines = [l for l in lines if l.startswith("  ")]
    hit_count = len(hit_lines)

    return make_result(
        "G5", "public-readiness",
        STATUS_DEGRADED,
        f"{hit_count} hit(s)",
        f"scan-leaks found {hit_count} potential leak(s) — review before release",
    )


def run_G1(repo_root: str, python: str) -> dict[str, Any]:
    """G1 — build smoke (fresh-install dimension).

    Invokes tools.engine.cli build --tier essential --target claude-code
    in a temp output dir; verifies plugin.json emitted + manifest selection
    nonzero.

    SKIP if the engine import fails (e.g. engine not installed).
    """
    with tempfile.TemporaryDirectory(prefix="rg_g1_") as tmp_out:
        try:
            proc = subprocess.run(
                [
                    python, "-m", "tools.engine.cli",
                    "build",
                    "--tier", "essential",
                    "--target", "claude-code",
                    "--output", tmp_out,
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            return make_result(
                "G1", "fresh-install",
                STATUS_SKIP, "skipped", f"Python not found: {python}",
            )
        except subprocess.TimeoutExpired:
            return make_result(
                "G1", "fresh-install",
                STATUS_FAIL, "timeout", "Build timed out after 120s",
            )

        # Check for import error (engine not importable)
        output = proc.stdout + proc.stderr
        if proc.returncode != 0:
            if "ModuleNotFoundError" in output or "ImportError" in output:
                return make_result(
                    "G1", "fresh-install",
                    STATUS_SKIP, "skipped",
                    f"Engine import failed: {output[:200]}",
                )
            return make_result(
                "G1", "fresh-install",
                STATUS_FAIL,
                f"exit={proc.returncode}",
                f"Build failed: {output[-300:]}",
            )

        # Verify plugin.json exists
        plugin_json_path = os.path.join(tmp_out, "plugin.json")
        if not os.path.isfile(plugin_json_path):
            return make_result(
                "G1", "fresh-install",
                STATUS_FAIL, "no plugin.json",
                f"plugin.json not found in output dir {tmp_out}",
            )

        # Verify manifest selection nonzero
        manifest_path = os.path.join(tmp_out, ".engram-build-manifest.json")
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    manifest_data = json.load(f)
                shipped = manifest_data.get("shipped_paths", [])
                if not shipped:
                    return make_result(
                        "G1", "fresh-install",
                        STATUS_FAIL, "empty manifest",
                        "Build manifest has zero shipped paths",
                    )
                return make_result(
                    "G1", "fresh-install",
                    STATUS_PASS,
                    f"{len(shipped)} paths shipped",
                    f"plugin.json present; {len(shipped)} manifest entries",
                )
            except (json.JSONDecodeError, OSError):
                pass

        # plugin.json exists but no manifest — SKIP the manifest-nonzero check.
        # The spec requires present + nonzero; older engines may omit the manifest.
        # Silently treating this as PASS would mask the gap — return SKIP instead.
        return make_result(
            "G1", "fresh-install",
            STATUS_SKIP,
            "manifest absent",
            "plugin.json present; manifest not found (older engine? — skip manifest-nonzero check)",
        )


def run_G2(repo_root: str, python: str) -> dict[str, Any]:
    """G2 — upgrade smoke (upgrade dimension).

    Runs a single build and checks whether the plugin.json version carries the
    per-rebuild stamp pattern introduced by #897
    (format: ``0.1.0-dev.<YYYYMMDDHHMMSS>.<sha>``).

    Property under test: version stamping is active.  Two consecutive sub-second
    builds may produce identical stamps (granularity is one second); that is not
    a regression — stamp presence is the invariant.  A second build is therefore
    not needed; comparing two versions would produce a false DEGRADED/FAIL on
    same-second collisions.

    PASS    — version matches the stamped pattern
    FAIL    — version does not match (unstamped = regression after #897)
    SKIP    — engine not importable or Python not found
    """
    with tempfile.TemporaryDirectory(prefix="rg_g2_") as tmp_out:
        try:
            proc = subprocess.run(
                [
                    python, "-m", "tools.engine.cli",
                    "build",
                    "--tier", "essential",
                    "--target", "claude-code",
                    "--output", tmp_out,
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            return make_result(
                "G2", "upgrade",
                STATUS_SKIP, "skipped", f"Python not found: {python}",
            )
        except subprocess.TimeoutExpired:
            return make_result(
                "G2", "upgrade",
                STATUS_FAIL, "timeout", "Build timed out",
            )

        if proc.returncode != 0:
            output = proc.stdout + proc.stderr
            if "ModuleNotFoundError" in output or "ImportError" in output:
                return make_result(
                    "G2", "upgrade",
                    STATUS_SKIP, "skipped",
                    "Engine import failed",
                )
            return make_result(
                "G2", "upgrade",
                STATUS_FAIL,
                f"exit={proc.returncode}",
                "Build failed",
            )

        plugin_json = os.path.join(tmp_out, "plugin.json")
        if not os.path.isfile(plugin_json):
            return make_result(
                "G2", "upgrade",
                STATUS_FAIL, "no plugin.json",
                "Build produced no plugin.json",
            )

        try:
            with open(plugin_json, encoding="utf-8") as f:
                data = json.load(f)
            version = data.get("version", "")
        except (json.JSONDecodeError, OSError) as e:
            return make_result(
                "G2", "upgrade",
                STATUS_FAIL, "parse-error",
                f"plugin.json unreadable: {e}",
            )

    status, metric, detail = check_stamp_pattern(version)
    return make_result("G2", "upgrade", status, metric, detail)


def run_G6(release_blocker_issues: list[int]) -> dict[str, Any]:
    """G6 — open blocker count (scope dimension).

    Queries GitHub for each issue in release_blocker_issues.
    PASS at 0 open; DEGRADED with open list; SKIP if gh/network unavailable.
    """
    # Check gh is available
    try:
        chk = subprocess.run(
            ["gh", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if chk.returncode != 0:
            raise FileNotFoundError("gh not working")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return make_result(
            "G6", "scope",
            STATUS_SKIP, "skipped",
            "gh CLI unavailable or network unreachable",
        )

    open_issues: list[int] = []
    for issue_num in release_blocker_issues:
        try:
            proc = subprocess.run(
                ["gh", "issue", "view", str(issue_num), "--json", "state"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode != 0:
                # Network/auth error → SKIP the whole gate
                return make_result(
                    "G6", "scope",
                    STATUS_SKIP, "skipped",
                    f"gh query failed for issue #{issue_num}: {proc.stderr.strip()[:100]}",
                )
            data = json.loads(proc.stdout)
            if data.get("state", "").upper() == "OPEN":
                open_issues.append(issue_num)
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            return make_result(
                "G6", "scope",
                STATUS_SKIP, "skipped",
                f"gh query error for issue #{issue_num}: {e}",
            )

    if not open_issues:
        return make_result(
            "G6", "scope",
            STATUS_PASS, "0 open blockers",
            "All tracked blocker issues are closed",
        )
    return make_result(
        "G6", "scope",
        STATUS_DEGRADED,
        f"{len(open_issues)} open blocker(s)",
        "Open: " + ", ".join(f"#{n}" for n in open_issues),
    )


# ---------------------------------------------------------------------------
# Scorecard runner
# ---------------------------------------------------------------------------

def run_scorecard(
    repo_root: str,
    config: dict[str, Any],
    python: str,
    skip_slow: bool = False,
    gate_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Run all gates (or just gate_filter) and return list of results."""
    expected_residual = config.get("expected_suite_residual", [])
    expected_tool_count = config.get("expected_tool_count", 49)
    forbidden_teachings = config.get("forbidden_teachings", [])
    release_blocker_issues = config.get("release_blocker_issues", [])

    gate_runners = {
        "G3a": lambda: run_G3a(repo_root, python, expected_residual, skip_slow),
        "G3b": lambda: run_G3b(repo_root, python, expected_tool_count),
        "G4a": lambda: run_G4a(repo_root),
        "G4b": lambda: run_G4b(repo_root, forbidden_teachings),
        "G5":  lambda: run_G5(repo_root),
        "G1":  lambda: run_G1(repo_root, python),
        "G2":  lambda: run_G2(repo_root, python),
        "G6":  lambda: run_G6(release_blocker_issues),
    }

    if gate_filter:
        if gate_filter not in gate_runners:
            raise ValueError(f"Unknown gate: {gate_filter!r}. Valid: {', '.join(ALL_GATE_IDS)}")
        return [gate_runners[gate_filter]()]

    return [gate_runners[gid]() for gid in ALL_GATE_IDS]


def is_releasable(results: list[dict[str, Any]]) -> bool:
    """Return True iff all non-SKIP gates are PASS."""
    for r in results:
        if r["status"] in (STATUS_FAIL, STATUS_DEGRADED):
            return False
    return True


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def format_human(results: list[dict[str, Any]]) -> str:
    """Format results as an aligned human-readable table + verdict."""
    lines: list[str] = []
    lines.append(f"  {'GATE':<5}  {'DIM':<20}  {'STATUS':<12}  {'METRIC':<30}  DETAIL")
    lines.append(f"  {'----':<5}  {'---':<20}  {'------':<12}  {'------':<30}  ------")

    for r in results:
        detail_truncated = r["detail"][:60]
        lines.append(
            f"  {r['gate']:<5}  {r['dimension']:<20}  {r['status']:<12}  "
            f"{r['metric']:<30}  {detail_truncated}"
        )

    lines.append("")
    if is_releasable(results):
        lines.append("RELEASABLE: yes")
    else:
        non_passing = [r["gate"] for r in results if r["status"] in (STATUS_FAIL, STATUS_DEGRADED)]
        lines.append(f"RELEASABLE: no  ({', '.join(non_passing)} need attention)")

    return "\n".join(lines)


def format_json_output(results: list[dict[str, Any]]) -> str:
    """Format results as JSON."""
    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "releasable": is_releasable(results),
        "results": results,
    }
    return json.dumps(payload, indent=2)


def format_markdown(results: list[dict[str, Any]]) -> str:
    """Format results as a markdown scorecard block for posting to EPIC #689."""
    lines: list[str] = []
    date_str = datetime.date.today().isoformat()

    lines.append("<!-- release-gates-scorecard -->")
    lines.append(f"## Release gates scorecard — {date_str}")
    lines.append("")
    lines.append("| Gate | Dimension | Status | Metric | Detail |")
    lines.append("|------|-----------|--------|--------|--------|")

    for r in results:
        # Markdown-safe: replace | in detail
        detail = r["detail"].replace("|", "\\|")[:80]
        lines.append(
            f"| {r['gate']} | {r['dimension']} | {r['status']} "
            f"| {r['metric']} | {detail} |"
        )

    lines.append("")
    if is_releasable(results):
        lines.append("**RELEASABLE: yes** — all non-SKIP gates PASS.")
    else:
        non_passing = [r["gate"] for r in results if r["status"] in (STATUS_FAIL, STATUS_DEGRADED)]
        lines.append(
            f"**RELEASABLE: no** — {', '.join(non_passing)} need attention before cut."
        )

    lines.append("")
    lines.append(
        "_Generated by `tools/release_gates.py`. "
        "Post with: `python3 tools/release_gates.py --markdown | gh issue comment 689 --body-file -`_"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ENGRAM v0.1 release-readiness scorecard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--json", dest="json_out", action="store_true",
        help="Emit JSON output",
    )
    parser.add_argument(
        "--markdown", action="store_true",
        help="Emit markdown scorecard block (for posting to EPIC #689)",
    )
    parser.add_argument(
        "--gate", metavar="ID",
        help=f"Run only this gate. Valid: {', '.join(ALL_GATE_IDS)}",
    )
    parser.add_argument(
        "--skip-slow", dest="skip_slow", action="store_true",
        help="Skip G3a (the ~3 min pytest suite); mark it SKIP(--skip-slow)",
    )
    parser.add_argument(
        "--repo-root", dest="repo_root", metavar="PATH", default=None,
        help="Repo root (default: parent dir of tools/)",
    )
    parser.add_argument(
        "--python", metavar="PATH", default=None,
        help="Python interpreter for G3a/G3b/G1/G2 (default: venv python or sys.executable)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # Resolve repo root: parent of the tools/ directory this file lives in
    if args.repo_root:
        repo_root = os.path.abspath(args.repo_root)
    else:
        repo_root = str(Path(__file__).resolve().parent.parent)

    # Load config
    try:
        config = load_config(repo_root)
    except FileNotFoundError as e:
        print(f"ERROR: Could not load config: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"ERROR: Config JSON invalid: {e}", file=sys.stderr)
        return 1

    # Resolve Python interpreter
    python = resolve_python(repo_root, args.python)

    # Run gates
    try:
        results = run_scorecard(
            repo_root=repo_root,
            config=config,
            python=python,
            skip_slow=args.skip_slow,
            gate_filter=args.gate,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Output
    if args.json_out:
        print(format_json_output(results))
    elif args.markdown:
        print(format_markdown(results))
    else:
        print(format_human(results))

    return 0 if is_releasable(results) else 1


if __name__ == "__main__":
    sys.exit(main())
