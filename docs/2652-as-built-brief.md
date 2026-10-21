# EngramManager — as built, 2026-10-11

One page for Lei's re-read. What is on `dev` today (`97f2a499`), what is switched on, what is not, and the next three steps. The design is `docs/2652-engrammanager-design.md`; the channel-by-channel state is `docs/2652-migration-ledger.md`. Numbers here are copied from those two files and from the cited PR bodies, not re-measured for this page.

## In one paragraph

One process per seat, the **owner** (`src/engram/engramd_owner.py`), can hold `knowledge.db` open and answer reads over a Unix socket. Two hooks can read through it. Each hook has its own flag in `config.json`. **Every flag defaults to OFF, and no seat is known to have turned one on** (no seat's own config was checked for this page). With a flag OFF the old direct path runs unchanged. The owner starts on Linux only; on a Mac it refuses with a named reason.

## What is built (Phase 1; every unit in this table is merged to `dev`, U7 is still open, see below)

| unit | what it is | PR |
|---|---|---|
| U1 | wire contract: three result states, version check, request id | #3102 |
| U2 | write-operation census generator (walks call paths, not statements) | #3134 |
| U3 | owner skeleton: handshake, ping, `snapshot()` | #3151 |
| U4a | socket permission layer: 0700 dir, peer credentials, one deadline through execution | #3201 |
| U4b | the owner's two connections and the authorizer floor | #3221 |
| U4c | read-template registry (26 registered SQL shapes) and its build-time scanner | #3321 |
| U4d | first migrated read: user-identity hook, flag `engramd.user_identity_reads_via_owner` | #3341 |
| U5 | launcher: one `systemd --user` unit per `ENGRAM_HOME`. A SessionStart fallback that does nothing unless a flag is ON. One shared "is the flag on" helper. | #3672 |
| U6a | second migrated read: the Stop hook's node count, flag `engramd.stop_reads_via_owner` | #3675 |
| ledger | every `sqlite3.connect` channel, its state, its measured delta; a completeness guard runs in `pr-checks` | #3667, #3669 |

Hardening along the way: pid identity across reuse and clock drift (#3285), fail closed on Python below 3.11 (#3369), one shared platform-unsupported reason (#3375), the cross-uid rejection control (#3421).

## What is ON

Nothing known. The ledger counts 43 channels that reach the live store (at `21ec2944`). Of those: 3 are the owner's own, 39 are direct, 1 is behind a flag, **0 are flag-on, 0 retired**. U6a has since put the stop hook's read behind its own flag (ledger Section 3); it shares `_hooklib`'s connection opener, so the 43-channel count above is unchanged. The ledger counts tracked source; it cannot see a seat's own config.

Glossary for the next section. **Owner**: the one process that holds the database open. **A6**: the design's name for the hook-latency test. **Gate cell**: one measured pair of hook and store. **p50**: the median of 1000 calls. **Synthetic store**: a generated 5,000-node graph. **Real copy**: a copy of a seat's actual graph.

## The gate, and where it stands

The rule (design §6.1): a hook's owner round trip must not be slower than the same hook's direct read. Same store, same host, same build, warm cache. It is a ratio; under 1.0 passes.

Latest measurement: U7, PR #3686 (**open, not yet on `dev`**). Installed plugin layout. **The SQLite build is per seat**: it comes from the base Python of the seat's `~/.engram/venv`, and it differs today. Measured 10-11: Borges 3.53.0 (linuxbrew Python 3.14); Ariadne and Kepler 3.45.1 (system Python 3.12.3); the other Linux seats unread. The owner unit and the hooks use the same venv interpreter on a seat (`setup-surface-daemon-service.sh` fills the unit's Python from `ENGRAM_VENV_PYTHON`; `hooks.json` runs `~/.engram/venv/bin/python3`), so one seat's ratio does not mix builds.

Gate cells on SQLite 3.53.0 (Borges's seat):

| hook | synthetic 5k store | real graph copy |
|---|---|---|
| stop | 0.87× | 0.55× |
| user-identity | 0.74× | 0.71× |

All four gate cells are under 1.0×. Bounds: one host per build, sequential p50, N=1000; the two real-copy cells are two different graphs. The contended and cold-cache arms are measurements the design still owes; they are not in these four cells.

On SQLite 3.45.1 (Ariadne's seat, and Kepler's build): stop 0.89× synthetic / 0.85× real; user-identity 0.67× / **1.04×**. **That last cell is over the gate on that build**, by 0.013 ms. The rule is a ratio, not a tolerance, so on a 3.45.1 seat the user-identity flag does not pass §6.1 today; the stop flag does on both builds. The U7 ruling was to report this rather than widen U7 to chase it.

Why U7 existed (#3678): U6a's measurements showed the owner's own cost as a near-constant floor of about half a millisecond in every cell, which loses to the cheapest direct reads. U7 cut two things named by the profile: a new thread per connection, and encoding each row twice.

## What is NOT done

- No flag is on anywhere, so there is no production datum yet, only benchmarks.
- Mac seats (Aleph, Clio, Luria): the owner refuses to start. The Darwin permission arm is #3666, design first (Aleph).
- Hook reads still direct: lesson-tripwire (U6b #3665, Luria; 3 templates in the hook, plus the `engram_core` and `engram_idf` reads it makes through the same owner connection; branch not yet pushed); `_hooklib._focused_ids` (1); the surface hook (4); the session-start hook (8). The MCP server and the tools migrate last, per design §6.
- A test fix for U5 is open: #3685 (Sol) replaces a git-SHA pin that the squash merge broke with a frozen fixture. It is the fix for the red nightly of 10-11.
- Writes: no write goes through the owner, and the typed write-operation set is not yet enumerated (design §8).
- Phase 2, the schema-driven mechanisms and the domain engines inside the owner, has not started.

## Next three steps, in order

1. U7 #3686 merges (Ariadne built it; Kepler reviews). Then U6b #3665 (Luria builds; Sol reviews; Kepler does the design-conformance read).
2. **First flag ON in production, on one Linux seat**: `stop_reads_via_owner`, the flag that passes the gate on both SQLite builds, supervised by the U5 unit, observed for a day. This is the first datum that is not a benchmark. Not `user_identity_reads_via_owner` first: on a 3.45.1 seat its real-copy cell fails the gate.
3. The Darwin arm design (#3666), so the Mac seats do not stay on the direct path indefinitely.

## Your two calls

- Which Linux seat turns the first flag ON, and when. My proposal: Kepler's seat (SQLite 3.45.1, where the stop flag measures 0.89× and 0.85×), the day after U7 merges; or Borges's seat (3.53.0) if you want the build with the widest margin.
- Whether the write path starts before or after the remaining hook reads migrate.
