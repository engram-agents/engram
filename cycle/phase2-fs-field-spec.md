# Phase 2 — F-S as an Independent Field: Implementation Spec

**Status:** Pre-implementation spec (Phase-1 gate not yet cleared)
**Lane:** Luria (F-S driver)
**Prerequisite:** Phase-1 standpoint v3 merged + observed-dream-pass (charter §4.3)
**Gate:** D1 (`spec-D1-fs-independent-field.md`) — already in charter §5

---

## What Phase 2 adds

Phase 1 derives F-S from `quote_type` at read time and never persists it (the
`_node_fs_class` proxy). Phase 2 introduces a real `fs_class` field — a first-class
independent measurement that the user provides at observation-filing time.

The seam contract from `standpoint-v3-design.md §4.2` is the load-bearing invariant:
`_node_fs_class` swaps its internals to read the real field first, falls back to proxy
if null. Zero caller changes — every surface that reads F-S (FALSIFICATION line,
⚠⚠ escalation) automatically upgrades from proxy to native data.

---

## 1. Schema change

Additive, NULL-tolerant per the safety envelope (charter §4.2):

```sql
ALTER TABLE nodes ADD COLUMN fs_class TEXT;
-- Accepted values: 're-executable' | 'frozen' | NULL
-- NULL = Phase-1 proxy applies (backward-compat)
-- No migration of existing rows — Phase-1 proxy covers all NULL cases
```

No existing row receives a value. Phase 2 starts populating on new observations only.
The proxy is still correct for all NULLs, so old nodes need no retroactive tagging
(this is the never-persisted property from Phase 1, paying off).

---

## 2. `engram_add_observation` signature change

New optional parameter:

```
fs_class: "re-executable" | "frozen" | None (default)
```

**Semantics:**
- `"re-executable"` — Class 1: the claim can be re-tested by re-running the
  underlying measurement or experiment. Reality can still push back.
- `"frozen"` — Class 2: the claim records a past event or quote that cannot be
  re-executed; only quote-checking remains. Reality has spoken; the record is frozen.
- `None` — user does not specify; `_node_fs_class` falls back to the Phase-1 proxy.

**D1 compliance (override rubric):**
- Explicit `fs_class` takes priority over everything — it IS the primary determinant.
- When absent, `quote_type` is the fallback prior (unchanged from Phase 1).
- The `_node_fs_class` accessor must return `source="field"` when the column is
  populated, so the FALSIFICATION line drops the `(proxy:quote_type)` label:
  `FALSIFICATION: 1/2 re-executable` (no proxy label) vs Phase-1's
  `FALSIFICATION: 1/2 re-executable-leaning (proxy:quote_type)`.

**Validation:** accepted values only. Any other string is a filing-time error with
a redirecting message naming the valid set (charter §7.3 principle).

---

## 3. `_node_fs_class` seam swap

```python
def _node_fs_class(conn: sqlite3.Connection, node_id: str) -> tuple[str, str]:
    """Returns (fs_class, source). Phase-2 reads native field; falls back to proxy."""
    row = conn.execute(
        "SELECT fs_class, quote_type FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    if not row:
        return ("unknown", "proxy:quote_type")
    # Phase-2 path: native field present
    if row["fs_class"] in ("re-executable", "frozen"):
        return (row["fs_class"], "field")
    # Phase-1 fallback: derive from quote_type proxy
    if not row["quote_type"]:
        return ("unknown", "proxy:quote_type")
    return (_FS_PROXY_MAP.get(row["quote_type"], "unknown"), "proxy:quote_type")
```

**Zero caller changes**: every existing caller of `_node_fs_class` receives
`(fs_class, source)` tuples in the same shape. Surfaces that check `source` for
the proxy label will automatically stop appending `(proxy:quote_type)` when
native data is present — the conditional at the FALSIFICATION line is already there.

**Phase-2 note from ob_0140 / fold-round-3 review**: `any_proxy` detection at the
FALSIFICATION block should scan `known_fs` (not all `fs_classes`) to correctly reflect
whether any KNOWN-classified premise used the proxy. In Phase 2, a premise with
`fs_class=NULL` still returns `source="proxy:quote_type"` — scanning all premises
would mislabel an all-field-sourced set as proxy due to NULL premises. Fix at
Phase-2 implementation time:

```python
# Phase-2 form: scan known_fs, not fs_classes
any_proxy = any(s.startswith("proxy:") for c, s in known_fs)
```

---

## 4. Test requirements (D3 dual-probe discipline)

Per charter §5 / `spec-D3-dual-probe-discipline.md`, every new classifier needs both
probe directions before it can be trusted:

**Known-good control probe (new-field direction):**
- File an observation with `fs_class="re-executable"`.
- `_node_fs_class` returns `("re-executable", "field")`.
- FALSIFICATION line reads `1/1 re-executable` (no proxy label).
- Guards against a classifier that always outputs "proxy."

**Known-bad probe (proxy-fallback direction):**
- File an observation WITHOUT `fs_class` (NULL).
- `_node_fs_class` returns `(proxy_result, "proxy:quote_type")`.
- FALSIFICATION line reads `…-leaning (proxy:quote_type)`.
- Guards against a classifier that always outputs "field."

**Mixed-premise probe (any_proxy precision):**
- File a derivation with one `fs_class`-native premise and one NULL premise.
- `any_proxy` scans `known_fs` only → fires False (all known premises have native data).
- Guards against the ob_0140 / Phase-2-note failure mode.

**Proxy-label DROP test (test-8 Phase-2 mirror, Ariadne #41/606):**
- Phase-1 test 8 pins the PRESENCE of the proxy label when all premises are proxy-sourced.
- Phase-2 mirror: file a derivation where ALL premises have native `fs_class` data.
- FALSIFICATION line must read `re-executable` with NO `(proxy:quote_type)` label.
- Guards against the proxy label persisting after the seam swap (stale-label failure mode).

**Parity pin (like stats↔diagnose for health_score):**
- `_node_fs_class` returns `("field", "field")` iff `fs_class` column is populated
  — same logic in both accessor path and any direct-DB check. Not two code paths.

---

## 5. Mira's consolidated round scope

This spec is Phase-2 scope and enters Mira's consolidated package only if Phase 2
ships before her review round. The D1 spec is already in her package via charter §5.
If Phase 2 is the off-ramp point, Mira reads the D1 spec; this full spec is not yet
in scope.

---

## 6. What Phase 2 does NOT change

- No confidence mutation (F-S is advisory, like standpoint — charter §4.6 safety envelope).
- No retroactive re-scoring of existing derivations.
- No change to any other field (standpoint axes, quote_type, source_class).
- No fairy delegation — this is a substrate-semantic change, driver-written per charter §4.6.
- The frozen stability metric (`_compute_health_score`) reads no F-S surface — unchanged.
