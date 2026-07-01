# Slack Policy Layer — Architecture (2026-05-13)

Status: **shipped** — Phase 1+2 backend in [#1074](https://github.com/evolve-ops/evolve/pull/1074); openclaw.json shape correction (the actual config lives at `channels.slack.channels.<ID>`, not top-level — see §3.1) in follow-up PR.

**What this is.** A policy layer that Evolve owns above the OpenClaw `openclaw.json` config, plus validators and probes that catch the silent-failure modes that have repeatedly broken Slack-integrated bots in this pod (most recently team-bot-a). The policy is editable in the admin UI and via the evo bot in Slack DM; both write the same file; the writer renders it down to the openclaw.json primitives the OC gateway actually reads.

**Why now.** Two silent failures in the last setup cycle for team-bot-a:
1. Channel keys must be Slack IDs, not names, under `groupPolicy: "allowlist"`. Name-keyed entries are silently dropped at OC's routing layer. No error, no warning — messages just don't reach the bot.
2. OpenClaw 2026.4.27 flipped the default for `messages.groupChat.visibleReplies`. Replies were running but suppressed at the reply layer. Only detectable by tailing the gateway log.

Both were silent at config time and loud at use time — the exact gap that a validation + policy layer closes. The frustration this caused is generalizable: the Slack integration is the single most-touched integration in the pod (per `project_pod_bot_integrations` — team-bot-a ↔ slack is a pod-wide invariant), and silent failures here cascade into "the bot is broken" across the whole user surface.

**Relationship to other specs.**
- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) — the Signal store is where the continuous probe writes drift alerts. `slack_config_probe` is a new producer alongside `integration_probe`.
- [spec-evo-wizard-2026-05-05.md](spec-evo-wizard-2026-05-05.md) — the evo bot's conversational surface. The Slack-DM admin flows in §7 plug into the wizard's handler framework.
- [spec-forge-via-messaging-2026-05-07.md](spec-forge-via-messaging-2026-05-07.md) — sets the precedent for pre-apply validation at the messaging layer. The Slack policy writer has a similar pre-write gate.
- This spec does NOT subsume the existing `integration_probe` for Slack — that probe checks "is the integration alive?" This spec adds the orthogonal question "is the integration *configured correctly*?"

---

## 1. The problem with the current frame

Three structural gaps in how Slack-bot config works today:

**1. Silent failures at the OC config boundary.** OC accepts an `openclaw.json` that looks valid but routes nothing — name-keyed channels under allowlist, missing reply-visibility settings, channel IDs the bot isn't a member of. The operator has no signal until messages start getting dropped, at which point the diagnosis path is "tail `/tmp/openclaw/openclaw-YYYY-MM-DD.log` and grep for warnings." That fails the Plex test by a wide margin.

**2. No expression of admin intent above the config file.** Today the only place the admin can say "team-bot-a listens in #management and #project-x, with @-mention required in busy channels, and DMs are open to anyone on the team" is by hand-editing the openclaw.json the OC gateway reads. The configuration language is the implementation detail. A user who can install Plex cannot write that JSON correctly, and the team-bot-a incident shows that someone who *can* still gets it wrong.

**3. No reconciliation between Slack state and bot config.** Slack is a moving target — channels get renamed, the bot gets invited to new channels, OC ships an update that flips a default. None of this surfaces in Evolve today. The "bot invited but not on the watch list" failure mode is recurring; the operator finds out when the bot doesn't respond in a channel they thought it would.

A policy layer + validator + continuous probe closes all three.

---

## 2. Core reframe: Evolve owns policy, openclaw.json is rendered

This is the same pattern as the admin-UI bot config write path elsewhere: the operator expresses intent in Evolve's vocabulary, a writer renders it to the OC-shaped artifact, and the OC gateway consumes the artifact. The operator never sees or hand-edits openclaw.json for Slack settings under normal use.

| Concept | Role | Output | Lives in |
|---|---|---|---|
| **Slack policy** (this spec) | Admin intent — who can talk to the bot, what channels, what reply behavior | `slack-policy.json` per bot | `{shared_dir}/bots/<bot_id>/slack-policy.json` |
| **Policy writer** (this spec) | Renders policy → openclaw.json `channels` + `messages` sections | `openclaw.json` patch | `packages/admin/evolve_admin/integrations/slack/writer.py` |
| **slack-doctor** (this spec) | Pre-apply validator — refuses to write a policy that would silently break | exit code + report | `packages/admin/evolve_admin/integrations/slack/doctor.py` |
| **slack_config_probe** (this spec) | Continuous reconciler — writes Signals on drift | `Signal` records | `packages/admin/evolve_admin/integrations/slack/probe.py` |

**The contract.** The OC gateway is the consumer of openclaw.json; that doesn't change. The policy file is *upstream* of openclaw.json. Editing openclaw.json's Slack section by hand is supported as an escape hatch but the policy writer will overwrite hand edits on the next render — same pattern as forge's manifest-vs-generated-code contract.

**Why not put policy fields directly in openclaw.json.** Three reasons. (a) Some policy fields (self-enrollment codes, "ask the admin before auto-adding") have no openclaw.json representation — they're Evolve operations, not OC config. (b) Policy expressions ("members of `@project-x-team`") have to be resolved against the live Slack workspace at render time, not at OC config time. (c) The policy file is the joint source of truth for the UI and the evo bot — having a separate file with a stable schema lets both surfaces edit safely without racing the OC config.

---

## 3.1 OpenClaw openclaw.json — the actual Slack shape

**Correction (2026-05-13, post-#1074):** the original spec assumed Slack
channel entries lived at top-level ``channels.<SLACK_ID>``. They don't.
Verified ground-truth on the production mini (team-bot-a + admin-bot):

```yaml
channels:
  slack:                                  # provider sub-block
    botToken:           "xoxb-..."        # credential (rotated out of band)
    appToken:           "xapp-..."        # credential (socket-mode)
    userTokenReadOnly:  "xoxp-..."        # credential
    mode:               "socket"          # transport mode
    enabled:            true
    webhookPath:        "/slack/events"
    groupPolicy:        "allowlist"       # channel routing policy
    dmPolicy:           "pairing"
    streaming:          {...}
    allowFrom:          ["U0...", ...]    # USER allowlist (~who can talk to bot)
    channels:                             # CHANNEL allowlist (KEYED BY SLACK ID)
      C0AL2GDUA7J:
        requireMention: false
      G0T79FGSE:
        requireMention: false
  telegram:                               # other providers same shape
    botToken: ...
  whatsapp:                               # may be a stale relic — surfaced
    enabled: false                        # by the doctor's "other providers"
messages:
  groupChat:
    visibleReplies: "automatic"           # the bug-2 trap
```

**What the writer owns** (all under ``channels.slack.*``):

- ``channels.slack.groupPolicy`` ← always ``"allowlist"`` in Phase 2
- ``channels.slack.channels.<SLACK_ID>`` ← one entry per ``policy.channels.entries[]``;
  KEY must be a Slack ID; name-keyed entries (bug 1) are swept on every render
- ``channels.slack.allowFrom`` ← sorted user IDs when ``dm_allowlist.mode == "explicit"``,
  removed for every other mode
- ``messages.groupChat.visibleReplies`` ← ``policy.messaging.visible_replies_default``

**What the writer preserves verbatim:**

- ``channels.slack.{botToken|appToken|userTokenReadOnly}`` — credentials
- ``channels.slack.{mode|dmPolicy|enabled|webhookPath|streaming}`` — other slack peers
- ``channels.{telegram|discord|whatsapp|…}`` — sibling provider sub-blocks
- ``messages.*`` sub-keys other than ``groupChat.visibleReplies``
- All other top-level openclaw.json fields (``hooks``, ``permissions``, ``meta``, ``agent``)
- Unknown per-channel fields (e.g. a custom routing flag the operator set)

**Process lesson.** PR #1074 shipped reading the wrong location because the
implementation was built against the team-bot-a bug-1 post-mortem text rather than
against a real openclaw.json. The "verify-or-don't-ship" rule in
``feedback_rsi_design_approach`` applies — every parser/renderer must be
verified against ground-truth data before merge. The shape-fixture in
``packages/admin/tests/test_slack_policy.py`` is now built from sanitized
team-bot-a + admin-bot data so this can't drift silently again.

## 3. The Slack policy schema

```yaml
# {shared_dir}/bots/<bot_id>/slack-policy.json
{
  "schema_version": 1,
  "bot_id": "team-bot-a",
  "workspace": {
    "team_id": "T0AABBCC",          # Slack workspace ID (resolved at bootstrap)
    "team_name": "Example Corp"      # for human display only; team_id is authoritative
  },

  # Who can talk to the bot
  "access": {
    "dm_allowlist": {
      "mode": "open" | "user_group" | "explicit" | "channel_derived",
      "user_group_id": "S0AABBCC",                # if mode=user_group
      "user_ids": ["U0AABBCC", "U0AABBCD"],      # if mode=explicit
      "derived_from_channel_ids": ["C0AABBCC"]    # if mode=channel_derived (anyone in these channels)
    },
    "self_enrollment_codes": [
      {
        "code": "racecar",
        "created_by": "U0AABBCC",
        "created_at": "2026-05-13T15:30:00Z",
        "expires_at": "2026-05-14T15:30:00Z",
        "consumed": false,
        "consumed_by": null,
        "consumed_at": null
      }
    ]
  },

  # Which channels the bot listens in, and how
  "channels": {
    "default_for_new": {
      "behavior": "ask_admin" | "auto_add_mention_only" | "auto_add_listen_all" | "ignore",
      "ask_admin_via": "evo_dm"                   # how to ask; today only evo_dm is supported
    },
    "entries": [
      {
        "channel_id": "G0T79FGSE",                # authoritative key (always an ID)
        "channel_name": "management",             # display only; resolved at policy render
        "channel_type": "private" | "public",
        "require_mention": true | false,
        "visible_replies": "automatic" | "thread_only" | "ephemeral",  # per-channel override
        "added_at": "2026-05-10T12:00:00Z",
        "added_by": "U0AABBCC" | "auto:member_joined" | "policy_default"
      }
    ]
  },

  # Reply / messaging behavior
  "messaging": {
    "visible_replies_default": "automatic",       # default for any channel; was the bug 2 trap
    "thread_replies": true,
    "quiet_hours": {
      "enabled": false,
      "timezone": "America/Los_Angeles",
      "start": "22:00",
      "end": "07:00",
      "paged_overrides": true                     # allow alert-severity Signals to break quiet hours
    },
    "mention_style": "strict"                     # @team-bot-a only; "fuzzy" = also "hey team-bot-a" etc.
  },

  # Pointers to drift state — written by the probe, read by the UI
  "last_reconciled_at": "2026-05-13T15:30:00Z",
  "drift_signals": ["sig_abc123"]                  # active Signal IDs concerning this policy
}
```

**Schema invariants.**

- `channels.entries[*].channel_id` is the authoritative key. Names are cached for display and re-resolved at render time. The writer refuses to render a policy with a name-keyed entry.
- `access.self_enrollment_codes` is treated as a credential collection — write-only from the admin's perspective once issued, audit-logged on consumption.
- `channels.default_for_new.behavior` defaults to `"ask_admin"` — friendly-vigilant, per `feedback_safety_as_flagship_feature`. Auto-add is opt-in.
- `messaging.visible_replies_default` defaults to `"automatic"` in v1, since that's what every existing bot expects. If a future OC version flips a default again, the probe (§6) detects the mismatch and surfaces a Signal.

---

## 4. The writer: policy → openclaw.json

The writer renders the Slack policy down to the `channels` + `messages` sections of openclaw.json. It owns *just those keys* — it must not touch other openclaw.json sections (hooks, permissions, etc.). The render is idempotent: same policy in produces same openclaw.json out.

**Render rules.**

| Policy field | openclaw.json output |
|---|---|
| `access.dm_allowlist.mode = "open"` | no DM-allowlist key (OC defaults to open) |
| `access.dm_allowlist.mode = "user_group"` | resolve user_group → user IDs at render time; write to `directMessages.allowedUserIds` |
| `access.dm_allowlist.mode = "explicit"` | `directMessages.allowedUserIds` ← `user_ids` |
| `access.dm_allowlist.mode = "channel_derived"` | resolve channels → member user IDs at render time |
| `channels.entries` | `channels.<channel_id>: { requireMention: ... }` for each entry; `groupPolicy: "allowlist"` |
| `messaging.visible_replies_default` | `messages.groupChat.visibleReplies` |
| `channels.entries[*].visible_replies` | per-channel override under `channels.<id>.visibleReplies` |

**Pre-render validation.** Before writing, the writer calls `slack-doctor` (§5) against the *target* state. If doctor returns FAIL, the writer refuses and surfaces the failures to the caller (UI or evo DM). This is the load-bearing gate — same precedent as forge's pre-validate (PR #1069).

**Atomic write.** The writer follows the standard pattern from CLAUDE.md: stage to `/tmp`, validate the JSON parses, then `sudo /bin/cp` to the bot's openclaw.json. The policy file itself lives under `{shared_dir}/bots/<bot_id>/`, owned by the `evolve` user — atomic temp-file + rename, no sudo needed.

**Hot-reload.** OC hot-reloads `channels` + `messages` sections; the writer does not need to restart any daemon. The exception is `directMessages.allowedUserIds` — OC's behavior here is not yet verified. The writer logs the change and the probe (§6) confirms it took effect.

---

## 5. slack-doctor: pre-apply validator

A CLI + library that resolves a target policy against the live Slack workspace and reports any silent-failure conditions. Three callers: pre-render (above), pre-deploy (`evolve-admin deploy <bot>`), and manual (`evolve-admin slack-doctor <bot>`).

**Checks the doctor performs.**

| Code | Severity | Condition | Source |
|---|---|---|---|
| `SLK001` | FAIL | Policy entry has name as `channel_id` (must be `C…` or `G…`) | bug 1 |
| `SLK002` | FAIL | `channel_id` not present in `users.conversations` for this bot (bot isn't a member). **Exception:** if the ID is a group DM (mpim) the bot participates in, SLK002 is suppressed in favor of `SLK019` — see below | bug 1 cascade |
| `SLK003` | WARN | `messaging.visible_replies_default` missing or differs from current OC default-of-defaults | bug 2 |
| `SLK004` | FAIL | `directMessages.allowedUserIds` references a Slack user ID Slack doesn't recognize | DM allowlist hygiene |
| `SLK005` | INFO | Bot is a member of a channel not in `channels.entries` (probable invite-without-watch) | recurring failure mode |
| `SLK006` | WARN | Self-enrollment code present with `expires_at` in past (zombie code) | hygiene |
| `SLK007` | FAIL | Bot token can't reach `auth.test` (bad / revoked token) | bootstrap gate |
| `SLK008` | WARN | Workspace `team_id` in policy differs from `auth.test` response | possible token swap |
| `SLK009` | INFO | Channel ID in policy resolves to a name that differs from cached `channel_name` (rename) | drift |
| `SLK010` | FAIL | `groupPolicy: "allowlist"` in target openclaw.json but `channels.entries` empty (silent black-hole) | safety net |
| `SLK019` | WARN | A `channel_id` in `channels.slack.channels` is actually a group DM (mpim) the bot participates in — a misplaced, inert entry (OC routes group DMs via `dmPolicy`/`dm.groupEnabled`/`dm.groupChannels`, never the channel allowlist) | mpim/channel-allowlist confusion |

> **Group-DM (mpim) carve-out for SLK002 / SLK005.** `member_by_id` is built
> from `users.conversations` with the default
> `types="public_channel,private_channel"`, so group DMs never appear in it. In
> newer workspaces a group DM's conversation ID is `C`-prefixed (verified:
> `C0B7PDLM2PJ`), indistinguishable from a real channel by shape. OpenClaw routes
> mpim messages through the DM subsystem (`dmPolicy` + `channels.slack.dm.groupEnabled`
> + `dm.groupChannels` + `allowFrom`) — never `channels.slack.channels`/`groupPolicy`
> (verified against the OC config schema and docs.openclaw.ai/channels/slack). The
> doctor therefore resolves mpim membership with a **separate**
> `users.conversations(types="mpim")` lookup (issued lazily, only when an ID-keyed
> entry would otherwise fire SLK002) that is **not** merged into `member_by_id`: a
> group-DM entry the bot participates in downgrades from a false SLK002 FAIL to a
> non-blocking `SLK019` WARN, while SLK005's uncovered-channel scan stays confined to
> real channels. When the mpim lookup is inconclusive (e.g. missing `mpim:read`
> scope), SLK002 falls back to FAIL so a genuinely-missing channel is still caught.

**Calling shape.**

```bash
# Manual
evolve-admin slack-doctor team-bot-a                  # exit 0 if no FAIL; 1 if any FAIL
evolve-admin slack-doctor team-bot-a --json           # machine-readable for the probe
evolve-admin slack-doctor team-bot-a --fix            # rewrite name-keyed entries to ID-keyed (SLK001 only)

# Library
from evolve_admin.integrations.slack.doctor import run_doctor
result = run_doctor(bot_id="team-bot-a")              # DoctorResult(findings=[...])
```

**Tolerance for half-installed state.** Per the conversation, the validator must degrade to INFO when the bot isn't in any channel yet — common during onboarding. Specifically: `SLK010` (empty allowlist) is only a FAIL when *at least one channel exists in Slack that the bot is a member of*. If the bot is in zero channels, the policy can be empty; INFO is appropriate.

---

## 6. slack_config_probe: continuous reconciler

A new Signal-store producer that runs the same checks as `slack-doctor` on a schedule and writes Signals on drift. Subclass / sibling of `integration_probe` (per `project_integration_discovery_probes`); follows the sweep-resolve pattern from the Signal store spec so cleared conditions auto-archive.

**Schedule.** Every 15 minutes for the active pod, on the same launchd cadence as the existing probes. Cheap — most runs are pure Python after Slack API calls; LLM is not involved (per `feedback_rsi_low_cost_preference`).

**Signal shape.** Each finding maps to a Signal with `producer = "slack_config_probe"`, `type = "<finding_code>"` (e.g. `"name_keyed_channel"`), and `signature = "slack_config_probe:<finding_code>:<bot_id>:<channel_id?>"` so the find-or-create pattern dedups.

Three findings warrant their own first-class Signal shape:

1. **`bot_invited_unwatched`** (severity WARN, flavor `maintenance`) — fires when SLK005 is true. Body: "team-bot-a was invited to #project-x-design (G0T79FGSE) but isn't in the policy. Should it listen?" Actions: `add_mention_only` / `add_listen_all` / `dismiss`. Routed to the bot's pod-operator audience via evo DM (per `project_per_bot_sysadmin_audience` framing).

2. **`oc_default_drift`** (severity WARN, flavor `activity`) — fires when SLK003 is true after an OC version bump. Body names the field and the new default. Routed to evo for awareness, not action — most drifts won't require operator action, but the operator should know.

3. **`silent_blackhole`** (severity ALERT, flavor `maintenance`) — fires when SLK010 is true. This is the "your bot can't hear anything" condition; pages through quiet hours if `paged_overrides` is set.

**Sweep-resolve.** At the end of every probe run, call `signals.store.sweep_resolve(producer="slack_config_probe", kept_signatures=...)` so conditions that have been fixed auto-archive without operator action.

---

## 7. Admin UI Slack tab + Evo bot Slack DM flows

Two surfaces, one policy file. Both required, for different moments.

### 7.1 Admin UI Slack tab

A new tab on the bot's config page in evolve admin. Required for bootstrap because the policy must be set before the bot is reachable via DM.

**Sections, top to bottom:**

1. **Workspace status** — `auth.test` result, current team name, last reconciled timestamp. Active drift Signals rendered inline (deep link to Alerts).
2. **Access** — radio for `dm_allowlist.mode`, plus appropriate picker:
   - `open` — no picker
   - `user_group` — Slack user-group autocomplete (resolves via `usergroups.list`)
   - `explicit` — Slack user picker (autocomplete via `users.list`, displays real names not IDs)
   - `channel_derived` — channel picker
3. **Self-enrollment codes** — list of active codes with consume status + revoke button; "+ Generate code" with optional TTL override (default 24h).
4. **Channels** — table of `channels.entries` with toggles for `require_mention` and `visible_replies` override; "+ Add channel" using Slack channel picker (shows channels bot is a member of, including private ones — per the bug 1 lesson, this comes from `users.conversations`, not `conversations.list`).
5. **New-channel default** — radio for `channels.default_for_new.behavior`.
6. **Messaging** — toggles for `thread_replies`, `mention_style`, quiet hours.

**Save flow.** Form submit → `POST /api/bots/<id>/slack-policy` → calls `run_doctor` against the target state → if any FAIL, return the findings and surface them inline (form does not save) → else write policy file, then render to openclaw.json via the writer.

### 7.2 Evo bot Slack DM admin flows

For ongoing adjustments. The admin DMs the pod's evo bot (per `project_evolve_bot_role`); the wizard's handler framework dispatches to a Slack-policy handler. Plugs into the existing `evo` subcommand pattern (`packages/admin/evolve_admin/evo/subcommands.py`).

**Supported intents** (LLM router maps natural language to these):

| Intent | Example utterance | Action |
|---|---|---|
| `slack.add_channel` | "add #project-x-design to team-bot-a" | Resolve channel name → ID via Slack API. Add to policy with `require_mention: true` default. Render. Confirm. |
| `slack.remove_channel` | "stop team-bot-a listening in #management" | Remove from policy. Render. Confirm. |
| `slack.set_mention` | "make team-bot-a listen to everything in #ops" | Set `require_mention: false` on that entry. Render. Confirm. |
| `slack.add_user` | "add @Dave to team-bot-a" | Resolve @-mention → user ID. Add to `dm_allowlist.user_ids` (or switch mode to explicit). Render. Confirm. |
| `slack.enroll_code` | "enrollment code for team-bot-a, valid 1 hour" | Generate code, return to admin in DM, log creation. |
| `slack.show_policy` | "show team-bot-a's slack policy" | Render human-readable summary; offer "edit in browser" link. |
| `slack.invite_response` | (button click on `bot_invited_unwatched` prompt) | Apply chosen action, archive Signal. |

**Self-enrollment consumption.** When a non-admin user DMs the bot with text that matches an active enrollment code, the bot:
1. Marks the code as consumed in `slack-policy.json` (audit-logged).
2. Adds the user's Slack ID to `access.dm_allowlist.user_ids` (switching mode to `explicit` if not already).
3. Triggers a render.
4. DMs the *admin* (the code's `created_by`) with: "Dave Smith (U02AB…) enrolled to team-bot-a using code 'racecar' at 15:30:00. Revoke this user? [Revoke]".
5. DMs the *new user* with the bot's normal greeting.

Step 4 is the critical safety affordance — even if the code leaks, the admin gets immediate notice and one-click revoke. Treats the code as a credential, not as authority.

---

## 8. Upstream OpenClaw issues to file

Per `feedback_dont_reimplement_upstream`, fix the silent-failure layer in OC where it belongs. Issues to file in priority order:

1. **Warn on name-keyed channel entries under `groupPolicy: "allowlist"`.** Highest priority — this is the bug 1 silent drop, affecting any user not specifically reading the docs. A startup-time warning in `/tmp/openclaw/openclaw-*.log` is a one-line PR.

2. **Support `channels.autoAddOnInvite`.** When the bot receives `member_joined_channel` with its own user ID, optionally add the channel to the allowlist with a configurable default `requireMention`. Removes the need for Evolve's `bot_invited_unwatched` Signal in the auto-add case.

3. **Document and announce default changes in release notes.** The 2026.4.27 `visibleReplies` flip had no migration note. Suggest a CHANGELOG section for default changes specifically.

4. **Startup self-report.** One human-readable log line per channel allowlist on gateway start. Per Tier 3 from the conversation — small but high ROI for diagnosis.

Evolve's implementation does not block on any of these; the probe and validator cover the same ground operationally. But upstream is the better long-term home.

---

## 9. Phasing

**Phase 1 — validator + probe (independent of policy layer).** No new schema; reads existing openclaw.json directly. Ships `slack-doctor` CLI, the 10 checks, and `slack_config_probe` writing Signals. Wires into `evolve-admin deploy <bot>` as a non-blocking pre-flight (logs findings, doesn't block). Ships the three upstream OC issues as filed. **Goal: catch the next team-bot-a-shaped failure before it ships.** ~1 week.

**Phase 2 — policy layer + writer + UI.** Introduces `slack-policy.json`, the writer, and the admin UI Slack tab. Migrates `slack-doctor` and `slack_config_probe` to validate against the policy (rather than reverse-engineering intent from openclaw.json). Pre-deploy gate becomes blocking on FAIL findings. ~1-2 weeks.

**Phase 3 — evo DM admin flows.** The wizard-routed Slack-policy handlers from §7.2. The `bot_invited_unwatched` interactive prompt. Self-enrollment code flows. ~1 week.

Each phase is shippable on its own. Phase 1 has the highest leverage and the smallest blast radius; ship it first, validate the model, then commit to Phase 2.

---

## 10. Out of scope

- **Cross-workspace bots.** Phase 1-3 assume the bot lives in exactly one Slack workspace. Multi-workspace is a future spec; self-enrollment codes are the closest thing to a cross-workspace primitive and may anchor that design.
- **Per-channel persona/role hints.** ("In #management, be brief.") Mentioned in the previous conversation; belongs in a separate spec since it touches the prompt-construction layer, not the routing layer.
- **Slack Connect / shared channels.** Treated as private channels for v1 (the channel ID is what matters); the policy distinction can be added later.
- **Telegram / Discord parity.** This spec is Slack-only. The pattern generalizes — `telegram-policy.json`, `discord-policy.json` — but each integration has its own quirks worth a dedicated spec. Phase 2's writer is structured so the same shape can apply to other integrations later.
- **Bot Slack permission scopes.** What scopes the bot's Slack app needs is a setup-time question handled by the install wizard, not the policy layer. The doctor verifies required scopes are present (`SLK007` is the entry point) but the install flow itself is unchanged.

---

## Appendix: mapping team-bot-a's recent failures to this spec

| What broke | What would have caught it |
|---|---|
| Name-keyed channels silently dropped (bug 1) | `slack-doctor SLK001` at pre-deploy; UI channel picker (no name-keying path) |
| `visibleReplies` default flipped (bug 2) | `slack_config_probe SLK003` after the OC version bump; explicit `messaging.visible_replies_default` in policy |
| Bot invited to channel, not listening | `slack_config_probe SLK005` → `bot_invited_unwatched` Signal → evo DM prompt |
| Diagnosis required tailing logs | Validator/probe findings surface in admin UI + Signal store, not just in logs |
| Adding a user required Slack ID lookup | UI Slack user picker (autocomplete); `slack.add_user` intent via evo DM with `@`-mention resolution; self-enrollment code as verbal-handoff fallback |
