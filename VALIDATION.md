# JTAPI Helper Validation

This file records public-safe validation notes for the helper. It intentionally avoids private hostnames, credentials, device IDs, directory numbers, and generated evidence payloads.

## Golden Validation

Date: 2026-05-04

Environment: isolated CUCM 15 lab with Cisco 8875, Cisco 8811, and Cisco Jabber/CSF targets.

Validated device families:

| Device family | Result | Notes |
|---|---|---|
| Cisco 8875 | PASS | Live JTAPI control validated |
| Cisco 8811 | PASS | Live JTAPI control validated |
| Cisco Jabber / CSF softphone | PASS | Live JTAPI control validated after adding the CSF device to the application user's controllable devices |

Validation set:

| Scenario | Device coverage | Mode | Result | Observed states |
|---|---|---|---|---|
| `control_probe` | 8875, 8811, Jabber/CSF | live | PASS | `IDLE -> DISCONNECTED` |
| `basic_dial_smoke` | hard phone | live | PASS | `IDLE -> CONNECTED -> DISCONNECTED` |
| `hold_resume_smoke` | hard phone and Jabber/CSF | live | PASS | `IDLE -> CONNECTED -> HELD -> TALKING -> DISCONNECTED` |
| `dtmf_smoke` | hard phone | live | PASS | `IDLE -> CONNECTED -> DISCONNECTED` |

Validated targets were inspected before and after command-path testing and ended `IN_SERVICE`, registered, and `IDLE`.

Application-user control is a required setup step. If the CUCM application user cannot control a device, JTAPI may fail with a provider-domain error even when the device exists in CUCM and has a valid line.

Use `python phone.py coverage` for the first no-call live check in a new environment. Use `python phone.py coverage --scenario scenarios/control_probe.json` after inspect coverage passes to prove the command path across all configured targets without placing calls.

Generated evidence should stay local and ignored by git.

## Public Release Notes

- Do not publish private `config.json`, generated evidence, CUCM hostnames, phone MACs, directory numbers, or credentials.
- Do not publish vendor-provided Cisco JTAPI jars. Users should download the Cisco JTAPI plugin from their own CUCM and place the jars in `lib/` locally.
- The reusable scenarios are target-agnostic. Use `--target` to choose the controlled phone.
- For hold, resume, and DTMF tests, use a real answered far end. A second CUCM phone is preferred over a mobile/PSTN leg when validating deterministic JTAPI call control.