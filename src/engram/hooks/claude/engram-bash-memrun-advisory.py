#!/usr/bin/env python3
"""
PreToolUse hook for Bash: warns (never blocks) when a memory-heavy command is
about to start without the host's `memrun` gate in front of it.

THE PROBLEM (engram-alpha#4435 item 2)
======================================
A heavy job started bare from a session shell lives in the session's cgroup.
On a host whose systemd runs OOMPolicy=stop, an OOM kill there stops the whole
session; `memrun` runs the job in its own scope instead.

WHAT THIS HOOK DOES
===================
When the command contains a HEAVY SEGMENT with no memrun prefix, it emits one
advisory through `_hooklib.emit_context()` (bounded by the harness budget):
the tag `[memrun-advisory]` and the matched shape, the host's MemAvailable
read from /proc/meminfo, and either the memrun line (only when the FILE
/home/agents-shared/bin/memrun exists and is executable) or a plain
"no host gate installed here" line. It never blocks: always exit 0, no
decision key. Any error, unparseable stdin, non-dict tool_input, non-string or
empty command, or a command shlex cannot split -> exit 0 with no output.

HEAVY SEGMENTS
==============
A segment is one simple command between `;`, `&&`, `||`, `|`, `&`, `(`, `)`
or a newline (found with shlex, so quoted text is never read as a command:
`echo "pytest -n 4"` and `git log --grep rembg` are silent). The command word
is the first word after any `VAR=value` prefixes. Heavy:
  a. `pytest` (or `python`/`python3` ... `-m pytest`) WITH `-n <x>`, `-n<x>`,
     `-n=<x>` or `--numprocesses[= ]<x>`;
  b. `-m ltx_pipelines` or `-m ltx_pipelines.<anything>` in a python segment;
  c. `rembg`, or `-m rembg` in a python segment.
`xargs -P`/`--max-procs` and a trailing `&` do NOT fire on their own; they
matter only because the segment they wrap is heavy (`ls *.png | xargs -P4
-n1 rembg i` fires via c). The hook looks through xargs's options to the
command it runs.
`-n 0` / `-n 1` (and `--numprocesses=0|1`, `-n0`, `-n=1`) disable xdist and
are silent; `-n 2`, `-n auto`, `-n logical` fire.

TRANSPARENT WRAPPERS (looked through before classifying)
========================================================
`env [-i] [-u NAME]... [VAR=val]...`, `timeout [opts] DURATION`,
`nice [-n N | -N]`, `time [-p]`, `command`, `exec`, `sudo [opts]` (option
tokens skipped, plus the argument of the usual value-taking ones),
`uv run [opts]`, `poetry run`, `pipenv run`, and `bash|sh|zsh -c <string>`
(also `-lc`, `-ec`, ...): the string is re-split with the same splitter and
each segment classified, recursion depth <= 3. A heredoc (`<<` / `<<-`) ends
segmenting: the rest of the command is its body, not commands.

KNOWN SILENT (bounds, deliberately not handled)
===============================================
`tox -- -n 4`, `make test-parallel` (a heavy command hidden behind a build
tool), backtick command substitution, double-quoted `"$(...)"` and process
substitution `<( ... )`, shell functions and aliases, `bash script.sh`
(script contents are not read), `-n` bundled into other short flags, and
anything inside a heredoc body. NOT silent: an unquoted `$(pytest -n 4)` and
a `( ... )` subshell fire, because the parentheses are separators and the
inner command is classified (measured on `echo $(pytest -n 4)` and
`x=$(pytest -n 4)`).

LENGTH CAP
==========
A command longer than 200,000 characters makes the hook exit 0 silently
before any splitting. A single huge token can outrun the 3 s hook timeout
(measured: a 1 MB single token took 6.3 s in shlex), and no real heavy
command line is that long, so the advisory loses nothing.

MEMRUN PREFIX
=============
A heavy segment is covered when its command word is `memrun` or a path ending
in `/memrun` (the heavy command follows memrun's own `--`). If ANY heavy
segment is uncovered the hook fires; if all are covered it is silent.

TEST KNOBS (env)
================
  ENGRAM_MEMRUN_PATH   path probed for the memrun file
                       (default /home/agents-shared/bin/memrun)
  ENGRAM_MEMINFO_PATH  meminfo file (default /proc/meminfo)
The memrun probe is os.access(path, X_OK) on the FILE, never PATH: PreToolUse
runs in a non-login shell where /home/agents-shared/bin is not on PATH.

TIER: T2 (Convenience) -- advisory only, recoverable.
"""

import json
import os
import re
import shlex
import sys

DEFAULT_MEMRUN = "/home/agents-shared/bin/memrun"
DEFAULT_MEMINFO = "/proc/meminfo"
MAX_COMMAND_CHARS = 200_000

_SEP_CHARS = set(";&|\n()")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PY_RE = re.compile(r"^python[0-9.]*$")
_N_RE = re.compile(r"^-n=?.+$")
# xargs options whose value is the NEXT token when written separately.
_XARGS_SHORT_ARG = set("PnILsdEa")
_XARGS_LONG_ARG = {"--max-procs", "--max-args", "--max-lines", "--delimiter",
                   "--arg-file", "--eof", "--max-chars"}


def _split_segments(command):
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|\n()<>")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    segments, cur = [], []
    for tok in lex:
        if tok.startswith("<<") and not tok.startswith("<<<"):
            break  # heredoc: everything after is body text, not commands
        if tok and all(c in _SEP_CHARS for c in tok):
            if cur:
                segments.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


def _strip_env(seg):
    i = 0
    while i < len(seg) and _ENV_RE.match(seg[i]):
        i += 1
    return seg[i:]


def _unwrap_xargs(args):
    """Return the command xargs would run (its options skipped)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a.startswith("--"):
            i += 2 if (a in _XARGS_LONG_ARG) else 1
        elif len(a) == 2 and a[1] in _XARGS_SHORT_ARG:
            i += 2
        else:
            i += 1
    return args[i:]


def _n_enables(val):
    """xdist is off for -n 0 / -n 1; anything else (auto, logical, 2, ...) is on."""
    return val.strip() not in ("0", "1")


def _has_n_flag(args):
    for i, a in enumerate(args):
        if a == "-n":
            if i + 1 < len(args) and _n_enables(args[i + 1]):
                return True
        elif _N_RE.match(a) and not a.startswith("--"):
            if _n_enables(a[2:].lstrip("=")):
                return True
        elif a == "--numprocesses":
            if i + 1 < len(args) and _n_enables(args[i + 1]):
                return True
        elif a.startswith("--numprocesses=") and len(a) > len("--numprocesses="):
            if _n_enables(a.split("=", 1)[1]):
                return True
    return False


def _skip_opts(args, short_arg="", long_arg=()):
    """Skip leading option tokens (and the value of the value-taking ones)."""
    i = 0
    while i < len(args) and args[i].startswith("-") and len(args[i]) > 1:
        a = args[i]
        if a.startswith("--"):
            i += 2 if a in long_arg else 1
        elif len(a) == 2 and a[1] in short_arg:
            i += 2
        else:
            i += 1
    return args[i:]


_SUDO_LONG = ("--user", "--group", "--host", "--prompt", "--close-from", "--role",
              "--type", "--other-user", "--chdir", "--chroot")
_UV_LONG = ("--with", "--python", "--project", "--directory", "--env-file", "--group",
            "--extra", "--with-requirements", "--package", "--index")


def _unwrap(base, args):
    """If `base args` is a transparent wrapper, return the wrapped command, else None."""
    if base == "env":
        return _skip_opts(args, "uS", ("--unset", "--split-string", "--chdir"))
    if base == "timeout":
        rest = _skip_opts(args, "ks", ("--kill-after", "--signal"))
        return rest[1:]  # drop DURATION
    if base == "nice":
        return _skip_opts(args, "n", ("--adjustment",))
    if base in ("time", "command"):
        return _skip_opts(args)
    if base == "exec":
        return _skip_opts(args, "a")
    if base == "sudo":
        return _skip_opts(args, "ugphCrtUDRT", _SUDO_LONG)
    if base == "uv" and args[:1] == ["run"]:
        return _skip_opts(args[1:], "pw", _UV_LONG)
    if base in ("poetry", "pipenv") and args[:1] == ["run"]:
        return _skip_opts(args[1:])
    return None


def _shell_c_string(base, args):
    """For `bash|sh|zsh [-lc ...] <string>` return the -c string, else None."""
    if base not in ("bash", "sh", "zsh"):
        return None
    for i, a in enumerate(args):
        if not a.startswith("-") or a.startswith("--") or len(a) < 2:
            return None  # a script file or operand reached before any -c
        if a.endswith("c") and a[1:].isalpha():
            return args[i + 1] if i + 1 < len(args) else None
    return None


def classify(seg, depth=0):
    """Return (shape, segment_tokens) for a heavy uncovered segment, else None."""
    orig = seg
    seg = _strip_env(seg)
    if not seg:
        return None
    word, args = seg[0], seg[1:]
    base = word.rsplit("/", 1)[-1]
    if base == "memrun":
        return None  # covered: whatever follows is memrun's own payload
    if base == "xargs":
        r = classify(_unwrap_xargs(args), depth)
        return (r[0], orig) if r else None
    inner = _unwrap(base, args)
    if inner is not None:
        r = classify(inner, depth)
        return (r[0], orig) if r else None
    string = _shell_c_string(base, args)
    if string is not None:
        if depth >= 3:
            return None
        try:
            segs = _split_segments(string)
        except ValueError:
            return None
        for s2 in segs:
            r = classify(s2, depth + 1)
            if r is not None:
                return r
        return None
    if base == "rembg":
        return ("rembg", orig)
    if base == "pytest":
        return ("pytest -n", orig) if _has_n_flag(args) else None
    if _PY_RE.match(base):
        mod = None
        for i, a in enumerate(args):
            if a == "-m" and i + 1 < len(args):
                mod = args[i + 1]
                break
        if mod == "pytest" and _has_n_flag(args):
            return ("pytest -n", orig)
        if mod == "ltx_pipelines" or (mod or "").startswith("ltx_pipelines."):
            return ("ltx_pipelines", orig)
        if mod == "rembg":
            return ("rembg", orig)
    return None


def find_uncovered(command):
    """Return (shapes, first_uncovered_segment_tokens)."""
    shapes, first = [], None
    for seg in _split_segments(command):
        r = classify(seg)
        if r is not None:
            if r[0] not in shapes:
                shapes.append(r[0])
            if first is None:
                first = r[1]
    return shapes, first


def _mem_line():
    path = os.environ.get("ENGRAM_MEMINFO_PATH") or DEFAULT_MEMINFO
    try:
        with open(path, "r") as fh:
            text = fh.read()
        vals = {}
        for line in text.splitlines():
            m = re.match(r"^(MemTotal|MemAvailable):\s+(\d+)\s*kB", line)
            if m:
                vals[m.group(1)] = int(m.group(2))
        gib = 1024.0 * 1024.0
        return "MemAvailable: %.1f GiB of %.1f GiB" % (
            vals["MemAvailable"] / gib, vals["MemTotal"] / gib)
    except Exception:
        return "MemAvailable: unknown on this platform"


def build_advisory(seg, shapes):
    memrun = os.environ.get("ENGRAM_MEMRUN_PATH") or DEFAULT_MEMRUN
    head = "[memrun-advisory] heavy command without memrun (%s)\n%s" % (
        ", ".join(shapes), _mem_line())
    cmd_show = shlex.join(seg)
    if len(cmd_show) > 200:
        cmd_show = cmd_show[:200] + "..."
    if os.path.isfile(memrun) and os.access(memrun, os.X_OK):
        gate = ("prefix the heavy part: %s --need <measured peak> -- %s\n"
                "measure --need with /usr/bin/time -v, never guess" % (memrun, cmd_show))
    else:
        gate = ("no host gate installed here; check free memory before starting "
                "several of these at once")
    why = ("Why: a heavy job started bare from a session shell lives in the session's "
           "cgroup, and on a host whose systemd runs OOMPolicy=stop an OOM kill there "
           "can stop the whole session; memrun runs the job in its own scope.")
    return head, [gate, why]


def main():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        from _hooklib import emit_context
        from _harness import detect_harness, get_harness_budgets

        hook_input = json.loads(sys.stdin.read())
        if not isinstance(hook_input, dict):
            return
        tool_input = hook_input.get("tool_input") or hook_input.get("input") or {}
        if not isinstance(tool_input, dict):
            return
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return
        if len(command) > MAX_COMMAND_CHARS:
            return  # length cap: see the module docstring
        shapes, seg = find_uncovered(command)
        if not shapes:
            return
        head, body = build_advisory(seg, shapes)
        budget = get_harness_budgets(detect_harness())["budget"]
        text = emit_context(head, body, budget=budget, target=min(1800, budget),
                            allow_single_overflow=True)
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "additionalContext": text}}))
    except Exception:
        return


if __name__ == "__main__":
    main()
    sys.exit(0)
