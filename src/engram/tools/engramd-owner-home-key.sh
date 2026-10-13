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

# engramd_owner_physical_path <absolute path>: print the PHYSICAL path of a path that may not exist
# yet, creating nothing. Take the deepest EXISTING ancestor, resolve it (`cd && pwd -P`), and append
# the not-yet-existing remainder. A dangling symlink among the remainder is followed (its target
# replaces the component), so the answer is where a later `mkdir -p` would really create things.
# Absolute paths only; bash 3.2 (no `realpath -m`). Exit 1, nothing on stdout, on failure.
engramd_owner_physical_path() {
    local path="${1-}" anc rest comp link hops=0 phys
    case "$path" in
        /*) ;;
        *) echo "engramd-owner-home-key: not an absolute path: $path" >&2; return 1 ;;
    esac
    while :; do
        anc="$path" rest=""
        while [ "$anc" != "/" ] && [ ! -e "$anc" ]; do
            comp="${anc##*/}"
            rest="/$comp$rest"
            anc="${anc%/*}"
            [ -n "$anc" ] || anc="/"
        done
        # The first missing component may be a dangling symlink: follow it and look again.
        if [ -n "$rest" ]; then
            comp="${rest#/}"; comp="${comp%%/*}"
            if [ -L "${anc%/}/$comp" ]; then
                hops=$((hops + 1))
                if [ "$hops" -gt 40 ]; then
                    echo "engramd-owner-home-key: too many symbolic links resolving $1" >&2
                    return 1
                fi
                link="$(readlink "${anc%/}/$comp")" || return 1
                rest="${rest#/"$comp"}"
                case "$link" in
                    /*) path="$link$rest" ;;
                    *) path="${anc%/}/$link$rest" ;;
                esac
                continue
            fi
        fi
        break
    done
    # A `.`/`..` in the part that does not exist cannot be resolved without creating it.
    case "$rest/" in
        */./*|*/../*) echo "engramd-owner-home-key: cannot resolve '.' or '..' in the not-yet-existing part of $1" >&2; return 1 ;;
    esac
    if ! phys="$(cd "$anc" 2>/dev/null && pwd -P)" || [ -z "$phys" ]; then
        echo "engramd-owner-home-key: cannot resolve the physical path of $anc" >&2
        return 1
    fi
    [ "$phys" = "/" ] && phys=""
    [ -n "$phys$rest" ] || phys="/"
    printf '%s%s\n' "$phys" "$rest"
}

engramd_owner_home_key() {
    local home="${1-}" physical
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
