# Spec: Evolve Security Protocol v2

*Created: 2026-04-13*
*Supersedes: docs/spec-security-bot.md*
*Status: Approved — ready to build*

---

## Why this replaces the Security-bot bot

The Security-bot spec was designed around a strong independence principle: a separate macOS user that Evolve cannot write to, with its own Telegram token, that watches everything including Evolve itself. That principle is sound. The implementation was over-engineered for the threat model.

On a single-machine pod, process-user isolation between `security-bot` and `evolve` provides almost no additional security boundary — both users share the same filesystem, the same kernel, and the same administrator. A root-compromised machine defeats either approach equally. The real security value Security-bot provided was:

1. **An audit trail** — knowing what changed, when, and whether it was authorized
2. **Identity integrity** — detecting unexpected changes to SOUL.md, AGENTS.md, openclaw.json
3. **Machine-level checks** — SSH config, firewall, unexpected user accounts
4. **Alert independence** — a channel that Evolve itself cannot suppress

All four can be delivered by extending the existing `evolve` user infrastructure, with one addition (a dedicated security Telegram token) that provides the alert-independence property without a separate user.

The Security-bot bot concept becomes worth revisiting **if the pod grows to multiple machines** — at that point, genuine network-level isolation between the monitor and the monitored becomes meaningful. Until then, the operational complexity of an additional user account, additional provisioning flow, and additional update protocol is not justified.

---

## Threat Model

What we are defending against on a single-machine macOS pod:

| Threat | Likelihood | Impact |
|--------|-----------|--------|
| Buggy proposal cascades — bad proposal passes review and corrupts a bot config | Medium | Medium — caught by rollback + outcome tracking, but slow |
| Unauthorized proposal injection — a script writes directly to `approved/` bypassing review | Low | High — apply.py would execute it |
| Forged forge result — `recommendation: promote` injected to force a bad proposal through | Low | High |
| Config drift — openclaw.json changed outside the proposal pipeline | Low | Medium — gateway may break silently |
| Identity drift — SOUL.md or AGENTS.md modified unexpectedly, changing a bot's behavioral constraints | Low | High — bot behavior changes without operator awareness |
| Runaway costs — a bot loops or a prompt goes wrong, burning through API budget | Medium | Medium — already monitored by spend_alert.py |
| Machine-level changes — SSH config weakened, new user added, firewall disabled | Very Low | Very High |
| External access — someone with SSH or physical access to the Mac | Very Low | Very High — HMAC signing helps detect but cannot prevent this |

What we are NOT defending against:
- A root-compromised machine (HMAC keys and git credentials are also compromised)
- Anthropic API-level attacks (outside scope)
- Network attacks on bot gateways (gateways bind to 127.0.0.1 only)

---

## The Four Security Layers

### Layer 1: Pipeline Integrity (HMAC Signing)

Every proposal that enters the pipeline is signed at creation and verified at each gate. An unsigned or tampered proposal is rejected before it can do harm.

**What gets signed:**
- Proposal at write time (`analyze.py`, `cost.py`, `heal.py`, `expansion.py`) → `evolve_sig` field
- Review stamp after passing review (`review.py`) → `review_stamp.sig` field
- Forge result at validation time (`validate.py`) → `forge_sig` field
- `apply.py` verifies all three before applying anything

**Key management:**
- Shared secret: `shared_dir/keystore/evolve-signing.key` (32 random bytes, hex-encoded)
- Permissions: `0600`, owned by `evolve` user
- Generated during `evolve-admin setup evolve-user`
- Rotation via `evolve-admin keys rotate-signing-key` (invalidates all unsigned proposals)

**Status:** Workorder exists in the internal design archive.

---

### Layer 2: Git Backup + Drift Detection

Each bot backs up its security-relevant state to a private GitHub repository nightly. After each backup, `heal.py` compares the live state against the committed state. Any difference not accounted for by a recent apply-result is **unauthorized drift** — an immediate alert fires.

**What gets backed up per bot:**
```
bot-backups/{bot_id}/
  openclaw.json              ← primary config — most important
  identity/SOUL.md           ← behavioral constraints
  identity/AGENTS.md         ← agent instructions
  identity/HEARTBEAT.md      ← session start content
  metrics/latest.json        ← current metrics snapshot
  proposals/applied-ids.json ← list of proposal IDs applied since last backup
```

`shared_dir` state (proposals, incidents, outcomes, apply-results) is backed up separately to an `evolve-shared` branch — higher volume, useful for forensics and recovery.

**Backup commit structure:**
```
[backup] admin-bot 2026-04-13T02:00Z
applied-since-last: prop-2026-04-12-abc123, prop-2026-04-12-def456
config-hash: a3f7b2c1...
soul-hash: 98d12ef0...
```

The commit message embeds the expected-change manifest. The drift detector cross-references this.

**Drift detection logic (in `heal.py`):**
1. After each backup, read the last commit's `openclaw.json` via `git show HEAD:openclaw.json`
2. Diff against live `openclaw.json`
3. For each changed field, check whether a recent apply-result accounts for it
4. Any unexplained diff → 🔴 CRITICAL alert + incident recorded + investigation proposal

**Recovery:** `git checkout HEAD:openclaw.json` to restore last known-good config.

**Infrastructure required:**
- Private GitHub repo (one per pod, operator creates during setup)
- Ed25519 SSH deploy key per bot (generated by `deploy.py` at bot provisioning time)
  - Private key: `~/.ssh/evolve-backup-{bot_id}` (owned by `evolve` user)
  - Public key: registered as deploy key in GitHub repo (write access to bot's branch only)
- New script: `backup.py` (~150 lines), deployed to evolve user, runs nightly at 2am

---

### Layer 3: Audit Checks (`audit.py`)

A new `audit.py` script, run every 15 minutes by the `evolve` user via launchd, performs the checks the Security-bot spec defined. These run with the same sudo grants evolve already has.

**Identity audit:**
- SHA256 hash each bot's `SOUL.md`, `AGENTS.md`, and `HEARTBEAT.md` against the last backed-up version
- Hash mismatch → 🔴 CRITICAL alert (behavioral constraints changed outside the pipeline)
- Baseline: the last git backup commit (not a separate baseline file — git IS the baseline)

**Config audit:**
- For each bot: verify `gateway.bind == "127.0.0.1"`, port matches network.json, no unexpected exec allowlist entries
- For `evolve` user: verify sudoers grant at `/etc/sudoers.d/evolve` matches expected content (hash-checked against a stored baseline)
- New plugin entries in openclaw.json → 🟡 WARN if not in apply-results

**Machine-level audit:**
- Firewall enabled: `pfctl -s rules` returns non-empty output
- SSH config: `PasswordAuthentication no`, `PermitRootLogin no`
- Listening ports: diff against a stored baseline of expected ports
- User accounts: diff `/etc/passwd` against stored baseline — new accounts → 🔴 CRITICAL
- OC binary mtime: flag if the openclaw binary was modified unexpectedly

**Cost audit:**
- Daily spend per bot vs. configured threshold
- Any bot exceeding threshold → 🔴 CRITICAL (moved from 🟡 because real money now)

**Proposal volume audit:**
- If `pending/` or `approved/` has more than N proposals in 24 hours → 🟡 WARN (detector misfiring)
- If apply-results show consecutive rollback_triggered=true → 🟡 WARN

**Alert tiers:**
```
🔴 CRITICAL → immediate Telegram alert (via dedicated security token)
   SOUL.md/AGENTS.md hash mismatch, unauthorized config drift,
   new user account, SSH config weakened, firewall disabled,
   spend threshold exceeded, unauthorized apply-result

🟡 WARN → logged + included in weekly review report (not immediate)
   Backup stale >24h, new plugin entry, gateway slow,
   proposal volume spike, consecutive rollbacks

✅ OK → single log line, silent
```

---

### Layer 4: Alert Independence

Audit alerts use a **dedicated security Telegram bot token** — separate from the general notification token used by `analyze.py`, `heal.py`, and `outcome.py`. This means:

- Security alerts cannot be silenced by a misconfigured `alerts` block in network.json
- The security token is stored separately: `shared_dir/keystore/security-alert-token` (0600, evolve-owned)
- If the general notification channel is broken (bad token, expired), security alerts still get through

This is the core independence property from the Security-bot spec, preserved without a separate user account.

---

## What Changes

### Setup Wizard (`setup_wizard.py`)

**Remove:** `_run_security-bot_step()` entirely — the three-choice Security-bot Bot setup flow.

**Replace with:** A "Security Configuration" step that:
1. Asks for a **security Telegram bot token** (separate from general alerts — operator creates a second bot at @BotFather). Optional but strongly recommended.
2. Asks for a **GitHub backup repo URL** (private repo the operator has already created). Optional but strongly recommended.
3. Generates SSH deploy keys for each bot (no user input needed — happens automatically during bot provisioning in the subsequent steps).
4. Stores security token in `shared_dir/keystore/security-alert-token`.
5. Generates the HMAC signing key: `shared_dir/keystore/evolve-signing.key`.

All of this runs as the `evolve` user with existing permissions — no admin authorization required.

### `wizard.py` (`_run_security_wizard_only`)

Remove the "dedicated mode" option (which prompted for a `security_bot_id`). Replace with a simplified flow that configures the security token and backup repo.

### `deploy.py`

- **Remove:** `repair_security-bot_config()` function — keep for one release cycle (call it only if `/Users/security-bot` exists on the machine, for existing pods with a security-bot user), then remove.
- **Add:** SSH deploy key generation during `deploy_bot()` — generates an ed25519 keypair, saves private key to `~/.ssh/evolve-backup-{bot_id}`, returns the public key text in the deploy result so the operator can add it to GitHub.
- **Add:** `_install_launchd_backup()` — installs `backup.py` plist for `evolve` user, nightly at 02:00.
- **Add:** `_install_launchd_audit()` — installs `audit.py` plist for `evolve` user, every 15 minutes.
- **Add:** HMAC key generation in `setup_evolve_user()`.

### New Scripts (`packages/analyzer/`)

| Script | What it does | Runs as | Schedule |
|--------|-------------|---------|----------|
| `backup.py` | Per-bot git backup — stages config + identity files, commits with expected-change manifest, pushes to GitHub | evolve | Nightly 02:00 |
| `audit.py` | Identity hashes, config audit, machine-level checks, proposal volume audit, cost audit | evolve | Every 15 minutes |

### `heal.py`

Add drift detection after backup:
- After `backup.py` completes (or on a configurable schedule), read the last git commit's `openclaw.json` for each bot, diff against live, cross-reference apply-results
- Flag unexplained diffs as unauthorized drift

### `review.py`

Once HMAC signing is in place: verify `evolve_sig` before processing. Reject unsigned proposals with reason `"unsigned_proposal"`.

### `apply.py`

Once HMAC signing is in place: verify `evolve_sig` + `review_stamp.sig` + forge result sig before applying. Move unsigned/invalid proposals to `quarantine/`.

### Admin Dashboard (Security Panel)

The existing Security page gains new data sources:

| Panel | Data source | Current |
|-------|------------|---------|
| Pipeline integrity | HMAC verification status | ❌ not built |
| Git backup freshness | Last commit timestamp per bot | ❌ not built |
| Identity hash status | audit.py results | ❌ not built |
| Config drift status | heal.py drift check | ❌ not built |
| Machine security | audit.py machine checks | ❌ not built |
| Security alert channel | Token presence + test button | ❌ not built |
| Existing: Config baseline, scoring, audit button | Already working | ✅ |

---

## Migration: Existing Security-bot User

For pods that already have a `security-bot` macOS user (including this pod):

1. `repair_security-bot_config()` in deploy.py already handles cleanup of the Security-bot openclaw.json
2. The Security-bot launchd plist (`ai.openclaw.security-bot.audit`) should be unloaded: `launchctl bootout user/$(id -u security-bot) ai.openclaw.security-bot.audit`
3. The Security-bot user account can be left in place (it's harmless) or removed via System Settings → Users & Groups
4. The sudoers entry `/etc/sudoers.d/security-bot` can be removed once the security-bot launchd job is unloaded
5. `repair_security-bot_config()` is removed from deploy.py after the next release cycle

Migration is not urgent — the existing security-bot user is idle and harmless.

---

## Build Order

These are independent enough to build in parallel across phases, but have dependencies within phases:

### Phase 3a: Foundation
1. HMAC signing (`evolve_config.py` helpers + key generation in setup) — *from existing workorder*
2. `backup.py` (new script + launchd plist + SSH key generation in deploy.py)
3. Security token setup step in `setup_wizard.py` (remove Security-bot step, add security config step)

### Phase 3b: Verification
4. HMAC verification in `review.py` and `apply.py` — *depends on 3a.1*
5. Drift detection in `heal.py` — *depends on 3a.2*
6. `audit.py` (identity hashes, config audit, machine checks) — *depends on 3a.2 for git baseline*

### Phase 3c: Dashboard
7. Security panel additions (backup freshness, identity status, drift status, machine checks)
8. Security alert channel test button
9. Quarantine proposal UI (proposals rejected by unsigned/tampered detection)

---

## What We Are Not Building

| Item | Reason |
|------|--------|
| Security-bot as separate macOS user | Unnecessary on single machine — complexity not justified |
| Security-bot self-tamper detection | Git commit hashes for audit scripts serve this purpose |
| Multi-step acknowledgment for security-critical proposals | HMAC signing + human review is sufficient; add if Security-bot class proposals are introduced later |
| Air-gapped Telegram channel | Separate security token provides this without separate user |

---

## Relationship to Remaining Roadmap

| Roadmap item | Status after this spec |
|---|---|
| Security-bot independent auditor | Retired — replaced by audit.py + security token |
| Security-critical proposal class | Deferred — revisit when multi-machine pod |
| Security-bot spec | Archived — superseded by this document |
| Security-bot in setup_wizard | Removed — replaced by security config step |
| Proposal pipeline integrity signing | Unchanged — still Phase 3a.1, same workorder |
| Security-bot full system implementation | Removed from roadmap |
