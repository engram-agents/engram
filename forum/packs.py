"""Pack validation and storage for the forum pack registry.

Validation pipeline (run server-side before accepting a pack upload):
  1. Package shape: knowledge.sql + scripts/ + README present.
  2. Closure invariant: rebuild knowledge.sql into a temp DB; run
     _assert_closure_completeness from engram-pkg (imported, not copied).
  3. Size guard: node_count <= MAX_NODES, edge_count <= MAX_EDGES (mirrors
     engram-pkg defaults).

Storage layout:
  <packs_dir>/<pack_id>/
      package.tar.gz   — the uploaded tarball, stored as-is
      meta.json        — author, name, version, uploaded_at, closure stats

Pack-id = <author>-<name>-v<N> where N = 1 + max existing version for
same author+name.  Non-alphanumeric chars in author/name are slugified to
hyphens for filesystem safety.

Import pattern for engram-pkg helpers: uses the same importlib loader as
tests/test_scope_export.py's _import_scope_export_helpers — one source,
zero drift.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Size-guard thresholds — mirror engram-pkg defaults.
# ---------------------------------------------------------------------------
MAX_NODES = 200
MAX_EDGES = 400


# ---------------------------------------------------------------------------
# Thread-safety: serialise the sys.exit patch window.
#
# _validate_closure_completeness temporarily replaces sys.exit globally so
# that _assert_closure_completeness (which calls sys.exit on violation) can
# be caught and converted to PackValidationError.  Flask runs threaded by
# default; two concurrent uploads racing through the patch window can
# interleave like this:
#
#   Thread A: original_exit_A = sys.exit      # sys.exit is real exit
#   Thread B: original_exit_B = sys.exit      # sys.exit is real exit
#   Thread A: sys.exit = _patched_A
#   Thread B: sys.exit = _patched_B           # overwrites A's patch
#   Thread A: finally → sys.exit = original_exit_A  # restores real exit
#   Thread B: finally → sys.exit = original_exit_B  # also restores (fine here)
#
#   BUT if Thread B's finally runs first and restores real exit, then Thread A's
#   finally restores real exit — harmless.  The dangerous case is if Thread A
#   grabs original_exit_A = _patched_B (another thread's stub) and restores
#   that, leaving sys.exit pointing to the _CaptureExit-raising stub forever.
#
# Serialising with this lock makes the patch window atomic: each thread sees
# the real sys.exit when it captures, and restores the real sys.exit in its
# finally.  Validation is I/O-bound anyway (DB rebuild), so the lock
# contention on a LAN-only registry is negligible.
#
# Long-term fix: refactor engram-pkg's _assert_closure_completeness to raise a
# typed exception instead of calling sys.exit, eliminating the patch entirely.
# That refactor is tracked in issue #651's thread (engram-pkg exception-refactor).
# ---------------------------------------------------------------------------
_CLOSURE_CHECK_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# engram-pkg helpers import (reuse, don't copy)
# ---------------------------------------------------------------------------

_CLI_MODULE = None  # lazy-loaded


def _load_engram_pkg_cli():
    """Import the engram-pkg CLI as a module (cached after first call).

    Uses the same importlib loader pattern as tests/test_scope_export.py
    _import_scope_export_helpers() so there is one source for the convention.
    """
    global _CLI_MODULE
    if _CLI_MODULE is not None:
        return _CLI_MODULE

    # Walk up from this file to locate the repo root, then find the CLI.
    here = Path(__file__).resolve()
    repo_root = here.parent.parent  # forum/ -> repo root
    cli_path = repo_root / "tools" / "engram-pkg" / "engram-pkg"

    if not cli_path.exists():
        raise FileNotFoundError(
            f"engram-pkg CLI not found at {cli_path}. "
            "Cannot run closure validation."
        )

    spec = importlib.util.spec_from_loader(
        "engram_pkg_cli",
        importlib.machinery.SourceFileLoader("engram_pkg_cli", str(cli_path)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _CLI_MODULE = mod
    return mod


# ---------------------------------------------------------------------------
# Pack-id helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(s: str) -> str:
    """Lower-case and replace non-alphanumeric runs with hyphens."""
    s = s.lower()
    s = _SLUG_RE.sub("-", s)
    return s.strip("-") or "pack"


def make_pack_id(author: str, name: str, version: int) -> str:
    """Construct a pack id: <slugified-author>-<slugified-name>-v<N>."""
    return f"{_slugify(author)}-{_slugify(name)}-v{version}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------

class PackValidationError(ValueError):
    """Raised with a human-readable reason when a pack upload is rejected."""


# ---------------------------------------------------------------------------
# Validation pipeline
# ---------------------------------------------------------------------------

def _validate_package_shape(extract_dir: Path) -> None:
    """Check that the extracted package contains the required files.

    Required (per engram-pkg format):
      - knowledge.sql
      - scripts/ directory
      - README.md (any case variant accepted)

    Raises PackValidationError with a specific message on any violation.
    """
    missing = []

    if not (extract_dir / "knowledge.sql").exists():
        missing.append("knowledge.sql")

    if not (extract_dir / "scripts").is_dir():
        missing.append("scripts/ directory")

    readme_names = {"README.md", "README", "readme.md"}
    has_readme = any((extract_dir / n).exists() for n in readme_names)
    if not has_readme:
        missing.append("README.md")

    if missing:
        raise PackValidationError(
            f"Package shape invalid — missing: {', '.join(missing)}. "
            f"A valid engram-package must contain knowledge.sql, scripts/, and README.md."
        )


def _validate_closure_completeness(extract_dir: Path) -> tuple[int, int]:
    """Rebuild knowledge.sql into a temp DB and run the closure invariant check.

    Reuses engram-pkg's _assert_closure_completeness so the invariant has one
    home and cannot drift.

    Returns (node_count, edge_count) on success.
    Raises PackValidationError if the invariant is violated.
    Raises PackValidationError if knowledge.sql cannot be loaded.
    """
    cli = _load_engram_pkg_cli()

    sql_path = extract_dir / "knowledge.sql"
    sql_content = sql_path.read_text(encoding="utf-8")

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_db = f.name
    try:
        conn = sqlite3.connect(tmp_db)
        try:
            conn.executescript(sql_content)
            conn.row_factory = sqlite3.Row

            # Load all nodes and edges into the shape _assert_closure_completeness expects.
            try:
                node_rows = conn.execute("SELECT * FROM nodes").fetchall()
                edge_rows = conn.execute("SELECT * FROM edges").fetchall()
            except sqlite3.OperationalError as exc:
                raise PackValidationError(
                    f"knowledge.sql does not contain the expected schema "
                    f"(missing 'nodes' or 'edges' table): {exc}"
                )

            nodes = {dict(r)["id"]: dict(r) for r in node_rows}
            edges = [dict(r) for r in edge_rows]

        finally:
            conn.close()
    finally:
        try:
            os.unlink(tmp_db)
        except OSError:
            pass

    # Run the closure-completeness invariant from engram-pkg.
    # _assert_closure_completeness raises SystemExit on violation; we catch
    # that and convert it to PackValidationError so the server can return 400.
    #
    # The patch window is serialised with _CLOSURE_CHECK_LOCK (module-level).
    # Without the lock, concurrent Flask threads can interleave the
    # capture/restore and leave sys.exit permanently pointing to a stub after
    # both threads exit.  See _CLOSURE_CHECK_LOCK's comment for the full race.
    import sys as _sys

    class _CaptureExit(Exception):
        def __init__(self, msg):
            self.msg = msg

    def _patched_exit(msg=""):
        raise _CaptureExit(str(msg))

    with _CLOSURE_CHECK_LOCK:
        original_exit = _sys.exit
        _sys.exit = _patched_exit
        try:
            cli._assert_closure_completeness(nodes, edges)
        except _CaptureExit as exc:
            raise PackValidationError(
                f"Closure-completeness invariant violated: {exc.msg}"
            )
        finally:
            _sys.exit = original_exit

    return len(nodes), len(edges)


def _validate_size_guard(node_count: int, edge_count: int) -> None:
    """Enforce the same size limits as engram-pkg scope-export.

    Raises PackValidationError if either limit is exceeded.
    """
    if node_count > MAX_NODES:
        raise PackValidationError(
            f"Pack exceeds node size guard ({node_count} > {MAX_NODES} max). "
            f"Re-export with a narrower root set."
        )
    if edge_count > MAX_EDGES:
        raise PackValidationError(
            f"Pack exceeds edge size guard ({edge_count} > {MAX_EDGES} max). "
            f"Re-export with a narrower root set."
        )


def validate_pack(tarball_path: Path) -> dict[str, Any]:
    """Run the full validation pipeline on an uploaded pack tarball.

    Steps:
      1. Extract the tarball to a temp dir.
      2. Validate package shape (knowledge.sql + scripts/ + README).
      3. Rebuild knowledge.sql, run closure-completeness invariant.
      4. Enforce size guard.
      5. Load manifest.json (if present) for metadata.

    Returns a dict with keys:
      name, root_count, node_count, edge_count

    Raises PackValidationError with a human-readable reason on any failure.
    """
    with tempfile.TemporaryDirectory(prefix="forum_pack_validate_") as tmpdir:
        extract_dir = Path(tmpdir) / "pkg"
        extract_dir.mkdir()

        # Extract the tarball.
        try:
            with tarfile.open(tarball_path, "r:gz") as tf:
                # Security: prevent path traversal and symlink attacks.
                # Belt-and-braces: manual checks come first (work on all Python
                # versions), then filter="data" (Python 3.12+) rejects anything
                # the manual pass might miss (symlinks, device files, absolute
                # paths) and silences the Python 3.14 DeprecationWarning.
                for member in tf.getmembers():
                    member_path = Path(member.name)
                    # Reject absolute paths and path components that escape the
                    # extract_dir (e.g. ../../etc/passwd).
                    if member_path.is_absolute():
                        raise PackValidationError(
                            f"Tarball contains absolute path: {member.name!r}. "
                            "Refusing to extract."
                        )
                    resolved = (extract_dir / member.name).resolve()
                    try:
                        resolved.relative_to(extract_dir.resolve())
                    except ValueError:
                        raise PackValidationError(
                            f"Tarball contains path-traversal entry: {member.name!r}. "
                            "Refusing to extract."
                        )
                    # Reject symlinks and hard links explicitly: a symlink whose
                    # linkname points outside extract_dir is a separate traversal
                    # vector that the name check above does not catch.
                    if member.issym() or member.islnk():
                        raise PackValidationError(
                            f"Tarball contains (sym)link entry: {member.name!r}. "
                            "Refusing to extract."
                        )
                # filter="data" (Python 3.12+): rejects symlinks, device files,
                # and absolute paths at the tarfile layer — defence in depth on
                # top of the manual checks above, and silences the Python 3.14
                # DeprecationWarning about the default filter changing.
                try:
                    tf.extractall(extract_dir, filter="data")
                except TypeError:
                    # Python < 3.12 does not accept the filter= kwarg.
                    tf.extractall(extract_dir)  # manual checks above are the guard
        except tarfile.TarError as exc:
            raise PackValidationError(
                f"Tarball cannot be extracted: {exc}. "
                "Upload a valid .tar.gz created by `tar czf`."
            )

        # If the tarball has a single top-level directory, descend into it.
        # (Common pattern: `tar czf pack.tar.gz my-pack/` creates a top-level dir.)
        top_level = list(extract_dir.iterdir())
        if len(top_level) == 1 and top_level[0].is_dir():
            pkg_dir = top_level[0]
        else:
            pkg_dir = extract_dir

        # Step 2: package shape.
        _validate_package_shape(pkg_dir)

        # Step 3: closure completeness (also returns counts).
        node_count, edge_count = _validate_closure_completeness(pkg_dir)

        # Step 4: size guard.
        _validate_size_guard(node_count, edge_count)

        # Step 5: load manifest.json for metadata (optional but expected for
        # scope-export packs).
        # The pack name is always the top-level directory name inside the
        # tarball — manifest.json carries no "name" field (verified against
        # engram-pkg's scope-export manifest writer).
        name = pkg_dir.name
        root_count = 0

        manifest_path = pkg_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                root_ids = manifest.get("root_node_ids", [])
                root_count = len(root_ids) if isinstance(root_ids, list) else 0
            except (json.JSONDecodeError, OSError):
                pass  # manifest is optional; use fallback values

        return {
            "name": name,
            "root_count": root_count,
            "node_count": node_count,
            "edge_count": edge_count,
        }


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def store_pack(packs_dir: Path, pack_id: str, tarball_path: Path) -> Path:
    """Store the uploaded pack tarball and write meta.json.

    Creates <packs_dir>/<pack_id>/ and copies the tarball as package.tar.gz.

    Returns the pack directory path.
    """
    pack_dir = packs_dir / pack_id
    pack_dir.mkdir(parents=True, exist_ok=True)

    dest = pack_dir / "package.tar.gz"
    shutil.copy2(str(tarball_path), str(dest))

    return pack_dir


def write_pack_meta(
    pack_dir: Path,
    pack_id: str,
    author: str,
    name: str,
    version: int,
    uploaded_at: str,
    root_count: int,
    node_count: int,
    edge_count: int,
) -> None:
    """Write meta.json alongside the stored tarball."""
    meta = {
        "id": pack_id,
        "author": author,
        "name": name,
        "version": version,
        "uploaded_at": uploaded_at,
        "root_count": root_count,
        "node_count": node_count,
        "edge_count": edge_count,
    }
    (pack_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
