# Validation run-sheet — fresh install on a dedicated MacBook (roadmap 8.2)

**Purpose:** the proof artifact for roadmap item 8.2 (dedicated-Apple
support, PR #2586): *a fresh install completes on a dedicated MacBook with
sleep configured and the dedication acknowledgment recorded.* Everything
else in 8.2 is verified (unit tests, stubbed step renders, live read-path
checks); this run-sheet is the remaining real-hardware exercise.

Budget ~15 minutes of attention plus install wait time. Check items off as
you go; §5 lists what to capture as evidence.

---

## 1. Machine prep

- [ ] Apple-Silicon MacBook, fresh macOS 14+ (or a clean enough existing
  install — see [pre-install-checklist.md](pre-install-checklist.md)).
- [ ] One admin account (e.g. `pod-admin-user`), no other user accounts.
- [ ] On AC power. Lid open for the install.
- [ ] **Leave power settings at factory defaults.** The point is to exercise
  the wizard's detection path — a MacBook ships with system sleep ON for the
  AC profile, and the wizard must catch that. If the machine was previously
  tuned, reset first:

  ```bash
  sudo pmset -c restoredefaults
  pmset -g custom        # AC Power section should show a nonzero "sleep"
  ```

- [ ] FileVault: your call — the trade-off (at-rest protection vs. waiting at
  pre-boot auth after a power-loss reboot) is in the
  [pre-install checklist](pre-install-checklist.md). Either choice is valid
  for this run.
- [ ] Keys ready: Anthropic key/MAX + Telegram bot token + your Telegram user
  ID (minimum viable pod).
- [ ] Repo + venv per the README quick start (clone to
  `/Users/Shared/evolve-repo`, venv at `/Users/Shared/evolve-venv`,
  analyzer installed before admin, compat mode — copy the commands from
  [README.md](../README.md) so they're current).

## 2. Run the wizard

```bash
sudo evolve-admin setup --fresh
```

Expectations at the 8.2-relevant steps:

- [ ] **Step 5 (Check prerequisites):** a green `Apple Silicon (arm64)` row
  appears. (On an Intel Mac you'd get a yellow best-effort warning instead —
  not this run's target.)
- [ ] **Step 6 (Host power & sleep):**
  - Shows `Hardware: MacBook<model> — portable (battery present)`.
  - Warns `This Mac is set to sleep after N min on AC power.`
  - Accept the prompt → `✅ AC power profile updated: system sleep off,
    display sleep off`.
  - The portable-host notes block prints (AC power, lid-closed semantics,
    `pmset disablesleep 1` option). If you're on battery it tells you to
    plug in — plug in and note that it fired.
- [ ] **Step 7 (Dedicated-host acknowledgment):**
  - The "Dedicated host" panel renders: four controls (sudoers, /tmp
    staging, keystore perms, loopback authz), chassis-irrelevance, drift
    warning.
  - Answer yes → `✅ Dedicated-host acknowledgment recorded`.
- [ ] Steps 8–18 complete as a normal install (OpenClaw, bot accounts,
  deploy, verify, HTTPS).

## 3. Post-install checks

- [ ] **Recorded host posture** — on the MacBook:

  ```bash
  python3 -c "import json; print(json.dumps(json.load(open('/Users/Shared/evolve/network.json'))['host'], indent=2))"
  ```

  Expect: `hardware_model` = MacBook\*, `portable: true`, `ac_sleep: 0`,
  `ac_displaysleep: 0`, and `dedication_ack` with `acknowledged: true`,
  your admin username, an ISO timestamp, and `mode: "interactive"`.

- [ ] **Power profile stuck:** `pmset -g custom` → AC Power shows `sleep 0`
  and `displaysleep 0`; Battery Power still has its normal values.

- [ ] **Pod is alive:** admin UI loads at `http://127.0.0.1:5050`; send the
  bot a Telegram message and get a reply.

- [ ] **Lid-closed operation** (the scenario this item exists for): with AC
  connected and either an external display attached or
  `sudo pmset disablesleep 1`, close the lid, wait ~10 minutes, message the
  bot from your phone. It must respond.

- [ ] **`host_slept` signal fires** (optional, proves the monitor): force a
  sleep with `sudo pmset sleepnow`, leave it asleep **at least 3 minutes**
  (gaps under 2 minutes are deliberately ignored), wake it, then open the
  admin UI → Alerts. Within a monitor sweep you should see *"Host slept on
  \<hostname\>"* with the pmset remediation hint. It auto-resolves after 24h.

- [ ] **Repair re-run is idempotent:** run `sudo evolve-admin setup --fresh`
  again. Step 6 now reports `System sleep on AC power: never` with no prompt;
  step 7 skips with `Dedicated-host acknowledgment already recorded (…)`.

## 4. If something fails

Capture the wizard output around the failing step plus, for steps 6/7
specifically: `pmset -g custom`, `pmset -g batt`, `sysctl -n hw.model`, and
the `host` section of `network.json` (if written). Those four inputs are
everything the detection logic consumes.

## 5. Record the proof

Paste into the session (or a roadmap note):

1. The `network.json` `host` section from §3.
2. `pmset -g custom` output.
3. One line each for the lid-closed test and (if run) the `host_slept`
   signal.

Then mark roadmap 8.2's proof artifact done in
[roadmap-80-to-100-2026-06-09.md](roadmap-80-to-100-2026-06-09.md).
