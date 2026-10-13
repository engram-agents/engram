# launchctl fixtures for #3709 (REAL captures, not hand-typed)

Captured on macOS 26.6.2 (arm64), 2026-10-13, with

    launchctl print gui/$(id -u)/<label>      # stdout -> *.out, stderr -> *.err, exit status -> *.rc

User id 501 is left as captured. Every home path `/Users/<name>` was replaced by
`/Users/PLACEHOLDER_USER`; nothing else was edited, so TABS ARE LOAD-BEARING: the top-level
`state =` line is indented by exactly one tab, and the nested coalition dicts below it carry
deeper-indented `state = active` lines that a parser must NOT take.

| fixture | label captured | what it shows |
|---|---|---|
| `running.out` | `com.engram.viz-server` | `state = running`, no `last exit code` line (rc 0) |
| `not_running_exit0.out` | `com.engram.aleph-wake` | idle job that exited 0: `state = not running`, `last exit code = 0` (rc 0) |
| `spawn_scheduled_exit1.out` | `com.engram.test.exit1-16420` (3 s after bootstrap) | KeepAlive {SuccessfulExit:false}, ThrottleInterval 60, exited 1: `state = spawn scheduled`, `last exit code = 1` (rc 0) |
| `notfound.err` / `notfound.rc` | `com.engram.test.notfound-x` (never registered) | stdout empty, rc **113**, stderr `Bad request.` + `Could not find service "..." in domain for user gui: 501` |

The labels above are the ones that were captured; the cells that use these files only rely on
the text and exit status, never on the label in the file.
