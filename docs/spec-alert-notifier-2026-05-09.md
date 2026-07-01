# Alert Notifier — Design Spec

**Date:** 2026-05-09
**Status:** Draft, pending review
**Owner:** Evolve admin server
**Related:** [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md), [spec-better-engine-conversational-approval-2026-04-18.md](spec-better-engine-conversational-approval-2026-04-18.md), Security-Bot's gateway-liveness spec at `/Users/security-bot/.openclaw/workspace/docs/gateway-liveness-spec.md` on the mini.

---

## Goal

Unify every sysadmin-facing push message that the Evolve install sends into the operator's chat channel behind a **single dispatcher** with per-source enable/disable toggles. Five emitters already push to the channel today; each rolls its own gating, recipient lookup, and subprocess call. We add a sixth (Signal-store-transition notifier) at the same time as we consolidate the existing five — operators get one place to mute, throttle, or audit every push the pod produces.

The Evolve bot is the universal sysadmin partner: every Evolve install ships with one, and the dispatcher is the principal way it earns its keep. Security-Bot-style gateway monitoring becomes a built-in capability rather than something the operator has to provision per-install.

## Non-goals

- **Not** a new monitor. Detection lives in existing producers (audit, pod_report, spend_alert, cron_alert, plus Signal-store producers). If a check is missing, we add it to the relevant producer, not to the dispatcher.
- **Not** a new UI surface. Overview tiles already show liveness; the Alerts page already shows the Signal feed. The dispatcher pushes *out* of the admin UI, not adds another panel inside it.
- **Not** a remediation actor. Read-only on producer state, write-only on the channel.
- **Not** a security-bot replacement on Pod-Admin's mini. Security-Bot stays running; redundancy is fine while shaking out. Once dispatcher is trusted, the security-bot gateway-watch cron can be retired by the operator.

---

## Existing emitters (the five that need consolidation)

All five pushers currently call `openclaw message send --channel <ch> --to <chat_id> -m <msg>` directly, read recipient from `network.json::alerts.{channel,chatId}`, and run their own detection / gating logic. None coordinate.

| Source | File | Trigger | What it pushes | Signal store? |
|---|---|---|---|---|
| `audit` | [packages/analyzer/audit.py](packages/analyzer/audit.py) `_send_security_alert` | LaunchDaemon `ai.evolve.evolve.audit`, every 15 min | "🔴 *Evolve Security Audit — CRITICAL Findings*" + bullets across identity / config / machine / cost / proposal categories | Writes Signals as side-effect |
| `pod_report` | [packages/analyzer/pod_report.py](packages/analyzer/pod_report.py) `send_message` | LaunchDaemon `ai.evolve.evolve.pod-report-daily`, hourly, self-gates on `pod_report.report_hour` | "📊 *Pod Report — <label>*" daily summary or "🟢 All clear" | Writes Signals as side-effect |
| `spend_alert` | [packages/analyzer/spend_alert.py](packages/analyzer/spend_alert.py) `send_alert` | LaunchDaemon `ai.evolve.evolve.spend-alert`, hourly at :10 | "⚠️ Evolve Spend Alert\n<bot> spent $X today (above $Y threshold)" | No (predates store) |
| `cron_alert` | [packages/analyzer/cron_alert.py](packages/analyzer/cron_alert.py) `_send_alert` | LaunchDaemon `ai.evolve.evolve.cron-alert`, hourly at :15 | Dead-man's switch — fires when a watched cron in `alerts.watchedCrons` hasn't run within `alerts.cronSilenceThresholdDays` | No |

A sixth precedent is forge-engine operator notifications ([packages/admin/evolve_admin/applications/forge_engine.py:1090](packages/admin/evolve_admin/applications/forge_engine.py:1090)) which uses the same subprocess call but is operator-initiated rather than scheduled.

### Observed redundancy

`spend_alert` and `audit`'s `cost`-category emit overlapping content. Recent screenshots show both `🔴 CRITICAL: admin-bot daily spend $5.96 exceeds threshold $5.00` (audit framing) and `⚠️ Evolve Spend Alert\nscout spent $5.96 today...` (spend_alert framing) for the same underlying state. After consolidation, only one source should own each detection — see § "Migration".

---

## Architecture (sources → dispatcher → channel)

```
┌─────────────────────────────────────────────────────────────┐
│  Sources                                                     │
│  ┌──────────┐ ┌────────────┐ ┌──────────────┐ ┌──────────┐  │
│  │  audit   │ │ pod_report │ │ spend_alert  │ │cron_alert│  │
│  └──────────┘ └────────────┘ └──────────────┘ └──────────┘  │
│  ┌──────────────┐ ┌────────────────────┐                    │
│  │ forge_engine │ │signal_notifier(NEW)│                    │
│  └──────────────┘ └────────────────────┘                    │
└──────────────────────────┬──────────────────────────────────┘
                           │  alerts.dispatcher.send(...)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Dispatcher (NEW)                                            │
│  - per-source enable/disable check                           │
│  - per-source/per-signature governor (cooldown, dedup)       │
│  - severity → emoji rendering                                │
│  - recipient resolution (network.json::alerts → bot fallback)│
│  - dispatch log + suppression log                            │
└──────────────────────────┬──────────────────────────────────┘
                           │  openclaw message send (existing CLI)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Channel (Telegram default; configurable per install)        │
└─────────────────────────────────────────────────────────────┘
```

The dispatcher is the only call site for `openclaw message send` in the admin codebase after migration.

---

## Dispatcher API

New module: `packages/admin/evolve_admin/alerts/dispatcher.py`. Single public function:

```python
def send(
    source: str,                 # "audit", "spend_alert", "signal_notifier", ...
    message: str,                # rendered text (source owns its own template)
    *,
    severity: Severity = Severity.INFO,    # critical | error | warning | info
    dedup_key: str | None = None,          # e.g. "audit/team-bot-a/gateway.loopback_no_auth"
    cooldown_seconds: int = 600,           # default 10-min per dedup_key
    recipient_override: tuple[str, str] | None = None,  # (channel, chat_id) — rare
) -> DispatchResult:
    """Send a sysadmin alert. Returns DispatchResult with one of:
       SENT | SUPPRESSED_DISABLED | SUPPRESSED_COOLDOWN | FAILED."""
```

### Behavior

1. **Enable check.** If `alerts.<source>.enabled == False` → return `SUPPRESSED_DISABLED`, log, no push.
2. **Cooldown check.** If `dedup_key` is set and `now - last_dispatch_at[dedup_key] < cooldown_seconds` → return `SUPPRESSED_COOLDOWN`, log, no push.
3. **Recipient resolution.** Use `recipient_override` if given, else read `network.json::alerts.{channel,chatId}`, else fall back to inferred primary channel from `network.json::bots.evolve.primary_user.external_ids` (first present, in configured priority order).
4. **Render.** Prepend severity emoji; the rest of the message is what the source provided. Sources own their own templates.
5. **Dispatch.** Subprocess `openclaw message send …`. 15s timeout. On non-zero exit → return `FAILED`, log with stderr, do *not* update cooldown state (so a retry on the next run can succeed).
6. **State write.** On `SENT`, write `last_dispatch_at[dedup_key] = now` to `{shared_dir}/alerts/dispatcher-state.json` atomically. Empty state file is fine on first run.
7. **Audit log.** Every call appends one line to `{shared_dir}/alerts/dispatcher.jsonl` with `{ts, source, severity, dedup_key, result, message_excerpt}`. 30-day retention via existing log-roll convention.

### Recovery messages

Recovery (the "🟢 X is back" message) is a *call* to `dispatcher.send` with severity `info` and a `dedup_key` that mirrors the original fire's key. The dispatcher does not know about recovery semantics; it's the source's job to gate its own recovery push on "did I send a fire?". This keeps the dispatcher dumb.

The Signal-store-driven `signal_notifier` source implements Security-Bot-style "send recovery only if we sent fire" via per-signature `alerted_for_signal_id` flag in its own state file — see § "signal_notifier source".

---

## Sources

### `signal_notifier` (NEW)

Scope of work the user asked for in the original ask: dead/alive messages when bots in the pod go down or up, plus broader sysadmin-relevant Signal transitions.

- **Cron:** 1-min cadence, idempotent.
- **State:** `{shared_dir}/signals/notifier-state.json`. Per-signature `{alerted_for_signal_id, last_fire_pushed_at, last_resolve_pushed_at}`.
- **Producer allowlist (default-on):** `pod_health`, `host_health`, `integration_probe`, `error_reporter`, `audit`, `security_warden`, `watchdog`. Default-off: `test_runner`, `pod_report` (covered by their own emitters).
- **Debounce:** Skip `firing` Signals younger than 240s. Brief flaps inside the window stay silent. Matches Security-Bot's "≥4 min real downtime" rule.
- **Cooldown:** `dispatcher.send(..., cooldown_seconds=600, dedup_key=signal.signature)`. Repeat fires within 10 min suppressed at dispatcher layer.
- **Recovery:** On `firing → resolved` for a signature where `alerted_for_signal_id` is set, push immediately with severity `info`. No cooldown for the recovery push itself — but the source gates on `alerted_for_signal_id` so unannounced fires don't generate orphan recovery messages.
- **`pod_health` gap-check (Phase 0):** Verify `pod_health` already does Security-Bot's two checks: HTTP `/health` port-up and `<shared>/security-bot-pings/<bot>-alive.txt` heartbeat freshness. If either is missing, add as `pod_health` producer extension — not in the notifier.

### Migration of existing sources

Each existing emitter changes by ~5 lines: replace its inline `subprocess.run([…openclaw message send…])` call with `dispatcher.send(source="audit", message=msg, severity=…, dedup_key=…)`. No schedule changes, no template changes.

| Source | Recommended `dedup_key` | Default `cooldown_seconds` |
|---|---|---|
| `audit` | `f"audit/{bot_id}/{check_id}"` | **86400** — same unresolved finding announced ≤1×/day. Audit scans every 15 min, but a finding the operator hasn't fixed shouldn't re-page on every scan |
| `pod_report` | `f"pod_report/{label}"` | 0 — daily, natural cadence |
| `spend_alert` | `f"spend_alert/{bot_id}/{date}"` | 86400 — once per bot per day |
| `cron_alert` | `f"cron_alert/{cron_label}"` | 86400 — a stalled cron warns ≤1×/day |
| `forge_engine` | `f"forge/{job_id}"` | 0 — job notifications are unique |
| `signal_notifier` | `signal.signature` | 600 — Security-Bot-style flap suppression for repeat fires |

**These are defaults; each is operator-tunable** via the `alerts.<source>.cooldown_seconds` config keys (see § "Config"). The defaults reflect the screenshot evidence: the dominant noise pattern observed in this install was `audit`'s `gateway.loopback_no_auth` finding repeating across team-bot-a/team-bot-c/personal-bot/admin-bot/team-bot-b at 5:00 AM (May 8), 4:12 AM (May 9), and 12:09 PM (May 9) — the same unresolved condition re-paging on every scan. Default 24h cooldown silences the repeat without silencing the first appearance or any new finding.

### Structural fix: transition-based, not scan-based

The reason `audit` is the dominant noise source is structural: it pushes whatever it finds on every scan, regardless of whether the operator already saw it. `signal_notifier` doesn't have this problem — it pushes on `firing → resolved` transitions only, so an unresolved condition generates exactly one fire and one recovery message, regardless of how long it persists.

The long-term answer for `audit` is the same shape: convert it from "scan-based push" to "Signal-store transition-based push". After conversion, an audit finding's lifecycle becomes:

1. Scan finds `gateway.loopback_no_auth` for team-bot-a → write Signal `audit/team-bot-a/gateway.loopback_no_auth` to store, push fire via dispatcher.
2. Subsequent scans find the same condition → re-touch Signal (no state change), no push.
3. Operator fixes config → next scan finds condition cleared → Signal transitions to `resolved`, push recovery.
4. Condition recurs later → new Signal id, fire pushed again.

This eliminates the cooldown knob entirely for `audit`. Tracked as Phase 5b.

### Source ↔ Signal-store overlap

`audit` writes Signals as a side-effect today. After migration:
- Keep `audit` pushing directly via dispatcher — it has its own cadence and templating.
- `signal_notifier` allowlist excludes the `audit` producer by default to avoid double-push.
- Operator can flip if they want Signal-store-driven semantics for audit (debounce + transition-based) instead of "every 15-min sweep".

`spend_alert` does *not* write Signals today and overlaps with `audit`'s cost category. Pick one owner; recommended: `audit` cost-category goes silent (still emits Signal for UI), `spend_alert` keeps the chat push. The redundancy showed up in the screenshots Pod-Admin pasted.

---

## Channel layer (recipient resolution)

### Today

All five existing emitters read `network.json::alerts.channel` and `network.json::alerts.chatId`. This is the de facto "primary channel" — operator-set during install. Dispatcher continues to honor this for back-compat.

### Future-friendlier fallback

If `alerts.channel` is unset, dispatcher falls back to:

1. `network.json::bots.evolve.primary_channel` if explicitly set (new optional override).
2. First channel present in `network.json::bots.evolve.primary_user.external_ids`, in priority order: telegram → signal → whatsapp → slack → discord (configurable as `alerts.channel_priority`).
3. No channel → log a warning, write a `pod_health` Signal of severity `warning` titled "Evolve bot has no primary channel configured", suppress dispatch.

We don't migrate the `alerts.channel` legacy field — installs that have it set continue to work unchanged.

### Send-as-evolve

`openclaw message send` uses the credentials of the openclaw user it runs as — running it from the admin server (`evolve` user, same user the evolve bot runs as) means messages land *as the evolve bot*. No identity plumbing.

---

## Config

Schema additions in [packages/admin/evolve_admin/config_sandbox/schema.py](packages/admin/evolve_admin/config_sandbox/schema.py), mapped to `better-engine-config.json::pod_defaults.alerts.*`.

```python
# Master switch — kills all dispatcher output
TunableKey(
    path="alerts.dispatcher_enabled",
    stock_default=True,
    type_hint="bool",
    description="Master switch for sysadmin push alerts. When off, no source can push.",
),

# Per-source enables (each defaults to current behavior)
TunableKey(path="alerts.audit.enabled",            stock_default=True),
TunableKey(path="alerts.pod_report.enabled",       stock_default=True),
TunableKey(path="alerts.spend_alert.enabled",      stock_default=True),
TunableKey(path="alerts.cron_alert.enabled",       stock_default=True),
TunableKey(path="alerts.forge_engine.enabled",     stock_default=True),
TunableKey(path="alerts.signal_notifier.enabled",  stock_default=False),  # new — opt-in v1

# Per-source cooldowns (operator-tunable frequency control)
# Same dedup_key within cooldown_seconds → suppressed at dispatcher.
TunableKey(path="alerts.audit.cooldown_seconds",            stock_default=86400),
TunableKey(path="alerts.pod_report.cooldown_seconds",       stock_default=0),
TunableKey(path="alerts.spend_alert.cooldown_seconds",      stock_default=86400),
TunableKey(path="alerts.cron_alert.cooldown_seconds",       stock_default=86400),
TunableKey(path="alerts.forge_engine.cooldown_seconds",     stock_default=0),
TunableKey(path="alerts.signal_notifier.cooldown_seconds",  stock_default=600),

# signal_notifier producer allowlist
TunableKey(
    path="alerts.signal_notifier.producers",
    stock_default=[
        "pod_health", "host_health", "integration_probe",
        "error_reporter", "audit", "security_warden", "watchdog",
    ],
    type_hint="list[str]",
    description="Signal producers whose transitions are pushed by signal_notifier.",
),
```

**No quiet hours.** Send when the data is appropriate. If a finding is worth telling the operator about, time-of-day shouldn't gate it — the cooldown knob is what handles "I've already heard about this".

The dispatcher reads each source's cooldown from config at call-time, falling back to the source's compiled-in default if not set. Operators dial individual sources up or down without a code change.

Existing per-source toggles default `True` — no behavior change for current installs. The new `signal_notifier` defaults `False` (opt-in v1; flip to default-on after a week of operator usage as Phase 7).

The admin UI's config page picks these up automatically via the customizations endpoint. No new UI work.

---

## Where it runs

- **Dispatcher** — library imported by each source. No daemon of its own.
- **`signal_notifier`** — new CLI entrypoint `python3 -m evolve_admin.alerts.signal_notifier --shared-dir {shared_dir}`. New LaunchDaemon `ai.evolve.evolve.alerts-signal-notifier`, 1-min cadence. Installed by `deploy.py:_install_launchd_alerts_signal_notifier()`.
- **Existing sources** — keep their existing LaunchDaemon plists; no schedule changes.
- **State** — `{shared_dir}/alerts/dispatcher-state.json` and `{shared_dir}/signals/notifier-state.json`. Same ACL setup as the Signal store — owned by `evolve` user, no /tmp staging.
- **Logs** — `{shared_dir}/alerts/dispatcher.jsonl` (every dispatch attempt), `{shared_dir}/alerts/dispatcher-suppressed.jsonl` (cooldown / disabled suppressions, separate file for easy grep).

---

## Open questions for review

1. **Dispatcher master switch default-on?** I have it default-on so existing installs see no change after migration. The signal_notifier source defaults off so its addition is opt-in.
2. **`spend_alert` vs `audit` cost-category overlap.** Recommended: `audit` cost goes Signal-store-only (no chat push), `spend_alert` keeps chat push. Confirm.
3. **`forge_engine` toggle.** Forge notifications are operator-initiated (a job *they* started). Less surprising than scheduled crons. Worth a toggle anyway? My take: yes, for completeness.
4. **Severity rendering.** Today emitters embed their own emoji ("🔴", "⚠️", "📊", "🟢"). After consolidation, dispatcher can either (a) prepend its own based on `severity`, (b) leave rendering 100% to sources, (c) hybrid. I have it as (a) but accept (b) is less invasive. Pick.
5. **Audit-log retention.** 30 days for dispatch + suppression logs is my default; happy to align with whatever the rest of the project standardizes on.

---

## Phase plan

| Phase | Scope | Estimate |
|---|---|---|
| 0 | Verify `pod_health` covers gateway port + heartbeat (gap analysis only; produces a follow-up if gap found) | 1 PR |
| 1 | `alerts.dispatcher` module + state file + audit log + recipient resolution | 1 PR |
| 2 | Config-sandbox schema entries (master + per-source + `signal_notifier` producers) | 1 PR |
| 3 | Migrate `audit`, `pod_report`, `spend_alert`, `cron_alert`, `forge_engine` to call `dispatcher.send` | 1 PR per source, parallelizable |
| 4 | `signal_notifier` source + LaunchDaemon plist + cron wiring | 1 PR |
| 5a | Resolve `spend_alert` ↔ `audit` cost-category overlap (per § "Source ↔ Signal-store overlap") | 1 PR |
| 5b | Convert `audit` from scan-based push to Signal-store transition-based push (eliminates audit cooldown knob) | 1 PR |
| 6 | `pod_health` heartbeat producer if Phase 0 found a gap | 1 PR (conditional) |
| 7 | Default-on flip for `signal_notifier` after a week of operator-opt-in | 1 PR |

Phase 1 + 2 unblock everything else. Phase 3 migrations are mechanical and can land in parallel — each one is a ~5-line subprocess-call replacement plus tests.

---

## Why this is safer than "spec a new notifier and ignore the existing five"

The original framing of this work was a fresh Signal-store-driven notifier living next to existing emitters. That would have produced a sixth gating logic to maintain and zero answer to "I'm getting too many alerts, what do I turn off?" Consolidating now — while the existing emitters are still ~5 lines each of subprocess plumbing — costs roughly one PR per source and gives the operator the per-source mute switch the user asked for in the same change.
