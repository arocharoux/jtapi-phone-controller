#!/usr/bin/env python3
"""
JTAPI Phone Controller — interactive call scenarios.

Run any of these scenarios against any phone in your config:

    doctor       Check the local JTAPI setup before placing a call
    run          Run a reusable JSON scenario with pass/fail assertions
        coverage     Prove configured phone targets can be inspected or controlled
        dial         Dial a number, stay connected, hang up
        hold-resume  Dial, hold, resume, hang up  (the classic)
        transfer     Dial, then transfer to a second number (consult or blind)
        conference   Dial, add a third party, stay in conference, hang up
        dtmf         Dial a number and send DTMF tones (navigate an IVR, unlock a door, etc.)
        answer       Wait for an inbound call, answer it, stay connected, hang up

Usage:
    cd jtapi-phone-controller
  cp config.example.json config.json
  # edit config.json — fill in your CUCM address, credentials, and phone

    python phone.py dial         --destination 82001234
    python phone.py hold-resume  --destination 82001234
    python phone.py transfer     --destination 82001234 --to 82005678
    python phone.py transfer     --destination 82001234 --to 82005678 --type blind
    python phone.py conference   --destination 82001234 --to 82005678
    python phone.py dtmf         --destination 82001234 --digits 5551
        python phone.py answer
    python phone.py run scenarios/hold_resume_smoke.json
        python phone.py coverage

Requires Python 3.9+ and a JDK (javac/java on PATH).
No pip install needed — Python stdlib only. Live runs still need Cisco JTAPI jars in lib/.

Add --target <name> to any command to use a specific phone from your config.
Add --help to any subcommand for all options.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── Standalone runner (no acme_test_automation required) ─────────────
# _runner.py lives in the same directory as this script.  We add that
# directory to sys.path so Python can import it without installing a
# package or activating a virtualenv.

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _runner import (  # noqa: E402
    PHONE_CONTROLLER_SOURCE,
    ensure_compiled,
    load_config,
    resolve_lib_dir,
    resolve_target,
    run_commands,
)

# ── Signal handling ───────────────────────────────────────────────────
# Map SIGTERM (sent by `kill <pid>` or a process manager) to
# KeyboardInterrupt so the same try/except cleanup block in run() handles
# both Ctrl+C and external kill signals.  SIGINT (Ctrl+C) already raises
# KeyboardInterrupt by default, so no handler is needed for it.


def _request_cleanup(signum, frame) -> None:
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, _request_cleanup)

# Default config path: config.json in the same directory as this script.
# Copy config.example.json → config.json and fill in your CUCM details.
DEFAULT_CONFIG = Path(__file__).parent / "config.json"
DEFAULT_EVIDENCE_DIR = Path(__file__).parent / "runs"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_slug(value: str) -> str:
    slug = "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "scenario"


# ── Scenario builders ─────────────────────────────────────────────────
# Each function takes a parsed argparse.Namespace and returns a 3-tuple:
#   title   (str)        — printed as the scenario header before execution
#   steps   (list[str])  — human-readable plan printed before execution so
#                          the user can review what is about to happen
#   commands (list[dict]) — JTAPI command dicts sent to PhoneController
#
# Separating the plan-print from the execution makes it easier to review
# the scenario without running it, and provides a paper trail if something
# goes wrong mid-scenario.


def _scenario_dial(args) -> tuple[str, list[str], list[dict]]:
    seconds = args.seconds
    return (
        f"DIAL  →  {args.destination}",
        [
            f"1. Dial {args.destination}",
            f"2. Stay connected for {seconds}s",
            "3. Hang up",
        ],
        [
            {"cmd": "dial", "destination": args.destination},
            {"cmd": "sleep", "seconds": seconds},
            {"cmd": "disconnect"},
        ],
    )


def _scenario_hold_resume(args) -> tuple[str, list[str], list[dict]]:
    c, h, r = args.connected, args.hold, args.resume
    return (
        f"HOLD / RESUME  →  {args.destination}",
        [
            f"1. Dial {args.destination}",
            f"2. Stay connected for {c}s",
            "3. Hold (far end hears Music on Hold)",
            f"4. Hold for {h}s",
            "5. Resume (audio restored)",
            f"6. Stay connected for {r}s",
            "7. Hang up",
        ],
        [
            {"cmd": "dial", "destination": args.destination},
            {"cmd": "sleep", "seconds": c},
            {"cmd": "hold"},
            {"cmd": "sleep", "seconds": h},
            {"cmd": "resume"},
            {"cmd": "sleep", "seconds": r},
            {"cmd": "disconnect"},
        ],
    )


def _scenario_transfer(args) -> tuple[str, list[str], list[dict]]:
    ttype = args.type  # "consult" or "blind"
    wait = args.wait
    if ttype == "blind":
        # Blind transfer: our leg drops immediately when the transfer is
        # initiated.  The destination rings without our involvement.  Use
        # this when you do not need to announce the transfer or verify the
        # far end picks up.
        steps = [
            f"1. Dial {args.destination}",
            f"2. Stay connected for {wait}s",
            f"3. Blind-transfer to {args.to}  (our leg drops immediately)",
        ]
        commands = [
            {"cmd": "dial", "destination": args.destination},
            {"cmd": "sleep", "seconds": wait},
            {"cmd": "transfer", "destination": args.to, "type": "blind"},
        ]
    else:
        # Consult (supervised) transfer: we first call the transfer target
        # ourselves.  The original caller is put on hold (hears MOH) while
        # we ring the target.  Once the target answers, CUCM completes the
        # transfer and bridges the two parties together without us.
        steps = [
            f"1. Dial {args.destination}",
            f"2. Stay connected for {wait}s",
            f"3. Consult-transfer to {args.to}",
            f"   → Our leg holds while {args.to} rings",
            f"   → Once {args.to} answers, transfer completes and our leg drops",
        ]
        commands = [
            {"cmd": "dial", "destination": args.destination},
            {"cmd": "sleep", "seconds": wait},
            {"cmd": "transfer", "destination": args.to, "type": "consult"},
        ]
    return (f"TRANSFER ({ttype.upper()})  →  {args.destination}  →  {args.to}", steps, commands)


def _scenario_conference(args) -> tuple[str, list[str], list[dict]]:
    wait = args.wait
    duration = args.duration
    return (
        f"CONFERENCE  →  {args.destination}  +  {args.to}",
        [
            f"1. Dial {args.destination}",
            f"2. Stay connected for {wait}s",
            f"3. Consult {args.to}  (original caller hears MOH while we ring {args.to})",
            f"4. Once {args.to} answers, merge all three into a conference",
            f"5. Stay in conference for {duration}s",
            "6. Hang up (remaining parties may stay connected depending on CUCM config)",
        ],
        [
            {"cmd": "dial", "destination": args.destination},
            {"cmd": "sleep", "seconds": wait},
            {"cmd": "conference", "destination": args.to},
            {"cmd": "sleep", "seconds": duration},
            {"cmd": "disconnect"},
        ],
    )


def _scenario_dtmf(args) -> tuple[str, list[str], list[dict]]:
    wait = args.wait
    digits = args.digits
    after = args.after
    return (
        f"DTMF  →  {args.destination}  →  sending '{digits}'",
        [
            f"1. Dial {args.destination}",
            f"2. Wait {wait}s for IVR/auto-attendant to answer",
            f"3. Send DTMF digits: {digits}",
            f"4. Stay connected for {after}s",
            "5. Hang up",
        ],
        [
            {"cmd": "dial", "destination": args.destination},
            {"cmd": "sleep", "seconds": wait},
            {"cmd": "send_dtmf", "digits": digits},
            {"cmd": "sleep", "seconds": after},
            {"cmd": "disconnect"},
        ],
    )


def _scenario_answer(args) -> tuple[str, list[str], list[dict]]:
    wait = args.wait
    duration = args.duration
    return (
        "ANSWER  ←  waiting for inbound call",
        [
            f"1. Wait up to {wait}s for an inbound call to arrive",
            "2. Answer the call",
            f"3. Stay connected for {duration}s",
            "4. Hang up",
        ],
        [
            {"cmd": "answer", "timeout": wait},
            {"cmd": "sleep", "seconds": duration},
            {"cmd": "disconnect"},
        ],
    )


# ── CLI ───────────────────────────────────────────────────────────────


def _common_args(p: argparse.ArgumentParser) -> None:
    """Attach --target and --config to any subparser.

    Every scenario supports the same two optional arguments:
      --target  select a specific phone from config (defaults to default_target)
      --config  point to a different config.json (useful in CI or multi-env setups)
    Defined as a helper function to avoid repeating the add_argument calls in
    every subparser block.
    """
    p.add_argument("--target", default=None, help="Phone name from config (default: uses default_target)")
    p.add_argument(
        "--config", default=str(DEFAULT_CONFIG), help="Path to config.json (default: config.json in this directory)"
    )


# ── Doctor checks ─────────────────────────────────────────────────────


def _doctor_line(status: str, label: str, detail: str = "") -> None:
    suffix = f" - {detail}" if detail else ""
    print(f"  {status:<4} {label}{suffix}")


def _command_version(command: str) -> tuple[bool, str]:
    executable = shutil.which(command)
    if not executable:
        return False, f"{command} not found on PATH"
    completed = subprocess.run(
        [command, "-version"],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    version = (
        (completed.stderr or completed.stdout).splitlines()[0] if (completed.stderr or completed.stdout) else executable
    )
    return completed.returncode == 0, version


def _check_tcp(host: str, port: int, timeout: float) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} reachable"
    except OSError as exc:
        return False, f"{host}:{port} not reachable ({exc})"


def _run_doctor(args: argparse.Namespace) -> int:
    print()
    print("JTAPI helper doctor")
    print("-------------------")

    failures = 0

    def check(condition: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        if condition:
            _doctor_line("PASS", label, detail)
        else:
            failures += 1
            _doctor_line("FAIL", label, detail)

    def warn(label: str, detail: str = "") -> None:
        _doctor_line("WARN", label, detail)

    check(sys.version_info >= (3, 9), "Python version", sys.version.split()[0])

    java_ok, java_detail = _command_version("java")
    check(java_ok, "java", java_detail)
    javac_ok, javac_detail = _command_version("javac")
    check(javac_ok, "javac", javac_detail)

    check(PHONE_CONTROLLER_SOURCE.exists(), "PhoneController.java", str(PHONE_CONTROLLER_SOURCE))

    config = None
    config_path = Path(args.config).resolve()
    try:
        config, config_path = load_config(args.config)
        check(True, "Config file", str(config_path))
    except Exception as exc:
        check(False, "Config file", str(exc))

    lib_dir = None
    if config is not None:
        jtapi = config.get("jtapi", {})

        provider = str(jtapi.get("provider") or "").strip()
        check(bool(provider), "JTAPI provider", provider or "missing jtapi.provider")

        username = str(jtapi.get("username") or "").strip()
        check(bool(username), "JTAPI username", username or "missing jtapi.username")

        password = str(jtapi.get("password") or "")
        if password.startswith("env:"):
            env_name = password.split(":", 1)[1]
            check(
                bool(os.environ.get(env_name)),
                "JTAPI password env var",
                f"{env_name} is set" if os.environ.get(env_name) else f"{env_name} is not set",
            )
        elif password:
            warn("JTAPI password", "literal password configured; env:VAR_NAME is safer")
        else:
            check(False, "JTAPI password", "missing jtapi.password")

        try:
            target = resolve_target(config, args.target)
            target_name = args.target or jtapi.get("default_target")
            device_name = str(target.get("device_name") or "")
            directory_number = str(target.get("directory_number") or "")
            check(
                bool(device_name),
                "Target device",
                f"{target_name}: {device_name}" if device_name else f"{target_name}: missing device_name",
            )
            check(bool(directory_number), "Target directory number", directory_number or "missing directory_number")
        except Exception as exc:
            check(False, "Target config", str(exc))

        try:
            lib_dir = resolve_lib_dir(config, config_path)
            check(lib_dir.exists(), "JTAPI lib directory", str(lib_dir))
            jars = sorted(lib_dir.glob("*.jar")) if lib_dir.exists() else []
            check(bool(jars), "Cisco JTAPI jars", f"{len(jars)} jar(s) found" if jars else "no .jar files found")
        except Exception as exc:
            check(False, "JTAPI lib directory", str(exc))

        if provider and not args.skip_network:
            ok, detail = _check_tcp(provider, 2748, args.timeout)
            check(ok, "CUCM CTI port", detail)
        elif args.skip_network:
            warn("CUCM CTI port", "skipped by --skip-network")

    if config is not None and lib_dir is not None and not args.skip_compile:
        try:
            ensure_compiled(lib_dir)
            check(True, "Java bridge compile", "PhoneController.class is current")
        except Exception as exc:
            check(False, "Java bridge compile", str(exc))
    elif args.skip_compile:
        warn("Java bridge compile", "skipped by --skip-compile")

    print()
    if failures:
        print(f"Doctor result: FAIL ({failures} blocking check(s) failed)")
        return 1
    print("Doctor result: PASS")
    return 0


# ── Scenario file runner ──────────────────────────────────────────────


def _load_scenario_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Scenario file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"Scenario file must contain a JSON object: {path}")
    return payload


def _parse_cli_variables(entries: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for entry in entries:
        if "=" not in entry:
            raise ValueError(f"Invalid --var value {entry!r}; expected KEY=VALUE")
        key, value = entry.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid --var value {entry!r}; key is empty")
        values[key] = value
    return values


def _render_value(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        rendered = value
        for key, replacement in variables.items():
            rendered = rendered.replace("${" + key + "}", replacement)
        return rendered
    if isinstance(value, list):
        return [_render_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _render_value(item, variables) for key, item in value.items()}
    return value


def _scenario_variables(scenario: dict[str, Any], cli_variables: list[str]) -> dict[str, str]:
    raw_variables = scenario.get("variables") or scenario.get("vars") or {}
    if not isinstance(raw_variables, dict):
        raise ValueError("Scenario variables must be a JSON object")
    variables = {str(key): str(value) for key, value in raw_variables.items()}
    variables.update(_parse_cli_variables(cli_variables))
    return variables


def _scenario_commands(scenario: dict[str, Any], variables: dict[str, str]) -> list[dict[str, Any]]:
    rendered = _render_value(scenario.get("commands", []), variables)
    if not isinstance(rendered, list) or not rendered:
        raise ValueError("Scenario must include a non-empty commands list")
    commands: list[dict[str, Any]] = []
    for index, entry in enumerate(rendered, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Scenario command #{index} must be a JSON object")
        if not str(entry.get("cmd") or "").strip():
            raise ValueError(f"Scenario command #{index} is missing cmd")
        commands.append(entry)
    return commands


def _mock_run_commands(commands: list[dict[str, Any]], target: str | None) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    states: list[dict[str, Any]] = [{"at": _utc_now(), "state": "IDLE"}]
    events: list[dict[str, Any]] = [
        {
            "at": _utc_now(),
            "event": "mock-scenario-runner",
            "detail": f"No live Cisco phone controlled; target={target or 'not-set'}",
        }
    ]

    for entry in commands:
        cmd = str(entry.get("cmd") or "").lower()
        actions.append(
            {"at": _utc_now(), "action": cmd, "detail": {key: value for key, value in entry.items() if key != "cmd"}}
        )
        if cmd == "dial":
            states.append({"at": _utc_now(), "state": "CONNECTED"})
        elif cmd == "answer":
            states.append({"at": _utc_now(), "state": "RINGING"})
            states.append({"at": _utc_now(), "state": "TALKING"})
        elif cmd == "inspect":
            events.append(
                {
                    "at": _utc_now(),
                    "event": "terminal-diagnostics",
                    "detail": "name="
                    + (target or "mock-target")
                    + ",state=IN_SERVICE,registration=IN_SERVICE,deviceState=IDLE,registered=true",
                }
            )
            events.append(
                {
                    "at": _utc_now(),
                    "event": "address-diagnostics",
                    "detail": "state=IN_SERVICE,registration=IN_SERVICE,inServiceTerminals=1",
                }
            )
            events.append({"at": _utc_now(), "event": "inspect-complete", "detail": target or "mock-target"})
        elif cmd == "hold":
            states.append({"at": _utc_now(), "state": "HELD"})
        elif cmd == "resume":
            states.append({"at": _utc_now(), "state": "TALKING"})
        elif cmd == "transfer":
            states.append({"at": _utc_now(), "state": "DISCONNECTED"})
        elif cmd == "conference":
            states.append({"at": _utc_now(), "state": "TALKING"})
        elif cmd == "disconnect":
            states.append({"at": _utc_now(), "state": "DISCONNECTED"})
        elif cmd == "wait" and entry.get("state"):
            states.append({"at": _utc_now(), "state": str(entry["state"]).upper()})

    return {
        "status": "completed",
        "actions": actions,
        "states": states,
        "events": events,
    }


def _state_names(result: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for entry in result.get("states", []):
        if isinstance(entry, dict) and entry.get("state"):
            names.append(str(entry["state"]).upper())
    return names


def _contains_state_sequence(observed: list[str], expected: list[str]) -> bool:
    if not expected:
        return True
    expected_index = 0
    for state in observed:
        if state == expected[expected_index]:
            expected_index += 1
            if expected_index == len(expected):
                return True
    return False


def _evaluate_scenario(scenario: dict[str, Any], result: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
    failures: list[str] = []
    expected_status = str(scenario.get("expect_status") or "completed")
    actual_status = str(result.get("status") or "unknown")
    if actual_status != expected_status:
        failures.append(f"expected status {expected_status!r}, observed {actual_status!r}")

    expected_states = [str(state).upper() for state in scenario.get("expect_states", [])]
    observed_states = _state_names(result)
    if expected_states and not _contains_state_sequence(observed_states, expected_states):
        failures.append(
            "expected state sequence "
            + " -> ".join(expected_states)
            + "; observed "
            + (" -> ".join(observed_states) or "none")
        )
    return not failures, failures, observed_states


def _scenario_needs_cleanup(commands: list[dict[str, Any]], result: dict[str, Any]) -> bool:
    if result.get("status") == "completed":
        return False
    call_commands = {"dial", "answer", "hold", "resume", "transfer", "conference", "send_dtmf"}
    return any(str(command.get("cmd") or "").lower() in call_commands for command in commands)


def _evidence_path(evidence_dir: str | Path, scenario_name: str) -> Path:
    root = Path(evidence_dir)
    if not root.is_absolute():
        root = _HERE / root
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{_timestamp_slug()}-{_safe_slug(scenario_name)}.json"


def _write_evidence(summary: dict[str, Any], evidence_dir: str | Path, scenario_name: str) -> Path:
    path = _evidence_path(evidence_dir, scenario_name)
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return path


def _print_scenario_summary(summary: dict[str, Any], evidence_path: Path | None) -> None:
    outcome = "PASS" if summary["passed"] else "FAIL"
    print()
    print(f"{outcome} {summary['name']}")
    print(f"Mode: {summary['mode']}")
    print(f"Target: {summary.get('target') or 'not-set'}")
    print(f"Status: {summary['result'].get('status', 'unknown')}")
    print("States: " + (" -> ".join(summary["observed_states"]) or "none"))
    if summary["failures"]:
        print("Failures:")
        for failure in summary["failures"]:
            print(f"  - {failure}")
    if evidence_path:
        print(f"Evidence: {evidence_path}")


# ── Device coverage runner ───────────────────────────────────────────


def _configured_target_names(config: dict[str, Any]) -> list[str]:
    targets = config.get("jtapi", {}).get("targets", [])
    if not isinstance(targets, list):
        return []
    names: list[str] = []
    for target in targets:
        if isinstance(target, dict) and str(target.get("name") or "").strip():
            names.append(str(target["name"]))
    return names


def _targets_sharing_directory_number(config: dict[str, Any], target_name: str, directory_number: str) -> list[str]:
    if not directory_number:
        return []
    peers: list[str] = []
    targets = config.get("jtapi", {}).get("targets", [])
    if not isinstance(targets, list):
        return peers
    for target in targets:
        if not isinstance(target, dict):
            continue
        peer_name = str(target.get("name") or "")
        peer_directory_number = str(target.get("directory_number") or "")
        if peer_name and peer_name != target_name and peer_directory_number == directory_number:
            peers.append(peer_name)
    return peers


def _action_names(result: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for entry in result.get("actions", []):
        if isinstance(entry, dict) and entry.get("action"):
            names.append(str(entry["action"]).lower())
    return names


def _event_details(result: dict[str, Any], event_name: str) -> list[str]:
    details: list[str] = []
    for entry in result.get("events", []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("event") or "") == event_name and entry.get("detail") is not None:
            details.append(str(entry["detail"]))
    return details


def _evaluate_inspect_coverage(result: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
    failures: list[str] = []
    observed_states = _state_names(result)
    actual_status = str(result.get("status") or "unknown")
    if actual_status != "completed":
        failures.append(f"expected status 'completed', observed {actual_status!r}")
        if result.get("error"):
            failures.append(str(result["error"]))
        return False, failures, observed_states

    actions = _action_names(result)
    if "inspect" not in actions:
        failures.append("expected inspect action in bridge output")
    if "IDLE" not in observed_states:
        failures.append("expected target to report IDLE before coverage commands")

    terminal_details = " ".join(_event_details(result, "terminal-diagnostics")).lower()
    address_details = " ".join(_event_details(result, "address-diagnostics")).lower()
    if not terminal_details:
        failures.append("missing terminal diagnostics")
    else:
        if "registered=true" not in terminal_details:
            failures.append("terminal is not reporting registered=true")
        if "devicestate=idle" not in terminal_details:
            failures.append("terminal device state is not IDLE")
        if "state=in_service" not in terminal_details and "registration=in_service" not in terminal_details:
            failures.append("terminal is not reporting IN_SERVICE")

    if not address_details:
        failures.append("missing address diagnostics")
    elif "state=in_service" not in address_details and "registration=in_service" not in address_details:
        failures.append("address is not reporting IN_SERVICE")

    return not failures, failures, observed_states


def _coverage_record(
    *,
    config: dict[str, Any],
    target_name: str,
    target: dict[str, Any],
    mode: str,
    passed: bool,
    failures: list[str],
    observed_states: list[str],
    result: dict[str, Any],
) -> dict[str, Any]:
    directory_number = str(target.get("directory_number") or "")
    shared_with = _targets_sharing_directory_number(config, target_name, directory_number)
    warnings: list[str] = []
    if shared_with:
        warnings.append(
            "directory number "
            + directory_number
            + " is also configured for "
            + ", ".join(shared_with)
            + "; inspect coverage can prove device registration now, but unique DNs make call-flow proof cleaner"
        )
    return {
        "target_name": target_name,
        "device_name": str(target.get("device_name") or ""),
        "directory_number": directory_number,
        "description": target.get("description"),
        "mode": mode,
        "passed": passed,
        "failures": failures,
        "warnings": warnings,
        "observed_states": observed_states,
        "result": result,
    }


def _print_coverage_summary(summary: dict[str, Any], evidence_path: Path | None) -> None:
    print()
    print("JTAPI device coverage")
    print("---------------------")
    print(f"Mode: {summary['mode']}")
    print(f"Coverage: {summary['coverage_type']}")
    print(f"Targets: {summary['passed_targets']}/{summary['total_targets']} passed")
    print()
    print(f"{'Result':<6} {'Target':<18} {'Device':<16} {'DN':<10} States")
    print(f"{'-' * 6} {'-' * 18} {'-' * 16} {'-' * 10} {'-' * 30}")
    for record in summary["records"]:
        result_label = "PASS" if record["passed"] else "FAIL"
        states = " -> ".join(record.get("observed_states") or [])
        print(
            f"{result_label:<6} "
            f"{str(record.get('target_name') or '')[:18]:<18} "
            f"{str(record.get('device_name') or '')[:16]:<16} "
            f"{str(record.get('directory_number') or '')[:10]:<10} "
            f"{states}"
        )
        for warning in record.get("warnings") or []:
            print(f"  warning: {warning}")
        for failure in record.get("failures") or []:
            print(f"  failure: {failure}")
    if evidence_path:
        print()
        print(f"Evidence: {evidence_path}")


def _coverage_commands(args: argparse.Namespace) -> tuple[str, str, dict[str, Any] | None, list[dict[str, Any]], int | None]:
    if args.scenario_file:
        scenario_path = Path(args.scenario_file).resolve()
        scenario = _load_scenario_file(scenario_path)
        variables = _scenario_variables(scenario, args.variables)
        rendered_scenario = _render_value(scenario, variables)
        commands = _scenario_commands(rendered_scenario, {})
        scenario_name = str(rendered_scenario.get("name") or scenario_path.stem)
        timeout = int(rendered_scenario["timeout_seconds"]) if rendered_scenario.get("timeout_seconds") else None
        return "scenario", scenario_name, rendered_scenario, commands, timeout

    commands = [{"cmd": "inspect", "address_timeout": args.address_timeout}]
    return "inspect", "device_coverage_inspect", None, commands, args.timeout


def _run_coverage(args: argparse.Namespace) -> int:
    config, config_path = load_config(args.config)
    target_names = args.target_names or _configured_target_names(config)
    if not target_names:
        raise ValueError("No JTAPI targets are configured; add jtapi.targets or pass --target")

    coverage_type, coverage_name, rendered_scenario, commands, scenario_timeout = _coverage_commands(args)
    timeout = args.timeout if args.timeout is not None else scenario_timeout
    mode = "mock" if args.mock else "live"
    records: list[dict[str, Any]] = []

    for target_name in target_names:
        try:
            target = resolve_target(config, target_name)
            if args.mock:
                result = _mock_run_commands(commands, target_name)
            else:
                result = run_commands(config, config_path, target_name, commands, timeout=timeout)
            if rendered_scenario is None:
                passed, failures, observed_states = _evaluate_inspect_coverage(result)
            else:
                passed, failures, observed_states = _evaluate_scenario(rendered_scenario, result)
            records.append(
                _coverage_record(
                    config=config,
                    target_name=target_name,
                    target=target,
                    mode=mode,
                    passed=passed,
                    failures=failures,
                    observed_states=observed_states,
                    result=result,
                )
            )
        except Exception as exc:
            records.append(
                {
                    "target_name": target_name,
                    "device_name": "",
                    "directory_number": "",
                    "description": None,
                    "mode": mode,
                    "passed": False,
                    "failures": [str(exc)],
                    "warnings": [],
                    "observed_states": [],
                    "result": {"status": "failed", "error": str(exc)},
                }
            )

    passed_targets = sum(1 for record in records if record["passed"] is True)
    summary: dict[str, Any] = {
        "generated_at": _utc_now(),
        "name": coverage_name,
        "coverage_type": coverage_type,
        "mode": mode,
        "target_names": target_names,
        "commands": commands,
        "scenario": rendered_scenario,
        "total_targets": len(records),
        "passed_targets": passed_targets,
        "failed_targets": len(records) - passed_targets,
        "passed": passed_targets == len(records),
        "records": records,
    }

    evidence_path = None
    if not args.no_evidence:
        evidence_path = _write_evidence(summary, args.evidence_dir, coverage_name)
        summary["evidence_path"] = str(evidence_path)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        _print_coverage_summary(summary, evidence_path)
    return 0 if summary["passed"] else 1


def _run_scenario_file(args: argparse.Namespace) -> int:
    scenario_path = Path(args.scenario_file).resolve()
    scenario = _load_scenario_file(scenario_path)
    variables = _scenario_variables(scenario, args.variables)
    rendered_scenario = _render_value(scenario, variables)
    commands = _scenario_commands(rendered_scenario, {})
    scenario_name = str(rendered_scenario.get("name") or scenario_path.stem)

    config: dict[str, Any] | None = None
    config_path = Path(args.config).resolve()
    if args.mock and not config_path.exists():
        config = {"jtapi": {}}
    else:
        config, config_path = load_config(args.config)

    target = args.target or rendered_scenario.get("target")
    if not target and config is not None:
        target = config.get("jtapi", {}).get("default_target")
    target_name = str(target) if target else None

    timeout = args.timeout
    if timeout is None and rendered_scenario.get("timeout_seconds") is not None:
        timeout = int(rendered_scenario["timeout_seconds"])

    if args.mock:
        result = _mock_run_commands(commands, target_name)
        cleanup_result = None
    else:
        if config is None:
            raise ValueError("Live scenario runs require a config file")
        try:
            result = run_commands(config, config_path, target_name, commands, timeout=timeout)
        except KeyboardInterrupt:
            print("\nInterrupted — disconnecting call...", flush=True)
            time.sleep(2)
            try:
                run_commands(config, config_path, target_name, [{"cmd": "disconnect"}], timeout=30)
                print("Call disconnected.", flush=True)
            except Exception as exc:
                print(f"Cleanup note: {exc}", flush=True)
            return 130
        cleanup_result = None
        if _scenario_needs_cleanup(commands, result):
            try:
                cleanup_result = run_commands(config, config_path, target_name, [{"cmd": "disconnect"}], timeout=30)
            except Exception as exc:
                cleanup_result = {"status": "failed", "error": str(exc)}

    passed, failures, observed_states = _evaluate_scenario(rendered_scenario, result)
    summary: dict[str, Any] = {
        "generated_at": _utc_now(),
        "name": scenario_name,
        "description": rendered_scenario.get("description"),
        "scenario_file": str(scenario_path),
        "mode": "mock" if args.mock else "live",
        "target": target_name,
        "commands": commands,
        "expect_status": str(rendered_scenario.get("expect_status") or "completed"),
        "expect_states": [str(state).upper() for state in rendered_scenario.get("expect_states", [])],
        "observed_states": observed_states,
        "passed": passed,
        "failures": failures,
        "result": result,
        "cleanup_result": cleanup_result,
    }

    evidence_path = None
    if not args.no_evidence:
        evidence_path = _write_evidence(summary, args.evidence_dir, scenario_name)
        summary["evidence_path"] = str(evidence_path)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        _print_scenario_summary(summary, evidence_path)
    return 0 if passed else 1


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="phone.py",
        description="JTAPI phone scenarios — drive a Cisco phone from the command line.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  phone.py dial        --destination 82001234\n"
            "  phone.py hold-resume --destination 82001234 --hold 30\n"
            "  phone.py transfer    --destination 82001234 --to 82005678\n"
            "  phone.py transfer    --destination 82001234 --to 82005678 --type blind\n"
            "  phone.py conference  --destination 82001234 --to 82005678\n"
            "  phone.py dtmf        --destination 82001234 --digits 5551\n"
            "  phone.py answer      --wait 60 --duration 30\n"
            "  phone.py run         scenarios/hold_resume_smoke.json\n"
            "  phone.py coverage\n"
        ),
    )
    sub = root.add_subparsers(dest="scenario", metavar="SCENARIO")
    sub.required = True

    # ── doctor ────────────────────────────────────────────────────────
    p_doctor = sub.add_parser("doctor", help="Check local JTAPI helper setup without placing a call")
    p_doctor.add_argument(
        "--config", default=str(DEFAULT_CONFIG), help="Path to config.json (default: config.json in this directory)"
    )
    p_doctor.add_argument("--target", default=None, help="Phone name from config (default: uses default_target)")
    p_doctor.add_argument("--timeout", type=float, default=3.0, help="TCP connect timeout in seconds (default: 3)")
    p_doctor.add_argument("--skip-network", action="store_true", help="Skip CUCM CTI port reachability check")
    p_doctor.add_argument("--skip-compile", action="store_true", help="Skip javac compile check")

    # ── run ───────────────────────────────────────────────────────────
    p_run = sub.add_parser("run", help="Run a reusable JSON scenario with pass/fail assertions")
    p_run.add_argument("scenario_file", help="Path to a JSON scenario file")
    p_run.add_argument(
        "--config", default=str(DEFAULT_CONFIG), help="Path to config.json (default: config.json in this directory)"
    )
    p_run.add_argument("--target", default=None, help="Override target phone name from config")
    p_run.add_argument("--timeout", type=int, default=None, help="Override Java helper timeout in seconds")
    p_run.add_argument(
        "--evidence-dir",
        default=str(DEFAULT_EVIDENCE_DIR),
        help="Directory for scenario evidence JSON (default: runs/)",
    )
    p_run.add_argument("--no-evidence", action="store_true", help="Do not write an evidence JSON file")
    p_run.add_argument("--json", action="store_true", help="Print the full scenario result JSON")
    p_run.add_argument("--mock", action="store_true", help="Simulate the scenario without controlling a live phone")
    p_run.add_argument(
        "--var",
        action="append",
        default=[],
        dest="variables",
        metavar="KEY=VALUE",
        help="Override a scenario variable, for example --var destination=82001234",
    )

    # ── coverage ─────────────────────────────────────────────────────
    p_coverage = sub.add_parser("coverage", help="Run an inspect or scenario proof across configured targets")
    p_coverage.add_argument(
        "--config", default=str(DEFAULT_CONFIG), help="Path to config.json (default: config.json in this directory)"
    )
    p_coverage.add_argument(
        "--target",
        action="append",
        default=[],
        dest="target_names",
        help="Target phone name from config; repeat to test a subset (default: all targets)",
    )
    p_coverage.add_argument("--scenario", dest="scenario_file", help="Optional scenario JSON to run on each target")
    p_coverage.add_argument("--timeout", type=int, default=None, help="Override Java helper timeout in seconds")
    p_coverage.add_argument(
        "--address-timeout",
        type=int,
        default=15,
        help="Seconds INSPECT waits for address IN_SERVICE when no scenario is supplied (default: 15)",
    )
    p_coverage.add_argument(
        "--evidence-dir",
        default=str(DEFAULT_EVIDENCE_DIR),
        help="Directory for coverage evidence JSON (default: runs/)",
    )
    p_coverage.add_argument("--no-evidence", action="store_true", help="Do not write an evidence JSON file")
    p_coverage.add_argument("--json", action="store_true", help="Print the full coverage result JSON")
    p_coverage.add_argument("--mock", action="store_true", help="Simulate coverage without controlling live phones")
    p_coverage.add_argument(
        "--var",
        action="append",
        default=[],
        dest="variables",
        metavar="KEY=VALUE",
        help="Override a scenario variable when --scenario is supplied",
    )

    # ── dial ─────────────────────────────────────────────────────────
    p_dial = sub.add_parser("dial", help="Dial a number, stay connected, hang up")
    p_dial.add_argument("--destination", required=True, help="Number to dial")
    p_dial.add_argument("--seconds", type=int, default=30, help="Seconds to stay connected (default: 30)")
    _common_args(p_dial)

    # ── hold-resume ───────────────────────────────────────────────────
    p_hr = sub.add_parser("hold-resume", help="Dial, hold, resume, hang up")
    p_hr.add_argument("--destination", required=True, help="Number to dial")
    p_hr.add_argument("--connected", type=int, default=10, help="Seconds connected before hold (default: 10)")
    p_hr.add_argument("--hold", type=int, default=10, help="Seconds on hold (default: 10)")
    p_hr.add_argument("--resume", type=int, default=10, help="Seconds connected after resume (default: 10)")
    _common_args(p_hr)

    # ── transfer ──────────────────────────────────────────────────────
    p_tx = sub.add_parser("transfer", help="Dial then transfer to a second number")
    p_tx.add_argument("--destination", required=True, help="Number to dial first")
    p_tx.add_argument("--to", required=True, help="Number to transfer to")
    p_tx.add_argument(
        "--type",
        choices=["consult", "blind"],
        default="consult",
        help="consult = supervised (default), blind = immediate redirect",
    )
    p_tx.add_argument(
        "--wait", type=int, default=10, help="Seconds to stay connected before transferring (default: 10)"
    )
    _common_args(p_tx)

    # ── conference ────────────────────────────────────────────────────
    p_conf = sub.add_parser("conference", help="Dial, add a third party, stay in conference, hang up")
    p_conf.add_argument("--destination", required=True, help="First number to dial")
    p_conf.add_argument("--to", required=True, help="Third party to add to the conference")
    p_conf.add_argument(
        "--wait", type=int, default=10, help="Seconds connected before adding the third party (default: 10)"
    )
    p_conf.add_argument(
        "--duration", type=int, default=30, help="Seconds to stay in conference before hanging up (default: 30)"
    )
    _common_args(p_conf)

    # ── dtmf ──────────────────────────────────────────────────────────
    p_dtmf = sub.add_parser("dtmf", help="Dial a number and send DTMF tones (IVR, door unlock, etc.)")
    p_dtmf.add_argument("--destination", required=True, help="Number to dial")
    p_dtmf.add_argument("--digits", required=True, help="DTMF digits to send, e.g. 5551 or 1234#")
    p_dtmf.add_argument(
        "--wait", type=int, default=5, help="Seconds to wait after call connects before sending DTMF (default: 5)"
    )
    p_dtmf.add_argument(
        "--after", type=int, default=10, help="Seconds to stay connected after sending DTMF (default: 10)"
    )
    _common_args(p_dtmf)

    # ── answer ────────────────────────────────────────────────────────
    p_ans = sub.add_parser("answer", help="Wait for an inbound call, answer it, hang up")
    p_ans.add_argument("--wait", type=int, default=60, help="Seconds to wait for the inbound call (default: 60)")
    p_ans.add_argument(
        "--duration", type=int, default=30, help="Seconds to stay connected after answering (default: 30)"
    )
    _common_args(p_ans)

    return root


# ── Execution ─────────────────────────────────────────────────────────

# Dispatch table: maps the argparse 'scenario' dest value to its builder
# function.  When a new scenario is added, register it here and write a
# matching _scenario_<name>() function above.
SCENARIO_BUILDERS = {
    "dial": _scenario_dial,
    "hold-resume": _scenario_hold_resume,
    "transfer": _scenario_transfer,
    "conference": _scenario_conference,
    "dtmf": _scenario_dtmf,
    "answer": _scenario_answer,
}


def run(args: argparse.Namespace) -> None:
    if args.scenario == "doctor":
        raise SystemExit(_run_doctor(args))
    if args.scenario == "run":
        raise SystemExit(_run_scenario_file(args))
    if args.scenario == "coverage":
        raise SystemExit(_run_coverage(args))

    config, config_path = load_config(args.config)

    builder = SCENARIO_BUILDERS[args.scenario]
    title, steps, commands = builder(args)
    target = args.target or config["jtapi"].get("default_target")

    # ── Print plan ────────────────────────────────────────────────────
    width = 60
    print()
    print("─" * width)
    print(f"  {title}")
    print(f"  Phone: {target}")
    print("─" * width)
    for step in steps:
        print(f"  {step}")
    print("─" * width)
    print()

    # ── Execute with cleanup on interrupt ─────────────────────────────
    # If Ctrl+C is pressed (or SIGTERM received) while the JVM is running,
    # the subprocess is killed and we land here.  We open a fresh JTAPI
    # session and immediately send DISCONNECT so the call clears from the
    # phone — otherwise it stays active until CUCM's idle timeout fires.
    try:
        result = run_commands(config, config_path, target, commands)
    except KeyboardInterrupt:
        print("\nInterrupted — disconnecting call...", flush=True)
        # Brief pause to let the killed Java process fully release the CTI
        # provider before we try to open a new one on the same device.
        # Without this, the cleanup JTAPI session may fail to register.
        time.sleep(2)
        try:
            run_commands(config, config_path, target, [{"cmd": "disconnect"}], timeout=30)
            print("Call disconnected.", flush=True)
        except Exception as exc:
            # The call may have already dropped when the CTI connection died.
            print(f"Cleanup note: {exc}", flush=True)
        # Exit 130 is the POSIX standard for processes terminated by SIGINT.
        # Shells use this to detect keyboard interrupts vs other failures.
        sys.exit(130)

    # ── Print result ──────────────────────────────────────────────────
    status = result.get("status", "unknown")

    print(f"  Status: {status.upper()}")
    print()

    states = result.get("states", [])
    if states:
        print("  State transitions:")
        for s in states:
            # Timestamps from the JVM are ISO-8601; trim to just the time
            # portion (HH:MM:SS.mmm) for compact output.
            ts = s.get("at", "")[-15:]
            print(f"    {ts}  →  {s.get('state', '')}")
        print()

    if status != "completed":
        error = result.get("error", "unknown error")
        print(f"  ERROR: {error}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
