#!/bin/bash
# seat-run.sh -- a Mac-safe test runner for colleague reads [engram-alpha#3974].
#
# Usage:
#   seat-run.sh --pr N [--ref REF] [--keep] [--control] -- <pytest args, literal>
#   seat-run.sh --ref REF [--keep] [--control] -- <pytest args, literal>
#
# Why it exists: a colleague read must name the seat's configuration, and three
# instrument traps are silent on a Mac -- zsh does not word-split an unquoted
# flag variable (a fused "-o k=v" reaches pytest as one ignored argument),
# /usr/bin/python3 is 3.9 (collection fails on `dict | None`), and macOS has no
# GNU coreutils timeout. This tool prints the configuration as a header, reads
# every -o flag back from pytest itself, and refuses loudly.
#
# Exit codes (no others): 0 all named files green; 1 any failed/errors/crash;
# 2 tooling refusal or usage error; 5 a named file collects zero tests; 6 a
# requested -o flag did not land.
#
# Env: ENGRAM_PY (default: the seat venv python), SEAT_RUN_ROOT (default
# ${TMPDIR:-/tmp}). bash 3.2-safe: indexed arrays only, no GNU-only commands.
# Writes only under SEAT_RUN_ROOT (scratch worktree, removed on exit/INT/TERM).
#
# C8 covers the tool's OWN writes only. The pytest child inherits HOME and
# ENGRAM_HOME unchanged (setting them for the child can make tests fail that
# pass under plain pytest); only TMPDIR is redirected into the scratch dir.
# With no named test file in the pytest args (or none at all), the whole suite
# runs from the repo root of the scratch worktree and is reported as "(all)".
#
# ONE classifier (classify_pyargs) reads the pytest args once and decides, for
# every element, whether it is an option, the value of a value-taking option, a
# named test file, or another positional. The -o read-back, the zero-test
# guard and the per-file run all use its answer, because an element's meaning
# depends on the arity of the option before it and this tool reorders argv (the
# named file goes FIRST in the per-file run, so no option can swallow it).
# Two tables below (value_opt, flag_opt) give the arity of known options;
# `--opt=value` and attached short forms (-kexpr, -rs, -oK=V) are
# self-contained. An option in NEITHER table followed by something that looks
# like a file (`*.py` or `::`) is refused (exit 2): its arity is unknown.
# A named-file run that also has another positional (a directory) is refused.
# Quiet level: when the args ask for -q -q (or more), the tool adds one -v to
# the real run so pytest still prints the summary line the counts come from.
# A named-file run that exits 0 with NO parsable summary line (-qqq, or
# `-o addopts=-qqq`) fails closed: exit 1, RAW-TAIL, "no parsable summary".
#
# Paths stay inside the ref. Every positional / node id that is absolute or has
# a `..` path segment is refused (exit 2, "escapes the ref"): the per-file run
# happens in the scratch worktree, so such a token would run the user's own
# tree under a header naming a SHA that does not contain it. An option VALUE
# that is a path (-c --rootdir --confcutdir --basetemp --junitxml --ignore
# --log-file, `-o pythonpath=`) and is absolute or has `..` is legitimate
# (`--junitxml /tmp/out.xml`), so it is only flagged: one
# `WARN option value points outside the ref: <opt>=<value>` line.
# A fused single argv element `-o k=v` (what zsh hands over) is refused with
# exit 6 on sight, whatever the value: pytest sees it as one stray argument.
#
# Per-file runs differ from ONE pytest invocation over all the files:
# `--junitxml` keeps only the last file's report; `-x` / `--maxfail` stop per
# file, not globally; a `-k` that matches nothing in a later file makes that
# file exit 5. A cluster ending in a value option (`-xk expr`, `-xo k=v`) is
# treated as self-contained, so the value after it is a positional and is
# refused as ambiguous when a named file is also given.
#
# Channels the tool cannot classify (pytest receives arguments or paths through
# them without the tool seeing the words): an `@argsfile` positional is refused
# (exit 2: pytest reads its contents as more arguments); `-o addopts=<words>`
# and the environment variable PYTEST_ADDOPTS are split on whitespace and each
# word that is absolute or has `..` draws `WARN addopts word points outside the
# ref: <word>` (a WARN, not a refusal, like every option value). The header
# names every environment variable that changes pytest's arguments or plugins
# (`env PYTEST_ADDOPTS=...`, `env PYTEST_PLUGINS=...`) when it is set.
# `--co` / `--collect-only` with a named file prints `FILE <file> collect-only
# rc=0` (no counts: a collection listing has no passed/failed summary). An
# `(all)` run whose summary cannot be parsed reports `passed=unparsed`.
#
# Trust boundary (stated, not enforced): the ref's own contents are trusted. A
# symlink committed in the ref that points outside it is followed (the tool
# proves "this SHA ran", and the ref's author controls the link). A cluster
# ending in a value option (`-xc/abs`) gets no WARN for that value. A pytest
# plugin option of unknown arity (e.g. `--cov-config /abs`, `--html file`) must
# use the `--opt=value` form, or its value is refused as an escaping positional.

# A RELATIVE ENGRAM_HOME is refused (alpha #3863): it would resolve against the calling process's cwd, a
# different home for every process. Unset or empty means unset; a leading ~ is expanded against $HOME and
# re-exported (~user is not expanded: it is not absolute, so it is refused). Checked before anything else
# runs; `return` covers a script that is sourced, `exit` one that is executed.
if [ -n "${ENGRAM_HOME-}" ]; then
    _eh_raw="$ENGRAM_HOME"
    case "$ENGRAM_HOME" in
        "~") ENGRAM_HOME="${HOME-}"; export ENGRAM_HOME ;;
        "~/"*) ENGRAM_HOME="${HOME-}/${ENGRAM_HOME#"~/"}"; export ENGRAM_HOME ;;
    esac
    case "$ENGRAM_HOME" in
        /*) ;;
        *)
            printf "ENGRAM_HOME must be an absolute path, got '%s'\n" "$_eh_raw" >&2
            unset _eh_raw
            return 2 2>/dev/null || exit 2 ;;
    esac
    unset _eh_raw
fi

ENGRAM_PY=${ENGRAM_PY:-$HOME/.engram/venv/bin/python}
ROOT=${SEAT_RUN_ROOT:-${TMPDIR:-/tmp}}

PR=""
REF=""
KEEP=0
SCRATCH=""
WT=""
TOP=""
CLEANED=0
CHILD=""
RC=0

usage() {
  echo "usage: seat-run.sh (--pr N | --ref REF) [--keep] [--control] -- <pytest args>" >&2
}

refuse() { # refuse <exit-code> <one-line reason>
  echo "seat-run: $2" >&2
  exit "$1"
}

cleanup() {
  [ "$CLEANED" = 1 ] && return 0
  CLEANED=1
  if [ -n "$CHILD" ]; then
    kill "$CHILD" 2>/dev/null
    wait "$CHILD" 2>/dev/null
    CHILD=""
  fi
  if [ "$KEEP" = 1 ]; then
    [ -n "$SCRATCH" ] && echo "scratch: kept at $SCRATCH (remove: git worktree remove --force $WT)"
    return 0
  fi
  if [ -n "$WT" ] && [ -n "$TOP" ]; then
    git -C "$TOP" worktree remove --force "$WT" >/dev/null 2>&1
    git -C "$TOP" worktree prune >/dev/null 2>&1
  fi
  [ -n "$SCRATCH" ] && rm -rf "$SCRATCH"
  return 0
}
trap 'cleanup' EXIT
trap 'cleanup; exit 1' INT TERM HUP

# run_child <outfile> <cmd...>: run <cmd> from the scratch worktree as a
# BACKGROUND child and `wait` on it (bash 3.2: a trapped signal interrupts
# `wait`, whereas it is deferred until a foreground child exits), so the trap
# can kill the child and clean up promptly. Sets RC to the child's exit code.
run_child() {
  local outf=$1
  shift
  (cd "$WT" && exec "$@") >"$outf" 2>&1 &
  CHILD=$!
  wait "$CHILD"
  RC=$?
  CHILD=""
  return 0
}

# escapes <path>: absolute, or has a `..` path segment (not the ref's tree)
escapes() {
  case "$1" in /*) return 0 ;; esac
  case "/$1/" in */../*) return 0 ;; esac
  return 1
}
# warn_addopts <value>: split an addopts value (-o addopts=... or the env var
# PYTEST_ADDOPTS) on whitespace; WARN (never refuse) per word that escapes.
warn_addopts() {
  local w
  set -f # no pathname expansion while splitting
  for w in $1; do
    if escapes "$w"; then echo "WARN addopts word points outside the ref: $w"; fi
  done
  set +f
  return 0
}

# ---- args ------------------------------------------------------------------
PYARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --pr) [ $# -ge 2 ] || { usage; exit 2; }; PR=$2; shift 2 ;;
    --ref) [ $# -ge 2 ] || { usage; exit 2; }; REF=$2; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --control) shift ;; # the zero-test guard is on by default for named files
    --) shift; PYARGS=("$@"); break ;;
    *) usage; exit 2 ;;
  esac
done
if [ -n "$PR" ] && [ -n "$REF" ]; then usage; exit 2; fi
if [ -z "$PR" ] && [ -z "$REF" ]; then usage; exit 2; fi
case "$PR" in *[!0-9]*) usage; exit 2 ;; esac

# ---- header + preflight (C2, C3): nothing below runs pytest until it passes -
echo "host: $(uname -sm)"
echo "bash: $BASH_VERSION"

PY=$ENGRAM_PY
[ -x "$PY" ] || refuse 2 "ENGRAM_PY '$PY' is missing or not executable"
PYVER=$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)
echo "python: $PY $PYVER"
PYMAJ=${PYVER%%.*}
PYREST=${PYVER#*.}
PYMIN=${PYREST%%.*}
case "$PYMAJ$PYMIN" in
  ''|*[!0-9]*) refuse 2 "cannot read a Python version from '$PY' (got '$PYVER')" ;;
esac
if [ "$PYMAJ" -lt 3 ] || { [ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -lt 10 ]; }; then
  refuse 2 "python $PYVER at '$PY' is older than 3.10 (tests use 3.10 syntax and fail at collection)"
fi
PTVER=$("$PY" -c 'import pytest; print(pytest.__version__)' 2>/dev/null) ||
  refuse 2 "python $PYVER at '$PY' cannot import pytest"
echo "pytest: $PTVER"

TOP=$(git rev-parse --show-toplevel 2>/dev/null) || refuse 2 "not inside a git repository"
if [ -n "$PR" ]; then
  REFLABEL="refs/pull/$PR/head"
  git -C "$TOP" fetch -q origin "refs/pull/$PR/head" >/dev/null 2>&1 ||
    refuse 2 "cannot fetch refs/pull/$PR/head from origin"
  SHA=$(git -C "$TOP" rev-parse --verify -q FETCH_HEAD) || refuse 2 "fetched no commit for PR $PR"
else
  REFLABEL=$REF
  SHA=$(git -C "$TOP" rev-parse --verify -q "$REF^{commit}") || refuse 2 "ref '$REF' does not resolve to a commit"
fi
echo "ref: $REFLABEL"
echo "head: $SHA"
NL=$'\n'
NLESC='\n'
i=0
while [ $i -lt ${#PYARGS[@]} ]; do
  # a newline inside an element must not fake a header line
  echo "arg[$i]: ${PYARGS[$i]//$NL/$NLESC}"
  i=$((i + 1))
done

# environment variables that change pytest's arguments or plugins are part of
# the configuration a colleague read must name; set-but-empty counts as set
if [ -n "${PYTEST_ADDOPTS+x}" ]; then
  echo "env PYTEST_ADDOPTS=${PYTEST_ADDOPTS//$NL/$NLESC}"
  warn_addopts "$PYTEST_ADDOPTS"
fi
if [ -n "${PYTEST_PLUGINS+x}" ]; then
  echo "env PYTEST_PLUGINS=${PYTEST_PLUGINS//$NL/$NLESC}"
fi

# ---- scratch worktree (C1) --------------------------------------------------
[ -d "$ROOT" ] || refuse 2 "SEAT_RUN_ROOT '$ROOT' is not a directory"
SCRATCH=$(mktemp -d "${ROOT%/}/seat-run.XXXXXX") || refuse 2 "cannot create a scratch dir under '$ROOT'"
WT="$SCRATCH/wt"
mkdir "$SCRATCH/tmp" "$SCRATCH/plug"
git -C "$TOP" worktree add --detach -q "$WT" "$SHA" >/dev/null 2>&1 ||
  refuse 2 "cannot create a scratch worktree for $SHA"
# children (pytest tmp_path) must not write outside the scratch
export TMPDIR="$SCRATCH/tmp"

# ---- the ONE classifier over the pytest args --------------------------------
# The tables were derived from pytest 9.1.1's own parser (every core option,
# argparse nargs: 0 = flag, none = value). Options with an OPTIONAL value
# (nargs "?": --debug, --cache-show) are in NEITHER table on purpose: their
# arity depends on what follows, so a file after them is refused as ambiguous.
# -n is xdist's (a plugin), kept because it is common. Plugin options (e.g.
# pytest-cov) are not listed; an unknown option followed by a file is refused.
value_opt() { # options that take a value as the NEXT element (arity 1)
  case "$1" in
    -o | --override-ini | -k | -m | -p | -c | --config-file | --rootdir | \
      --confcutdir | --ignore | --ignore-glob | --deselect | --junitxml | \
      --junit-xml | --junitprefix | --junit-prefix | --basetemp | -W | \
      --pythonwarnings | --maxfail | --max-warnings | --durations | \
      --durations-min | --import-mode | --tb | --color | --capture | \
      --code-highlight | --show-capture | --verbosity | --report-chars | \
      --pdbcls | --pastebin | --assert | --doctest-report | --doctest-glob | \
      --lfnf | --last-failed-no-failures | --log-level | --log-cli-level | \
      --log-format | --log-date-format | --log-cli-format | \
      --log-cli-date-format | --log-file | --log-file-mode | \
      --log-file-level | --log-file-format | --log-file-date-format | \
      --log-auto-indent | --log-disable | -r | -n) return 0 ;;
  esac
  return 1
}
flag_opt() { # options that take no value
  case "$1" in
    -q | -v | -vv | -vvv | -x | -s | -l | -V | --co | --collect-only | \
      --collectonly | --lf | --ff | --nf | --sw | --no-header | --no-summary | \
      --quiet | --verbose | --exitfirst | --showlocals | --no-showlocals | \
      --strict | --strict-markers | --strict-config | --markers | \
      --disable-warnings | --disable-pytest-warnings | --last-failed | \
      --failed-first | --new-first | --stepwise | --stepwise-skip | \
      --stepwise-reset | --sw-skip | --sw-reset | --cache-clear | --runxfail | \
      --setup-show | --setupshow | --setup-only | --setuponly | --setup-plan | \
      --setupplan | --trace | --pdb | --fixtures | --funcargs | \
      --fixtures-per-test | --keep-duplicates | --keepduplicates | \
      --continue-on-collection-errors | --collect-in-virtualenv | --noconftest | \
      --pyargs | --disable-plugin-autoload | --traceconfig | --trace-config | \
      --no-fold-skipped | --force-short-summary | --xfail-tb | --fulltrace | \
      --full-trace | --doctest-modules | --doctest-ignore-import-errors | \
      --doctest-continue-on-failure | -h | --help | --version | --) return 0 ;;
  esac
  return 1
}

# note_path_opt <opt> <value>: WARN (never refuse) for a path-valued option
# whose value points outside the ref.
note_path_opt() {
  case "$1" in
    -c | --config-file | --rootdir | --confcutdir | --basetemp | --junitxml | \
      --junit-xml | --ignore | --log-file) ;;
    *) return 0 ;;
  esac
  if escapes "$2"; then echo "WARN option value points outside the ref: $1=$2"; fi
  return 0
}
# note_path_ini <key> <value>: the same for a path-valued -o ini key
note_path_ini() {
  case "$1" in
    pythonpath | testpaths | cache_dir) ;;
    addopts) warn_addopts "$2"; return 0 ;;
    *) return 0 ;;
  esac
  if escapes "$2"; then echo "WARN option value points outside the ref: $1=$2"; fi
  return 0
}

# classify_pyargs fills FILES (named test files), OTHER (everything else, in
# the original order), REQ_KEYS/REQ_VALS (every -o/--override-ini key=value)
# and VERBOSITY (net -v minus -q). It refuses (exit 2) when the args are
# ambiguous. Needs $WT (a named file must exist in the ref).
FILES=()
OTHER=()
REQ_KEYS=()
REQ_VALS=()
VERBOSITY=0
COLLECT_ONLY=0 # --co / --collect-only given: a listing, not a run
classify_pyargs() {
  local i=0 n=${#PYARGS[@]} a f spec expect="" unknown="" npos=0 firstpos="" q fk fv
  while [ $i -lt "$n" ]; do
    a=${PYARGS[$i]}
    i=$((i + 1))
    if [ -n "$expect" ]; then # the value of a value-taking option
      OTHER[${#OTHER[@]}]=$a
      case "$expect" in
        -o | --override-ini)
          case "$a" in
            *=*)
              REQ_KEYS[${#REQ_KEYS[@]}]=${a%%=*}; REQ_VALS[${#REQ_VALS[@]}]=${a#*=}
              note_path_ini "${a%%=*}" "${a#*=}"
              ;;
          esac
          ;;
        *) note_path_opt "$expect" "$a" ;;
      esac
      expect=""
      continue
    fi
    if [ -n "$unknown" ]; then
      case "$a" in
        *.py | *::*) refuse 2 "ambiguous: option '$unknown' (arity unknown) is followed by '$a'; put files first or use --opt=value" ;;
      esac
      unknown=""
    fi
    case "$a" in
      "-o "*) # one fused element, as zsh hands it over: refused on sight, any value
        spec=${a#-o }
        case "$spec" in
          *=*) fk=${spec%%=*}; fv=${spec#*=} ;;
          *) fk=$spec; fv="" ;;
        esac
        refuse 6 "-o flag did not land: $fk(requested=$fv effective=<none: one fused argv element '$a' is not a -o flag>)"
        ;;
      --override-ini=*)
        spec=${a#--override-ini=}
        case "$spec" in
          *=*)
            REQ_KEYS[${#REQ_KEYS[@]}]=${spec%%=*}; REQ_VALS[${#REQ_VALS[@]}]=${spec#*=}
            note_path_ini "${spec%%=*}" "${spec#*=}"
            ;;
        esac
        OTHER[${#OTHER[@]}]=$a
        ;;
      --*=*)
        note_path_opt "${a%%=*}" "${a#*=}"
        OTHER[${#OTHER[@]}]=$a
        ;;
      -o?*)
        spec=${a#-o}
        case "$spec" in
          *=*)
            REQ_KEYS[${#REQ_KEYS[@]}]=${spec%%=*}; REQ_VALS[${#REQ_VALS[@]}]=${spec#*=}
            note_path_ini "${spec%%=*}" "${spec#*=}"
            ;;
        esac
        OTHER[${#OTHER[@]}]=$a
        ;;
      -c?*) # attached short form: -c/path
        note_path_opt -c "${a#-c}"
        OTHER[${#OTHER[@]}]=$a
        ;;
      -)
        OTHER[${#OTHER[@]}]=$a
        ;;
      -*)
        OTHER[${#OTHER[@]}]=$a
        case "$a" in
          --quiet) VERBOSITY=$((VERBOSITY - 1)) ;;
          --verbose) VERBOSITY=$((VERBOSITY + 1)) ;;
          --co | --collect-only | --collectonly) COLLECT_ONLY=1 ;;
          -[qv]*) # -q, -qq, -v, -vv: a cluster of one letter only
            q=${a#-}
            case "$q" in
              *[!q]*) case "$q" in *[!v]*) ;; *) VERBOSITY=$((VERBOSITY + ${#q})) ;; esac ;;
              *) VERBOSITY=$((VERBOSITY - ${#q})) ;;
            esac
            ;;
        esac
        if value_opt "$a"; then
          expect=$a
        elif flag_opt "$a"; then
          :
        else
          case "$a" in
            --*) unknown=$a ;; # a long option of unknown arity
            -?) unknown=$a ;;  # a single short option of unknown arity
            *) : ;;            # -kexpr, -rs, -xq...: attached / clustered, self-contained
          esac
        fi
        ;;
      *)
        case "$a" in
          @*) refuse 2 "positional '$a' is an argsfile; its contents cannot be classified (name the files on the command line)" ;;
        esac
        f=${a%%::*}
        if escapes "$f"; then
          refuse 2 "positional '$a' escapes the ref (absolute or '..' path); name a path inside the ref"
        fi
        case "$f" in
          *.py) if [ -f "$WT/$f" ]; then
            FILES[${#FILES[@]}]=$a
          else
            OTHER[${#OTHER[@]}]=$a
            npos=$((npos + 1)); firstpos=${firstpos:-$a}
          fi ;;
          *)
            OTHER[${#OTHER[@]}]=$a
            npos=$((npos + 1)); firstpos=${firstpos:-$a}
            ;;
        esac
        ;;
    esac
  done
  if [ ${#FILES[@]} -gt 0 ] && [ "$npos" -gt 0 ]; then
    refuse 2 "ambiguous: positional '$firstpos' alongside named test file '${FILES[0]}'; each file runs on its own, so name files only or no files"
  fi
  return 0
}

# ---- C4: read every -o flag back from pytest itself -------------------------
classify_pyargs

# norm_bool <v>: pytest's spellings of true/false, else the input unchanged
norm_bool() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    1 | true | t | yes | y | on) echo true ;;
    0 | false | f | no | n | off) echo false ;;
    *) printf '%s\n' "$1" ;;
  esac
}
realp() { "$PY" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null; }

# landed <key> <requested> <effective>: did the request land, after the
# normalization pytest itself applies (bool spellings, path keys by realpath,
# list keys by membership)? The raw string compare is only the first test.
landed() {
  local k=$1 want=$2 got=$3 line item w
  [ "$got" = "$want" ] && return 0
  case "$got" in
    true | false) [ "$(norm_bool "$want")" = "$got" ] && return 0 ;;
  esac
  while IFS= read -r line; do
    case "$line" in
      "SEATRUN_ITEM $k="*)
        item=${line#"SEATRUN_ITEM $k="}
        [ "$item" = "$want" ] && return 0
        case "$item" in
          /*)
            case "$want" in /*) w=$want ;; *) w="$WT/$want" ;; esac
            [ "$(realp "$item")" = "$(realp "$w")" ] && return 0
            ;;
        esac
        ;;
    esac
  done <"$SCRATCH/probe.out"
  return 1
}

if [ ${#REQ_KEYS[@]} -gt 0 ]; then
  cat >"$SCRATCH/plug/seat_run_ini_probe.py" <<'PYEOF'
import os


def pytest_configure(config):
    for key in os.environ.get("SEAT_RUN_KEYS", "").split("\n"):
        if not key:
            continue
        try:
            v = config.getini(key)
        except Exception:
            print("SEATRUN_INI %s=<unregistered>" % key)
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, (list, tuple)):
            for x in v:  # one line per element, so membership is exact
                print("SEATRUN_ITEM %s=%s" % (key, str(x).replace("\n", " ")))
            v = " ".join(str(x) for x in v)
        print("SEATRUN_INI %s=%s" % (key, str(v).replace("\n", " ")))


def pytest_collection(session):
    return True  # configure-only probe: skip collection and any test run
PYEOF
  KEYS=$(printf '%s\n' "${REQ_KEYS[@]}")
  run_child "$SCRATCH/probe.out" env SEAT_RUN_KEYS="$KEYS" \
    PYTHONPATH="$SCRATCH/plug${PYTHONPATH:+:$PYTHONPATH}" \
    "$PY" -m pytest -p seat_run_ini_probe "${PYARGS[@]}"
  BAD=""
  i=0
  while [ $i -lt ${#REQ_KEYS[@]} ]; do
    k=${REQ_KEYS[$i]}
    want=${REQ_VALS[$i]}
    line=$(grep -F "SEATRUN_INI $k=" "$SCRATCH/probe.out" | head -n 1)
    if [ -n "$line" ]; then got=${line#"SEATRUN_INI $k="}; else got="<none>"; fi
    echo "ini $k=$got (effective)"
    landed "$k" "$want" "$got" || BAD="$BAD $k(requested=$want effective=$got)"
    i=$((i + 1))
  done
  [ -z "$BAD" ] || refuse 6 "-o flag did not land:$BAD"
fi

# ---- C5: zero-test guard, before any real run -------------------------------
# (the classifier above already named the files; the same OTHER args apply, so
# a -c / -o python_files / -k decides collection here exactly as in the run)
for a in ${FILES[@]+"${FILES[@]}"}; do
  run_child "$SCRATCH/collect.out" "$PY" -m pytest --collect-only -q "$a" ${OTHER[@]+"${OTHER[@]}"}
  if [ "$RC" -eq 5 ]; then
    refuse 5 "named file '$a' collects 0 tests"
  fi
done

# ---- C6/C7: run per file, report, never fold skipped into passed -----------
TP=0; TF=0; TS=0; TE=0; NF=0; UNPARSED=0
run_one() { # run_one <label> [file-arg]
  label=$1
  OUTF="$SCRATCH/run.out"
  # tool-owned additions: -rs (skip reasons) and, only for -q -q or quieter,
  # one -v so pytest still prints the summary line. The named file goes
  # FIRST, ahead of every user option, so no option can swallow it.
  extra=(-rs)
  [ "$VERBOSITY" -le -2 ] && extra=(-rs -v)
  if [ $# -ge 2 ]; then
    run_child "$OUTF" "$PY" -m pytest "${extra[@]}" "$2" ${OTHER[@]+"${OTHER[@]}"}
  else
    run_child "$OUTF" "$PY" -m pytest "${extra[@]}" ${OTHER[@]+"${OTHER[@]}"}
  fi
  rc=$RC
  sumline=$(grep -E '[0-9]+ (passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)|no tests ran' "$OUTF" | grep -E ' in [0-9.]+s' | tail -n 1)
  if [ $rc -eq 5 ]; then refuse 5 "'$label' ran zero tests"; fi
  p=$(printf '%s' "$sumline" | grep -Eo '[0-9]+ passed' | head -n 1 | grep -Eo '^[0-9]+')
  f=$(printf '%s' "$sumline" | grep -Eo '[0-9]+ failed' | head -n 1 | grep -Eo '^[0-9]+')
  s=$(printf '%s' "$sumline" | grep -Eo '[0-9]+ skipped' | head -n 1 | grep -Eo '^[0-9]+')
  e=$(printf '%s' "$sumline" | grep -Eo '[0-9]+ errors?' | head -n 1 | grep -Eo '^[0-9]+')
  p=${p:-0}; f=${f:-0}; s=${s:-0}; e=${e:-0}
  # a crash is a nonzero exit with no failure/error token; rc 0 never is one
  # ... and a NAMED file that exits 0 with no parsable summary line is treated
  # the same (fail closed: its counts cannot be read, so they are not "0")
  crash=0
  if [ $rc -ne 0 ] && [ $((f + e)) -eq 0 ]; then crash=1; fi
  if [ -z "$sumline" ] && [ $rc -eq 0 ] && [ "$label" != "(all)" ]; then crash=1; fi
  # --co / --collect-only on a named file: rc 0 with a listing and no summary
  # is neither a crash nor a pass; report it as what it is, with no counts
  if [ "$COLLECT_ONLY" -eq 1 ] && [ $rc -eq 0 ] && [ "$label" != "(all)" ]; then
    echo "FILE $label collect-only rc=0"
    NF=$((NF + 1))
    return 0
  fi
  # an (all) run with nothing parsable must not read as "0 passed"
  if [ -z "$sumline" ] && [ "$label" = "(all)" ]; then UNPARSED=1; fi
  if [ $crash -eq 1 ]; then e=$((e + 1)); fi
  echo "FILE $label passed=$p failed=$f skipped=$s errors=$e"
  grep '^SKIPPED' "$OUTF" | sed 's/^/  /'
  if [ "$label" != "(all)" ] && [ "$p" -eq 0 ] && [ "$s" -gt 0 ]; then
    echo "WARN named file ran 0 tests (all skipped): $label"
  fi
  if [ -z "$sumline" ] && [ $rc -eq 0 ] && [ $crash -eq 0 ]; then
    echo "WARN no pytest summary line for $label (exit 0); counts above are not parsed"
  fi
  if [ $crash -eq 1 ]; then
    echo "RAW-TAIL $label (pytest exit $rc, no parsable summary)"
    tail -n 25 "$OUTF"
  fi
  TP=$((TP + p)); TF=$((TF + f)); TS=$((TS + s)); TE=$((TE + e)); NF=$((NF + 1))
}

if [ ${#FILES[@]} -gt 0 ]; then
  for a in "${FILES[@]}"; do run_one "$a" "$a"; done
else
  run_one "(all)"
fi
SHOWP=$TP
[ "$UNPARSED" -eq 1 ] && SHOWP=unparsed
echo "SUMMARY files=$NF passed=$SHOWP failed=$TF skipped=$TS errors=$TE"
if [ $((TF + TE)) -gt 0 ]; then exit 1; fi
exit 0
