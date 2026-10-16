# darwin_check fixtures

Real captures from a macOS 26.6.2 seat (lsof 4.91) running the installed rc4
`monitor-supervisor.py --purpose collab-loop` under a Monitor wrapper of
`/bin/zsh -c` (alpha #3330). They feed the `--check` ps backend through the
injectable runner (`CHECK_RUNNER`) in `tests/test_monitor_supervisor_check_darwin.py`.
Pinned verbatim, never tidied.

Files:

- `ps_snapshot.txt` -- `ps -U <uid> -o pid=,ppid=,stat=,args=` rows (one foreign
  macOS account's rows were filtered out by the capturer).
- `lsof_supervisor_pid65874.txt` -- `lsof -n -P -a -p 65874 -F pftdn`.
- `lsof_child_pid65876_fd0-2.txt` -- `lsof -n -P -a -p 65876 -d 0-2 -F pftdn`
  (the forum-updates child; no real captures exist for the other two children).
- `platform_versions.txt` -- `sw_vers -productVersion` and `lsof -v`.

The ONLY substitutions applied to the captures:

1. The capturing seat's home directory is replaced by `/Users/seat` (the seat
   home path).
2. The world URL is replaced by `https://world.example:5006`.
3. The shell-snapshot file name and the `/tmp` cwd file name are replaced by
   placeholder digits.

Pids, fd numbers and PIPE / unix addresses are unchanged. The bell row's
`aleph` argument is the world agent handle exactly as captured.
