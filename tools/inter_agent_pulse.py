#!/usr/bin/env python3
"""inter_agent_pulse — liveness pulse mechanism for the v0.1 inter-agent channel.

Spec: active-work/v0.1-liveness-marker-design-2026-04-28.md
Resolves: ob_1384 (silent-ack cumulative drift).

Each agent's heartbeat calls `tick` once per fire after processing inbox.
The script tracks silent cycles since the counterpart's last substance message
and emits a pulse file when the threshold is hit (default 2 cycles) — at most
one pulse per silent stretch, decoupling liveness from substance.

State: $ENGRAM_HOME/inter-agent-pulse-state.json
Pulses: $INTER_AGENT_DIR/<UTC>_<agent>_pulse.md (e.g. 2026-04-29T03-00-00Z_borges_pulse.md)

Refactored 2026-04-29 PM to use shared modules per Lei's single-source-of-truth
mandate. Substance-filename detection, atomic write, and timestamp helpers
moved to _channel.py. Env helpers in _common.py.

Both agents run the same script with their own env config:
    AGENT_NAME=borges  COUNTERPART_NAME=mneme   ENGRAM_HOME=$HOME/.engram
    AGENT_NAME=mneme   COUNTERPART_NAME=borges  ENGRAM_HOME=$HOME/.engram-gemini

Heartbeat wiring (single one-shot call after inbox processing):
    python tools/inter_agent_pulse.py tick

To inspect state without modifying:
    python tools/inter_agent_pulse.py inspect

State file shape:
    {
      "last_seen_their_substance_id": "2026-04-29T02-45-00Z_mneme.md" | null,
      "silent_cycles": 0,
      "pulse_sent_for_stretch": false
    }
"""

import argparse
import json
import sys
from pathlib import Path

# Shared helpers
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import engram_home, channel_dir, agent_name, counterpart_name  # noqa: E402
from _channel import (  # noqa: E402
    is_substance_filename, their_latest_substance, my_outbound_after,
    write_channel_file, atomic_write, utc_filename_timestamp, utc_iso_timestamp,
)


DEFAULT_THRESHOLD = 2
VALID_STATES = ("idle-no-substance", "processing-high-volume-foreground")


def _state_path() -> Path:
    return engram_home() / "inter-agent-pulse-state.json"


_DEFAULT_STATE = {
    "last_seen_their_substance_id": None,
    "silent_cycles": 0,
    "pulse_sent_for_stretch": False,
}


def load_state() -> dict:
    p = _state_path()
    if not p.exists():
        return dict(_DEFAULT_STATE)
    with p.open() as f:
        loaded = json.load(f)
    # Forward-compat: ensure all keys exist even if state file is older.
    state = dict(_DEFAULT_STATE)
    state.update(loaded)
    return state


def save_state(state: dict) -> None:
    """Atomic write via _channel.atomic_write."""
    atomic_write(_state_path(), json.dumps(state, indent=2), mode=0o644)


def write_pulse(state_value: str, last_read_id: str, silent_cycles: int) -> Path:
    """Write a pulse message to the channel directory atomically. Returns its path."""
    if state_value not in VALID_STATES:
        raise ValueError(f"Invalid state {state_value!r}; must be one of {VALID_STATES}")
    return write_channel_file(
        channel_dir=channel_dir(),
        filename=f"{utc_filename_timestamp()}_{agent_name()}_pulse.md",
        frontmatter={
            "from": agent_name(),
            "to": counterpart_name(),
            "timestamp": utc_iso_timestamp(),
            "type": "pulse",
            "state": state_value,
            "last_read_id": last_read_id,
            "silent_cycles": silent_cycles,
        },
        body="",  # pulses are frontmatter-only, no body
        mode=0o644,
    )


def tick(pulse_state: str = "idle-no-substance",
         threshold: int = DEFAULT_THRESHOLD) -> dict:
    """One-shot per-heartbeat-fire pulse logic. Returns result dict.

    Behavior unchanged from pre-refactor version. Logic:
    - If counterpart posted new substance since last_seen → reset (start new stretch)
    - Else if no last_seen exists yet → no-op (nothing to be silent against)
    - Else if I've responded after their last substance → reset (not silent)
    - Else → increment silent cycle counter
    Pulse fires once per stretch when silent_cycles >= threshold.
    """
    state = load_state()
    channel = channel_dir()
    agent = agent_name()
    counterpart = counterpart_name()

    latest_their = their_latest_substance(channel, counterpart)
    new_substance = bool(
        latest_their and latest_their != state["last_seen_their_substance_id"]
    )

    if new_substance:
        state["last_seen_their_substance_id"] = latest_their
        state["silent_cycles"] = 0
        state["pulse_sent_for_stretch"] = False
    elif state["last_seen_their_substance_id"] is None:
        # No counterpart substance has ever arrived — nothing to be silent against.
        pass
    elif my_outbound_after(channel, agent, state["last_seen_their_substance_id"]):
        # I've replied to their last substance — counter resets.
        state["silent_cycles"] = 0
        state["pulse_sent_for_stretch"] = False
    else:
        state["silent_cycles"] += 1

    should_pulse = (
        state["last_seen_their_substance_id"] is not None
        and state["silent_cycles"] >= threshold
        and not state["pulse_sent_for_stretch"]
    )

    pulse_path = None
    if should_pulse:
        pulse_path = str(write_pulse(
            state_value=pulse_state,
            last_read_id=state["last_seen_their_substance_id"],
            silent_cycles=state["silent_cycles"],
        ))
        state["pulse_sent_for_stretch"] = True

    save_state(state)

    return {
        "pulsed": should_pulse,
        "pulse_path": pulse_path,
        "silent_cycles": state["silent_cycles"],
        "last_seen": state["last_seen_their_substance_id"],
        "pulse_sent_for_stretch": state["pulse_sent_for_stretch"],
    }


# --- CLI ---

def main() -> int:
    parser = argparse.ArgumentParser(description="Inter-agent liveness pulse")
    sub = parser.add_subparsers(dest="cmd", required=True)

    tick_p = sub.add_parser("tick", help="Once-per-heartbeat tick (the main API)")
    tick_p.add_argument("--state", default="idle-no-substance", choices=VALID_STATES,
                        help="Pulse state if a pulse fires this tick")
    tick_p.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                        help="Silent cycles before a pulse fires (default 2)")

    sub.add_parser("inspect", help="Print current state without modifying")

    args = parser.parse_args()

    if args.cmd == "tick":
        result = tick(pulse_state=args.state, threshold=args.threshold)
        print(json.dumps(result, indent=2))
        return 0
    if args.cmd == "inspect":
        print(json.dumps(load_state(), indent=2))
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
