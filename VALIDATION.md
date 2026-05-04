# JTAPI Helper Validation

This file records public-safe validation notes for the helper. It intentionally avoids private hostnames, credentials, device IDs, directory numbers, and generated evidence payloads.

## Golden Validation

Date: 2026-05-03

Environment: isolated CUCM lab with two Cisco hard phones, one caller and one answer phone.

Validation set:

| Scenario | Mode | Result | Observed states |
|---|---|---|---|
| `basic_dial_smoke` | live | PASS | `IDLE -> CONNECTED -> DISCONNECTED` |
| `hold_resume_smoke` | live | PASS | `IDLE -> CONNECTED -> HELD -> TALKING -> DISCONNECTED` |
| `dtmf_smoke` | live | PASS | `IDLE -> CONNECTED -> DISCONNECTED` |

Both phones were inspected before and after the run and ended `IN_SERVICE`, registered, and `IDLE`.

Generated evidence should stay local and ignored by git.

## Public Release Notes

- Do not publish private `config.json`, generated evidence, CUCM hostnames, phone MACs, directory numbers, or credentials.
- Do not publish vendor-provided Cisco JTAPI jars. Users should download the Cisco JTAPI plugin from their own CUCM and place the jars in `lib/` locally.
- The reusable scenarios are target-agnostic. Use `--target` to choose the controlled phone.
- For hold, resume, and DTMF tests, use a real answered far end. A second CUCM phone is preferred over a mobile/PSTN leg when validating deterministic JTAPI call control.