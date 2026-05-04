# jtapi-phone-controller

> Drive real Cisco phones and Jabber/CSF softphones from a Python CLI over CUCM JTAPI; no vendor client, no GUI, no pip dependencies. Bring your CUCM JTAPI jars.


`jtapi-phone-controller` is a compact Python + Java bridge that lets you script real Cisco IP phones and Jabber/CSF softphones registered to Cisco Unified Communications Manager (CUCM) over JTAPI. Dial, hold, resume, transfer, conference, send DTMF, answer inbound calls, or run reusable JSON smoke scenarios — all from a single command line, all producing structured JSON output suitable for CI or test automation.


**Tested on CUCM 15 with Cisco 8875, Cisco 8811, and Cisco Jabber/CSF.** Should work on any CUCM 12.5+ cluster with CTI-controllable hard phones or softphones, but live validation so far has been on CUCM 15.

**Cross-platform:**
- **Linux (Oracle Linux 8 and 9):** Original development and smoke testing target.
- **macOS (Apple Silicon):** Tested and working on macOS (Darwin/arm64, Python 3.14, OpenJDK 17). No known issues.
- **Windows:** Not tested. Please open an issue if you hit any platform-specific bugs.

If you run into any cross-platform issues, please open an issue — the goal is for this to work out-of-the-box on any modern Linux or macOS system with Python 3.9+, JDK 11+, and the Cisco JTAPI plugin jars from your CUCM cluster.

| Area | Status |
|---|---|
| CUCM 15 | Live validated |
| CUCM 12.5+ | Expected, not fully matrix-tested |
| Cisco 8875 | Live validated |
| Cisco 8811 | Live validated |
| Cisco Jabber / CSF softphone | Live validated |
| macOS arm64 | Tested |
| Linux | Primary target |
| Windows | Not tested |

```bash
# One-off
python phone.py dial --destination 14155550123

# In a test script
python phone.py hold-resume --destination 14155550123 --hold 30

# Repeatable scenario file, no live phone required
python phone.py run scenarios/hold_resume_smoke.json --mock --no-evidence
```

---

## Why this exists

Most CUCM-adjacent test automation I've seen either:

- drives phones by screen-scraping the CUCM Self-Care portal (fragile), or
- uses a heavyweight commercial test harness (expensive, slow to onboard), or
- stops at pure AXL/RIS introspection and never actually makes a call.

This tool does the one thing those options skip: **drive a real registered CTI-controllable phone or softphone through ring, answer, hold, resume, and disconnect states under script control**, then emit a structured log you can assert against.

I originally built it as part of a lab SBC upgrade certification framework — we needed repeatable, audit-ready evidence that real phones completed real calls end-to-end through each step of an upgrade. It's small, portable, and hopefully useful to anyone doing CUCM testing, SBC certification, or regression automation for voice infra.

---

## Features

- **Setup doctor** — `python phone.py doctor` checks Python, Java, `javac`, config, JTAPI jars, target config, compile status, and CUCM CTI reachability.
- **Six call scenarios** out of the box: `dial`, `hold-resume`, `transfer` (consult or blind), `conference`, `dtmf`, `answer`.
- **Target coverage rollup** — `python phone.py coverage` proves configured targets are visible, registered, idle, and optionally runs a scenario across all targets.
- **Reusable JSON scenarios** — `python phone.py run scenarios/hold_resume_smoke.json` runs repeatable smoke tests with pass/fail assertions.
- **Mock mode** — validate scenario parsing, state assertions, and CI smoke checks without controlling a live phone.
- **Structured JSON output** — every run emits an ordered `actions` log, a `states` timeline (IDLE → CONNECTED → HELD → DISCONNECTED), and raw JTAPI `events` for full audit trail.
- **No pip dependencies** — the Python side uses only the stdlib. Live runs require Python 3.9+, JDK 11+, and the Cisco JTAPI plugin jars from your CUCM cluster.
- **Self-cleaning** — `SIGINT` / `SIGTERM` triggers a graceful `DISCONNECT`, and the Java bridge removes observers and shuts down the JTAPI provider on exit.
- **Portable** — compact Python + Java repo. Drop it anywhere.

---

## How it works

```
phone.py
  └─ _runner.py
       └─ compiles src/acme/jtapi/PhoneController.java (once, on first run)
            └─ spawns JVM
                 ├─ stdin:  line protocol (DIAL destination=14155550123)
                 └─ stdout: single JSON object when the session completes
                      └─ PhoneController connects to CUCM CTI (port 2748) via JTAPI
```

The Python side handles argparse, config, subprocess management, signal trapping, and pretty-printing. The Java side is a thin JTAPI bridge that takes commands off stdin, drives a `javax.telephony.Provider` + `Address` + `Terminal`, and streams events + states back as structured JSON.

---

## Quick start


### 1. Prerequisites

- **Linux host recommended.** (Tested on Oracle Linux 8 and 9. Should work on any modern Linux.)
- Python 3.9 or newer (`python3 --version`)
- JDK 11 or newer (`javac -version` **and** `java -version` must both work)
- Network reachability to your CUCM CTI Manager on TCP 2748

### 2. Configure

```bash
git clone https://github.com/arocharoux/jtapi-phone-controller.git
cd jtapi-phone-controller
cp config.example.json config.json
```

Edit `config.json`:

| Field | What to put |
|---|---|
| `jtapi.provider` | IP or FQDN of the CUCM node running CTI Manager |
| `jtapi.username` | The CUCM **Application User** you created for JTAPI (see [CUCM_SETUP.md](CUCM_SETUP.md)) |
| `jtapi.password` | Literal password, or `env:VAR_NAME` to read from an env var |
| `jtapi.targets[].device_name` | The CUCM device name, such as `SEPxxxxxxxxxxxx` for hard phones or `CSF...` for Jabber |
| `jtapi.targets[].directory_number` | The extension on that target's first line |

The CUCM application user must be allowed to control every target device. Either grant `Standard CTI Allow Control of All Devices`, or explicitly move each hard phone or Jabber/CSF device into the application user's **Controlled Devices** list.

> **First time touching CUCM?** Read [CUCM_SETUP.md](CUCM_SETUP.md) before going further. It walks through creating the Application User, enabling CTI control on a phone, and verifying the CTI Manager is reachable.

### 3. Run

```bash
python phone.py doctor
python phone.py dial --destination 14155550123
```

You should see a printed plan, state transitions in real time, and a final JSON summary.

Add `--help` to any subcommand for every flag.

### 4. Try the scenario runner without a phone

Mock mode lets you validate the CLI, JSON scenario files, expected state assertions, and CI smoke path without CUCM, Cisco JTAPI jars, or a registered phone:

```bash
python phone.py run scenarios/hold_resume_smoke.json --mock --no-evidence
python phone.py coverage --config config.example.json --mock --no-evidence
```

Expected shape:

```text
PASS hold_resume_smoke
Mode: mock
Target: my-phone
Status: completed
States: IDLE -> CONNECTED -> HELD -> TALKING -> DISCONNECTED
```

---

## Scenarios

| Scenario | Key flags | Behavior |
|---|---|---|
| `doctor` | `--target`, `--skip-network`, `--skip-compile`, `--timeout` | Check local setup and CTI reachability before placing calls |
| `run` | `scenario_file`, `--mock`, `--var`, `--evidence-dir`, `--json` | Run reusable JSON scenarios with pass/fail assertions |
| `coverage` | `--target`, `--scenario`, `--mock`, `--var`, `--evidence-dir`, `--json` | Run inspect coverage or a scenario across configured targets |
| `dial` | `--destination`, `--seconds` | Dial, stay connected, hang up |
| `hold-resume` | `--destination`, `--connected`, `--hold`, `--resume` | Dial, hold, resume, hang up |
| `transfer` | `--destination`, `--to`, `--type {consult,blind}`, `--wait` | Dial then transfer |
| `conference` | `--destination`, `--to`, `--wait`, `--duration` | Dial, merge in a third party, hang up |
| `dtmf` | `--destination`, `--digits`, `--wait`, `--after` | Dial an IVR, pause, send DTMF |
| `answer` | `--wait`, `--duration` | Wait for inbound, answer, stay, hang up |

All live scenarios accept `--target <name>` and `--config <path>`.

Reusable scenarios live in [scenarios](scenarios):

```bash
python phone.py run scenarios/basic_dial_smoke.json
python phone.py run scenarios/hold_resume_smoke.json
python phone.py run scenarios/dtmf_smoke.json
python phone.py run scenarios/control_probe.json
```

Use `--mock` when you want to validate the scenario file without a CUCM connection or Cisco JTAPI jars:

```bash
python phone.py run scenarios/hold_resume_smoke.json --mock --no-evidence
```

Override scenario variables without editing the JSON file:

```bash
python phone.py run scenarios/hold_resume_smoke.json --var destination=82001234 --var pre_hold_seconds=15
```

By default, live scenario runs write evidence JSON under `runs/`, which is ignored by git.

### Target coverage

Use `coverage` before placing live calls. With no scenario, it sends `inspect` to every configured target and checks for CTI visibility, registration, `IN_SERVICE`, and `IDLE` state without placing a call.

```bash
python phone.py coverage
python phone.py coverage --target desk-8875 --target jabber-csf
python phone.py coverage --mock --config config.example.json --no-evidence
```

To prove command execution across every configured target, pass a scenario. `control_probe` is the safest first live scenario because it inspects the device and sends an idempotent `disconnect` without dialing.

```bash
python phone.py coverage --scenario scenarios/control_probe.json
python phone.py coverage --scenario scenarios/hold_resume_smoke.json --var destination=82001234
```

If multiple targets share a directory number, coverage records a warning. Inspect coverage can still prove each device by terminal name; unique DNs make call-flow proof cleaner.

---

## Output format

```json
{
  "status": "completed",
  "actions": [
    { "at": "2026-04-16T03:08:15Z", "action": "dial", "detail": "14155550123" }
  ],
  "states": [
    { "at": "2026-04-16T03:08:15Z", "state": "IDLE" },
    { "at": "2026-04-16T03:08:17Z", "state": "CONNECTED" },
    { "at": "2026-04-16T03:08:27Z", "state": "DISCONNECTED" }
  ],
  "events": [
    { "at": "...", "event": "connect-returned", "detail": "..." }
  ]
}
```

- `actions` — commands the controller executed, in order
- `states` — phone state transitions (the headline timeline for assertions)
- `events` — raw JTAPI events from CUCM (full audit trail)

Pipe to `jq`, diff runs, assert on specific state transitions — whatever your test harness needs.

---

## Available low-level commands

If you want to script your own sequences instead of using the built-in scenarios, every command below is available to `_runner.run_commands()`:

| Command | Required | Optional | Behavior |
|---|---|---|---|
| `dial` | `destination` | `timeout` (60) | Dial, wait until TALKING |
| `answer` | — | `timeout` (60) | Wait for RINGING, answer, wait until TALKING |
| `hold` | — | `timeout` (30) | Hold active call, wait for HELD |
| `resume` | — | `timeout` (30) | Resume held call, wait for TALKING |
| `transfer` | `destination` | `type` (consult\|blind), `timeout` (60) | Consult waits for far-end answer before completing |
| `conference` | `destination` | `timeout` (60) | Consult then merge |
| `send_dtmf` | `digits` | — | Send DTMF on active call |
| `disconnect` | — | — | Hang up |
| `sleep` | `seconds` | — | Pause; call stays up |
| `wait` | `state` | `timeout` (60) | Block until named state |
| `inspect` | — | — | Diagnostics only — no call |

Valid `wait` states: `TALKING`, `HELD`, `RINGING`, `DISCONNECTED`, `ALERTING`.

---

## Validation

The current helper was live-validated in an isolated CUCM lab with Cisco 8875, Cisco 8811, and Cisco Jabber/CSF targets. The public-safe validation notes are in [VALIDATION.md](VALIDATION.md).

The repository also includes a GitHub Actions smoke workflow that checks Python syntax, JSON scenario files, mock scenario execution, and the absence of private/generated files. Live JTAPI calls are not run in CI because they require a CUCM cluster, CTI credentials, Cisco JTAPI jars, and registered phones.

---

## Troubleshooting

**`Config file not found`** — you haven't copied `config.example.json` to `config.json`.

**`javac: command not found`** — install a JDK (not just a JRE). `brew install openjdk@17` on macOS.

**`JTAPI lib directory not found`** — the `lib/` folder is missing the Cisco JTAPI jars. Download the Cisco JTAPI plugin from `https://<your-cucm>/plugins/` (CUCM Admin → Application → Plugins → Cisco JTAPI) and drop the `.jar` files into `lib/`. They are **not included in this repo** because they're Cisco-licensed.

**Provider times out / no `IN_SERVICE` within 60s**
1. `telnet <cucm> 2748` — is port reachable?
2. Are credentials correct? (Log into CUCM Admin with the app user — if that fails, JTAPI will fail too.)
3. Is CTI Manager service running on that CUCM node? Check **Cisco Unified Serviceability → Tools → Control Center - Feature Services**.

**`No TALKING event received`** — call connected at CUCM but JTAPI didn't see it. Check:
- "Allow Control of Device from CTI" is enabled on the phone's line (CUCM Admin → Device → Phone → Line → Advanced).
- The application user has the device associated to it.

**`Terminal <device> is not in provider's domain`** — CTI Manager authenticated, but the application user cannot control that device. Add the phone or Jabber/CSF device to the application user's **Controlled Devices** list, or grant `Standard CTI Allow Control of All Devices`. Also confirm the line has "Allow Control of Device from CTI" enabled and that the target is registered.

**Call left active after a crash** — pick up and hang up, or:
```bash
python phone.py dial --destination <any> --seconds 1
```

**Stale compiled class after editing `PhoneController.java`** — the engine recompiles when `.java` is newer than `.class`. Force with:
```bash
rm -rf build/classes
```

---

## Repository layout

```
.
├── phone.py                               # Main CLI — six scenarios
├── example_basic_call.py                  # Minimal starter script, heavily commented
├── _runner.py                             # Shared engine: config, subprocess, parse
├── config.example.json                    # Config template (copy to config.json)
├── CHANGELOG.md                           # Public release notes
├── scenarios/                             # Reusable JSON smoke scenarios
├── VALIDATION.md                          # Public-safe validation notes
├── src/acme/jtapi/PhoneController.java    # JTAPI bridge
├── lib/README.md                          # Place Cisco JTAPI jars here locally
├── build/classes/                         # Compiled Java (auto-created, gitignored)
├── .github/workflows/smoke.yml            # Public smoke checks
├── CUCM_SETUP.md                          # How to create the application user + enable CTI control
└── README.md                              # This file
```

---

## What this is not

- **Not a soft phone.** No media path, no codec negotiation. It drives a real registered hardphone via CUCM's CTI.
- **Not a full JTAPI library.** It's a scenario runner with enough bridge logic to cover the 90% case. If you need conference barge, shared-line hold, or silent monitor, you'll need to extend `PhoneController.java`.
- **Not tested on softphones** (Jabber, Webex App). In theory it should work — the CTI layer is the same — but I haven't verified.

---

## License

MIT. See `LICENSE`.

## Releases

See [CHANGELOG.md](CHANGELOG.md) for release notes.

---

## Credits

Built by [Alexis Rocha-Roux](https://www.linkedin.com/in/alexisrocharoux/) as part of a larger SBC upgrade certification framework. Extracted here because the phone-control bridge turned out to be useful on its own.

Contributions welcome. Open an issue if something in `CUCM_SETUP.md` doesn't match your cluster — CUCM version drift is real.
