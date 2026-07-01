# Alert Subscriptions — Design Spec

**Date:** 2026-05-10
**Status:** Draft, pending review
**Owner:** Evolve admin server
**Supersedes the operator-facing parts of:** [spec-alert-notifier-2026-05-09.md](spec-alert-notifier-2026-05-09.md). The earlier spec's Phase 1–4 (dispatcher + Phase 4 signal_notifier source) all stand and have shipped (PRs #919, #921, #922, #924, #926, #927, #929, #930, #932). This spec replaces Phases 5–7 of the earlier roadmap with a broader design that takes the operator's POV as primary.

---

## 1. Goal

Make the Evolve sysadmin's chat channel a **calm, accurate, configurable** notification stream. The operator decides what to receive and how often; the system never lies about severity ("Security CRITICAL" for a spend overage), and every operator-facing chat message in the codebase flows through one catalog the operator controls.

The operating principle, distilled from observed pain:
- **Accurate severity** — a usage notice is not `🔴 CRITICAL`. Reserve red/CRITICAL for things that need immediate operator response.
- **Operator-chosen subscription** — the sysadmin opts in to message types, the way they would in any modern notification preferences UI.
- **One catalog, one dispatcher** — every existing emitter either routes through the catalog or is retired. No silent paths.

---

## 2. Inventory of every operator-facing emitter today

Direct grep of the codebase as of 2026-05-10:

### 2.1 Already routed through the alert dispatcher (Phase 1–4 + Phase 3a–3f)

| Source | File | Migrated in |
|---|---|---|
| `audit` | `packages/analyzer/audit.py` | #924 |
| `spend_alert` | `packages/analyzer/spend_alert.py` (3 sites) | #926 |
| `pod_report` | `packages/analyzer/pod_report.py` | #927 |
| `cron_alert` | `packages/analyzer/cron_alert.py` | #929 |
| `forge_engine` | `packages/admin/evolve_admin/applications/forge_engine.py` | #932 |
| `signal_notifier` | `packages/admin/evolve_admin/alerts/signal_notifier.py` (Phase 4 source) | #922 |

### 2.2 NOT yet routed through the dispatcher (14 emitters across 11 files)

| # | File:line | Event today | Header text |
|---|---|---|---|
| 1 | `packages/admin/evolve_admin/repo_puller.py:1398` | repo-puller LaunchDaemon got wedged | `🚧 repo-puller wedged on {repo}` |
| 2 | `packages/analyzer/analyze.py:1061` | new proposals generated, awaiting review | `⚡ Evolve — N new proposals ready for review` |
| 3 | `packages/analyzer/apply.py:864` | proposal-applier executed (succeed/rollback) | `Apply Result: {action}` |
| 4 | `packages/analyzer/cost.py:347` | shared key rotation overdue | `⚠️ Evolve: Shared keys need sync` |
| 5 | `packages/analyzer/cost.py:405` | per-bot daily spend threshold breached | `⚠️ Evolve: Spend Alert` |
| 6 | `packages/analyzer/heal.py:1008` | unauthorized config drift detected | `🔴 CRITICAL: Unauthorized Config Drift Detected — {bot_id}` |
| 7 | `packages/analyzer/heal.py:1354` | gateway auto-restart failed | `🔴 Evolve: Gateway Restart Failed` |
| 8 | `packages/analyzer/heal.py:1376` | watchdog event (timeout / heartbeat) | watchdog detail |
| 9 | `packages/analyzer/outcome.py:151` | post-apply outcome checkin ("Did this help?") | "Change applied: …" |
| 10 | `packages/analyzer/report.py:138` | generic analyzer report | varies |
| 11 | `packages/analyzer/review.py:427` | proposal rejected by reviewer | `Apply Result: REJECTED` |
| 12 | `packages/analyzer/review.py:462` | proposal reviewed/approved | review summary |
| 13 | `packages/analyzer/review.py:488` | proposal quarantined (security-screened) | quarantine reason |
| 14 | `packages/analyzer/test_runner.py:778` | weekly regression initial failures | `N/M tests failed` |
| 15 | `packages/analyzer/validate.py:523` | manifest validation failed | validation detail |
| 16 | `packages/analyzer/weekly_review.py:770` | weekly RSI process-health report | summary |

### 2.3 Intentional bypasses (kept as-is)

- `packages/analyzer/audit.py:117` `_send_telegram_direct` — dedicated security-token resilience path, fires direct via Telegram Bot API even if dispatcher / OC gateway is broken. Operators mute by removing keystore files. Documented in code; remains out-of-catalog by design.

### 2.4 Test endpoints (not production sources)

- `packages/admin/evolve_admin/web/server.py:5104` `api_security_alert_channel_test` — admin-UI "Send a test alert" button.
- `packages/admin/evolve_admin/wizard.py:479` and `setup_wizard.py:1531` — `_send_telegram_test` setup-time channel verification.

### 2.5 Event types the operator asked about that have no emitter today

- **OpenClaw new version available** — no emitter exists. Need new producer that polls npm or GitHub releases for the openclaw package and emits when a newer version than installed is published.
- **Evolve repo updates landed on origin/main** — adjacent to repo_puller's wedge alert; could surface "since you last looked, N new commits landed."
- **Plugin / dep updates available** — same shape as OpenClaw updates but per-bot for plugin packages.

### 2.6 Overlaps and duplicates surfaced by the inventory

| Overlap | Result for operator today |
|---|---|
| `audit` cost-category vs `spend_alert` (Phase 5a in earlier spec) | Two messages for the same spend event — `🔴 CRITICAL: bot daily spend $X exceeds threshold $Y` *and* `⚠️ Evolve Spend Alert\nbot spent $X today...` |
| `cost.py:405` vs `spend_alert.send_alert` | **Triple-emit possible** — three separate code paths for the same daily-spend-overage event, all of which fire if config aligns. Discovered during this audit. |
| `heal.py:1008` (config drift, labeled CRITICAL) vs `audit` (security category) | Both surface security-category findings via different paths |
| `heal.py:1354/1376` (gateway restart failed / watchdog) vs `signal_notifier` (gateway down/up) | `signal_notifier` reports the *transition*; `heal` reports auto-restart's *failure to recover*. Distinct events but operator sees both for a single outage |
| `review.py:462` (reviewed/approved) vs `apply.py:864` (apply result) | Different stages of the same proposal lifecycle, but both pushed to chat — likely overcommunicated |

---

## 3. Catalog: every event the operator can subscribe to

Each catalog entry is **one operator-relevant event type**, regardless of how many code paths produce it. Sources route through the dispatcher with a `catalog_event` key (new field, see § 7); the dispatcher consults the operator's subscription preferences before sending.

The catalog is the **single source of truth** for what events exist.

### Categories and event types

#### 🛡️ Security
| Event key | Fires when | Default | Allowed frequencies | Producers (today → after migration) |
|---|---|---|---|---|
| `security.audit_finding` | New finding from audit's security scans | **on**, immediate | immediate \| once-per-day max \| daily digest \| off | `audit` (security category only after Phase 5a) |
| `security.config_drift` | Bot's openclaw.json or AGENTS.md drifted from expected state | **on**, immediate | immediate \| off | `heal:1008` migrated to dispatcher |
| `security.proposal_quarantined` | Reviewer security-screen rejected a proposal | **on**, immediate | immediate \| off | `review:488` migrated |
| `security.key_rotation_overdue` | Per-bot API key hasn't rotated in N days | **on**, weekly digest | immediate \| weekly digest \| off | `cost:347` migrated |

#### 💰 Cost
| Event key | Fires when | Default | Allowed frequencies | Producers |
|---|---|---|---|---|
| `cost.daily_threshold` | Per-bot daily spend exceeded threshold | **on**, once per bot per day | once per day \| daily digest \| off | `spend_alert.send_alert` (canonical), `cost:405` retired, `audit` cost-category retired (Phase 5a) |
| `cost.hard_cap_hit` | Per-bot hard cap hit; enforcement action taken | **on**, immediate | immediate \| off | `spend_alert._send_cap_alert` |
| `cost.weekly_summary` | Weekly spend rollup | **on**, Mondays | weekly \| off | `spend_alert._maybe_send_weekly_summary` |

#### 🟢 System health
| Event key | Fires when | Default | Allowed frequencies | Producers |
|---|---|---|---|---|
| `system.gateway_state_change` | Gateway transitions firing → resolved or vice versa | **on**, immediate | immediate \| off | `signal_notifier` (Phase 4) |
| `system.gateway_autorestart_failed` | heal.py couldn't restart a gateway after N attempts | **on**, immediate | immediate \| off | `heal:1354` migrated |
| `system.watchdog_event` | host_health / watchdog excursion | **on**, immediate | immediate \| once-per-day max \| off | `heal:1376` migrated, `signal_notifier` for Signal-store transitions |
| `system.daemon_error_spike` | error_reporter Signal-store firing transitions | **on** (via Phase 4 signal_notifier), immediate | immediate \| off | `signal_notifier` |
| `system.repo_puller_wedged` | repo-puller LaunchDaemon stuck >24h | **on**, immediate | immediate \| off | `repo_puller:1398` migrated |
| `system.stalled_cron` | Watched cron silent past threshold | **on**, once per day | once per day \| daily digest \| off | `cron_alert` (Phase 3d) |
| `system.test_runner_failures` | Weekly regression suite has failures | **on**, weekly digest | weekly \| off | `test_runner:778` migrated |
| `system.manifest_validation_failed` | Manifest validation produced errors | **on**, immediate | immediate \| daily digest \| off | `validate:523` migrated |

#### 🔄 Updates *(new category — no emitter today)*
| Event key | Fires when | Default | Allowed frequencies | Producers (new) |
|---|---|---|---|---|
| `updates.openclaw_available` | Newer openclaw npm version published | **on**, immediate | immediate \| weekly digest \| off | new daily cron `update_watcher.py` |
| `updates.evolve_repo` | New commits on origin/main since last notification | **off** (opt-in; many will find this noisy) | weekly digest \| off | new |
| `updates.plugin_available` | Per-bot plugin update available | **off** (opt-in) | weekly digest \| off | new |

#### 📋 Decisions needed
| Event key | Fires when | Default | Allowed frequencies | Producers |
|---|---|---|---|---|
| `decisions.proposal_ready` | RSI generator emitted a proposal awaiting approval | **on**, immediate | immediate \| daily digest \| off | `analyze:1061` migrated |
| `decisions.proposal_applied` | Proposal applier ran (success/rollback) | **off** (opt-in; visible in admin UI) | immediate \| daily digest \| off | `apply:864` migrated |
| `decisions.proposal_rejected` | Reviewer rejected a proposal | **off** (opt-in) | immediate \| daily digest \| off | `review:427` migrated |
| `decisions.proposal_outcome_checkin` | Post-apply "did this help?" prompt | **on**, immediate | immediate \| off | `outcome:151` migrated |
| `decisions.forge_job_ready` | Forge build is ready for review | **on**, immediate | immediate \| daily digest \| off | `forge_engine` (Phase 3f) |

#### 📊 Summaries
| Event key | Fires when | Default | Allowed frequencies | Producers |
|---|---|---|---|---|
| `summaries.daily_pod_report` | Daily pod-status digest | **on**, daily | daily \| off | `pod_report` (Phase 3c) |
| `summaries.weekly_rsi_review` | Weekly RSI process-health report | **on**, weekly | weekly \| off | `weekly_review:770` migrated |

#### 🔧 Reports *(catch-all for `report.py:138`)*
| Event key | Fires when | Default | Allowed frequencies | Producers |
|---|---|---|---|---|
| `reports.adhoc_analyzer` | `analyzer/report.py` operator-invoked | **on**, immediate | immediate \| off | `report:138` migrated |

**Total: 24 distinct operator-relevant event types across 7 categories.**

---

## 4. Message structure — Team-Bot-A-style template

Every event renders to a body that follows this shape:

```
{emoji} {category-line: short, accurate, no "CRITICAL" unless it actually is}
{one fact per line, max 4 lines}

{conversational close-out: action offered or next step}
```

### Severity → emoji mapping

| Severity | Emoji | When |
|---|---|---|
| `critical` | 🚨 | Pod-wide outage, suspected breach, hard cap hit |
| `error` | 🔴 | Single-bot down, security finding, restart failed |
| `warning` | 🟡 | Threshold breaches, validation failures, drift |
| `info` | 🔵 / category-specific | Routine updates, summaries, available actions |
| recovery | 🟢 | Previously-firing condition cleared |
| update available | 🔄 | New version, new commit, new artifact |
| decision needed | 📋 | Operator action requested |
| summary | 📊 | Periodic digest |
| security | 🛡️ | Security-flavored body — replaces 🔴 in Security category for non-critical |

### Bad → good rewrites

```diff
- 🔴 *Evolve Security Audit — CRITICAL Findings*
- • 🔴 CRITICAL: admin-bot daily spend $5.96 exceeds threshold $5.00
- • 🔴 CRITICAL: security-bot daily spend $10.21 exceeds threshold $5.00
+ 💰 Daily spend over threshold
+ admin-bot: $5.96   security-bot: $10.21
+ Threshold: $5.00 each
+ Open the Cost page in admin UI for the breakdown.

- 🔴 *Evolve Security Audit — CRITICAL Findings*
- • 🔴 CRITICAL: team-bot-a (gateway.loopback_no_auth): Gateway auth missing on loopback — Fix: Set gateway.auth (token recommended) or keep the Control UI local-only.
+ 🛡️ New security finding
+ team-bot-a: gateway loopback auth missing
+ Fix: set gateway.auth in openclaw.json (token recommended).
+ Reply 'fix team-bot-a auth' if you'd like me to prep the change.

- 🚧 repo-puller wedged on /Users/Shared/evolve-repo
- Last successful pull: 2026-05-04T03:15:42Z
- Wedge reason: untracked working tree files would be overwritten
+ 🚧 repo-puller stuck on evolve-repo
+ Last successful pull: 6d ago
+ Reason: untracked file conflict
+ I tried the auto-quarantine; manual cleanup may be needed.

- ⚡ Evolve — 3 new proposals ready for review
+ 📋 3 new proposals ready
+ admin-bot: trim AGENTS.md size  ·  team-bot-a: lower context cap  ·  team-bot-b: enable test gate
+ Open the Proposals page to review.
```

### Composition rules

- **Header line is always one line, ≤60 chars.** No nested asterisks/markdown stacking.
- **Body lines are ≤80 chars** each, max 4 lines (digest mode is the exception).
- **No `*Section Headers*` mid-message** — the structure is flat.
- **Close-out is the actionability line** (next subsection) — present iff the event has a known straightforward fix; absent for purely informational events.

### 4.1 Actionability — make it easy to act

Every catalog entry declares an **action offer** (or `None` for informational events). The renderer appends one line after the body, picked from one of four modes:

| Mode | Format | When to use |
|---|---|---|
| `bot` | `Reply '{verb}' and I'll take care of it.` | The Evolve bot has a handler that can execute the fix. Fastest path for the operator — one reply, done. |
| `ui` | `Open admin UI → {breadcrumb}.` | The fix needs operator judgment in the admin UI. Provide the breadcrumb (which page + section) so they don't hunt. |
| `cli` | `Run:` followed by ` ` ``` ``{command}`` ``` ` ` on a new line. | The fix is a known terminal command. Provide it copy-paste-ready. |
| `None` | (no line emitted) | Purely informational events (summaries, recoveries) — don't manufacture an action. |

**Rule:** if multiple modes apply to a single event, prefer `bot` > `cli` > `ui` (operator effort, low → high). Only the highest-priority available mode renders.

Sample mapping for the catalog:

| Event | Mode | Action line |
|---|---|---|
| `security.audit_finding` (`gateway.loopback_no_auth` w/ handler) | bot | `Reply 'fix team-bot-a auth' and I'll take care of it.` |
| `security.audit_finding` (no handler) | cli or none | `Run:` ` ``sudo evolve-admin verify --bot team-bot-a`` ` |
| `security.config_drift` | cli | `Run:` ` ``sudo evolve-admin verify --bot team-bot-a`` ` |
| `security.proposal_quarantined` | ui | `Open admin UI → Proposals → Quarantined.` |
| `security.key_rotation_overdue` | cli | `Run:` ` ``sudo evolve-admin keys sync --all`` ` |
| `cost.daily_threshold` | ui | `Open admin UI → Cost for the breakdown.` |
| `cost.hard_cap_hit` | ui | `Open admin UI → Cost → Spending Caps to clear.` |
| `cost.weekly_summary` | none | (informational) |
| `system.gateway_state_change` (down) | bot or cli | `Reply 'restart team-bot-a' and I'll kick it.` / `Run:` ` ``sudo launchctl kickstart -k system/ai.openclaw.team-bot-a-gateway`` ` |
| `system.gateway_state_change` (up) | none | (recovery) |
| `system.gateway_autorestart_failed` | cli | `Run:` ` ``sudo launchctl kickstart -k system/ai.openclaw.team-bot-a-gateway`` ` |
| `system.watchdog_event` | depends on event subtype | varies; default `ui` to Alerts page |
| `system.repo_puller_wedged` | cli | `Run:` ` ``ssh mini sudo launchctl kickstart -k system/ai.evolve.evolve.repo-puller`` ` |
| `system.stalled_cron` | cli | `Run:` ` ``sudo launchctl kickstart -k system/{job_label}`` ` |
| `system.test_runner_failures` | ui | `Open admin UI → Maintenance → Tests.` |
| `system.manifest_validation_failed` | ui | `Open admin UI → Apps → Manifest issues.` |
| `updates.openclaw_available` | bot | `Reply 'update openclaw' and I'll run it.` |
| `updates.evolve_repo` | none | (informational; opt-in, low-urgency) |
| `decisions.proposal_ready` | ui | `Open admin UI → Proposals → Pending.` |
| `decisions.proposal_applied` | ui | `Open admin UI → Proposals → Recent.` |
| `decisions.proposal_rejected` | ui | `Open admin UI → Proposals → Rejected.` |
| `decisions.proposal_outcome_checkin` | bot | `Reply 'yes' / 'no' / 'details' to record the outcome.` |
| `decisions.forge_job_ready` | ui | `Open admin UI → Apps → Forge → {app_id}.` |
| `summaries.daily_pod_report` | none | (informational) |
| `summaries.weekly_rsi_review` | none | (informational) |
| `reports.adhoc_analyzer` | none | (informational; varies) |

### 4.2 Bot-mode handler availability

`bot` mode is only valid for an event if the Evolve bot has a handler that can actually execute the fix. The catalog entry's `action_offer.bot_command` is a contract: when the operator replies with that verb, the bot must do the thing.

**Today, very few of these handlers exist.** The catalog ships with most events using `cli` or `ui` mode. Each `bot`-mode event requires a corresponding handler in the Evolve bot's session (a separate track, see §11). As handlers land, the catalog entry flips from `cli`/`ui` to `bot`. Migration is per-event; the dispatcher and operator UI don't need to change.

### 4.3 Bad → good rewrites (with action offers)

```diff
- 🔴 *Evolve Security Audit — CRITICAL Findings*
- • 🔴 CRITICAL: admin-bot daily spend $5.96 exceeds threshold $5.00
- • 🔴 CRITICAL: security-bot daily spend $10.21 exceeds threshold $5.00
+ 💰 Daily spend over threshold
+ admin-bot: $5.96   security-bot: $10.21
+ Threshold: $5.00 each
+ Open admin UI → Cost for the breakdown.

- 🔴 *Evolve Security Audit — CRITICAL Findings*
- • 🔴 CRITICAL: team-bot-a (gateway.loopback_no_auth): Gateway auth missing on loopback — Fix: Set gateway.auth (token recommended) or keep the Control UI local-only.
+ 🛡️ New security finding
+ team-bot-a: gateway loopback auth missing
+ Run: `sudo evolve-admin verify --bot team-bot-a`

- 🚧 repo-puller wedged on /Users/Shared/evolve-repo
- Last successful pull: 2026-05-04T03:15:42Z
- Wedge reason: untracked working tree files would be overwritten
+ 🚧 repo-puller stuck on evolve-repo
+ Last successful pull: 6d ago
+ Reason: untracked file conflict (auto-quarantine attempted)
+ Run: `ssh mini sudo launchctl kickstart -k system/ai.evolve.evolve.repo-puller`

- ⚡ Evolve — 3 new proposals ready for review
+ 📋 3 new proposals ready
+ admin-bot: trim AGENTS.md size  ·  team-bot-a: lower context cap  ·  team-bot-b: enable test gate
+ Open admin UI → Proposals → Pending.
```

Notice the difference from the earlier draft: the close-out is **explicit and copy-pasteable** instead of a vague "next step." If the operator just wants to act, they can — without leaving the chat.

---

## 5. UI: Alerts page → Subscriptions tab

### Information architecture

```
Alerts (page)
├── Active     (existing — firing Signals)
├── History    (existing — archived Signals)
└── Subscriptions   ← NEW
```

### Subscriptions tab layout (sketch)

```
┌─ Subscriptions ─────────────────────────────────────────────────────┐
│                                                                       │
│  Sending to: telegram (Pod-Admin @ chat 12345)        [⚙ change channel]│
│  Last delivery: 2 minutes ago                     [📋 view audit log]│
│                                                                       │
│  [Master switch]  ●━━ All notifications ON                            │
│                                                                       │
│  ── 🛡️ Security ─────────────────────────────────────────────────── │
│  New audit finding              [✓] On   [Immediate ▾]      [Test]  │
│  Config drift detected          [✓] On   [Immediate ▾]      [Test]  │
│  Proposal quarantined           [✓] On   [Immediate ▾]      [Test]  │
│  Key rotation overdue           [✓] On   [Weekly digest ▾]  [Test]  │
│                                                                       │
│  ── 💰 Cost ───────────────────────────────────────────────────────  │
│  Daily threshold breach         [✓] On   [Once per day ▾]   [Test]  │
│  Hard cap hit                   [✓] On   [Immediate ▾]      [Test]  │
│  Weekly spend summary           [✓] On   [Mondays 8am ▾]    [Test]  │
│                                                                       │
│  ── 🟢 System health ──────────────────────────────────────────────  │
│  Gateway state change           [✓] On   [Immediate ▾]      [Test]  │
│  Gateway autorestart failed     [✓] On   [Immediate ▾]      [Test]  │
│  Watchdog excursion             [✓] On   [Once per day ▾]   [Test]  │
│  Daemon error spike             [✓] On   [Immediate ▾]      [Test]  │
│  repo-puller wedged             [✓] On   [Immediate ▾]      [Test]  │
│  Stalled cron                   [✓] On   [Once per day ▾]   [Test]  │
│  Weekly test failures           [✓] On   [Weekly digest ▾]  [Test]  │
│  Manifest validation failed     [✓] On   [Daily digest ▾]   [Test]  │
│                                                                       │
│  ── 🔄 Updates ────────────────────────────────────────────────────  │
│  OpenClaw new version           [✓] On   [Immediate ▾]      [Test]  │
│  Evolve repo updated            [ ] Off                              │
│  Plugin update available        [ ] Off                              │
│                                                                       │
│  ── 📋 Decisions needed ───────────────────────────────────────────  │
│  Proposal ready for review      [✓] On   [Immediate ▾]      [Test]  │
│  Proposal applied               [ ] Off  (visible in admin UI)       │
│  Proposal rejected              [ ] Off  (visible in admin UI)       │
│  Post-apply outcome checkin     [✓] On   [Immediate ▾]      [Test]  │
│  Forge job ready                [✓] On   [Immediate ▾]      [Test]  │
│                                                                       │
│  ── 📊 Summaries ──────────────────────────────────────────────────  │
│  Daily pod report               [✓] On   [Daily 8am ▾]      [Test]  │
│  Weekly RSI review              [✓] On   [Sundays ▾]        [Test]  │
│                                                                       │
│  ── 🔧 Reports ────────────────────────────────────────────────────  │
│  Ad-hoc analyzer report         [✓] On   [Immediate ▾]      [Test]  │
│                                                                       │
│  Bottom links:                                                       │
│  [Reset all to defaults]  [Export prefs]  [View audit log]          │
└──────────────────────────────────────────────────────────────────────┘
```

### Row anatomy

- **Label** — human-readable event name, no jargon (no `signature`, no `producer`).
- **Toggle** — operator-friendly on/off.
- **Frequency dropdown** — only options valid for this event type (computed from catalog metadata).
- **Test button** — fires a sample message immediately. Renders the same template the live event would produce.

### Frequency options (per-event vary)

| Option | Meaning |
|---|---|
| `immediate` | Push as soon as the event fires (subject to dispatcher cooldown) |
| `once_per_day_max` | First fire on a given UTC day pushes immediately, subsequent same-key fires suppressed |
| `once_per_week_max` | First fire of a given ISO week pushes immediately |
| `daily_digest` | Aggregated and pushed once per day at the operator's chosen hour |
| `weekly_digest` | Aggregated and pushed once per week |
| `<scheduled>` | For events that have a natural cadence (e.g. `summaries.daily_pod_report` → "Daily 8am") |
| `off` | Muted; events still write to Alerts page, just no chat push |

### Defaults shown in the UI come from the catalog

The catalog is the source of truth for which frequencies are *valid* per event. The UI dropdown only renders allowed options. This prevents nonsensical setups (e.g. "Immediate" for a weekly summary).

### Config-page banner

The existing Config-page knobs (`alerts.<source>.enabled`, `alerts.<source>.cooldown_seconds`) get a banner above them:

> **For everyday notification preferences, use the Alerts page → Subscriptions tab.** The settings here are low-level dispatcher controls that subscriptions build on.

The Config-page knobs **stay** — they're useful for power-user / debug muting and operate one layer below the catalog (subscriptions resolve to source-level toggles internally; if the source is also Config-disabled, neither layer fires). Leaving both lets operators kill an entire source with one click during incident response without touching every event.

---

## 6. Data model

### 6.1 Event catalog (compiled-in)

New module `packages/admin/evolve_admin/alerts/catalog.py`:

```python
@dataclass(frozen=True)
class ActionOffer:
    mode: Literal["bot", "ui", "cli"]
    text_template: str          # Rendered into the action line; supports {placeholders}.
    # Mode-specific fields (only one is used per entry):
    bot_command: str | None = None   # e.g. "fix team-bot-a auth" — must match a registered Evolve bot handler.
    ui_breadcrumb: str | None = None # e.g. "Cost" or "Proposals → Pending"
    cli_command: str | None = None   # e.g. "sudo evolve-admin keys sync --all"


@dataclass(frozen=True)
class CatalogEvent:
    key: str                           # "cost.daily_threshold"
    category: Category                 # Category.COST
    label: str                         # "Daily threshold breach"
    description: str                   # "Per-bot daily spend exceeded threshold."
    default_enabled: bool
    default_frequency: Frequency
    allowed_frequencies: tuple[Frequency, ...]
    severity: Severity                 # used by the renderer to pick emoji
    producer_source: str               # "spend_alert" — for routing back to dispatcher source
    body_template: str                 # The header + body lines; see §4
    action: ActionOffer | None = None  # Conditional close-out; None = informational
    is_safety_critical: bool = False   # If True, UI shows a warning before muting

CATALOG: tuple[CatalogEvent, ...] = (
    CatalogEvent(
        key="security.audit_finding",
        category=Category.SECURITY,
        label="New audit finding",
        description="Audit detected a new security-relevant finding.",
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=(
            Frequency.IMMEDIATE,
            Frequency.ONCE_PER_DAY_MAX,
            Frequency.DAILY_DIGEST,
            Frequency.OFF,
        ),
        severity=Severity.ERROR,
        producer_source="audit",
        body_template=(
            "🛡️ New security finding\n"
            "{bot_id}: {check_title}"
        ),
        action=ActionOffer(
            mode="cli",
            text_template="Run: `{cli_command}`",
            cli_command="sudo evolve-admin verify --bot {bot_id}",
        ),
        is_safety_critical=True,   # warn operator before muting security
    ),
    CatalogEvent(
        key="cost.daily_threshold",
        category=Category.COST,
        label="Daily threshold breach",
        description="Per-bot daily spend exceeded threshold.",
        default_enabled=True,
        default_frequency=Frequency.ONCE_PER_DAY_MAX,
        allowed_frequencies=(
            Frequency.ONCE_PER_DAY_MAX,
            Frequency.DAILY_DIGEST,
            Frequency.OFF,
        ),
        severity=Severity.WARNING,
        producer_source="spend_alert",
        body_template=(
            "💰 Daily spend over threshold\n"
            "{bot_id}: ${amount:.2f}\n"
            "Threshold: ${threshold:.2f}"
        ),
        action=ActionOffer(
            mode="ui",
            text_template="Open admin UI → {ui_breadcrumb}.",
            ui_breadcrumb="Cost",
        ),
    ),
    CatalogEvent(
        key="updates.openclaw_available",
        category=Category.UPDATES,
        label="OpenClaw new version available",
        description="A newer openclaw release is published.",
        default_enabled=True,
        default_frequency=Frequency.IMMEDIATE,
        allowed_frequencies=(
            Frequency.IMMEDIATE,
            Frequency.WEEKLY_DIGEST,
            Frequency.OFF,
        ),
        severity=Severity.INFO,
        producer_source="update_watcher",
        body_template=(
            "🔄 OpenClaw update available\n"
            "New: {new_version}\n"
            "Current: {current_version}"
        ),
        action=ActionOffer(
            mode="bot",
            text_template="Reply '{bot_command}' and I'll take care of it.",
            bot_command="update openclaw",
        ),
    ),
    CatalogEvent(
        key="cost.weekly_summary",
        category=Category.COST,
        label="Weekly spend summary",
        description="Pod-wide rollup of last week's spend.",
        default_enabled=True,
        default_frequency=Frequency.WEEKLY,
        allowed_frequencies=(Frequency.WEEKLY, Frequency.OFF),
        severity=Severity.INFO,
        producer_source="spend_alert",
        body_template=(
            "📊 Weekly spend ({iso_week})\n"
            "Total: ${total:.2f}\n"
            "{per_bot_breakdown}"
        ),
        action=None,   # informational — no action line
    ),
    # ... 21 more entries
)
```

The dispatcher's renderer is a small function: given an event key + a payload dict, look up the catalog entry, render `body_template`, then conditionally append the rendered action line. ~30 lines.

### 6.2 Operator preferences (on disk)

`{shared_dir}/alerts/subscriptions.json`:

```json
{
  "version": 1,
  "subscriptions": {
    "security.audit_finding":      {"enabled": true,  "frequency": "immediate"},
    "cost.daily_threshold":        {"enabled": true,  "frequency": "once_per_day_max"},
    "decisions.proposal_applied":  {"enabled": false},
    "updates.evolve_repo":         {"enabled": false}
  },
  "channel_override": null,
  "digest_hour_local": 8,
  "updated_at": "2026-05-10T14:32:00Z"
}
```

- Atomic temp+rename writes (same pattern as dispatcher-state.json).
- Missing entries fall back to catalog defaults — sparse storage, only operator overrides land in the file.
- Read by the dispatcher on every send (cached for ~1s to avoid stat storms).

### 6.3 Digest queues (when `frequency=daily_digest` etc.)

`{shared_dir}/alerts/digest-pending/<frequency>.jsonl`:
- One line per deferred event: `{ts, event_key, severity, message_excerpt, full_payload}`
- Flushed by a new daemon `digest_dispatcher.py` on the operator's chosen schedule (`digest_hour_local`)
- Renders a single batched message per category, then archives the JSONL

---

## 7. Architecture: catalog + dispatcher

### 7.1 New send path

```
producer
  └─> dispatcher.send(
          source="<existing-source>",
          message="<rendered text>",     # source-rendered today; catalog-rendered post-§9
          severity=…,
          dedup_key=…,
          catalog_event="cost.daily_threshold",   # NEW required for catalog routing
      )
       └─> catalog.lookup(catalog_event)
            ├── if subscription.enabled == False → SUPPRESSED_DISABLED
            ├── if frequency == daily_digest    → enqueue + return DEFERRED
            ├── apply per-event cooldown (frequency-driven)
            ├── existing source-level enable + cooldown checks (Config-page knobs)
            └── if all gates pass → openclaw subprocess → SENT
```

`catalog_event` becomes the canonical routing key. Every dispatcher.send call after migration includes one. Lookup is O(1) via the catalog's pre-built dict.

### 7.2 Backwards compatibility

- Sources that haven't yet been migrated to pass `catalog_event` continue to work — the dispatcher treats `catalog_event=None` as "use the source-level toggle, skip subscription gating." Migration of the 14 outstanding emitters in § 2.2 happens module-by-module post-spec.
- The seven already-migrated emitters from Phase 3 keep working unchanged; § 9 adds `catalog_event` to each as a one-line edit per source.

### 7.3 Frequency mode → cooldown mapping

| Frequency | Implementation |
|---|---|
| `immediate` | dispatcher cooldown only (existing) |
| `once_per_day_max` | force `cooldown_seconds=86400` on send, dedup_key per `<event_key>/<bot>/<date>` |
| `once_per_week_max` | force `cooldown_seconds=604800` on send |
| `daily_digest` | append to `digest-pending/daily.jsonl`; daily flush daemon renders + dispatches |
| `weekly_digest` | append to `digest-pending/weekly.jsonl`; weekly flush daemon |
| `<scheduled>` | source decides natural cadence; catalog gate applies enabled-only |
| `off` | dispatcher returns SUPPRESSED_DISABLED, but Signal still lands in Alerts page |

---

## 8. Routes

### 8.1 New API routes

| Route | Method | Purpose |
|---|---|---|
| `/api/alerts/subscriptions` | GET | Returns the merged catalog (defaults + operator overrides). UI renders from this. |
| `/api/alerts/subscriptions` | POST | Updates operator overrides for one or more events. Atomic write. |
| `/api/alerts/subscriptions/test` | POST `{event_key}` | Fires a sample message for the event_key using its template + sample data. |
| `/api/alerts/subscriptions/reset` | POST | Wipes the operator overrides file; everything reverts to catalog defaults. |

### 8.2 Existing route changes

- `/api/signals/refresh` (Phase 4 endpoint) unchanged.
- Config-page existing GET/POST `/api/config` unchanged (operates on the source-level dispatcher toggles).

---

## 9. Migration plan

### Phase A — catalog + glue (no operator-visible change)

| | What | Estimate |
|---|---|---|
| A1 | Catalog module + 25 entries + tests | 1 PR |
| A2 | Dispatcher accepts `catalog_event` kwarg, applies subscription gating | 1 PR |
| A3 | `subscriptions.json` reader/writer + atomic semantics | 1 PR |
| A4 | API routes `/api/alerts/subscriptions{,_test,_reset}` | 1 PR |

### Phase B — UI

| | What | Estimate |
|---|---|---|
| B1 | Subscriptions tab on Alerts page (renders from catalog endpoint) | 1 PR |
| B2 | Frequency dropdown wiring + Test button | 1 PR |
| B3 | Config-page banner pointing to Subscriptions | 1 PR |

### Phase C — migrate the 14 outstanding emitters

Each is a ~5-line dispatcher.send replacement plus catalog_event mapping. Parallelizable. PRs:
| | Source | catalog_event |
|---|---|---|
| C1 | `repo_puller:1398` | `system.repo_puller_wedged` |
| C2 | `analyze:1061` | `decisions.proposal_ready` |
| C3 | `apply:864` | `decisions.proposal_applied` |
| C4 | `cost:347` | `security.key_rotation_overdue` |
| C5 | `cost:405` | **retire** (overlap with `spend_alert`) |
| C6 | `heal:1008` | `security.config_drift` |
| C7 | `heal:1354` | `system.gateway_autorestart_failed` |
| C8 | `heal:1376` | `system.watchdog_event` |
| C9 | `outcome:151` | `decisions.proposal_outcome_checkin` |
| C10 | `report:138` | `reports.adhoc_analyzer` |
| C11 | `review:427` | `decisions.proposal_rejected` |
| C12 | `review:462` | (fold into `decisions.proposal_applied` semantics — verify) |
| C13 | `review:488` | `security.proposal_quarantined` |
| C14 | `test_runner:778` | `system.test_runner_failures` |
| C15 | `validate:523` | `system.manifest_validation_failed` |
| C16 | `weekly_review:770` | `summaries.weekly_rsi_review` |

### Phase D — already-migrated emitters get `catalog_event` annotations

| | Source | catalog_event |
|---|---|---|
| D1 | `audit` | `security.audit_finding` (post-cost-removal) |
| D2 | `spend_alert.send_alert` | `cost.daily_threshold` |
| D3 | `spend_alert._send_cap_alert` | `cost.hard_cap_hit` |
| D4 | `spend_alert._maybe_send_weekly_summary` | `cost.weekly_summary` |
| D5 | `pod_report` | `summaries.daily_pod_report` |
| D6 | `cron_alert` | `system.stalled_cron` |
| D7 | `forge_engine` | `decisions.forge_job_ready` |
| D8 | `signal_notifier` | dynamic per Signal — see § 7.3 below |

For `signal_notifier`, the `catalog_event` is computed from the Signal's `producer` + `type` (e.g. `pod_health/pod_health_gateways/*` → `system.gateway_state_change`).

### Phase E — new producers

| | What |
|---|---|
| E1 | Remove cost category from `audit` (Phase 5a from earlier spec, now part of this) |
| E2 | New `update_watcher.py` daily cron — emits `updates.openclaw_available` |
| E3 | (Optional) `updates.evolve_repo` source — repo-puller already knows when origin/main moved |
| E4 | (Optional) `updates.plugin_available` source |

### Phase F — message-template refresh (Team-Bot-A-style)

Each catalog entry's template gets the Team-Bot-A-style rewrite. Sources keep producing the structured fields they always did; the dispatcher's renderer applies the template. Concretely: sources move from message=str templates to message=dict payloads, and the catalog renders. This is the largest scope change of the spec.

### Phase G — digest mode

Implement digest queues + flush daemon for events where the operator has chosen `daily_digest` / `weekly_digest`. Conditional — only if at least one event is set to digest in any install.

### Phase H — defaults review + flip `signal_notifier` on

After ≥1 week of Phase A–F running, review the operator-feedback loop and flip `signal_notifier`'s default to on (was Phase 7 in earlier spec; carried forward).

---

## 10. Open questions for review

1. **Master switch placement.** Subscriptions tab has a top-level master switch (kills all chat pushes regardless of per-event toggles). The Config page already has `alerts.dispatcher_enabled`. Are these the same thing or do we want both layers? My take: keep both — Subscriptions master is the operator's daily-driver switch; dispatcher_enabled is the infrastructure kill switch (e.g. during an incident where chat itself is the wrong place to be loud).
2. **Digest hour timezone.** `digest_hour_local: 8` — local relative to what? `network.timezone` already exists. Read from there, fall back to UTC.
3. **Digest delivery format.** A daily digest of 12 events: one bundled message, or one per category, or one per event? Recommend: one bundled message per category, per fire ("📋 Today's decisions: 3 proposals ready / 1 forge job awaiting review / …"). Worth testing with a real day's worth of events.
4. **Test button realism.** The Test button needs sample data. Should it fire with synthetic placeholder data (`{bot_id} → "team-bot-a"`), or pick a real recent event from the producer's history? Synthetic is simpler; real makes the test message richer and more confidence-building.
5. **Per-bot scoping.** Currently subscriptions are pod-wide. Worth a per-bot override? E.g. "I want gateway-down alerts for team-bot-a but not for team-bot-b." Probably premature; defer until someone asks.
6. **Mute-with-warning for safety-critical events.** `is_safety_critical` flag exists for security.* events. UI shows a confirmation modal before muting? My take: yes, with a "I understand I won't be notified about new security findings" checkbox.
7. **Catalog event keys for already-migrated sources** — the mapping in §9 D is mostly 1:1, but `review:462` is unclear (overlaps with `apply:864`). Audit the proposal-lifecycle pushes more carefully during C12 migration.

---

## 11. What this isn't tracking (out of scope)

- **Per-bot member channels.** Member-bot users (Slack/Discord/Telegram) get messages from individual bots like team-bot-a/team-bot-b/admin-bot — those are the bots' own user-facing communication and have nothing to do with sysadmin notifications.
- **Hosted notification services** (Twilio, SendGrid). Possible v2 if someone wants SMS or email; not a V1 concern.
- **Mobile push.** The chat-channel itself is the push surface (Telegram delivers push notifications natively).
- **Rich formatting** (buttons, threads, attachments). All current emitters are plain text; that's deliberately what the dispatcher supports. Buttons would be a v2.
- **Evolve bot command handlers** (the `bot` action mode). The catalog can declare that an event offers `Reply 'fix team-bot-a auth'`, but actually wiring the Evolve bot to recognize and execute that reply is a separate track. Most events ship with `cli` or `ui` mode; events promote to `bot` mode as handlers land. Handler implementation is its own design pass — likely a small registry on the bot side that maps verb-prefix → tool, with operator authentication via the existing primary_user check.

---

## 12. Why this is right shape

- **One catalog → one source of truth.** Operators see and control everything from one page; future emitters land as catalog entries, never as ad-hoc subprocess calls.
- **Severity discipline.** Renaming the audit-cost-overlap framing from "🔴 CRITICAL: spend $5.96 exceeds $5.00" to "💰 Daily spend over threshold / admin-bot: $5.96" stops training operators to ignore CRITICAL.
- **Subscription model matches user expectation.** Operators who use Slack/iOS Settings/etc. already know this UX shape; we don't invent new mental models.
- **Doesn't break what works.** Dispatcher (Phase 1) and signal_notifier (Phase 4) keep ticking; this spec layers semantics on top of the plumbing they provide.
