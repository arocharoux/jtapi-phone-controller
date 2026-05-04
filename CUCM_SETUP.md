# CUCM Setup — Application User, CTI Permissions, and Phone Association

> Everything you need to do in CUCM **before** `jtapi-phone-controller` can make a phone ring.

This guide covers CUCM 12.5 through 15. Screens differ slightly between versions but the model is identical.

If you're the kind of person who just wants the minimum checklist, jump to [TL;DR](#tldr).

---

## The model, in one paragraph

JTAPI talks to CUCM's **CTI Manager** service over TCP 2748. CTI Manager authenticates you as an **Application User** (not an end user) and lets you control any phone that is (a) associated to that application user, (b) enabled for CTI control on the line appearance, and (c) registered to a CUCM node where CTI Manager is running. Two roles on the application user give you everything you need: **Standard CTI Enabled** (you may use CTI) and **Standard CTI Allow Control of All Devices** (you may control any phone without per-device association). If you prefer a narrower blast radius, skip the second role and manually associate only the phones this tool should touch.

---

## What you create, and why

| Object | Purpose | Where |
|---|---|---|
| **Application User** `jtapi_user` | Non-human identity JTAPI authenticates as | User Management → Application User |
| **Role assignments** | Grant the application user the right to use CTI and control devices | Same page, under Permissions Information |
| **Phone association** (optional if you used "All Devices") | Scope control to specific phones | Same page, under Device Information |
| **Line-level CTI flag** | Allow each phone's line to be driven by CTI | Device → Phone → Line → Advanced |
| **CTI Manager service running** | Accepts the JTAPI connection on port 2748 | Cisco Unified Serviceability → Control Center - Feature Services |

---

## Step 1 — Create the Application User

1. **Cisco Unified CM Administration** → **User Management** → **Application User** → **Add New**.
2. Fill in:
  - **User ID:** `jtapi_user` (use anything you like — you'll put this in `config.json`)
   - **Password:** strong, 12+ chars. You'll put this in `config.json` too (or better — an env var via `env:CUCM_JTAPI_PASSWORD`).
   - **Confirm Password:** same.
   - Leave everything else default.
3. **Save.** Do not click anything else yet — the permissions section isn't populated until after the first save.

---

## Step 2 — Grant CTI permissions

After the save, scroll down to **Permissions Information** and click **Add to Access Control Group**.

Pick the two groups that match your risk tolerance:

### Option A — Broad (recommended for lab, quick-start)

- `Standard CTI Enabled`
- `Standard CTI Allow Control of All Devices`

With these two groups, the application user can control any phone in the cluster that is flagged for CTI control at the line level.

### Option B — Scoped (recommended for anything touching production)

- `Standard CTI Enabled`
- *(Do **not** add "Allow Control of All Devices")*

Then, on the same Application User page, scroll to **Device Information** → **Available Devices** → move **only the specific phones** this tool is allowed to control into **Controlled Devices**. Everything else stays off-limits.

Save.

### Notes on other CTI access-control groups

You may see several more:

| Group | When you actually need it |
|---|---|
| `Standard CTI Allow Control of Phones supporting Connected Xfer and conf` | You're controlling a phone that only supports CTI over a newer protocol (most 7961 / older phones). Usually auto-satisfied by the two groups above. |
| `Standard CTI Allow Control of Phones supporting Rollover Mode` | Rare — needed for some shared-line edge cases. |
| `Standard CTI Allow Calling Number Modification` | Only if your script sets caller-ID on outbound. This tool does not. |
| `Standard CTI Allow Reception of SRTP Key Material` | Only if you need to decrypt media. This tool doesn't touch media. |
| `Standard CTI Secure Connection` | Forces TLS on the CTI link. Requires CAPF-signed certs on the app user. |

For this tool, **the two groups in Option A (or the one in Option B) are sufficient.**

---

## Step 3 — Enable CTI control on each phone's line

Even with the "All Devices" role, a phone's **line** has to be individually flagged for CTI control. This is not a phone-level flag — it's a **line appearance** flag.

For each phone you want to drive:

1. **Device** → **Phone** → pick the phone.
2. Click the first **Line [1]** under **Association Information**.
3. Scroll to **Line 1 on Device ...** → there is a section near the top with per-line CTI settings. The flag you want is:

   - **Allow Control of Device from CTI** → **checked**

   (On some CUCM versions this is labeled **CSS for device control** with a checkbox next to the line's own **Allow Control** toggle — same thing.)

4. **Save**, then **Apply Config** on the phone if prompted.

If the phone has multiple lines and you want to control any of them, repeat for each line.

> **Gotcha — shared lines.** If the line is a shared-line appearance across multiple phones, CTI control must be enabled on **each** device's instance of that line.

---

## Step 4 — Verify CTI Manager is running

1. **Cisco Unified Serviceability** (different app from CUCM Admin — different URL, usually `/ccmservice/`).
2. **Tools** → **Control Center - Feature Services**.
3. Pick the CUCM node whose IP you put in `config.json`.
4. Confirm **Cisco CTI Manager** is in the **Activated** and **Started** state.

If it's **Deactivated**, activate it via **Tools → Service Activation**, then come back and start it.

---

## Step 5 — Smoke-test reachability

From whatever box will run `phone.py`:

```bash
# Is CTI Manager reachable?
nc -vz <cucm-ctim-ip> 2748
# or
telnet <cucm-ctim-ip> 2748
```

You want a **connection succeeded**. A refused/timeout means a firewall, wrong IP, or CTI Manager is down.

Next, make sure the credentials actually work:

```bash
# Log into CUCM Admin with the application user you just made.
# If you can't log into CUCM Admin as jtapi_user, JTAPI won't authenticate either.
# (Application users can log into CUCM Admin but have no pages — a blank screen
# after successful auth is the expected outcome.)
```

---

## Step 6 — Run the tool

Back in the repo:

```bash
cp config.example.json config.json
# edit config.json — provider, username, password, device_name (SEP MAC), directory_number
export CUCM_JTAPI_PASSWORD='...'   # if you used env:CUCM_JTAPI_PASSWORD in config.json
python phone.py dial --destination 14155550123
```

If the phone rings, you're done.

---

## TL;DR

```text
CUCM Admin → User Management → Application User → Add New
  User ID:  jtapi_user
  Password: <strong password>
  Save.
  Permissions → Add to Access Control Group:
    ✓ Standard CTI Enabled
    ✓ Standard CTI Allow Control of All Devices
  Save.

CUCM Admin → Device → Phone → <your phone> → Line [1]
  ✓ Allow Control of Device from CTI
  Save → Apply Config.

Cisco Unified Serviceability → Control Center – Feature Services
  Cisco CTI Manager: Activated + Started

From controller host:
  nc -vz <cucm> 2748  → succeeds
  cp config.example.json config.json
  # fill in provider, username, password, device_name, directory_number
  python phone.py dial --destination 14155550123
```

---

## Security notes

- **Never commit `config.json`.** It's gitignored for a reason. If you must check in a config for CI, put the password in an env var and reference it as `env:CUCM_JTAPI_PASSWORD`.
- **Scope the application user tightly in production.** Option B (explicit device list) is not paranoid — it's how you avoid the "oops, my test script just hung up the CEO" moment.
- **Rotate the JTAPI password on a schedule.** This user has call-control authority over your fleet; treat it like you treat your CUCM DB credentials.
- **Audit.** CTI activity lands in CTI Manager traces. If something goes wrong, **Cisco Unified Serviceability → Trace → Troubleshooting Trace Settings → CTIManager** gives you the trail.

---

## Common failure modes and what they actually mean

| Symptom | Root cause |
|---|---|
| JTAPI returns "OUT_OF_SERVICE" immediately | Wrong `provider` IP, firewall blocking 2748, or CTI Manager not running |
| Auth fails | Application user disabled, password rotated, account locked (CUCM locks after N failed logins — unlock via CUCM Admin) |
| Connects but no phones visible | "All Devices" role not granted, **or** the phone isn't associated to the app user |
| Phone visible but `dial` hangs at `IDLE` | "Allow Control of Device from CTI" not checked on the line, or the line isn't registered |
| `ALERTING` but never `TALKING` | The far end didn't answer, **or** the phone's line has call-forwarding to voicemail with a shorter timeout than your `timeout=` |
| `No Terminal for device SEPxxx` | Phone isn't registered to the CUCM node CTI Manager is pointing at — check **Device → Phone → Status** |
| TLS handshake errors | You enabled `Standard CTI Secure Connection` without installing a CAPF-signed cert on the application user. Either remove that role or install the cert. |

---

*Written from experience doing this enough times to want it documented.*
