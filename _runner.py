"""Standalone JTAPI runner — Python stdlib only, no acme_test_automation required.

Loads config.json, compiles PhoneController.java if needed, and runs it
against a Cisco phone.  Used by phone.py and example_basic_call.py so
that the entire jtapi-phone-controller/ folder is self-contained.

Config file format (config.json next to phone.py):

    {
      "jtapi": {
        "provider":       "10.0.0.1",
        "username":       "jtapi-user",
        "password":       "yourpassword",    // or "env:MY_ENV_VAR"
        "lib_dir":        "lib",             // path to Cisco JTAPI jars
        "default_target": "my-phone",
        "targets": [
          {
            "name":             "my-phone",
            "device_name":      "SEP001122334455",
            "directory_number": "82001234",
            "description":      "My desk phone"
          }
        ]
      }
    }

lib_dir may be relative (resolved from the config file's directory) or absolute.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

# ── Module-level paths ────────────────────────────────────────────────────────
# All paths are resolved relative to this file so the folder stays portable
# no matter where it is placed on the recipient's machine.

HERE = Path(__file__).resolve().parent  # the jtapi-phone-controller/ directory itself
SOURCE_DIR = HERE / "src" / "acme" / "jtapi"  # where PhoneController.java lives
BUILD_DIR = HERE / "build" / "classes"  # javac output; created automatically on first run

# The Java source file and the fully-qualified class name used to launch the JVM.
# If you ever rename or move PhoneController.java, update both of these.
PHONE_CONTROLLER_SOURCE = SOURCE_DIR / "PhoneController.java"
PHONE_CONTROLLER_CLASS = "acme.jtapi.PhoneController"


# ── Config loading ─────────────────────────────────────────────────────────────


def load_config(config_path: str | Path) -> tuple[dict, Path]:
    """Load config JSON and return (config_dict, resolved_path).

    Returns a tuple rather than just the dict because several downstream
    functions need the resolved path to resolve relative sub-paths (e.g.
    lib_dir: "lib" is expanded relative to the config file's directory).

    Raises FileNotFoundError with a helpful message if the file is missing.
    """
    path = Path(config_path).resolve()
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\nCopy config.example.json → config.json and fill in your details."
        )
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if "jtapi" not in raw:
        raise ValueError(f"Config is missing top-level 'jtapi' key: {path}")
    return raw, path


def resolve_value(value: str) -> str:
    """Expand  env:VAR_NAME  references to the actual environment variable value.

    This lets you store sensitive values like passwords in environment variables
    instead of plain text in config.json.  Example config entry:
        "password": "env:CUCM_JTAPI_PASSWORD"
    Then set it in your shell:
        export CUCM_JTAPI_PASSWORD="mysecretpassword"
    The string must start with exactly 'env:' — anything else is returned as-is.
    """
    if isinstance(value, str) and value.startswith("env:"):
        var = value[4:]
        resolved = os.environ.get(var)
        if resolved is None:
            raise OSError(f"Environment variable {var!r} is not set (referenced in config as 'env:{var}')")
        return resolved
    return value


def resolve_target(config: dict, target_name: str | None) -> dict:
    """Find the phone target by name (or default_target when name is None).

    A 'target' is one entry from config['jtapi']['targets'] — it holds the
    device_name (SEP MAC) and directory_number for a specific phone.
    Raises a clear ValueError listing available target names if the requested
    one is not found, so the user knows exactly what to fix.
    """
    jtapi = config["jtapi"]
    name = target_name or jtapi.get("default_target")
    if not name:
        raise ValueError("No --target supplied and no default_target set in config.")
    targets = jtapi.get("targets", [])
    for t in targets:
        if t.get("name") == name:
            return t
    available = [t.get("name") for t in targets]
    raise ValueError(f"Target {name!r} not found in config.  Available: {available}")


def _lib_dir(config: dict, config_path: Path) -> Path:
    """Resolve the Cisco JTAPI jar directory from config.

    lib_dir in config.json can be:
      - Relative: "lib"  → resolved relative to config.json's directory
      - Absolute: "/opt/cisco/jtapi/lib"
    In the standard portable setup the jars live in lib/ and
    config.json sits next to them, so "lib" is all you need.
    """
    lib = config["jtapi"].get("lib_dir")
    if not lib:
        raise ValueError(
            "Config is missing 'jtapi.lib_dir' — the path to the Cisco JTAPI jar directory.\n"
            "Download from  https://<your-cucm>/plugins/  (Cisco JTAPI plug-in) and place the jars there."
        )
    p = Path(lib)
    if not p.is_absolute():
        # Resolve relative to where config.json lives, not the current working directory.
        # This means 'lib' always means <config-dir>/lib regardless of where you cd first.
        p = config_path.parent / p
    return p.resolve()


def resolve_lib_dir(config: dict, config_path: Path) -> Path:
    return _lib_dir(config, config_path)


# ── Compilation ────────────────────────────────────────────────────────────────


def _jar_classpath(lib_dir: Path) -> str:
    """Build a colon-separated classpath string from all .jar files in lib_dir.

    Sorted for reproducibility — the order affects which class wins if two jars
    export the same class (unlikely here, but good practice).
    Used for both javac (compile) and java (run) invocations.
    """
    jars = sorted(str(j) for j in lib_dir.glob("*.jar"))
    if not jars:
        raise FileNotFoundError(
            f"No .jar files found in {lib_dir}\n"
            "Download Cisco JTAPI from  https://<your-cucm>/plugins/  and place the jars there."
        )
    return ":".join(jars)


def _runtime_classpath(lib_dir: Path) -> str:
    """Return the full runtime classpath: Cisco jars + our compiled classes.

    BUILD_DIR is appended so the JVM can find acme.jtapi.PhoneController
    alongside the Cisco jars it depends on.
    """
    return f"{_jar_classpath(lib_dir)}:{BUILD_DIR}"


def _ensure_compiled(lib_dir: Path) -> None:
    """Compile PhoneController.java if the .class file is missing or stale.

    Uses mtime comparison — if the .java file is newer than the .class file,
    recompile.  This means you can edit PhoneController.java and the change
    takes effect automatically on the next run without any manual build step.
    To force a full recompile (e.g. after switching JDK versions):
        rm -rf build/classes
    """
    class_file = BUILD_DIR / "acme" / "jtapi" / "PhoneController.class"
    # Skip compilation if the class is up to date
    if class_file.exists() and class_file.stat().st_mtime >= PHONE_CONTROLLER_SOURCE.stat().st_mtime:
        return
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    # Remove stale class first so a failed compile doesn't leave a half-updated file
    if class_file.exists():
        class_file.unlink()
    result = subprocess.run(
        # -cp: Cisco jars (PhoneController.java imports from them)
        # -d:  output directory for the compiled .class files
        ["javac", "-cp", _jar_classpath(lib_dir), "-d", str(BUILD_DIR), str(PHONE_CONTROLLER_SOURCE)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Java compile failed:\n{result.stderr.strip() or result.stdout.strip()}")


def ensure_compiled(lib_dir: Path) -> None:
    _ensure_compiled(lib_dir)


# ── Command protocol ───────────────────────────────────────────────────────────


def _build_command_lines(commands: list[dict]) -> str:
    """Convert command dicts to the stdin line protocol PhoneController reads.

    PhoneController.java reads one command per line from stdin.  The format is:
        CMD key=value key=value ...
    For example:
        DIAL destination=82001234
        SLEEP seconds=10
        HOLD
        DISCONNECT

    Each dict has a 'cmd' key; all other keys become  key=value  params.
    Values containing spaces are automatically quoted.
    """
    lines: list[str] = []
    for entry in commands:
        if not isinstance(entry, dict):
            continue
        cmd = str(entry.get("cmd", "")).upper()
        if not cmd:
            continue
        parts = [cmd]
        for key, value in entry.items():
            if key == "cmd":
                continue
            sv = str(value)
            if " " in sv:
                sv = f'"{sv}"'
            parts.append(f"{key}={sv}")
        lines.append(" ".join(parts))
    return "\n".join(lines) + "\n" if lines else ""


# ── Execute ────────────────────────────────────────────────────────────────────


def run_commands(
    config: dict,
    config_path: Path,
    target_name: str | None,
    commands: list[dict],
    timeout: int | None = None,
) -> dict:
    """Compile (if needed) and run PhoneController against the named target.

    Returns the parsed JSON result dict from the Java bridge:
        {"status": "completed"|"failed", "actions": [...], "states": [...], "events": [...]}
    """
    jtapi = config["jtapi"]
    lib_dir = _lib_dir(config, config_path)

    if not lib_dir.exists():
        raise FileNotFoundError(
            f"JTAPI lib directory not found: {lib_dir}\n"
            "Download Cisco JTAPI from  https://<your-cucm>/plugins/  and place the jars there."
        )

    _ensure_compiled(lib_dir)

    target = resolve_target(config, target_name)
    provider = resolve_value(str(jtapi.get("provider", "")))
    username = resolve_value(str(jtapi.get("username", "")))
    password = resolve_value(str(jtapi.get("password", "")))

    # Build the positional argument list for the JVM.
    # PhoneController.java reads these from its main(String[] args) in this exact order.
    java_command = [
        "java",
        "-cp",
        _runtime_classpath(lib_dir),
        PHONE_CONTROLLER_CLASS,
        provider,  # args[0]: CUCM IP / hostname
        username,  # args[1]: CTI application user login
        password,  # args[2]: CTI application user password
        str(target["device_name"]),  # args[3]: SEP MAC, e.g. SEP001122334455
        str(target.get("directory_number", "")),  # args[4]: DN on the phone's first line
        "custom",  # args[5]: operation mode — "custom" = stdin protocol
    ]
    # Encode the command list into the line protocol and pass it to the JVM via stdin.
    # The JVM reads until EOF, executes each command in sequence, then prints one JSON
    # object to stdout and exits.
    stdin_payload = _build_command_lines(commands)

    try:
        completed = subprocess.run(
            java_command,
            input=stdin_payload,  # commands sent to the JVM over stdin
            text=True,  # treat stdin/stdout/stderr as text (not bytes)
            capture_output=True,  # capture stdout and stderr; don't print to terminal
            check=False,  # don't raise on non-zero exit — we handle it below
            timeout=timeout,  # None = no timeout; int = seconds before killing the JVM
        )
    except subprocess.TimeoutExpired as exc:
        # The JVM didn't finish in time.  Return a structured failure dict so the
        # caller can handle it the same way as any other failure.
        return {
            "status": "failed",
            "error": f"Timed out after {exc.timeout}s",
            "partial_output": (exc.stdout or "").strip(),
        }

    if completed.stdout:
        try:
            # Happy path: PhoneController printed a JSON object to stdout.
            return json.loads(completed.stdout)
        except json.JSONDecodeError:
            # Unexpected output — probably a JVM crash or startup error printed as plain text.
            return {"status": "failed", "error": f"Non-JSON output: {completed.stdout.strip()}"}

    # No stdout at all — the JVM either printed only to stderr or crashed silently.
    return {
        "status": "failed",
        "error": completed.stderr.strip() or f"Java exited with code {completed.returncode}",
    }
