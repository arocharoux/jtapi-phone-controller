# Changelog

## Unreleased

### Added

- `scenarios/control_probe.json` for a no-call JTAPI command-path check using `inspect` plus idempotent `disconnect`.
- `phone.py coverage` for all-target inspect coverage and optional multi-target scenario execution.

### Changed

- Updated public validation notes to show live-tested Cisco 8875, Cisco 8811, and Cisco Jabber/CSF targets.
- Clarified that the CUCM application user must be allowed to control each target device, either through `Standard CTI Allow Control of All Devices` or explicit **Controlled Devices** association.

## v0.2.0 - Reusable Scenario Validation

Released: 2026-05-03

### Added

- `phone.py doctor` setup checker for Python, Java, `javac`, config, JTAPI jars, target config, bridge compile status, and CUCM CTI reachability.
- `phone.py run` reusable JSON scenario runner with pass/fail assertions.
- Mock mode for scenario validation without CUCM, Cisco JTAPI jars, or registered phones.
- Reusable smoke scenarios for basic dial, hold/resume, and DTMF.
- Public-safe validation notes in `VALIDATION.md`.
- GitHub Actions smoke workflow for syntax checks, JSON validation, mock scenarios, and private/generated file checks.
- `lib/README.md` placeholder explaining that users must provide Cisco JTAPI jars locally.

### Changed

- Improved Java JTAPI bridge cleanup by removing observers and shutting down the provider on exit.
- Made `DISCONNECT` idempotent so scenarios tolerate calls already cleared by the remote side.
- Updated README and CUCM setup docs for the scenario runner and public-safe config defaults.

### Removed

- Replaced the old library placeholder with `lib/README.md`.

## v0.1.0 - Initial Public Release

Released: 2026-04-16

### Added

- Standalone Python CLI for Cisco phone control over CUCM JTAPI.
- Java `PhoneController` bridge for dial, hold, resume, transfer, conference, DTMF, answer, inspect, wait, sleep, and disconnect commands.
- Public CUCM setup guide and MIT license.