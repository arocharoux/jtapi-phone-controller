#!/usr/bin/env python3
"""
Basic JTAPI call example — dial, hold, resume, hang up.

This is a learning script. Run it once your phone is configured in
config.json and you will see a complete call flow:

  1. Dial a number
  2. Stay connected for 10 seconds
  3. Put the call on hold for 10 seconds
  4. Resume (take off hold) for 10 seconds
  5. Hang up

Usage:
    cd jtapi-phone-controller
  cp config.example.json config.json
  # edit config.json — fill in your CUCM address, credentials, and phone

  python example_basic_call.py --destination 2065551234
  python example_basic_call.py --destination 2065551234 --target my-phone
  python example_basic_call.py --destination 2065551234 --config /path/to/config.json

Requires Python 3.9+ and a JDK (javac/java on PATH).
No pip install needed — Python stdlib only. Live runs still need Cisco JTAPI jars in lib/.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

# ── Standalone runner (no acme_test_automation required) ─────────────
# Add this file's directory to sys.path so _runner.py can be imported
# without installing anything.

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _runner import load_config, run_commands  # noqa: E402

# ── Signal handling ───────────────────────────────────────────────────
# Make both Ctrl+C and `kill <pid>` raise KeyboardInterrupt so the
# same cleanup path handles both.


def _request_cleanup(signum, frame) -> None:
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, _request_cleanup)
# SIGINT (Ctrl+C) already raises KeyboardInterrupt by default


# ── Defaults ──────────────────────────────────────────────────────────

# Default config path: looks for config.json next to this script.
# Copy config.example.json → config.json and fill in your CUCM details.
DEFAULT_CONFIG = Path(__file__).parent / "config.json"

# How long to pause at each stage of the scenario.
# Adjust these to match how long your test needs to observe each state.
CONNECTED_SECONDS = 10  # how long to stay connected before holding
HOLD_SECONDS = 10  # how long to keep the call on hold
RESUMED_SECONDS = 10  # how long to stay connected after resuming


# ── Main ──────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Basic JTAPI call example: dial → hold → resume → hang up")
    parser.add_argument("--destination", required=True, help="Number to dial (e.g. 82001234 or 2065551234)")
    parser.add_argument("--target", default=None, help="Target phone name from config (default: uses default_target)")
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG), help="Path to config.json (default: config.json in this directory)"
    )
    args = parser.parse_args()

    # Load config
    config, config_path = load_config(args.config)

    print(f"  Destination : {args.destination}")
    print(f"  Target      : {args.target or config['jtapi'].get('default_target')} (from config)")
    print(
        f"  Sequence    : dial → {CONNECTED_SECONDS}s connected → hold → {HOLD_SECONDS}s held → resume → {RESUMED_SECONDS}s talking → hang up"
    )
    print()

    # ── Command sequence ──────────────────────────────────────────────
    # Each dict is one step. 'cmd' is the action; everything else is a
    # parameter. See README.md for the full command list.

    commands = [
        # Step 1: Dial the destination and wait until the far end picks up
        {"cmd": "dial", "destination": args.destination},
        # Step 2: Stay connected for 10 seconds so the call is established
        {"cmd": "sleep", "seconds": CONNECTED_SECONDS},
        # Step 3: Put the call on hold (caller hears MOH)
        {"cmd": "hold"},
        # Step 4: Leave it on hold for 10 seconds
        {"cmd": "sleep", "seconds": HOLD_SECONDS},
        # Step 5: Take the call off hold (resumes audio)
        {"cmd": "resume"},
        # Step 6: Stay on the call for 10 more seconds
        {"cmd": "sleep", "seconds": RESUMED_SECONDS},
        # Step 7: Hang up
        {"cmd": "disconnect"},
    ]

    # ── Execute ───────────────────────────────────────────────────────
    # If you press Ctrl+C (or the process is killed) while the call is
    # active, the except block below opens a fresh JTAPI session and
    # sends DISCONNECT so the call clears immediately on the phone.
    try:
        result = run_commands(config, config_path, args.target, commands)
    except KeyboardInterrupt:
        print("\nInterrupted — disconnecting call...", flush=True)
        # Brief pause to let the killed Java process fully release the CTI
        # provider before we try to open a new one on the same device.
        # Without this, the cleanup JTAPI session may fail to register.
        time.sleep(2)
        try:
            run_commands(config, config_path, args.target, [{"cmd": "disconnect"}], timeout=30)
            print("Call disconnected.", flush=True)
        except Exception as cleanup_exc:
            # The call may have already dropped when the CTI connection died — that's fine.
            print(f"Cleanup note: {cleanup_exc}", flush=True)
        # Exit 130 is the POSIX standard exit code for processes terminated by
        # SIGINT (128 + signal number 2).  Shells use this to distinguish
        # keyboard interrupts from other non-zero exits.
        sys.exit(130)

    # ── Print result ──────────────────────────────────────────────────
    status = result.get("status", "unknown")

    print(f"Status: {status.upper()}")
    print()

    # Show the state transitions so it is easy to see what happened
    states = result.get("states", [])
    if states:
        print("Phone state transitions:")
        for s in states:
            print(f"  {s.get('at', '')}  →  {s.get('state', '')}")
        print()

    if status != "completed":
        error = result.get("error", "unknown error")
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
