# OpenClaw Channel Coverage Audit — 2026-06-04

**Status**: completed via direct read of OC bundle on the mini + Evolve catalog source.
**Trigger**: discovery that `@openclaw/whatsapp` is an officially-shipped OC channel plugin (QR-link / Baileys pairing) that was never added to our Skills catalog; goal is to catch every other shipped-but-uncovered channel before the public launch.
**Method**: enumerated all three OC sources of truth (channel-catalog.json, bundled `extensions/`, `openclaw plugins search`); cross-referenced against the live Evolve skills catalog (`/api/skills/catalog` route in `web/server.py`) and `inventory.py`. Smoking-gun verification on the mini for every claim that OC ships a thing — `ssh mini grep` or `openclaw plugins inspect|search` for each.

---

## Executive summary

OpenClaw officially ships **27 distinct messaging channels** (19 in the installable catalog + 8 bundled in `extensions/`). Evolve's Skills catalog wraps **3 of them** (slack, telegram, discord). Of the remaining 24, six are high-leverage for Evolve's Plex-test / US-household / "Carla persona" audiences and should land before public launch; nine are niche-but-cheap-to-wrap follow-ups; nine are out of scope (China-region enterprise, Vietnam, voice control).

| Bucket | Count | Entries |
|---|---|---|
| **A — Already in our catalog** | 3 | discord, slack, telegram |
| **B — Officially shipped, MISSING from our catalog** | 15 | whatsapp, signal, matrix, mattermost, imessage (re-add), sms, synology-chat, nextcloud-talk, googlechat, msteams, line, feishu, nostr, qqbot, zalo |
| **C — Shipped but not worth wrapping (now)** | 9 | weixin, wecom, yuanbao, zalouser, tlon, twitch, irc, webhooks, phone-control/talk-voice |
| **D — Inventory mismatch** | 0 strict + 1 inverse | `_PLUGIN_DISPLAY` has no orphans, but `_CHANNEL_BACKED_SKILLS` only knows {slack,telegram,discord} → any bundled channel wired manually (signal/irc/mattermost/sms/imessage/webhooks) is invisible to the Skills page |

The WhatsApp gap is not a one-off. Eight other channels in the **same** `channel-catalog.json` shape have the same install ergonomics (npm or clawhub spec + `id`/`label`/`docsPath`) and the same wizard shape: install plugin → capture credential → write `channels.<id>` block in `openclaw.json` → kickstart gateway. The work to add one is ~the work to add five.

### Recommended bucket-B priority order (top 6 before launch)

1. **whatsapp** — the trigger; global default messenger; OC v2026.4.25+ ships with `clawhubSpec: clawhub:@openclaw/whatsapp` (verified: `openclaw plugins search whatsapp` returns 4 variants). Pairing is QR-code via WhatsApp Web — completely standalone, no Meta/Business API hoop. Plex-test trust: massive.
2. **signal** — bundled in OC core at `dist/extensions/signal/`; install path downloads `signal-cli` then links as a device. Hits the same privacy-aware US user the safety-as-flagship pitch was written for ([project_safety_as_flagship_feature](memory)). signal-cli has been vetted ([project_signal_cli_vetting_2026_05_14](memory)).
3. **matrix** — `clawhubSpec: clawhub:@openclaw/matrix`; pairing is homeserver-URL + user-ID + access-token (operator pastes from element.io account). Self-hosted-messaging audience overlaps heavily with Plex/Home Assistant users.
4. **imessage (re-add)** — the iMessage skill was withdrawn 2026-05-30 because the home-rolled install didn't wire `@openclaw/imessage`. But OC bundles the plugin at `dist/extensions/imessage/`. Re-add via the bundled-channel wizard shape and the load-bearing failures from the deep audit go away. This is the bundled-plugin rewire pattern that runway/dropbox/obsidian/notion now use — pure "stop reimplementing upstream" win.
5. **mattermost** — bundled in OC core at `dist/extensions/mattermost/`; pairing is server URL + personal access token. The self-hosted-Slack audience is exactly the Carla-persona service-business segment and the Diana-persona compartmentalized-team segment.
6. **sms** — bundled at `dist/extensions/sms/`; Twilio-backed inbound + outbound. Closes the "bot that texts me when the dishwasher's done" Plex-test loop. Pairing is Twilio account SID + auth token + phone number — one of the simpler wizards.

After those six, **synology-chat** and **nextcloud-talk** are cheap follow-ups (both pair via webhook URL only) and round out the self-hosted-Plex-test audience nicely.

---

## Per-OC-entry verdict table

Source: `dist/channel-catalog.json` (19 entries) + `dist/extensions/` directory enumeration (8 channel-shaped extensions) — verified live at OC v2026.6.1 on the mini.

| OC channel id | Label | Source | Install model | Bucket | Notes |
|---|---|---|---|---|---|
| `slack` | Slack | channel-catalog (official) | npm `@openclaw/slack` | A | `slack_install.py` — fix-in-place per 2026-05-30 audit (auto-install npm + appToken + symmetric revoke) |
| `discord` | Discord | channel-catalog (official) | npm `@openclaw/discord` | A | `discord_install.py` — fix-in-place per 2026-05-30 audit (P0-3 field name bug) |
| `telegram` | Telegram | bundled `dist/extensions/telegram/` | bundled | A | `telegram_install.py` — fix-in-place (symmetric revoke) |
| `whatsapp` | WhatsApp | channel-catalog (official) | clawhub `clawhub:@openclaw/whatsapp` (default) or npm `@openclaw/whatsapp`; `minHostVersion: >=2026.4.25` | **B (P1)** | **The catalyst.** QR-link via WhatsApp Web (per `selectionLabel: "WhatsApp (QR link)"`). Blurb says "works with your own number; recommend a separate phone + eSIM." Mirror the bundled-plugin install shape from runway; QR step is novel — see Bucket-B detail below. |
| `signal` | Signal | bundled `dist/extensions/signal/` | bundled; install downloads signal-cli (see `install-signal-cli-CVTb1L_7.js`) | **B (P1)** | Channel id `signal`. Pairing requires linking signal-cli as a secondary device from a primary Signal app. Doctor support exists (`audit.js` in extension). signal-cli vetting already done ([project_signal_cli_vetting_2026_05_14](memory)). |
| `matrix` | Matrix | channel-catalog (official) | clawhub `clawhub:@openclaw/matrix` (default) or npm `@openclaw/matrix` | **B (P1)** | Five CLI add options: `--homeserver`, `--user-id`, `--access-token`, `--device-name`, `--initial-sync-limit`. Wizard captures the three required (homeserver/user/token) and writes them into `channels.matrix.<bot_id>`. Markdown-capable, supports group rooms (`groupModel: sender`). |
| `imessage` | iMessage | bundled `dist/extensions/imessage/` | bundled | **B (P1, re-add)** | Was withdrawn 2026-05-30 because Evolve's home-rolled install didn't wire OC's `@openclaw/imessage`. **The OC plugin exists.** Re-add via bundled-plugin wizard shape; close all three load-bearing failures (poller, send path, install_imessage_poller call) by routing through OC. Per `aliases: ["imsg"]` and `cliAddOptions`, OC handles the imsg bridge itself. |
| `mattermost` | Mattermost | bundled `dist/extensions/mattermost/` | bundled (`peerDependencies: openclaw >=2026.6.1`) | **B (P2)** | Server URL + PAT. Self-hosted Slack alternative — direct fit for Carla-persona service businesses ([project_evolve_carla_persona_service_business](memory)) and Diana-persona compartmentalized teams ([project_evolve_diana_persona_multi_bot](memory)). |
| `sms` | SMS (Twilio) | bundled `dist/extensions/sms/` | bundled | **B (P2)** | Twilio account SID + auth token + phone number. Cleanest "household" channel — closes Plex-test loops like "text me when the dryer's done." Inbound webhook needs ngrok/cloudflared tunnel — call this out in wizard. |
| `synology-chat` | Synology Chat | channel-catalog (official) | npm `@openclaw/synology-chat` | **B (P2)** | Webhook-only ("Connect your Synology NAS Chat to OpenClaw"). NAS owners overlap massively with Plex/Home Assistant audience. Single-field wizard: webhook URL. |
| `nextcloud-talk` | Nextcloud Talk | channel-catalog (official) | npm `@openclaw/nextcloud-talk` | **B (P2)** | Self-host webhook bot. Same audience as matrix + synology-chat; cheap to wrap. |
| `googlechat` | Google Chat | channel-catalog (official) | npm `@openclaw/googlechat` | **B (P3)** | Workspace-only. Four CLI add options (webhook-path/url, audience-type/value). Useful for the small-business Workspace tier of Carla persona. |
| `msteams` | Microsoft Teams | channel-catalog (official) | npm `@openclaw/msteams` | **B (P3)** | Teams SDK + Azure app registration. Enterprise. Bot Framework registration is a 6-step Azure dance — wizard would be the longest of any channel. Useful for Diana-persona compartmentalized teams. |
| `line` | LINE | channel-catalog (official) | npm `@openclaw/line` | **B (P3)** | Japan-major. Webhook-based. Channel access token + channel secret. Wrap if any pod-user has Japan-resident family members; otherwise post-launch. |
| `feishu` | Feishu/Lark | channel-catalog (official) | npm `@openclaw/feishu`; `minHostVersion: >=2026.5.29` (newest channel) | **B (P3)** | China enterprise messaging. Aliases include `lark`. Likely only worth wrapping if Carla-persona expands into APAC. |
| `nostr` | Nostr | channel-catalog (official) | npm `@openclaw/nostr` | **B (P3)** | Decentralized; NIP-04 DMs. Privacy-aware audience but tiny absolute numbers. |
| `qqbot` | QQ Bot | channel-catalog (official) | npm `@openclaw/qqbot` | **B (P3)** | Official Tencent QQ Bot API. China audience. |
| `zalo` | Zalo | channel-catalog (official) | npm `@openclaw/zalo` | **B (P3)** | Vietnam Bot API. Region. |
| `weixin` | Weixin (Tencent personal) | channel-catalog (external Tencent) | npm `@tencent-weixin/openclaw-weixin@2.4.3` w/ pinned integrity | **C** | Personal WeChat via QR-code login. External (Tencent-maintained). China-personal — out of scope for US launch. |
| `wecom` | WeCom | channel-catalog (external Tencent) | npm `@wecom/wecom-openclaw-plugin@2026.5.7` | **C** | China enterprise. Out of scope. |
| `yuanbao` | Yuanbao | channel-catalog (external Tencent) | npm `openclaw-plugin-yuanbao@2.13.1` | **C** | Tencent AI assistant channel. Out of scope. |
| `zalouser` | Zalo Personal | channel-catalog (official) | npm `@openclaw/zalouser` | **C** | Vietnam personal account via QR — region niche. |
| `tlon` | Tlon (Urbit) | channel-catalog (official) | npm `@openclaw/tlon` | **C** | Decentralized Urbit messaging. Adoption near zero — skip. |
| `twitch` | Twitch | channel-catalog (official) | npm `@openclaw/twitch` | **C** | Twitch chat — fun but niche; revisit if any persona requires streamer overlap. |
| `irc` | IRC | bundled `dist/extensions/irc/` | bundled | **C** | Hobbyist; legacy. Skip. |
| `webhooks` | Webhooks | bundled `dist/extensions/webhooks/` | bundled | **C** | Generic bridge, not a user-facing channel — composes under other channels. No standalone install needed. |
| `phone-control` / `talk-voice` | Phone / Voice | bundled extensions | bundled | **C** | Voice control + talk runtime — not a messaging channel. Out of scope for Skills catalog. |

**Counts**: 19 channel-catalog entries + 8 bundled channel-shaped extensions = 27 total channel surfaces. Of those, 3 in Evolve (Bucket A), 15 Bucket B (worth wrapping), 9 Bucket C (skip for now).

---

## Bucket B — top 5 detailed recommendations

For each, the wizard shape mirrors an existing install module so the diff is mostly copy-and-rename. Status probes follow the pattern: write marker file at `~/.openclaw/skills/<id>.json`, surface in `inventory._FILESYSTEM_SKILLS`, write `channels.<id>.<bot_id>` block via the `_oc_install_common.enable_channel_in_oc_config` helper, kickstart gateway.

### 1. WhatsApp — `whatsapp_install.py`

**Install model**: clawhub install of `@openclaw/whatsapp` followed by QR-link pairing. OC manages the QR session via its own setup-entry flow. Verified: `openclaw plugins search whatsapp` returns 4 ClawHub variants, official `@openclaw/whatsapp v2026.6.1` at the top with install hint `openclaw plugins install clawhub:@openclaw/whatsapp`.

**Wizard shape**:
1. **Disclosure step** — "WhatsApp links your account as a Web client. WhatsApp recommends pairing a separate phone number + eSIM for unattended bots (per the blurb in OC's catalog)." Inline link to docs.
2. **Trigger install** — POST `/api/skills/install/whatsapp/start` runs `openclaw plugins install clawhub:@openclaw/whatsapp` as the bot user (mirror `runway_install.install_runway_plugin`).
3. **QR display** — OC's setup-entry emits a QR payload (per `WebWhatsAppConfig`-style — confirm shape during impl; falls back to a "scan the QR shown in `~/.openclaw/whatsapp/qr.png`" step if no inline emit).
4. **Liveness probe** — POST `/api/skills/install/whatsapp/status` polls OC's plugin status; flips to `active` when the linked-device handshake completes.
5. **Revoke** — clear `channels.whatsapp` from `openclaw.json` + delete the bot's linked-device session + kickstart. Mirror `slack_install` symmetric-revoke pattern from PR #1839.

**Closest existing module to mirror**: `runway_install.py` for the bundled-plugin install shape; `_oc_install_common.enable_channel_in_oc_config` for the channels-block write; the QR step is novel.

**Access panel `will/won't` care**: be explicit that the bot reads incoming WhatsApp messages and can send replies; bot DOES NOT post to status/stories or read other linked devices' message history. Plex-test trust: enormous — get this wording right.

---

### 2. Signal — `signal_install.py`

**Install model**: bundled OC plugin; install path downloads signal-cli (verified at `dist/install-signal-cli-CVTb1L_7.js`; macOS path uses Homebrew at `resolveBrewExecutable`). Pairing is "link as secondary device" against an existing primary Signal install.

**Wizard shape**:
1. **Prerequisite check** — POST `/api/skills/install/signal/check-cli` ensures signal-cli is installed or installs it via OC's `install-signal-cli` helper.
2. **Capture E.164 number** — single field; the operator's existing Signal account.
3. **Generate pairing QR** — call `signal-cli --account=<phone> link --name=<bot-id>`; display QR + `tsdevice://` URI.
4. **Wait for primary-device confirmation** — operator scans QR in their iPhone/Android Signal app's "Linked Devices."
5. **Verify** — POST `/api/skills/install/signal/verify` runs `signal-cli receive --timeout=2` to confirm the link is active.
6. **Write config** — `channels.signal.<bot_id> = { number, deviceName, configDir }` + kickstart.
7. **Revoke** — `signal-cli --account removeDevice <deviceId>` + clear channels.signal + kickstart.

**Closest existing module to mirror**: `telegram_install.py` for the BotFather-token-style direct config write; `runway_install.install_runway_plugin` for the prerequisite-binary-install shape.

**Signal-cli already vetted** per [project_signal_cli_vetting_2026_05_14](memory) — no new external dependency.

---

### 3. Matrix — `matrix_install.py`

**Install model**: clawhub install of `@openclaw/matrix` + access-token paste. Token comes from `element.io → All Settings → Help & About → Advanced → Access Token` (matrix.org default) or operator's self-hosted homeserver.

**Wizard shape**:
1. **Capture homeserver URL** — `https://matrix.org` default; operator can override (this is the self-host hook).
2. **Capture user-ID** — `@you:matrix.org` shape.
3. **Capture access token** — `syt_...` shape (Element-issued). Pre-write validation: hit `GET <homeserver>/_matrix/client/v3/account/whoami` with `Authorization: Bearer <token>` and confirm `user_id` matches step 2 (mirror `notion_install.verify_token` and `linear_install.verify_pat`).
4. **Optional device-name field** — default `evolve-<bot_id>`; helps the operator find the session in Element's device list when revoking.
5. **Write keystore + channels block** — token to keystore slot `matrix-<bot_id>` (mirror Notion); `channels.matrix.<bot_id>` block carrying homeserver/user-id/device-name.
6. **Revoke** — `DELETE <homeserver>/_matrix/client/v3/devices/<device_id>` (requires interactive auth — guide operator to log into Element instead) + clear keystore + clear channels.matrix + kickstart.

**Closest existing module to mirror**: `notion_install.py` (validate-before-write + keystore slot + symmetric revoke).

**Plex-test note**: Matrix's "homeserver URL" field is potentially intimidating. Default to matrix.org with a "Have your own server? Override here" link — don't lead with the self-host question. The Carla persona who hears "Matrix" first thinks Keanu, not protocol — copy should clarify "Open Discord/Slack alternative."

---

### 4. iMessage (re-add) — `imessage_install.py` (rewire)

**Install model**: bundled OC plugin at `dist/extensions/imessage/`. The module already exists in our tree but was withdrawn 2026-05-30 because none of the wiring touched the OC plugin. Rewire identical to the obsidian/dropbox/notion bundled-MCP rewire pattern from PR #1839.

**Wizard shape**:
1. **TCC grant check** — verify Full Disk Access for the bot user via existing `imessage_install.check_tcc` (preserve the working bits).
2. **Capture handle** — phone number or `@email.com`; the iMessage identity the bot will use.
3. **Trigger OC plugin install** — write `channels.imessage.<bot_id>` block with the handle; **also** call OC's setup-entry which installs the `imsg` bridge LaunchDaemon as the bot user (the current install does this manually via `install_imessage_poller`; let OC own it).
4. **Liveness probe** — call OC's doctor contract for the imessage channel (per the `doctor-contract-api.js` in the extension); poll until handshake.
5. **Revoke** — clear `channels.imessage.<bot_id>` + kickstart; OC's setup-entry handles LaunchDaemon teardown.

**Closest existing module to mirror**: `runway_install.py` (bundled-plugin pattern); the existing `imessage_install.py` keeps its TCC helpers but loses the home-rolled poller code.

**Risk note**: per the deep audit, the existing `imessage_install.py` carries three load-bearing failures (set-handle doesn't install poller; send has no consumer; poller one-way). All three are owned by OC's plugin once wired correctly — we just stop reimplementing what OC ships ([feedback_dont_reimplement_upstream](memory)).

---

### 5. Mattermost — `mattermost_install.py`

**Install model**: bundled OC plugin at `dist/extensions/mattermost/`. Pair via Mattermost server URL + personal access token.

**Wizard shape**:
1. **Capture server URL** — `https://chat.example.com` or `https://community.mattermost.com`.
2. **Capture personal access token** — issued from Mattermost's `Account Settings → Security → Personal Access Tokens`. Pre-write validation: `GET <server>/api/v4/users/me` with `Authorization: Bearer <token>` returns the user object.
3. **Capture team (optional)** — if the operator's account is in multiple teams, list them with `/api/v4/users/me/teams` and let them pick the team to which the bot belongs.
4. **Write keystore + channels block** — token to `mattermost-<bot_id>`; `channels.mattermost.<bot_id>` carries serverUrl/userId/teamId.
5. **Revoke** — `DELETE <server>/api/v4/users/tokens/revoke` with `token_id` from validation step + clear keystore + clear channels.

**Closest existing module to mirror**: `notion_install.py` (PAT + per-bot keystore + validate-before-write + symmetric revoke). Slack's installer is close on shape but slack uses OAuth2; Mattermost is a direct PAT — Notion is the cleaner mirror.

**Audience fit**: Mattermost is the open-source Slack the privacy-aware self-host audience already runs. Pairs naturally with the Plex/Home Assistant crowd. Service businesses (Carla persona) running Mattermost for client teams is a known pattern.

---

## Bucket D — inventory mismatches

**Strict definition** (entries in `inventory.py::_PLUGIN_DISPLAY` with no install module): **none.** Every channel-capable entry in `_PLUGIN_DISPLAY` (slack/telegram/discord) has an install module. The non-channel entries (anthropic/openai/google/xai LLM providers, evolve, dropbox, notion, linear, obsidian, github, google_workspace, brave) either have install modules in `skills/` or are first-party deploy-managed plugins.

**Inverse-failure mode (silent-failure risk worth raising)**:

`inventory.py::_CHANNEL_BACKED_SKILLS` only knows three channels:

```python
_CHANNEL_BACKED_SKILLS: dict[str, dict[str, Any]] = {
    "slack":    {...},
    "telegram": {...},
    "discord":  {...},
}
```

If an operator manually wires any of the **bundled** OC channels via the OC CLI (`openclaw channels add signal …`, `openclaw channels add irc …`, `openclaw channels add mattermost …`, `openclaw channels add sms …`, `openclaw channels add imessage …`), the resulting `channels.<id>.<bot_id>` block lands in `openclaw.json` and OC routes messages through it — but the Evolve **Skills page never shows it.** No skill row appears; no status chip; nothing.

Same fingerprint as the deep-audit's F3 ("status resolvers report active without probing capability") but in the opposite direction: capability exists, status surface doesn't.

**Recommendation**: once Bucket-B channels get install modules (which add their own `_FILESYSTEM_SKILLS` entries naturally), the gap closes for those channels. For the **remaining bundled channels** we're not wrapping (irc, webhooks, phone-control, talk-voice), add a passthrough block to `_CHANNEL_BACKED_SKILLS` so the Skills page at least reports `display="IRC (manual)"`, `status="configured"`, `install_source="channels"` when the operator wires them via OC CLI. Twelve-line PR; prevents the "I wired it but the UI lies" experience that bit us in the May incident.

Sample patch shape:

```python
# In inventory.py — add bundled channels we don't formally wrap but that
# operators can wire via `openclaw channels add <id>` directly. Surfaces
# them on the Skills page so the inventory doesn't silently miss them.
_CHANNEL_BACKED_SKILLS.update({
    "signal":     {"display": "Signal",     "category": "messaging", "token_fields": ("number", "configDir")},
    "irc":        {"display": "IRC",        "category": "messaging", "token_fields": ("server", "nickname")},
    "mattermost": {"display": "Mattermost", "category": "messaging", "token_fields": ("token", "serverUrl")},
    "sms":        {"display": "SMS (Twilio)", "category": "messaging", "token_fields": ("accountSid", "authToken")},
    "imessage":   {"display": "iMessage",   "category": "messaging", "token_fields": ("handle",)},
    "matrix":     {"display": "Matrix",     "category": "messaging", "token_fields": ("accessToken",)},
    "whatsapp":   {"display": "WhatsApp",   "category": "messaging", "token_fields": ("phoneNumber", "deviceId")},
})
```

This is **independent of** the install-module work: even if the wizards never ship, at least the inventory surface stops lying about coverage.

---

## Method

### Files read

OC bundle on the mini (read via `ssh mini cat …`):

- `/opt/homebrew/lib/node_modules/openclaw/dist/channel-catalog.json` — full catalog (19 entries, single JSON file)
- `/opt/homebrew/lib/node_modules/openclaw/dist/extensions/` — directory listing (49 bundled extension dirs)
- `/opt/homebrew/lib/node_modules/openclaw/dist/extensions/{telegram,signal,imessage,irc,mattermost,sms,webhooks}/package.json` — each verified `kind=channel` via the `openclaw.channel.id` block in the bundled `package.json`
- `/opt/homebrew/lib/node_modules/openclaw/dist/bundled/` — confirmed: only 5 hook-style extras (`boot-md`, `bootstrap-extra-files`, `command-logger`, `compaction-notifier`, `session-memory`) — no channels
- `/opt/homebrew/lib/node_modules/openclaw/dist/install-signal-cli-CVTb1L_7.js` — confirms signal-cli installer exists in OC core
- `/opt/homebrew/lib/node_modules/openclaw/dist/doctor-whatsapp-responsiveness-BL19OKUh.js` — confirms WhatsApp doctor support exists

Evolve catalog in the worktree:

- `packages/admin/evolve_admin/web/server.py` lines 18121-18380 — `/api/skills/catalog` route source-of-truth (11 catalog dispatches)
- `packages/admin/evolve_admin/skills/inventory.py` — `_PLUGIN_DISPLAY`, `_FILESYSTEM_SKILLS`, `_CHANNEL_BACKED_SKILLS`
- `packages/admin/evolve_admin/skills/*_install.py` — 16 install modules (12 active + 4 withdrawn-but-on-disk)
- `packages/admin/evolve_admin/skills/upstream_plugin_skills.py` — `SKILLS` dict (brave + github only after 2026-05-30 withdrawals)
- `docs/skills-deep-audit-2026-05-30.md` — framework + audit-conventions reference

### Mini CLI commands run

```
ssh mini 'cat /opt/homebrew/lib/node_modules/openclaw/dist/channel-catalog.json'
ssh mini 'PATH=/opt/homebrew/bin:$PATH openclaw plugins list --json'
ssh mini 'PATH=/opt/homebrew/bin:$PATH openclaw plugins search whatsapp'
ssh mini 'PATH=/opt/homebrew/bin:$PATH openclaw plugins search signal'
ssh mini 'PATH=/opt/homebrew/bin:$PATH openclaw plugins inspect whatsapp'  # "Plugin not found" — confirms not installed
ssh mini 'PATH=/opt/homebrew/bin:$PATH openclaw channels list --json'      # returns only slack — confirms no other channels wired anywhere
ssh mini 'ls /opt/homebrew/lib/node_modules/openclaw/dist/extensions/'     # 49-line listing
ssh mini 'cat /opt/homebrew/lib/node_modules/openclaw/dist/extensions/{telegram,signal,imessage,irc,mattermost,sms,webhooks}/package.json'
```

The `openclaw plugins list --installable --json` form mentioned in the task brief is **not a real flag** — `--installable` is rejected by OC v2026.6.1's CLI with `OpenClaw does not recognize option "--installable"`. The installable surface is the static `channel-catalog.json`; live ClawHub queries go via `openclaw plugins search <query>`.

### Sources I couldn't access

- **ClawHub web catalog (full enumeration)**. `openclaw plugins search <query>` works as a free-form text query but there's no `list-all`. I queried with `whatsapp` and `signal` for smoking-gun verification; a full ClawHub sweep would require either crawling the ClawHub website or having an OC release-team contact for the canonical registry dump. The 19 entries in `channel-catalog.json` are OC's curated subset that ships with the host — any community channel that isn't in this file would not appear in any wizard prompt anyway. Low-risk gap.
- **OC's outward MCP catalog**. Distinct from channels (this audit is channels-only). The 49 bundled extensions include many LLM providers, search tools, memory backends — wrapping any of those as Skills is a separate question (and largely already covered by deploy/providers logic). The Carla/Diana persona work suggests "outward MCP for evo" is a near-term promotion candidate per [project_google_io_2026_implications](memory) but that's not in this audit's scope.

### Smoking-gun snippets (per claim)

WhatsApp shipped by OC:
```json
{
  "name": "@openclaw/whatsapp",
  "openclaw": {
    "channel": { "id": "whatsapp", "label": "WhatsApp", "selectionLabel": "WhatsApp (QR link)" },
    "install": { "clawhubSpec": "clawhub:@openclaw/whatsapp", "npmSpec": "@openclaw/whatsapp", "defaultChoice": "clawhub", "minHostVersion": ">=2026.4.25" }
  }
}
```

Signal bundled in core:
```bash
$ ssh mini 'cat /opt/homebrew/lib/node_modules/openclaw/dist/extensions/signal/package.json'
{
  "name": "@openclaw/signal",
  "version": "2026.6.1",
  "openclaw": {
    "channel": { "id": "signal", "label": "Signal", "selectionLabel": "Signal (signal-cli)" }
  }
}
```

iMessage bundled in core (despite our withdrawal):
```bash
$ ssh mini 'cat /opt/homebrew/lib/node_modules/openclaw/dist/extensions/imessage/package.json'
{
  "name": "@openclaw/imessage",
  "version": "2026.6.1",
  "openclaw": {
    "channel": { "id": "imessage", "label": "iMessage", "selectionLabel": "iMessage (imsg)" }
  }
}
```

Mattermost bundled:
```bash
$ ssh mini 'cat /opt/homebrew/lib/node_modules/openclaw/dist/extensions/mattermost/package.json | grep -A2 channel'
"channel": { "id": "mattermost", "label": "Mattermost" }
```

SMS / IRC / webhooks bundled (channel-shaped extensions in the same dir): verified by package.json `openclaw.channel.id` block presence on each.

OC CLI confirms WhatsApp installable from ClawHub:
```
$ ssh mini 'PATH=/opt/homebrew/bin:$PATH openclaw plugins search whatsapp'
ClawHub plugins (4)
@openclaw/whatsapp  code-plugin | official | v2026.6.1 — OpenClaw WhatsApp channel plugin for WhatsApp Web chats.
  Install: openclaw plugins install clawhub:@openclaw/whatsapp
[+ 3 community variants]
```

Evolve catalog enumerates only 11 entries (gog, slack, discord, telegram, obsidian, dropbox, notion, runway, linear, brave+github via upstream, autocad) — verified by exhaustive grep of `/api/skills/catalog` dispatchers in `web/server.py:18121-18380`. Of these 11, the only three that map to OC channels are slack, discord, telegram — the rest are non-channel skills (storage, notes, search, etc.).

---

## Phased fix plan

### Phase 1 — Stop the WhatsApp-class bleed (this week, ~2 days)

1. **whatsapp_install.py** — clawhub install + QR-pair wizard + symmetric revoke
2. **signal_install.py** — signal-cli prerequisite + device-link wizard + symmetric revoke
3. **matrix_install.py** — homeserver/user/token capture + validate-before-write + symmetric revoke
4. **imessage rewire** — replace home-rolled `imessage_install.py` install path with bundled-plugin shape (close the three deep-audit load-bearing failures by handing the work to OC)
5. **inventory.py `_CHANNEL_BACKED_SKILLS` passthrough** — add the bundled channels we're not formally wrapping so manually-wired channels don't silently disappear

### Phase 2 — Bucket B P2 follow-ups (next two weeks)

6. **mattermost_install.py** — server-URL + PAT (mirror notion shape)
7. **sms_install.py** — Twilio account SID + auth token + phone + tunnel-URL helper
8. **synology-chat_install.py** — single-field webhook wizard
9. **nextcloud-talk_install.py** — single-field webhook wizard

### Phase 3 — Bucket B P3 (post-launch as personas demand)

10. **googlechat / msteams** — when first Diana/Carla persona case needs them
11. **line / feishu / qqbot / zalo / nostr** — region-by-region; only when a real persona requires
12. **twitch** — fun, defer

### Phase 4 — Never (Bucket C — closed)

Weixin, wecom, yuanbao, zalouser, tlon, irc, webhooks, phone-control, talk-voice are explicitly **not** in the Evolve catalog roadmap. Document this in `docs/contributing-skills.md` so future audits don't reopen.

---

## Cross-cutting findings (mirroring the deep-audit's F-section discipline)

### F1 — "Bundled" is opaque to the catalog audit

Eight channel-shaped plugins ship inside `dist/extensions/` and **do not appear in `channel-catalog.json`**. The audit-2026-05-30 framework only checked the catalog file. If the deep audit had looked at `extensions/` it would have caught the iMessage withdrawal as "withdrawing-a-skill-that's-actually-bundled" — a misread that we should not repeat.

**Recommendation**: any future skill audit must enumerate both `channel-catalog.json` AND `dist/extensions/*/package.json` looking for the `openclaw.channel` block; record both as "officially shipped." Add a script to `tools/` that emits the union list, runnable in CI against any OC version bump.

### F2 — Channel-catalog `minHostVersion` is a real gate

Every channel-catalog entry carries `minHostVersion` (whatsapp: `>=2026.4.25`, feishu: `>=2026.5.29`). The pod runs OC v2026.6.1 today so all 19 channels are reachable, but newer channels will appear during OC upgrades. The Evolve catalog has no equivalent — should add `min_oc_version` to install module metadata so the Skills page can hide entries that the running OC version doesn't support yet.

### F3 — The WhatsApp/Signal/Matrix wizards share 80% structure

The pattern is: capture credential → validate against vendor API → write keystore slot + `channels.<id>` block → kickstart → mirror in revoke. The deep audit already noted this for `_oc_install_common.enable_channel_in_oc_config`. The complement `disable_channel_in_oc_config` proposed in the F2 cross-cutting fix should land **before** any of the Phase-1 modules so each new channel skill inherits the symmetric revoke for free instead of repeating the asymmetric-revoke bug.

### F4 — Don't reimplement upstream (revisited)

Confirms [feedback_dont_reimplement_upstream](memory): the iMessage skill's three failures were all "OC has a plugin for this; we built parallel infrastructure that didn't wire it." Same anti-pattern blocked the gog/gdrive/unity/apple-local Bucket-1c withdrawals. Going forward, every new channel install module MUST start with "does OC bundle or catalog this?" — and if yes, mirror runway/dropbox/obsidian/notion (the bundled-plugin pattern) rather than building custom poller/sender infrastructure.

### F5 — `openclaw plugins search` is the source-of-truth for ClawHub

Confirmed live: `openclaw plugins search whatsapp` returns 4 results (1 official + 3 community variants). The community variants (`openclaw-channel-whatsapp-official` via imBee, `@kapso/openclaw-whatsapp`, `@lotfinity/atomic-waha-v3`) aren't in `channel-catalog.json` and aren't in scope for this audit, but worth noting that the WhatsApp space has 4 implementations — Evolve should pick **`@openclaw/whatsapp`** (the official one in `channel-catalog.json`) and not the community alternates. The catalog's `defaultChoice: clawhub` for `@openclaw/whatsapp` confirms the official pick.

---

## Appendix A — full OC channel surface enumeration (verified state at OC v2026.6.1)

**From `channel-catalog.json` (19 entries; `kind: channel` for all):**

| Position | npm/clawhub name | Channel id | Source | Default install |
|---|---|---|---|---|
| 1 | `@openclaw/discord` | discord | official | npm |
| 2 | `@openclaw/feishu` | feishu | official | npm |
| 3 | `@openclaw/googlechat` | googlechat | official | npm |
| 4 | `@openclaw/line` | line | official | npm |
| 5 | `@openclaw/matrix` | matrix | official | clawhub |
| 6 | `@openclaw/msteams` | msteams | official | npm |
| 7 | `@openclaw/nextcloud-talk` | nextcloud-talk | official | npm |
| 8 | `@openclaw/nostr` | nostr | official | npm |
| 9 | `@tencent-weixin/openclaw-weixin` | openclaw-weixin | external | npm (pinned) |
| 10 | `@openclaw/qqbot` | qqbot | official | npm |
| 11 | `@openclaw/slack` | slack | official | npm |
| 12 | `@openclaw/synology-chat` | synology-chat | official | npm |
| 13 | `@openclaw/tlon` | tlon | official | npm |
| 14 | `@openclaw/twitch` | twitch | official | npm |
| 15 | `@wecom/wecom-openclaw-plugin` | wecom | external | npm (pinned) |
| 16 | `@openclaw/whatsapp` | whatsapp | official | **clawhub** |
| 17 | `openclaw-plugin-yuanbao` | yuanbao | external | npm (pinned) |
| 18 | `@openclaw/zalo` | zalo | official | npm |
| 19 | `@openclaw/zalouser` | zalouser | official | npm |

**From `dist/extensions/` with `openclaw.channel.id` in package.json (8 bundled channel-shaped extensions):**

| Position | Extension dir | Channel id | Notes |
|---|---|---|---|
| 1 | `extensions/telegram/` | telegram | grammy-based; bundled in OC core |
| 2 | `extensions/signal/` | signal | signal-cli linked device; installer at `install-signal-cli-CVTb1L_7.js` |
| 3 | `extensions/imessage/` | imessage | imsg bridge; aliases `imsg` |
| 4 | `extensions/irc/` | irc | server+nick |
| 5 | `extensions/mattermost/` | mattermost | peerDep `openclaw >=2026.6.1` |
| 6 | `extensions/sms/` | sms | Twilio-backed |
| 7 | `extensions/webhooks/` | (no channel id — bridge) | Generic webhook bridge; composes under other channels |
| 8 | `extensions/phone-control/` + `extensions/talk-voice/` | voice | Voice/calling — not standard messaging |

Net total: **27 channels** in the union (19 + 8). De-duplicate the bridge/voice items and the audience-relevant count is **24 standard messaging channels** — of which Evolve currently wraps 3.

---

## Appendix B — Evolve catalog as of 2026-06-04 (for diffing)

Live entries from `web/server.py:18123` `/api/skills/catalog` route:

| Catalog id | Module | Skill type | Audit verdict (per 2026-05-30) |
|---|---|---|---|
| gog | `gog_install.py` | OAuth — Google Workspace | 🔴 withdrawn |
| slack | `slack_install.py` | Channel — OAuth | 🟡 fix-in-place |
| discord | `discord_install.py` | Channel — OAuth | 🟡 fix-in-place (P0-3) |
| telegram | `telegram_install.py` | Channel — bot token | 🟡 fix-in-place (revoke) |
| obsidian | `obsidian_install.py` | MCP-filesystem | 🔵 architecturally correct (P0-1/P0-2 blocked) |
| dropbox | `dropbox_install.py` | MCP-filesystem | 🔵 architecturally correct (P0-1/P0-2 blocked) |
| notion | `notion_install.py` | MCP-token | 🔵 architecturally correct (P0-1 blocked) |
| runway | `runway_install.py` | Bundled OC plugin | ✅ rewired 2026-05-30 |
| linear | `linear_install.py` | MCP-token | 🔵 architecturally correct (P0-1 blocked) |
| brave | `upstream_plugin_skills.py:SKILLS[brave]` | OC plugin (apiKey) | 🟡 fix-in-place (P0-5) |
| github | `upstream_plugin_skills.py:SKILLS[github]` | Workspace backup | 🟡 fix-in-place (P0-6) |
| autocad | `autocad_install.py` | Honest stub | ✅ honest stub |

Net of audit verdicts and the new finding: **3 wrapped channels of OC's 24 standard messaging channels** is the precise pre-launch gap. Phase 1 of this audit closes 5 of them (whatsapp, signal, matrix, imessage-rewire, plus the inventory-passthrough block).

---

## What changes (in summary)

If we ship Phase 1: **8 channels wrapped** (existing 3 + whatsapp/signal/matrix/imessage). Public launch reads as "your bot talks Discord, Slack, Telegram, WhatsApp, Signal, Matrix, iMessage, and you can plug in SMS or Mattermost in two clicks" instead of "Discord, Slack, Telegram, that's it."

If we ship Phase 2: **12 channels wrapped** — full Plex-test coverage (everything a Carla or Diana persona would expect to find).

If we ship nothing: the WhatsApp gap is the **one most likely** to be discovered by the first 10 users (it's the global default messenger) and will read as "Evolve doesn't support WhatsApp" — false, because OC v2026.4.25+ does, but Evolve never built a wizard for it.

The point of this audit is the same as the May-30 audit's point: **under-promise, don't over-promise.** Today the Skills page accurately lists what Evolve installs; the gap is between what OC ships and what Evolve packages. Closing it before launch keeps the trust contract intact.
