# WhatsApp Skill — Spec

**Status:** draft (2026-06-04)
**Goal:** add `@openclaw/whatsapp` to the Evolve Skills catalog with a wizard that mirrors `telegram_install.py` shape, with a new QR-pairing step replacing token paste.

**Companion docs:**
- [docs/skills-deep-audit-2026-05-30.md](skills-deep-audit-2026-05-30.md) — the audit framework + the seven-point end-to-end check every new skill must pass before catalog entry.
- [docs/spec-wizard-verification-gauntlet-2026-05-30.md](spec-wizard-verification-gauntlet-2026-05-30.md) — the four checks that gate "wizard declared success".
- [docs/system/imessage-architecture.md](system/imessage-architecture.md) — the closest precedent for a local-state-heavy channel install (auth dir + per-bot binary state).
- [packages/admin/evolve_admin/skills/telegram_install.py](../packages/admin/evolve_admin/skills/telegram_install.py) — the closest structural sibling (channel-block + plugin-entry + kickstart).
- [packages/admin/evolve_admin/skills/_oc_install_common.py](../packages/admin/evolve_admin/skills/_oc_install_common.py) — shared read/write/kickstart helpers.

---

## 0. Why this skill, why now

`@openclaw/whatsapp` is a first-class official OpenClaw channel plugin (verified on the mini, OC v2026.6.1):

```json
{
  "name": "@openclaw/whatsapp",
  "openclaw": {
    "channel": {
      "id": "whatsapp",
      "label": "WhatsApp",
      "selectionLabel": "WhatsApp (QR link)",
      "blurb": "works with your own number; recommend a separate phone + eSIM."
    },
    "install": { "clawhubSpec": "clawhub:@openclaw/whatsapp", "minHostVersion": ">=2026.4.25" }
  }
}
```

It shipped to ClawHub on 2026-04-25 and is documented at `docs.openclaw.ai/channels/whatsapp`. Evolve never wrote a `whatsapp_install.py` for it — the only WhatsApp references in our codebase are channel-name placeholders in pairing chips, the wizard's supported-channels list, and breaker-enforcement display names. Two pod bots (team-bot-a, team-bot-c in the reference deployment's role mapping) have `channels.whatsapp: {enabled: false, dmPolicy: "pairing", groupPolicy: "allowlist", debounceMs: 0, mediaMaxMb: 50}` blocks in their `openclaw.json` — these are OC's schema defaults; the plugin is **not installed** and **no account is paired**.

This is a sibling-of-Telegram gap, not a new architecture. The motivation:

- Operators expect parity across the four big channels (Slack, Telegram, Discord, WhatsApp). The skills page silently advertises three; "where's WhatsApp?" is the natural next question.
- The OC bundle is doing all the hard work — Baileys auth, multi-account, doctor checks, schema validation. We just need to wire the install wizard.
- It unblocks the **Diana** persona memory (multi-bot wealthy-individual, multi-domain compartmentalization) and the **Carla** persona memory (service-business client-facing project bots), both of which name WhatsApp as a default-expected channel.

The audit-doc lesson applies: don't ship if `resolve_status` would lie. The WhatsApp doctor module (`doctor-whatsapp-responsiveness-*.js`) gives us a reliable runtime probe; we'll use it.

---

## 1. How OC's WhatsApp channel works

Three relevant facts from the OC bundle's TypeScript definitions and CLI:

### 1.a. Pairing model: Baileys / WhatsApp Web

From `plugin-sdk/types.channels-*.d.ts`:

```ts
type WhatsAppAccountConfig = WhatsAppConfigCore & WhatsAppSharedConfig & {
  name?: string;
  enabled?: boolean;
  authDir?: string;  // Override auth directory (Baileys multi-file auth state)
  pluginHooks?: { messageReceived?: boolean };
};
```

`authDir` is the smoking gun: OC uses **Baileys** (the open-source Node.js WhatsApp Web library) under the hood. Pairing works exactly like WhatsApp Web — the operator scans a QR code with their phone's WhatsApp app, the device-link handshake completes, and Baileys writes its multi-file auth state to `authDir`. From then on the bot connects directly to WhatsApp's servers using that stored state.

This is **not the WhatsApp Business Cloud API**. No Meta approval, no business verification, no per-message billing, no phone-number provisioning. The cost model is: the operator already has a WhatsApp account (or sets up a new one on a spare phone + eSIM, per OC's blurb), and the bot uses it.

### 1.b. Multi-account support

`channels.whatsapp.accounts: Record<string, WhatsAppAccountConfig>` lets one bot connect to multiple WhatsApp accounts. Our v1 wizard supports a **single account per bot** (account id = bot_id for now); the schema slot is there for a follow-on where a single bot serves multiple WhatsApp numbers.

### 1.c. CLI for pairing

From the OC CLI help: `openclaw channels login --channel whatsapp` is the documented pairing command. It opens a Baileys session, emits a QR string to stdout (or a printable code), waits for the device-link to complete, persists the auth state, and exits. There is **no static token to paste** — the entire credential lives in `authDir` as Baileys session files.

### 1.d. Health probe

The OC bundle includes `doctor-whatsapp-responsiveness-*.js` (a built-in doctor module) and `openclaw channels status --channel whatsapp --probe`. Our `resolve_status` calls the CLI status probe and parses the JSON — this is the same pattern Telegram's `verify_bot_token` uses, just against OC's local CLI instead of a remote HTTP API.

---

## 2. Install flow state machine

State is per-bot, derived from three on-disk signals: the `@openclaw/whatsapp` plugin install record, the `channels.whatsapp.accounts.<account_id>.authDir` config field, and the live `openclaw channels status` probe.

```
plugin_not_installed
    ↓ (admin server runs `openclaw plugins install @openclaw/whatsapp` as the bot user)
account_not_paired
    ↓ (operator scans QR code; Baileys writes authDir)
auth_dir_present_not_probed
    ↓ (kickstart gateway; status probe returns ok)
active
```

Failure / edge states surfaced to the UI:

| State | Meaning | UI action |
|---|---|---|
| `plugin_install_failed` | npm/clawhub install errored out | retry plugin install + show stderr |
| `qr_expired` | Baileys QR expired without scan (~20s window) | refresh QR |
| `pairing_timeout` | operator never scanned within 5min | restart pairing |
| `revoked_remote` | operator logged out from phone Linked Devices | re-pair |
| `auth_dir_corrupt` | Baileys auth files unreadable / truncated | wipe authDir + re-pair |
| `disabled` | `channels.whatsapp.accounts.<id>.enabled: false` | re-enable in Skills page |
| `unknown` | CLI probe returned an error we don't understand | surface stderr; offer Help link |

The seven-point audit check (per [docs/skills-deep-audit-2026-05-30.md](skills-deep-audit-2026-05-30.md) §Method) — every gate must pass before the skill is added to the catalog list:

| # | Check | How this skill satisfies it |
|---|---|---|
| 1 | Discoverability | Catalog list endpoint includes `whatsapp`; access panel renders; status resolver never 500s |
| 2 | Install plan | POST `/api/skills/install/whatsapp` returns `[install_plugin, pair_qr, confirm]` steps |
| 3 | Credential lands somewhere real | `authDir` populated with Baileys multi-file auth state, owned by bot user |
| 4 | Runtime consumer exists | `@openclaw/whatsapp` plugin loaded by the bot's gateway (verified by post-install `plugins.entries.whatsapp.enabled: true` + gateway log line) |
| 5 | Actual capability | `openclaw channels status --channel whatsapp --probe --json` returns `connected: true`; optional E2E echo via Verification Gauntlet Check 4 |
| 6 | Status correctness | `resolve_status` returns `valid` ONLY when 1-5 all pass; defaults to `unknown` for any unclassified failure |
| 7 | Revoke path | `openclaw channels logout` + clear `accounts.<id>` + clear `enabled` flag if last account + kickstart |

---

## 3. The novel building block: QR pairing helper

This is the only piece that doesn't already exist in our codebase. Every other channel skill captures a static credential string (BotFather token, Slack OAuth code, Discord bot token). WhatsApp pairing is interactive: a Baileys session emits time-limited QR codes (~20s each) until the operator scans one, after which Baileys writes auth state and exits.

The design must work for both **WhatsApp** and (later, in the same pattern) **Signal**, which uses an identical device-link flow. So we extract the QR-streaming mechanics into a reusable helper.

### 3.a. Location

New module: `packages/admin/evolve_admin/skills/_qr_pairing.py`

Responsibilities:

1. Spawn the OC channels-login subprocess as the bot user, with controlled environment.
2. Parse the subprocess stdout for QR payloads (Baileys emits them as base64 strings or ANSI-rendered blocks — OC's CLI normalizes this to JSONL lines like `{"kind": "qr", "payload": "<base64>"}` since v2026.4.25; confirm in implementation).
3. Render each payload as a PNG data URL (using `qrcode` Python lib — already a transitive dep via deploy_signoff_qr.py if present, otherwise add it).
4. Expose a session API the server can short-poll:
   - `POST /api/skills/install/whatsapp/pair/start` → `{session_id, qr_png_data_url, expires_in_s}`
   - `GET /api/skills/install/whatsapp/pair/<session_id>` → `{state: "waiting"|"paired"|"expired"|"failed", qr_png_data_url?, error?}`
   - `POST /api/skills/install/whatsapp/pair/<session_id>/cancel` → tears down
5. Lifetime: 5 min hard ceiling, sessions stored in-process (no persistence — pairing is foreground operator work).
6. Concurrency: at most one pairing session per bot at a time; new `/start` for the same bot cancels the old session.

### 3.b. Why short-poll, not SSE/WebSocket

The admin UI never streams. Adding a streaming endpoint just for QR refresh would force a transport upgrade across the front end. A 2-second client poll is operationally identical for a 20-second QR cycle and matches the existing pattern (`/api/skills/install/<id>/status` is also poll-driven).

### 3.c. Bot user execution model

`openclaw channels login` writes to `authDir`, which must be owned by the bot user (so the bot's gateway can read it at runtime). The admin server runs as `evolve` and can't `sudo -u <bot>` (per CLAUDE.md). The path:

```
# 1. Admin server stages a one-shot pairing script to /tmp:
#    Python wrapper that exec's `openclaw channels login --channel whatsapp
#    --account-id <bot_id> --auth-dir <bot_home>/.openclaw/whatsapp/auth
#    --json-output` and forwards each stdout line to a fifo.

# 2. Admin server invokes via sudo:
#    sudo -u <bot_user> /tmp/evolve-whatsapp-pair-<sid>.sh
#    (need sudoers grant for `evolve` → `<bot_user>` for /usr/local/bin/openclaw,
#     scoped to the specific bin path — see §7).
```

**Open question:** the existing sudoers (`/etc/sudoers.d/evolve`) does NOT grant `sudo -u <bot>` for evolve (per the "Reads — use direct reads, not `sudo -u <bot>`" CLAUDE.md guidance). We have two options:

- **Option α** — add a narrow sudoers grant: `evolve ALL = (<bot-users>) NOPASSWD: /opt/homebrew/bin/openclaw channels login *` and only the `channels login` subcommand. Generated via `setup_wizard.py::_write_evolve_sudoers` and validated with `visudo -c`. Scope is one CLI verb against a fixed binary path; equivalent in blast radius to the `sudo /bin/cat /Users/*/.openclaw/openclaw.json` already granted.
- **Option β** — drive the pairing from `admin-daemon` (the per-bot privileged daemon stood up for the evo account-separation work). The daemon has the right permissions to spawn child processes as the bot user via its existing unix-socket API.
- **Option γ (chosen during implementation)** — reuse the existing broad grant `evolve ALL=(ALL) NOPASSWD: SETENV: /opt/homebrew/bin/openclaw` that `setup_wizard.py::_write_evolve_sudoers` already ships. Mirror `deploy.py:2811`'s pattern: `sudo --preserve-env=OPENCLAW_CONFIG_PATH -H -u <bot> openclaw …`. **No new sudoers grants needed.** This is what shipped — the spec's earlier α/β branches were superseded once we re-read the existing sudoers section.

Recommendation: Option α for v1 because it's localized to the WhatsApp install flow and doesn't entangle with the admin-daemon API. If we add Signal next and it needs the same grant, refactor into a shared narrow grant covering both.

### 3.d. authDir location

`<bot_home>/.openclaw/whatsapp/auth/` — sibling of `<bot_home>/.openclaw/openclaw.json`. The dir is created by the pairing script on first run, owned by the bot user (no sudo dance for evolve to read — `set_evolve_read_acl` already grants inherited ACL read across `.openclaw/`). The dir contains Baileys session files (`creds.json`, `app-state-sync-key-*.json`, etc.) — these ARE the credential; treat them with the same care as a stored access token.

---

## 4. The install module

New file: `packages/admin/evolve_admin/skills/whatsapp_install.py` (~500 LOC, mirrors `telegram_install.py` structure).

### 4.a. Public API

```python
WHATSAPP_SKILL_ID = "whatsapp"
WHATSAPP_PLUGIN_NPM = "@openclaw/whatsapp"
WHATSAPP_AUTH_DIR = ".openclaw/whatsapp/auth"  # relative to bot home

@dataclass
class InstallStatus:
    bot_id: str
    state: str  # see §2 table
    account_id: str | None = None
    paired_phone: str | None = None  # e.g. "+15551234567" — surfaced by Baileys after pairing
    auth_dir: str | None = None
    plugin_version: str | None = None
    error: str | None = None
    @property
    def status(self) -> str: return self.state  # orchestrator compat
    def to_dict(self) -> dict[str, Any]: ...

def build_install_plan(status: InstallStatus) -> list[InstallStep]:
    """plugin_not_installed → [install_plugin, pair_qr, confirm]
       account_not_paired   → [pair_qr, confirm]
       auth_dir_present_not_probed → [confirm]
       active               → []
       Failure states       → recovery-specific plans (e.g. revoked_remote → [pair_qr, confirm]).
    """

def install_plugin(bot_id: str) -> tuple[bool, str | None]:
    """Run `openclaw plugins install @openclaw/whatsapp` as the bot user.
       Uses the gap-fill pattern from deploy.py:1937 (the brave-equivalent flow).
       Idempotent — if already installed, returns (True, None).
    """

def start_pairing_session(bot_id: str) -> dict:
    """Start a QR pairing session. Returns the v1 of the QR PNG plus session_id.
       Delegates to _qr_pairing.start_session(bot_id, channel='whatsapp', ...).
    """

def poll_pairing_session(session_id: str) -> dict:
    """Return current state + (if waiting) the current QR PNG. Refreshed by the
       underlying _qr_pairing helper as Baileys emits new payloads."""

def cancel_pairing_session(session_id: str) -> bool: ...

def enable_account_in_oc_config(
    bot_id: str,
    account_id: str,
    auth_dir: str,
    paired_phone: str | None,
) -> tuple[bool, str | None]:
    """After pairing completes, merge into the bot's openclaw.json:
         channels.whatsapp.enabled = True
         channels.whatsapp.accounts[<account_id>] = {
             enabled: True, name: <bot_id>, authDir: <auth_dir>,
         }
         plugins.entries.whatsapp.enabled = True
       Uses _oc_install_common.read_oc_config / write_oc_config, then kickstart.
    """

def resolve_status(bot_id: str) -> InstallStatus:
    """Three-stage probe:
       1. Is @openclaw/whatsapp in the plugin install records? (read openclaw.json)
       2. Is channels.whatsapp.accounts.<id>.authDir populated AND the dir on disk
          contains a valid-shape creds.json?
       3. Does `openclaw channels status --channel whatsapp --probe --json` return
          connected:true for this account?
       Returns the highest-tier state that all preceding stages satisfy.
       CRITICAL: never returns 'valid' if stage 3 fails — same rule as the May
       incident's status-resolver mandate.
    """

def revoke_account(bot_id: str, account_id: str | None = None) -> tuple[bool, str | None]:
    """Tear down a paired WhatsApp account:
       1. `openclaw channels logout --channel whatsapp --account <id>` (best-effort)
       2. Remove channels.whatsapp.accounts[<id>] from openclaw.json
       3. If no accounts remain, set channels.whatsapp.enabled = False AND
          plugins.entries.whatsapp.enabled = False
       4. Delete <auth_dir> recursively (sudo rm -rf, ACL allows)
       5. Kickstart gateway
       Returns (ok, error). Best-effort — even if logout fails remotely
       (phone offline), local revoke still completes so the dashboard doesn't
       lie.
    """

SKILL_REGISTRY_ENTRY: dict[str, Any] = {
    "id": WHATSAPP_SKILL_ID,
    "display_name": "WhatsApp",
    "summary": WHATSAPP_ACCESS_PANEL["summary"],
    "access_panel": dict(WHATSAPP_ACCESS_PANEL),
}
```

### 4.b. Access panel — Plex-test compliant

```python
WHATSAPP_ACCESS_PANEL: dict[str, Any] = {
    "skill_id": WHATSAPP_SKILL_ID,
    "skill_display_name": "WhatsApp",
    "summary": (
        "Lets this bot send and receive WhatsApp messages on a phone number "
        "you connect. The bot links to WhatsApp the same way WhatsApp Web does — "
        "by scanning a QR code with your phone. We recommend using a separate "
        "phone number (a spare SIM or eSIM) rather than your personal one."
    ),
    "will": [
        "Send WhatsApp messages to people and groups it has been added to",
        "Read messages in those chats so it can respond",
        "Send photos, documents, and formatted text up to 50 MB",
        "Show as 'WhatsApp Web' under Linked Devices on your phone",
    ],
    "wont": [
        "Join chats or groups it hasn't been added to",
        "Read your other WhatsApp Web sessions or your phone's other chats",
        "Send messages without your instruction (unless it's a configured channel)",
        "Share your WhatsApp account with anyone outside this bot",
    ],
    "where_credentials_live": (
        "The connection to WhatsApp is stored only on this bot's user account on "
        "your machine, as the 'linked device' files WhatsApp Web uses. You can "
        "revoke access at any time from this page, or by going to WhatsApp on "
        "your phone → Settings → Linked Devices → tap this device → Log Out."
    ),
}
```

Note the "Linked Devices" terminology — this is the WhatsApp app's own UI label, recognizable to anyone who's used WhatsApp Web. Compliant with the design-constraint-plex-test memory: jargon-free, no "Baileys", "session", "auth dir", or "device pairing protocol".

---

## 5. Server routes

In `packages/admin/evolve_admin/web/server.py`, parallel to the Telegram block at `:20344-20520`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/skills/install/whatsapp/status` | resolve_status output |
| POST | `/api/skills/install/whatsapp` | build_install_plan for current state |
| POST | `/api/skills/install/whatsapp/install-plugin` | runs install_plugin step |
| POST | `/api/skills/install/whatsapp/pair/start` | starts QR pairing session |
| GET | `/api/skills/install/whatsapp/pair/<sid>` | polls pairing session state |
| POST | `/api/skills/install/whatsapp/pair/<sid>/cancel` | tears down session |
| POST | `/api/skills/install/whatsapp/revoke` | revoke_account |

Catalog wiring — three sites in `web/server.py` (audit doc cross-references these):

1. Catalog list endpoint (the `_skills_catalog_list` builder, ~line 18000) — append a `whatsapp` entry.
2. Catalog detail dispatcher (~line 18800) — map `whatsapp` → `WHATSAPP_ACCESS_PANEL`.
3. 404-hint string in the install-plan dispatcher (the post-imessage-withdrawal hint) — add WhatsApp to the supported list.

---

## 6. Status resolver detail

The most failure-prone surface, per the May incident retrospective and the audit-doc F3 cross-cutting finding ("status resolvers report active without probing capability"). The three-stage probe:

```python
def resolve_status(bot_id: str) -> InstallStatus:
    cfg, err = _oc_common.read_oc_config(bot_id)
    if cfg is None:
        return InstallStatus(bot_id, "unknown", error=err or "oc_read_failed")

    # Stage 1: plugin install record
    installs = cfg.get("plugins", {}).get("installs", {})
    if WHATSAPP_PLUGIN_NPM not in installs and "whatsapp" not in installs:
        return InstallStatus(bot_id, "plugin_not_installed")

    # Stage 2: account config + auth dir on disk
    whatsapp_cfg = cfg.get("channels", {}).get("whatsapp", {})
    accounts = whatsapp_cfg.get("accounts", {})
    if not accounts:
        return InstallStatus(bot_id, "account_not_paired")

    # v1 — single account per bot; use the first
    account_id, acct = next(iter(accounts.items()))
    auth_dir = acct.get("authDir")
    if not auth_dir or not _auth_dir_is_populated(auth_dir):
        return InstallStatus(bot_id, "account_not_paired", account_id=account_id)
    if acct.get("enabled") is False:
        return InstallStatus(bot_id, "disabled", account_id=account_id, auth_dir=auth_dir)

    # Stage 3: live probe via OC CLI (the load-bearing check — never skip)
    probe_result = _probe_whatsapp_status(bot_id, account_id)
    if probe_result.get("error") == "auth_dir_corrupt":
        return InstallStatus(bot_id, "auth_dir_corrupt", account_id=account_id,
                             auth_dir=auth_dir, error=probe_result.get("error"))
    if probe_result.get("error") == "revoked_remote":
        return InstallStatus(bot_id, "revoked_remote", account_id=account_id,
                             auth_dir=auth_dir, error=probe_result.get("error"))
    if not probe_result.get("connected"):
        return InstallStatus(bot_id, "unknown", account_id=account_id,
                             auth_dir=auth_dir,
                             error=probe_result.get("error") or "probe_failed")

    return InstallStatus(
        bot_id=bot_id, state="active", account_id=account_id,
        paired_phone=probe_result.get("paired_phone"),
        auth_dir=auth_dir,
        plugin_version=installs.get(WHATSAPP_PLUGIN_NPM, {}).get("version"),
    )
```

`_auth_dir_is_populated()` checks that `creds.json` exists, is parseable JSON, and contains the Baileys-expected `noiseKey` / `signedIdentityKey` / `signedPreKey` fields. This catches the "operator deleted the auth dir manually" case before we wasted a CLI probe.

`_probe_whatsapp_status()` runs `openclaw channels status --channel whatsapp --account <id> --probe --json --timeout 10000` as the bot user, parses the JSON, and translates the result to one of `connected`, `revoked_remote`, `auth_dir_corrupt`, or `error`. **It must always return within ~12s** — if OC's CLI hangs (rare but possible on cold-start), we return `unknown` rather than blocking the admin UI.

---

## 7. Sudoers + ACL changes

**Outcome (during implementation):** none of the per-channel grants in this section ended up landing. The existing broad grant in `setup_wizard.py::_write_evolve_sudoers` —

```
evolve ALL=(ALL) NOPASSWD: SETENV: /opt/homebrew/bin/openclaw
```

— already covers every `sudo -u <bot_user> openclaw …` invocation we need (it's marked `SETENV` so `--preserve-env=OPENCLAW_CONFIG_PATH` works). All WhatsApp install + revoke calls use this grant via the `deploy.py:2811`-style pattern: `sudo --preserve-env=OPENCLAW_CONFIG_PATH -H -u <bot-user> openclaw …`. **No new sudoers grants ship with this PR.**

ACL: no new grants needed. `set_evolve_read_acl` already covers `<bot_home>/.openclaw/` recursively, so the auth dir is readable by evolve for status probes.

The earlier draft of this section (preserved below for design-record) proposed a narrower per-channel grant set:

```
# (NOT shipped — superseded by reusing the existing broad grant above)
# evolve ALL = (<bot-users>) NOPASSWD: /opt/homebrew/bin/openclaw channels login *
# evolve ALL = (<bot-users>) NOPASSWD: /opt/homebrew/bin/openclaw channels status *
# evolve ALL = (<bot-users>) NOPASSWD: /opt/homebrew/bin/openclaw channels logout *
# evolve ALL = (<bot-users>) NOPASSWD: /opt/homebrew/bin/openclaw plugins install @openclaw/whatsapp*
```

A narrower grant set is a future tightening if `_write_evolve_sudoers` is ever scrubbed of the broad `(ALL)` SETENV grant — at which point the operator-facing bot-user list comes from `network.json::bots`, mirroring how every other sudoers block in the file is generated.

---

## 8. Verification Gauntlet integration

Add to [`wizard_verify.py`](../packages/admin/evolve_admin/wizard_verify.py)::`_CHANNEL_CHECKERS`:

```python
def _check_whatsapp(bot_id: str, network: dict) -> CheckResult:
    """Check 3: channel handshake. Calls resolve_status; passes only if 'active'."""
    from .skills.whatsapp_install import resolve_status
    status = resolve_status(bot_id)
    if status.state == "active":
        return CheckResult(ok=True, detail=f"paired as {status.paired_phone}")
    return CheckResult(ok=False, detail=f"whatsapp not active: {status.state} ({status.error or '—'})")
```

The spec's existing language about WhatsApp ("Out of scope for v1 — flag as `channel_check_unsupported`") gets updated to remove WhatsApp from that list once this checker lands.

Check 4 (end-to-end echo) requires a verifier WhatsApp number for the echo target — out of scope for v1 of this skill, deferred to the per-bot Verifier Address spec (TBD). Until then, `_check_whatsapp` covers Check 3 and operators can manually verify Check 4 by sending a message from their own phone.

---

## 9. Inventory + display

In `packages/admin/evolve_admin/skills/inventory.py`:

```python
_PLUGIN_DISPLAY["whatsapp"] = {"display": "WhatsApp", "category": "messaging"}
_CHANNEL_BACKED_SKILLS["whatsapp"] = {
    "display": "WhatsApp", "category": "messaging",
    "token_fields": ("accounts",),  # presence of any account = configured
}
```

The `_CHANNEL_BACKED_SKILLS` entry needs a small tweak to the resolver to treat `accounts` as a presence check (any non-empty dict → configured) rather than a string-token check. The other three channels work fine as-is; this is just for WhatsApp's account-shape config.

---

## 10. Test plan

Mirroring `tests/test_skills_telegram_install.py` structure (the parametrize-heavy table-driven layout).

| File | What it covers |
|---|---|
| `test_skills_whatsapp_install.py::TestStatusResolver` | Each branch of the resolve_status state machine, with fake `read_oc_config` / `_probe_whatsapp_status` stubs |
| `test_skills_whatsapp_install.py::TestBuildInstallPlan` | Each input state maps to the correct ordered step list |
| `test_skills_whatsapp_install.py::TestEnableAccountInOcConfig` | openclaw.json merges correctly (preserve existing operator-set fields, write new account + flip enabled) |
| `test_skills_whatsapp_install.py::TestRevokeAccount` | Single-account → channel disabled; multi-account → other accounts preserved; auth dir cleared |
| `test_skills_whatsapp_install.py::TestAccessPanelPlexTest` | No jargon strings in `will` / `wont` / `where_credentials_live` (denied: `baileys`, `token`, `oauth`, `auth dir`, `creds.json`) |
| `test_skills_whatsapp_install.py::TestSkillRoutes` | Catalog list includes whatsapp; install dispatcher returns plan; revoke endpoint exists |
| `test_skills_qr_pairing.py` | Session lifecycle: start → poll → expire → cancel; one-session-per-bot eviction; PNG generation roundtrips a known payload |
| `test_skills_install_orchestrator_parity.py` | WhatsApp registered alongside the other 4 messaging skills in the orchestrator's per-skill route map |

Negative-coverage tests required by audit doc F4 ("runtime consumer exists"):

```python
def test_status_never_active_without_plugin_installed():
    # Build an openclaw.json with channels.whatsapp.accounts populated
    # but plugins.installs empty. resolve_status MUST return plugin_not_installed,
    # NEVER active. (Prevents the "credential lands somewhere" false positive.)

def test_status_never_active_without_probe_success():
    # Build an openclaw.json with everything looking right, but stub _probe to
    # return {connected: False}. resolve_status MUST return unknown, NEVER active.
```

Integration test (lives in `tests/integration/test_skills_whatsapp_e2e.py`, runs only when `EVOLVE_INTEGRATION_TESTS=1` is set):

- Stub Baileys via a fake `openclaw` shim on PATH that emits canned QR JSONL.
- Run the full install plan against a sandboxed `openclaw.json` and assert each stage's on-disk effect.

---

## 11. Risks + limitations

### 11.a. Phone-app dependency

The bot's WhatsApp connection is tied to the operator's phone being online (Baileys is a WhatsApp Web client; if the linked phone is offline for 14+ days, WhatsApp servers may unlink it). Surface this clearly in the access panel and the post-install confirmation screen. Mitigation: the OC doctor module flags this state; our status resolver maps it to `revoked_remote` so the dashboard truthfully shows "needs re-pairing".

### 11.b. ToS posture

WhatsApp Web reverse-engineering is in a gray area of Meta's ToS. Baileys mitigates this by emulating a real WhatsApp Web client (handshake, encryption, message format). Risk profile is identical to anyone using WhatsApp Web for personal automation (Notion, Zapier, ManyChat all do this). We're not promising "WhatsApp Business compliance"; the access panel's "we recommend a separate phone + eSIM" language sets expectations honestly.

### 11.c. Per-bot Apple ID problem doesn't apply here

Unlike iMessage (where the Apple ID is per-macOS-user and operationally expensive to multiplex), each WhatsApp account is just a phone number + a Baileys auth dir. No host-OS state pollution. Bots can be configured with separate WhatsApp accounts without any macOS user juggling.

### 11.d. No groupChat init bootstrap

Baileys-driven WhatsApp bots can't proactively create groups or invite users — that's a WhatsApp Web limitation, not OC's. The access panel says "groups it has been added to"; never promise group creation.

### 11.e. Media size cap

The OC schema default is `mediaMaxMb: 50`. Don't expose this in the wizard; operators can edit it directly in `openclaw.json` if they need to (advanced use-case). The access panel cites "up to 50 MB" as a concrete user-facing number.

### 11.f. Multi-account v2

The schema supports `channels.whatsapp.accounts: Record<id, AccountConfig>` but v1 ships single-account-per-bot. A v2 follow-on (after Diana persona work) wires the wizard to add additional accounts under different ids. The install module's `account_id` parameter is already plumbed through for this.

---

## 12. Phased delivery

Designed so each phase ships as one PR and the catalog flip happens only after Phase 4's gates pass — same discipline as the May audit's Phase 1/2/3 split.

### Phase 1 — QR pairing helper (foundation, no user-visible change)

- Add `_qr_pairing.py` with session API + `qrcode` dep.
- Add sudoers grants via `setup_wizard.py` + roll out via `sudo evolve-admin install-infra-jobs` (canary on a low-traffic bot first per the canary-for-one-file-edits memory). **Update:** no sudoers grants ended up being needed — see §7.
- Tests: `test_skills_qr_pairing.py` full coverage.
- **Ship as: PR 1.** Mergeable independently — nothing references the helper yet.

### Phase 2 — Install module (no UI, no catalog entry)

- Add `whatsapp_install.py` with full public API (§4).
- Add inventory entries (§9).
- Tests: `TestStatusResolver`, `TestBuildInstallPlan`, `TestEnableAccountInOcConfig`, `TestRevokeAccount`, `TestAccessPanelPlexTest`.
- **Ship as: PR 2.** Still no UI; module is callable but not wired to routes.

### Phase 3 — Server routes (still gated off catalog)

- Add the seven routes in `web/server.py` (§5).
- Add `_check_whatsapp` to `wizard_verify.py` (§8).
- Tests: `TestSkillRoutes`.
- **Ship as: PR 3.** Routes exist; catalog list still excludes whatsapp — no front-end can yet see it.

### Phase 4 — Live canary + catalog flip

- Pick canary bot (recommend a low-traffic team-bot whose `channels.whatsapp` schema-default block already exists so the install lands cleanly).
- Walk one full pair-on-mini install end-to-end manually (operator scans QR with personal phone or spare).
- Run `_check_whatsapp` Gauntlet check; confirm `active`.
- Send a message from the canary's WhatsApp to a verifier number; confirm the bot's reply arrives.
- **Only after the canary verifies clean:** add `whatsapp` to the catalog list in `web/server.py`. **Ship as: PR 4.**

### Phase 5 — Post-launch polish (optional, after operator feedback)

- Multi-account v2 (TBD trigger: Diana-persona work or first operator request).
- Group-message routing UI in skills page (currently operator-edits-openclaw.json only).
- `paired_phone` display in inventory page tile.

---

## 13. What this spec does NOT cover

- **`openclaw.json` schema migrations** for bots with the schema-default `channels.whatsapp` block but no plugin install. The drift-repair pass in `deploy.py` already preserves operator-touched fields; verify the `_DEFAULT_TELEGRAM_CHANNEL_FIELDS`-equivalent for WhatsApp doesn't strip an enabled-but-unpaired block on next deploy. Add a regression-guard test if not.
- **L1/L2 applier architecture extension** for WhatsApp. Bot config writes generally go through `UpdatePermissionConfig` (L2). Our install flow uses `_oc_install_common.write_oc_config` directly (L1-ish, but with the bot-user chown) — this is consistent with how `telegram_install.enable_channel_in_oc_config` already operates and doesn't require any L2 changes. Flag for review during PR 2.
- **Cost watchdog wiring.** WhatsApp Web has no per-message OC cost (Baileys is free). No `cost_watchdog` integration needed.
- **The remaining bucket of "OpenClaw ships X, we don't offer it" skills** — covered by the OpenClaw coverage audit spawned as a sibling task to this work (2026-06-04 audit chip).

---

## 14. Cross-cutting audit findings this spec respects

- **F1** (missing keystore CLI): N/A — WhatsApp's credential is a directory of Baileys session files, not a keystore-resolved env var.
- **F2** (asymmetric install/revoke): §4 `revoke_account` mirrors `enable_account_in_oc_config` line-for-line (config write inverted, plus auth dir teardown).
- **F3** (status lies): §6 mandates the live probe stage; never returns `active` from config presence alone.
- **F4** (runtime consumer): the audit doc's smoking-gun check — `ssh mini grep -rln whatsapp /opt/homebrew/lib/node_modules/openclaw/dist/` returns multiple hits including the dedicated doctor module. The runtime consumer demonstrably exists.
- **F5** (access panel honesty): §4.b uses present-tense `will`/`wont` only for capabilities the probe-confirmed runtime delivers; no future-tense aspirational claims.

---

## 15. Open questions to resolve during implementation

1. Confirm OC's `channels login --json` output format. The spec assumes JSONL `{kind, payload}` lines; if it's something else, the `_qr_pairing.py` parser changes shape, not the design.
2. Confirm `openclaw plugins install @openclaw/whatsapp` works when invoked as the bot user (not just as the admin). The brave gap-fill at `deploy.py:1937` proves the npm-install-via-bot-user pattern works; verify the `@openclaw/whatsapp` package is on clawhub/npm and the registry lookup succeeds.
3. Confirm the Baileys auth dir survives an `openclaw plugins reinstall` (it should — `authDir` is in `channels.whatsapp.accounts`, separate from `plugins.installs`, but worth a smoke test).
4. Decide on QR display size — Baileys QR codes are dense; the wizard UI should render them at least 240×240 px to ensure phones can scan from arm's length. Confirm with the front-end change.
