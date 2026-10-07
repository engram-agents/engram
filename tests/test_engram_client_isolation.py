"""Regression guard for #1679 — family-module staleness across EngramClient re-instantiation.

EngramClient.__init__ re-imports `server` fresh on every instantiation, purging
`server` + `engram_core` from sys.modules so multiple EngramClient instances with
different db_dirs don't share path state (see #894 / test_two_clients_two_dirs_isolated
in test_engram_core_seam.py, which covers server.py's OWN `core` binding).

But server.py also imports a set of "family" modules at its own module level
(engram_focus, engram_recall_summaries, engram_trust, engram_lifecycle,
engram_epistemic, engram_cornerstone, engram_derive, engram_tasks,
engram_revision, engram_stats, engram_query, engram_observation, as of this
writing), and every one of those family modules does `import engram_core as
core` at ITS OWN module level (the project's canonical shared-state-access
convention — see any family module's docstring).

If the purge only drops `server` + `engram_core`, a second EngramClient
re-imports `server` fresh, but when server.py's module body re-executes e.g.
`import engram_stats as _stats_mod`, Python's import system finds
`engram_stats` ALREADY in sys.modules (never purged) and returns the CACHED
module object — which still holds `core` bound to the FIRST client's now-stale
engram_core module, not the freshly re-imported one. Calls routed through a
family module on the second client can then silently read/write against the
first client's DATA_DIR instead of the second client's.

This test constructs client A, then client B with a different db_dir, and
checks a family module's OWN `core` reference (not server.py's) directly —
the exact seam the #894 fix (server+engram_core purge only) did not cover.
It is written to FAIL against the pre-fix purge tuple (server, engram_core
only) and PASS once the family modules are included in the purge.

FAMILY_MODULES below is derived DYNAMICALLY by scanning src/engram/engram_*.py
for the `import engram_core as core` line (MODULE-LEVEL only: a function-local
import re-resolves on every call and cannot hold a stale `core`, so it is
deliberately outside the family set, #3527), rather than hand-enumerated — an
enumerated list here would be exactly the kind of silently-stale duplicate
that caused #1679 in the first place (the production purge used to be a
hardcoded list too; it's now prefix-based in engram_client.py, but a
hand-maintained list in the test would still drift out of sync with reality
even though production no longer needs one). See also
test_purge_matches_prefix_not_enumerated_list below, which proves the
production purge is prefix-based (not tied to today's known module set) by
purging a synthetic engram_* module that isn't a real family module at all.
"""

import re
import sys
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
if str(SRC_ENGRAM) not in sys.path:
    sys.path.insert(0, str(SRC_ENGRAM))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engram_client import EngramClient  # noqa: E402


def _discover_family_modules(src_dir: pathlib.Path = SRC_ENGRAM) -> list[str]:
    """Scan src/engram/engram_*.py for modules that bind `import engram_core
    as core` at module level — the project's canonical shared-state-access
    convention (see module docstring above). Derived at collection time so
    this list can't silently go stale as family modules are added/removed,
    unlike a hand-maintained enumeration.
    """
    pattern = re.compile(r"^import\s+engram_core\s+as\s+core\s*$", re.MULTILINE)
    found = []
    for path in sorted(src_dir.glob("engram_*.py")):
        if path.stem in ("engram_client", "engram_core"):
            continue
        if pattern.search(path.read_text()):
            found.append(path.stem)
    return found


FAMILY_MODULES = _discover_family_modules()


def test_discovery_excludes_function_local_core_imports(tmp_path):
    """#3527: only a MODULE-LEVEL `import engram_core as core` makes a family
    module. A function-local one re-resolves on every call and cannot hold a
    stale `core`, so a module whose only such import is indented is excluded.
    """
    (tmp_path / "engram_toplevel.py").write_text(
        "import engram_core as core\n\n\ndef f():\n    return core\n"
    )
    (tmp_path / "engram_lazy.py").write_text(
        "def f():\n    import engram_core as core\n    return core\n"
    )
    assert _discover_family_modules(tmp_path) == ["engram_toplevel"]


def test_family_module_core_reference_tracks_latest_client(tmp_path):
    """A family module's own `core` binding must point at the LATEST client's
    engram_core, not a stale one left over from an earlier EngramClient.

    Reproduces #1679: instantiate client A (dir_a), then client B (dir_b) in
    the same process. Directly introspect engram_stats's own `core` reference
    (the family module routes engram_stats.py's tool calls) — it must reflect
    dir_b, not dir_a.

    NOTE: each check below does a fresh `import engram_stats` and reads
    `sys.modules["engram_stats"]` immediately before asserting, rather than
    holding a local variable across the two client constructions. A plain
    `import` statement rebinds its local name to whatever object currently
    sits in sys.modules — after the FIX (which purges + re-imports
    engram_stats too), that object identity legitimately changes across
    client constructions. Holding an earlier local reference would therefore
    compare against a stale, purged-out module in the post-fix world, which
    is a test-harness artifact, not the bug under test. Re-reading
    sys.modules fresh at each check point is what makes the assertion mean
    "what does calling into engram_stats resolve to right now."
    """
    dir_a = tmp_path / "client-a"
    dir_b = tmp_path / "client-b"

    EngramClient(db_dir=str(dir_a))

    # Sanity: right after client A, the family module's core should track dir_a.
    assert str(sys.modules["engram_stats"].core.DATA_DIR) == str(dir_a), (
        "sanity check failed — engram_stats.core.DATA_DIR did not track "
        "client A's db_dir immediately after construction"
    )

    EngramClient(db_dir=str(dir_b))

    # The bug: without purging engram_stats (and its siblings), server.py's
    # re-import of "engram_stats as _stats_mod" returns the CACHED module
    # object from client A's construction, whose `core` attribute still
    # points at client A's (purged-and-replaced) engram_core module — so
    # engram_stats.core.DATA_DIR silently stays at dir_a even though a new
    # client for dir_b was just constructed.
    assert str(sys.modules["engram_stats"].core.DATA_DIR) == str(dir_b), (
        "engram_stats.core.DATA_DIR still points at client A's dir "
        f"({sys.modules['engram_stats'].core.DATA_DIR}) after constructing "
        f"client B for {dir_b} — the family module survived the purge with "
        "a stale `core` reference (#1679)."
    )


def test_all_family_modules_track_latest_client(tmp_path):
    """Broader sweep: every one of the 12 family modules must re-bind `core`
    on each new EngramClient instantiation, not just engram_stats.
    """
    dir_a = tmp_path / "client-a2"
    dir_b = tmp_path / "client-b2"

    EngramClient(db_dir=str(dir_a))
    EngramClient(db_dir=str(dir_b))

    stale = []
    for mod_name in FAMILY_MODULES:
        mod = sys.modules.get(mod_name)
        assert mod is not None, f"{mod_name} not found in sys.modules after client construction"
        if str(mod.core.DATA_DIR) != str(dir_b):
            stale.append((mod_name, str(mod.core.DATA_DIR)))

    assert not stale, (
        "the following family modules retained a stale `core.DATA_DIR` "
        f"(expected {dir_b}) after constructing a second EngramClient:\n"
        + "\n".join(f"  - {name}: {seen}" for name, seen in stale)
    )


def test_two_clients_family_module_call_routes_to_correct_dir(tmp_path):
    """End-to-end: a tool call on client B that routes through a family
    module (engram_stats -> engram_stats.py) must operate against dir_b's
    DB, not dir_a's — the indirect, observable-behavior version of the
    direct `core` introspection above.
    """
    dir_a = tmp_path / "client-a3"
    dir_b = tmp_path / "client-b3"

    client_a = EngramClient(db_dir=str(dir_a))
    client_b = EngramClient(db_dir=str(dir_b))

    # engram_diagnose routes through engram_stats.py's _diagnose_impl.
    result_b = client_b.call("engram_diagnose", {})
    assert result_b is not None

    # The family module backing this call must see dir_b as its DATA_DIR.
    import engram_stats
    assert str(engram_stats.core.DATA_DIR) == str(dir_b), (
        "engram_diagnose on client B routed through a family module still "
        f"pointed at {engram_stats.core.DATA_DIR} instead of {dir_b}"
    )

    # client_a's own server+core pair must remain untouched.
    assert str(client_a._server.core.DATA_DIR) == str(dir_a)


def test_family_modules_discovered_nonempty():
    """Sanity guard on the discovery helper itself: if a future refactor
    changes the `import engram_core as core` convention (or moves the family
    modules out of src/engram/), FAMILY_MODULES would silently become empty
    and the two sweep tests above would vacuously pass over zero modules.
    Fail loudly instead.
    """
    assert len(FAMILY_MODULES) >= 10, (
        f"expected at least 10 family modules discovered via the "
        f"`import engram_core as core` scan, found {len(FAMILY_MODULES)}: "
        f"{FAMILY_MODULES} — either the family-module convention changed "
        "(update the discovery pattern) or something broke the scan."
    )


def test_purge_matches_prefix_not_enumerated_list(tmp_path):
    """Proves the production purge in EngramClient.__init__ is PREFIX-based
    (any `engram_*` module except `engram_client` itself), not tied to an
    enumerated list of today's known family modules — the exact generality
    gap #1679 exposed (the original purge tuple silently fell out of sync as
    family modules were added over #872 waves 2-9).

    Inserts a synthetic `engram_zzz_test_family` module into sys.modules
    that isn't a real family module and was never part of any purge list,
    gives it a `core` attribute pointing at a sentinel, then constructs a
    fresh EngramClient. If the purge is genuinely prefix-based, the
    synthetic module must be purged from sys.modules (its stale `core`
    reference can't survive) — proving the fix generalizes to ANY future
    engram_* module, not just the ones enumerated today.
    """
    import types

    dir_a = tmp_path / "client-prefix-a"

    fake_mod = types.ModuleType("engram_zzz_test_family")
    fake_mod.core = types.SimpleNamespace(DATA_DIR="stale-sentinel-value")
    sys.modules["engram_zzz_test_family"] = fake_mod

    try:
        EngramClient(db_dir=str(dir_a))

        assert "engram_zzz_test_family" not in sys.modules, (
            "a synthetic engram_* module survived EngramClient.__init__'s "
            "purge — the purge is not genuinely prefix-based (or its prefix "
            "match is too narrow), which means a real future family module "
            "could silently survive the purge the same way #1679's modules did."
        )
    finally:
        # Cleanup: don't leak the synthetic module into other tests even if
        # the assertion above failed.
        sys.modules.pop("engram_zzz_test_family", None)


def test_engram_client_module_survives_its_own_purge(tmp_path):
    """Proves the self-exclusion: EngramClient.__init__ must NOT purge
    "engram_client" itself out of sys.modules. Purging it mid-__init__ would
    orphan the currently-executing module's cache entry — a subsequent
    `import engram_client` elsewhere in the process would re-execute the
    module body and mint a NEW EngramClient class object, breaking
    isinstance checks against instances created before the purge.

    Deliberately does NOT rely on the `EngramClient` name imported at this
    test module's collection time — other test files in this suite use
    conftest.py's `fresh_module` helper, which (unlike this file's own
    dynamic-purge production code) does NOT exclude "engram_client" from ITS
    purges, so sys.modules["engram_client"] can legitimately point to a
    DIFFERENT module object than the one this file imported at collection
    time by the time this test runs — a cross-test-file ordering effect
    unrelated to what's under test here. Re-resolving `engram_client` fresh
    at the START of this test (immediately before constructing the client)
    isolates the assertion to what EngramClient.__init__'s OWN purge loop
    does across a single call, independent of suite-order module reloads.
    """
    dir_a = tmp_path / "client-self-exclusion-a"

    import engram_client as ec_mod
    mod_before = sys.modules["engram_client"]
    cls_before = ec_mod.EngramClient

    client_before = cls_before(db_dir=str(dir_a))

    mod_after = sys.modules.get("engram_client")
    assert mod_after is mod_before, (
        "engram_client's own sys.modules entry changed identity across a "
        "single EngramClient.__init__ call — the purge loop is evicting "
        "'engram_client' itself, which it must not do (self-exclusion "
        "required)."
    )
    assert isinstance(client_before, mod_after.EngramClient), (
        "the EngramClient instance no longer isinstance-matches "
        "engram_client's current class — engram_client was purged out "
        "from under its own executing frame during construction."
    )
