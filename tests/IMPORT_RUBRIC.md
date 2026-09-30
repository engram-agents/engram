# Test import-pattern contract — `tests/IMPORT_RUBRIC.md`

Version: v1.1 (co-design complete 2026-06-07, Borges + Ariadne)

This rubric governs how test files in `tests/` declare and manage their
dependencies on repo modules. Wave fairies implementing #948 must satisfy
all five rules before a wave PR lands. The audit gates (`run_touched_tests.py
--audit-imports` and `--audit-patches`) enforce R2 and R3 mechanically; R1,
R4, and R5 are checked during PR review.

---

## R1 — Module-under-test is an import statement in the test file

**Statement:** Every test file imports the family module(s) it exercises. The
load-bearing inverse: a family-internals test file must contain **no `import server`
at all**, fixtures included. `ast.walk` is used for import detection and sees both
top-level and function-local `import server` statements.

**Why:** `run_touched_tests.py` uses a static import graph to select tests. A
family edit (e.g. `engram_focus.py`) triggers only the tests that import
`engram_focus`. Files that exercise `engram_focus` internals through
`import server` inside a `fresh_server()` fixture are invisible to the import
graph — they land in `server.py`'s closure instead, causing all ~83 such files
to be selected on any server.py edit. Under the contract, the import edge is
the selection edge.

**Two declared layers:**

- *Family-internals test*: imports `engram_<family>`, never `server`. Selected
  precisely when its family changes.
- *MCP-contract test*: imports `server`, exercises the `payload_json` wrapper
  surface. Named `test_*_payload.py` (see R2). Selected on `server.py` changes
  and conservatively on family changes (the wrapper forwards there).

**How to comply:**

```python
# WRONG — triggers the server.py closure on every server change
def fresh_server():
    import server  # function-local — still visible to ast.walk
    ...

# RIGHT — top-level import declares the dependency
import engram_focus

def test_get_focused_nodes():
    mod = fresh_module(engram_focus)  # from conftest.py
    conn = sqlite3.connect(":memory:")
    # call engram_focus functions directly
    ...
```

**How the gate checks it:** `run_touched_tests.py --audit-imports` identifies
all `tests/test_*.py` files whose AST contains an `import server` (or
`from server import ...`) statement. Files not matching `test_*_payload.py`
AND not in `server_import_allowlist` (see R2/R5) are flagged as violations.

---

## R2 — MCP-contract tests use the filename `test_*_payload.py`

**Statement:** Test files that import `server` and exercise the `payload_json`
MCP wrapper surface must follow the naming pattern `test_*_payload.py`.
This convention (the corpus already had 41 compliant files before wave 0) names
the mechanism being tested (the payload_json dispatch) and is the audit gate's
declaration mechanism for legitimate server imports.

Files that legitimately import server but cannot or should not use the `_payload`
suffix (e.g. `test_engram_core_seam.py`, which performs a static grep-gate on
server.py's source text) belong in a small explicit allow-list:

```json
{
  "server_import_allowlist": [
    "test_engram_core_seam.py",
    ...
  ]
}
```

Entries are **bare basenames** (e.g. `test_engram_core_seam.py`), not
`tests/`-prefixed paths — the gate matches `basename in allowlist`.

This allow-list lives in `src/engram/packaging/test-map.json` under the top-level key
`"server_import_allowlist"`. It is seeded at wave 0 with all current
non-`_payload` server-importing files so the gate passes immediately; each
subsequent wave drains it as files are converted or retired.

**Why:** Whole-file granularity matches how selection works. A file that
legitimately needs per-test granularity (some tests are internals, some are
MCP-contract) is itself an anti-pattern — the need for a per-test marker is
the signal to split the file.

**How to comply:**

```
# WRONG — server-importing file with no _payload suffix and not in allow-list
tests/test_engram_focus_stuff.py   (imports server)

# RIGHT — MCP-contract tests use the suffix
tests/test_focus_payload.py         (imports server, tests engram_focus MCP layer)

# RIGHT — legitimate residue in allow-list
tests/test_engram_core_seam.py      (listed in server_import_allowlist; static grep-gate)
```

**How the gate checks it:** `run_touched_tests.py --audit-imports` runs R2: for
every `tests/test_*.py` that imports `server`, the file must either match
`test_*_payload.py` OR appear in `src/engram/packaging/test-map.json`'s
`server_import_allowlist`. Violations print `file:line: not a payload file and
not in server_import_allowlist — add to allow-list or rename to
test_*_payload.py`.

---

## R3 — String-patch targets must be imported

**Statement:** A test that monkeypatches a family module by string
(`monkeypatch.setattr("engram_focus._get_db", ...)`) must also import that
module (`import engram_focus`). String patches are invisible to the static
import graph; without the import statement, the selection tool does not know
the test depends on `engram_focus`.

**Why:** Seam-migrated tests sometimes patch family modules by string for
isolation. Without a corresponding import statement, changing `engram_focus.py`
would not select the test that patches `engram_focus._get_db` by string, so
the regression would go unnoticed until the full suite run.

**How to comply:**

```python
# WRONG — string patch with no matching import
def test_something(monkeypatch):
    monkeypatch.setattr("engram_focus._get_db", lambda: mock_conn)
    # ... test body ...

# RIGHT — import declares the dependency; string patch is for isolation only
import engram_focus  # declares the dependency for the selection tool

def test_something(monkeypatch):
    monkeypatch.setattr("engram_focus._get_db", lambda: mock_conn)
    # ... test body ...
```

**How the gate checks it:** `run_touched_tests.py --audit-patches` runs R3: for
every `monkeypatch.setattr("X.…", ...)` call where the stem `X` is a known repo
module, `X` must appear in that file's top-level import set. Violations print
`file:line: string-patch target "X.attr" not imported — add "import X"`.

---

## R4 — The shared helper takes a module object, not a string

**Statement:** The shared conftest helper for fresh-module isolation is
`fresh_module(mod)` — the caller passes the imported module **object** (e.g.
`fresh_module(engram_focus)`), not a string (`fresh_module("engram_focus")`).
The helper does the `sys.modules` purge (server + engram_* family modules) +
re-import + `_configure_paths` dance and returns the reloaded module.

**Why:** A string-parameterized helper (`fresh_module("engram_focus")`) is
invisible to `ast.walk` — it would reintroduce the exact opacity the contract
removes. The import statement `import engram_focus` at the call site (necessary
to pass the module object) IS the AST-visible dependency edge.

**How to comply:**

```python
# In the test file — import at top level (AST-visible):
import engram_focus

# In a test or fixture:
def test_something():
    mod = fresh_module(engram_focus)  # module object, not "engram_focus"
    # mod is a freshly reloaded engram_focus with clean state
    ...
```

The `fresh_module` helper lives in `tests/conftest.py` but is a **plain function**,
not a `@pytest.fixture` — pytest's conftest auto-discovery applies to fixtures and
hooks, not plain helpers. Each test file that uses it must import it explicitly.

The purge is not permanent: the autouse module-scoped fixture
`_restore_sys_modules_per_file` in `tests/conftest.py` restores the original
`server` / `engram_*` entries at the end of each test file (#3225), so a later
file's import-time bindings are not orphaned. Within a file the fresh modules
persist.
Use the two-line pattern from the pilot (`tests/test_fts_retract_trigger.py`):

```python
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup
```

**Note on wave-0 conftest edits:** `conftest.py` is an always-full Phase-0
trigger in `run_touched_tests.py`, so the wave-0 PR's own touched-test run will
be a full run by design. Acceptance measurement for wave-0 must therefore use a
family module edit **after** conftest lands, not on the wave-0 PR itself — run:

```
# Edit one line in the target family module, then:
python3 tools/run_touched_tests.py --dry-run
# Record selected count; stash the edit to get the before-count.
```

---

## R5 — Non-imported surfaces stay in the test-map

**Statement:** Non-imported surfaces (hooks, skills, templates, build scripts,
shell tools) are not affected by this contract. They remain in
`src/engram/packaging/test-map.json`'s `mappings` array as layer-2 convention entries
with explicit glob-to-test mappings. The contract does not move them; it only
prevents the import layer from being polluted with server transitive closure.

**Why:** Some surfaces (e.g. `hooks/claude/engram-session-start-hook.py`) cannot
be imported by test files — they are scripts, not importable modules. The
convention map (Layer 2 of selection) correctly fills this gap. The contract
focuses on making Layer 1 (import graph) accurate for importable modules.

**How to comply:** When adding tests for a hook, skill, template, or build
script, add a `mappings` entry in `src/engram/packaging/test-map.json` pointing to the
test glob. Do NOT import the hook script from the test file to trigger Layer 1
selection — use the convention map entry.

---

## Falsifiable acceptance (per wave)

After wave N converts family F: a 1-line edit to `engram_<F>.py` →
`run_touched_tests --dry-run` selects `{F's internals tests + the MCP-contract
set + map residue}` and **none of the other families' internals**. Record the
selected count in the wave PR. Baseline before wave 0: 108 of 172 files
selected on a family edit. Each wave should produce a monotone decrease
toward `(|F's tests| + |contract set|)`. A wave that does not move the number
failed its acceptance — that is the deliverable, not a side effect.

The dry-run banner split is supported natively by `run_touched_tests.py
--dry-run` (via #949): the output breaks the selected set into the three native
buckets — internals, contract, and map-residue — so the per-wave acceptance
measurement is one command rather than a manual grep.
