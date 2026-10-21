#!/usr/bin/env bash
# engramd-owner-home-key.sh -- the ONE derivation of the per-ENGRAM_HOME key the engramd
# owner's supervisor names are built from (engram-alpha#3709, ruling Q1).
#
#   key = first 8 hex chars of sha256 over the NORMALIZED home:
#         "~" / "~/..." expanded against $HOME, made physical (`cd && pwd -P`, so every
#         symlink -- macOS /var -> /private/var is the live case -- is resolved), and
#         with trailing slashes stripped.
#
# Three consumers, and none of them keeps its own copy of this derivation:
#   * tools/setup-surface-daemon-service.sh --owner  -> the systemd unit name
#                                                       engramd-owner-<key>.service AND the
#                                                       launchd label
#                                                       com.engram.engramd-owner.<key>
#   * hooks/start-engramd-owner.sh                   -> the same unit name / label, to ask
#                                                       systemd / launchd about THIS home
#
# Two ways to use it (bash 3.2 compatible: this runs under macOS's stock /bin/bash):
#   bash engramd-owner-home-key.sh <home>        prints the 8-hex key, exit 0
#   source engramd-owner-home-key.sh             defines engramd_owner_home_key <home>
#
# A home that does not exist (so cannot be made physical), or a seat with neither
# `sha256sum` nor `shasum`, is an error: nothing on stdout, a reason on stderr, exit 1.
# The home is never created here.

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

# engramd_owner_physical_path <absolute path>: print the PHYSICAL path of a path that may not exist
# yet, creating nothing. Take the deepest EXISTING ancestor, resolve it (`cd && pwd -P`), and append
# the not-yet-existing remainder. A dangling symlink among the remainder is followed (its target
# replaces the component), so the answer is where a later `mkdir -p` would really create things.
# Absolute paths only; bash 3.2 (no `realpath -m`). Exit 1, nothing on stdout, on failure.
#
# Optional 2nd argument: the NAME of a variable to assign the result to (no stdout). A caller that
# wants a non-ASCII path in a variable must use it: bash 3.2 mangles the bytes of a `$(...)` capture
# (inserting its internal \001 escape byte after each byte >= 0x80) when the CAPTURING shell runs in
# a multibyte locale, whatever this function pins inside, so the stdout form is only safe where the
# caller's locale is safe (#3780). Every capture INSIDE this file runs under the function-scoped
# `local LC_ALL=C` and is therefore exact. A name that is one of this function's own locals (anything
# starting `_eopp_`, or `LC_ALL`) is refused -- exit 1, one stderr line, nothing assigned -- rather than
# assigned to the local with the caller's variable left unset (#3794).
engramd_owner_physical_path() {
    # LC_ALL=C: a path VALUE is bytes, so the locale must not touch it (see above; #3780).
    local LC_ALL=C _eopp_path="${1-}" _eopp_outvar="${2-}" _eopp_anc _eopp_rest _eopp_comp _eopp_link _eopp_hops=0 _eopp_phys
    # `printf -v "$_eopp_outvar"` would assign this function's OWN local, not the caller's variable, if the
    # caller's name were one of its locals (rc 0, caller's variable left unset; #3794): refuse such a name.
    case "$_eopp_outvar" in
        _eopp_*|LC_ALL)
            echo "engramd-owner-home-key: cannot assign to the variable name '$_eopp_outvar': it is one of this function's own local names" >&2
            return 1 ;;
    esac
    case "$_eopp_path" in
        /*) ;;
        *) echo "engramd-owner-home-key: not an absolute path: $_eopp_path" >&2; return 1 ;;
    esac
    while :; do
        _eopp_anc="$_eopp_path" _eopp_rest=""
        while [ "$_eopp_anc" != "/" ] && [ ! -e "$_eopp_anc" ]; do
            _eopp_comp="${_eopp_anc##*/}"
            _eopp_rest="/$_eopp_comp$_eopp_rest"
            _eopp_anc="${_eopp_anc%/*}"
            [ -n "$_eopp_anc" ] || _eopp_anc="/"
        done
        # The first missing component may be a dangling symlink: follow it and look again.
        if [ -n "$_eopp_rest" ]; then
            _eopp_comp="${_eopp_rest#/}"; _eopp_comp="${_eopp_comp%%/*}"
            if [ -L "${_eopp_anc%/}/$_eopp_comp" ]; then
                _eopp_hops=$((_eopp_hops + 1))
                if [ "$_eopp_hops" -gt 40 ]; then
                    echo "engramd-owner-home-key: too many symbolic links resolving $1" >&2
                    return 1
                fi
                _eopp_link="$(readlink "${_eopp_anc%/}/$_eopp_comp")" || return 1
                _eopp_rest="${_eopp_rest#/"$_eopp_comp"}"
                case "$_eopp_link" in
                    /*) _eopp_path="$_eopp_link$_eopp_rest" ;;
                    *) _eopp_path="${_eopp_anc%/}/$_eopp_link$_eopp_rest" ;;
                esac
                continue
            fi
        fi
        break
    done
    # A `.`/`..` in the part that does not exist cannot be resolved without creating it.
    case "$_eopp_rest/" in
        */./*|*/../*) echo "engramd-owner-home-key: cannot resolve '.' or '..' in the not-yet-existing part of $1" >&2; return 1 ;;
    esac
    if ! _eopp_phys="$(cd "$_eopp_anc" 2>/dev/null && pwd -P)" || [ -z "$_eopp_phys" ]; then
        echo "engramd-owner-home-key: cannot resolve the physical path of $_eopp_anc" >&2
        return 1
    fi
    [ "$_eopp_phys" = "/" ] && _eopp_phys=""
    [ -n "$_eopp_phys$_eopp_rest" ] || _eopp_phys="/"
    if [ -n "$_eopp_outvar" ]; then
        printf -v "$_eopp_outvar" '%s%s' "$_eopp_phys" "$_eopp_rest"
    else
        printf '%s%s\n' "$_eopp_phys" "$_eopp_rest"
    fi
}

engramd_owner_home_key() {
    local LC_ALL=C home="${1-}" physical  # C: path/hash bytes, not locale text (#3780)
    case "$home" in
        "~") home="$HOME" ;;
        "~/"*) home="$HOME/${home#"~/"}" ;;
    esac
    if [ -z "$home" ]; then
        echo "engramd-owner-home-key: no ENGRAM_HOME given" >&2
        return 1
    fi
    # The key needs a home that EXISTS; the one resolver then does the physical step.
    case "$home" in /*) ;; *) home="$PWD/$home" ;; esac
    if [ ! -d "$home" ] || ! physical="$(engramd_owner_physical_path "$home" 2>/dev/null)" || [ -z "$physical" ]; then
        echo "engramd-owner-home-key: cannot resolve the physical path of $home" >&2
        return 1
    fi
    while [ "$physical" != "/" ] && [ "${physical%/}" != "$physical" ]; do
        physical="${physical%/}"
    done
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s' "$physical" | sha256sum | cut -c1-8
    elif command -v shasum >/dev/null 2>&1; then
        printf '%s' "$physical" | shasum -a 256 | cut -c1-8
    else
        echo "engramd-owner-home-key: neither sha256sum nor shasum found" >&2
        return 1
    fi
}

# Run directly (not sourced): print the key for the argument.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    engramd_owner_home_key "${1-}"
fi
