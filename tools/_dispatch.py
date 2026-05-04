"""_dispatch — shared subprocess-spawn pattern for agent dispatchers.

Centralizes the "spawn focused agent invocation, classify failure, log everything"
pattern used by:
- telegram_dispatcher.py (Telegram @mention → claude/gemini -p response)
- channel_dispatcher.py (future: filesystem-channel hot-seat dispatch)

Single source of truth: failure classification rules, subprocess invocation
shape, log format, retry semantics — all in one place. v0.2 enhancements
(retry-on-429, alternate-auth fallback, etc.) layer on top of these helpers.
"""

import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

from _channel import utc_iso_timestamp


def classify_failure(stdout: str, stderr: str, returncode: int) -> Optional[str]:
    """Classify a non-zero exit subprocess into a brief human-readable reason.

    Returns None if returncode == 0 (success).
    Returns a short string suitable for posting back to the user channel, e.g.:
      "rate-limit (429)", "quota exhausted", "model service unavailable (503)",
      "invalid session", "auth setup needed", "compose timeout (180s)",
      "exited with code <N>"
    """
    if returncode == 0:
        return None
    combined = (stderr or "") + (stdout or "")
    low = combined.lower()
    if "rate limit" in low or "429" in combined:
        return "rate-limit (429)"
    if "QUOTA_EXHAUSTED" in combined or "quota exhausted" in low or "quota" in low and "exceed" in low:
        return "quota exhausted"
    if "503" in combined or "service unavailable" in low:
        return "model service unavailable (503)"
    if "invalid session" in low:
        return "invalid session"
    if "authentication page" in low or "OAuth" in combined or "oauth" in low:
        return "auth setup needed"
    # Fallback: first error-ish line (truncated)
    for line in combined.splitlines():
        line = line.strip()
        if line and ("error" in line.lower() or "failed" in line.lower()):
            return line[:120]
    return f"exited with code {returncode}"


def run_agent_subprocess(cli: str, cmd: list, prompt: str,
                         log_path: Path, timeout: int = 180) -> Tuple[int, str, str, Optional[str]]:
    """Run an agent CLI as a subprocess with proper handling for the two CLIs.

    Differences this function abstracts:
    - claude reads prompt from stdin (avoids --add-dir variadic-arg issue)
    - gemini reads prompt as -p arg, needs stdin=DEVNULL to avoid TTY-OAuth path

    Both: capture stdout + stderr, write to log_path, classify failure on
    non-zero exit, return (returncode, stdout, stderr, failure_reason).

    On TimeoutExpired: captures partial stdout/stderr from the killed subprocess,
    writes to log, returns (-1, partial_stdout, partial_stderr, "compose timeout (Xs)").
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as logf:
        logf.write(f"\n=== DISPATCH {utc_iso_timestamp()} ===\n")
        logf.write(f"CLI: {cli}\n")
        logf.write(f"cmd: {cmd[:3]} ... ({len(cmd)} args)\n")
        logf.flush()
    try:
        if cli == "claude":
            proc = subprocess.run(
                cmd, input=prompt, text=True, capture_output=True, timeout=timeout,
            )
        elif cli == "gemini":
            proc = subprocess.run(
                cmd, text=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
            )
        else:
            with log_path.open("a") as logf:
                logf.write(f"=== UNKNOWN-CLI {cli} ===\n")
            return (2, "", f"unknown CLI: {cli}", f"unknown CLI: {cli}")
        with log_path.open("a") as logf:
            if proc.stdout:
                logf.write(proc.stdout)
            if proc.stderr:
                logf.write("--- stderr ---\n")
                logf.write(proc.stderr)
            logf.write(f"\n=== EXIT {proc.returncode} ===\n")
        reason = classify_failure(proc.stdout, proc.stderr, proc.returncode)
        return (proc.returncode, proc.stdout, proc.stderr, reason)
    except subprocess.TimeoutExpired as e:
        # Capture partial output for diagnostics (otherwise log shows
        # DISPATCH→TIMEOUT with no data on what the subprocess was doing).
        partial_out = ""
        partial_err = ""
        if e.stdout:
            partial_out = e.stdout if isinstance(e.stdout, str) else e.stdout.decode(errors="replace")
        if e.stderr:
            partial_err = e.stderr if isinstance(e.stderr, str) else e.stderr.decode(errors="replace")
        with log_path.open("a") as logf:
            if partial_out:
                logf.write(partial_out)
            if partial_err:
                logf.write("--- stderr (partial, before timeout kill) ---\n")
                logf.write(partial_err)
            logf.write(f"\n=== TIMEOUT {utc_iso_timestamp()} ===\n")
        return (-1, partial_out, partial_err, f"compose timeout ({timeout}s)")
    except Exception as e:
        with log_path.open("a") as logf:
            logf.write(f"=== EXCEPTION {utc_iso_timestamp()}: {type(e).__name__}: {e} ===\n")
        return (1, "", str(e), f"dispatch error: {type(e).__name__}")
