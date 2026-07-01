# Spec: Proactive-Delivery Monitor + Heal Path (U2.1 / U2.2)

**Date:** 2026-06-10
**Status:** draft for design sync (decision doc: options, recommendations, open questions)
**Roadmap:** [roadmap-user-value-2026-06-10.md](roadmap-user-value-2026-06-10.md) — Phase U2, thesis T2
**Related:**
[principle-tri-state-status.md](principle-tri-state-status.md) ·
[principle-signals-precede-proposals.md](principle-signals-precede-proposals.md) ·
[operator-message-style.md](operator-message-style.md) ·
[principle-plex-test.md](principle-plex-test.md) ·
[spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) ·
[spec-launchd-python-signal-2026-06-03.md](spec-launchd-python-signal-2026-06-03.md)

---

## 1 — Motivation

The 2026-06-10 field refresh (roadmap §3) found that the **#1 stated killer of proactive
setups** — briefings and watchers, the highest-value OpenClaw pattern — is scheduled-task
unreliability: *"breaks every other morning, telling me that it fixed itself."* Two failure
modes compound there: the delivery silently doesn't happen, and the system then *lies about
it*. Users lose trust in exactly the feature that was converting them at week three.

Evolve's most mature muscle is watchdogging scheduled things — daemon health, gateway
liveness, stalled crons. None of that machinery is pointed at the thing the *user* cares
about: **did the briefing actually arrive this morning?** This spec points the existing
muscle at user-facing delivery (thesis T2: "our ops DNA is a user-value feature in
disguise").

Two deliverables:

- **U2.1 — Detection.** A Signal when a scheduled user-facing app misses its delivery
  window, tri-state honest: (a) didn't run, (b) ran but didn't deliver, (c) cannot
  determine.
- **U2.2 — Heal + honest reporting.** Auto-remediation where it is provably safe, and an
  operator message that tells the truth: *"missed today's 7:00 briefing, delivered at 7:40
  after a restart"* beats silent self-healing — silent self-healing is the exact behavior
  users said they distrust.

## 2 — Scope

**In scope**

- Monitoring per-run delivery outcomes of manifest-declared `scheduled_actions[]` whose
  output is user-facing (a message to the operator's channel).
- A delivery-window contract: how an app declares (or the monitor derives) when delivery
  is due and what evidence proves it happened.
- A safe, single-attempt heal path per scheduling mechanism.
- Operator messaging through the Signal store → signal_notifier path, including the 🟢
  recovery convention.
- Producer registration end-to-end (the known silent-routing bug class).

**Out of scope**

- `event_triggers[]` as a first-class manifest field (U2.3) is owned by the parallel
  manifest-v7 slicing spec. This monitor consumes only `scheduled_actions[]`. Event-driven
  watchers have no deterministic delivery window; a future "responsiveness monitor" may
  reuse this spec's evidence-contract shape, but is not designed here (see §6.4 for the
  interface note).
- Fixing *why* an app chronically fails — that is a generator's job (§12), per
  signals-precede-proposals.
- Content quality of deliveries (U3 effectiveness layer).
- Apps scheduled by mechanisms the pod cannot observe (`external`, `unknown`) — excluded
  from the monitored set, visibly (§6.3), not silently.

## 3 — What exists today, and the gap

Survey of the machinery this monitor must fit between (file refs as of 2026-06-10):

| Layer | What it answers | What it does NOT answer |
|---|---|---|
| **Tier-2 structural audit** (`packages/analyzer/app_audit_structural.py`, 6-hourly, producer `app_structural_verifier`) | Is the app correctly installed? Do declared files/crons/anchors exist? Is the OpenClaw cron job registered, and did its *last* run error (`openclaw_cron_error/_skipped/_delivery_failure`)? | Whether *today's* delivery reached the user inside its window. Static contract + last-run status, not per-window outcomes. |
| **Tier-3 semantic audit** (`packages/analyzer/app_audit_tier3.py`, per-app cadence) | Does the code do what the manifest claims? | Runtime outcomes of any kind. |
| **cron_alert** (`packages/analyzer/cron_alert.py`) | Dead-man switch on operator-listed OpenClaw crons (`alerts.watchedCrons`), threshold measured in **days** (default 2). | Window-level misses (minutes), launchd jobs, anything not hand-listed in pod config. |
| **heal.py** (`packages/analyzer/heal.py`, 5-min) | Gateway/process liveness; restarts dead gateways. | App-level scheduled runs and their deliveries. |
| **verify daemon** (`packages/analyzer/verify/daemon.py`) | Proposal-claim verification (RSI loop). Despite gallery manifests saying output signals are "parsed by the verify daemon", **nothing parses `BRIEFING_SENT:` lines today.** | App run outcomes. (Naming collision to keep in mind when writing operator-adjacent docs.) |
| **launchd_python_signal wrapper** ([spec](spec-launchd-python-signal-2026-06-03.md)) | *Positive* signal emission: a scheduled script's stdout pattern → Signal. | Absence detection — a wrapper that never runs emits nothing. A miss is precisely the case where the positive path is silent. |
| **tile_metrics** (`packages/analyzer/tile_metrics.py`) | Human-vs-scheduled activity split from cost-event annotations. | Delivery success; it counts triggers, not outcomes. |
| **Gallery Morning Briefing v2** (`gallery/morning-briefing/p-a9a74bf7.json`) | Writes `memory/briefing-runs/YYYY-MM-DD.json` **only after successful gateway delivery** (atomic), emits `BRIEFING_SENT:/FAILED:/SKIPPED:` stdout lines, enforces one-per-day idempotency. | — (this is the model evidence producer; note the older bot-template variant `gallery/bot-templates/morning-briefing/` still writes markdown `memory/YYYY-MM-DD.md` — see §5.4) |

**The gap:** every existing check is either static (audits), coarse (cron_alert's
2-day threshold), or aimed at infrastructure (heal.py). No component answers *"did the
7:00 briefing reach the user by 7:30 today — and if not, why?"* The evidence to answer it
already exists (run records, launchd job state, OpenClaw `jobs-state.json`); nothing reads
it per-window.

## 4 — Design overview

A new monitor, **`delivery_monitor`**, in the established producer mold:

- **Pure Python, no LLM** (per the RSI-infrastructure-must-be-cheap house preference).
  Reads manifests, run records, launchd state, and OpenClaw cron state; writes Signals.
- **Runs as the `evolve` user, admin-side**, where ACL reads on every bot's
  `.openclaw/workspace/` already work (CLAUDE.md "File Access Pattern"). Falls back to the
  `sudo /bin/cat` grant; if *that* fails, the window is `unmeasurable`, never silently OK.
- **LaunchDaemon `ai.evolve.evolve.delivery-monitor`**, 5-minute `StartInterval`, plist
  emitted via the **JobSpec renderer** (`packages/analyzer/runtime/scheduler.py`)
  — never hand-formatted XML (4.3 Phase C S0 invariant). Installed by
  `sudo evolve-admin install-infra-jobs` alongside the signal-subscriber.
- Per-run flow: build the monitored set from installed manifests (§5) → compute due
  windows (§6) → classify each elapsed window tri-state (§6.2) → heal where declared safe
  (§8) → `observe()` / `sweep_resolve()` Signals (§7) → append to the delivery ledger
  (§6.5, shared evidence with U0).
- Monitor working state (which windows were already checked/healed/announced) lives at
  `{shared_dir}/delivery_monitor/state.json` — same idempotent-across-restarts pattern as
  the signal-subscriber ledger.

Remediation *proposals* (chronic flakiness, schedule changes) are explicitly not this
monitor's job: it emits Signals; a generator consumes them (§12), per
[principle-signals-precede-proposals.md](principle-signals-precede-proposals.md).

## 5 — The delivery contract: how windows are declared

The monitor needs three facts per scheduled action: **(1)** when a run is due, **(2)** what
evidence proves "ran" and what proves "delivered", **(3)** whether the action is
user-facing at all. Three options considered:

### Option A — derive everything from the existing manifest

The current `scheduled_actions[]` (manifest schema v20) already carries
`trigger.schedule`, `mechanism`, `install.schedule` (`{cron: {Hour, Minute}}` or
`{every_minutes: N}`), `installed_artifact`, and `outputs[{kind, channel}]`. Derive:

- *Due*: from `install.schedule` (calendar fire time or interval), TZ from the plist's
  `EnvironmentVariables.TZ`, plus a pod-default grace period.
- *User-facing*: `outputs[]` contains a message/channel-kind entry.
- *Evidence*: per-mechanism defaults (launchd logs, OC `jobs-state.json`).

**Pros:** zero manifest change; day-one coverage of everything already installed.
**Cons:** cannot express per-app grace (a 7:00 briefing that matters by 7:30 vs a
15-minute sync where one missed tick is fine); cannot name a delivery-proof file like
`briefing-runs/<date>.json`, so "ran vs delivered" degrades to log heuristics; cannot mark
re-running as safe, so no heal path; "user-facing" inference from `outputs[]` is a guess on
the ~8 of 12 gallery packages with thin entries.

### Option B — explicit `delivery_contract{}` sub-block on each scheduled action

Add an optional block to each `scheduled_actions[]` entry (manifest bump to v21; this is
an ordinary scheduled-actions schema evolution — v13→v20 has bumped this field seven times
— and does not collide with the v7 Spec/Instance split, whose slicing the parallel session
owns):

```jsonc
"delivery_contract": {
  "user_facing": true,              // this action's output reaches a person
  "window_minutes": 30,             // grace after the scheduled fire time
  "evidence": {
    "ran":       {"kind": "scheduler_state"},                     // default per mechanism
    "delivered": {"kind": "run_file", "path": "memory/briefing-runs/{date}.json"}
                                      // or {"kind": "signal_line", "pattern": "BRIEFING_SENT:", "log": "..."}
  },
  "heal": "rerun" | "none"          // "rerun" asserts the command is idempotent /
                                    // one-per-window-guarded and safe to force-run
}
```

**Pros:** precise windows; first-class "ran vs delivered" distinction; heal is gated on an
explicit safety assertion by the app author (Forge) rather than the monitor guessing;
tri-state classification gets honest evidence instead of heuristics.
**Cons:** manifest churn — Forge prompt updates, gallery backfill, Tier-2 assertions for
the new block.

### Option C — pod-level watch config (the `alerts.watchedCrons` pattern)

List monitored apps + windows in `network.json`.
**Rejected:** this is a product capability and ships in code/manifests, not per-pod config
(product-defaults-in-code). It also recreates cron_alert's weakness — only hand-listed
things are watched, so the default install protects nothing.

### Recommendation: **B layered over A**

`delivery_contract` is optional. When present, it is authoritative. When absent, the
monitor derives Option-A defaults: window = scheduled fire + 30 minutes (the gallery
briefing's own success criterion: "runs file exists by 30 minutes after delivery_time");
user-facing iff `outputs[]` declares a channel-kind output; evidence = per-mechanism
defaults (§6.1); **heal = none** (never force-run an app that hasn't asserted re-run
safety). Day-one coverage without a backfill, precision and healing where declared.

Backfill plan: add contracts to the gallery apps that already have the evidence shape
(Morning Briefing v2, Evening Sweep, Commitment Tracker, Pre-Meeting Brief) in the same PR
series; Forge populates the block for new apps going forward.

### 5.4 — Run-record contract (the `delivered` evidence)

The Morning Briefing v2 pattern is promoted to the **recommended delivery-proof contract**
for proactive apps:

- One file per expected delivery: `memory/<app>-runs/{YYYY-MM-DD}.json` (or per-window
  granularity for sub-daily schedules).
- Written **atomically, only after delivery succeeds** (temp + rename). A crash or gateway
  failure must not leave a run file — the file's existence *is* the claim "the user got
  it", with `sent_at` inside as the timeliness witness.
- The older bot-template briefing (`gallery/bot-templates/morning-briefing/`) writes
  markdown `memory/YYYY-MM-DD.md` instead; it should either gain the JSON run record or
  declare a `signal_line` evidence contract in the backfill pass.

This contract is documented in the spec rather than enforced retroactively; Tier-2 gains
an assertion only for apps that *declare* `run_file` evidence (file pattern must appear in
`interface_contract.data_files`).

## 6 — Detection

### 6.1 — Evidence sources per mechanism

| Mechanism | "Ran?" evidence | "Delivered?" evidence (default) |
|---|---|---|
| `launchd` | `launchctl print system/<label>` (last exit status / run timestamps; needs the evolve sudoers grant — probe with `sudo -n`, tri-state on failure) and/or stdout-log mtime | declared `run_file` / `signal_line`; without a contract: log-line heuristics on declared `signal_prefixes` (e.g. `BRIEFING_SENT:`) |
| `launchd_python_signal` | wrapper's own run ledger + launchd state (the wrapper already timestamps each invocation) | the wrapper's positive Signal, or declared evidence |
| OpenClaw cron | `jobs-state.json` → `state.lastRunAtMs`, `lastRunStatus`, `consecutiveErrors` (via `cron_manager.read_jobs_state`) | `lastRunStatus == ok` **plus** declared evidence where present (OC "ran" ≠ "user saw it") |
| `oc_heartbeat_instruction` / `oc_session_instruction` | none deterministic — the LLM decides per heartbeat | **excluded from v1.** Reported once per app as `coverage: unmonitorable` in the ledger (§6.5), never as a fake green. |
| `crontab` / `external` / `unknown` | n/a | excluded, counted in the coverage denominator |

### 6.2 — Tri-state classification

At each monitor tick, for every monitored action whose window (`scheduled fire time` →
`fire + window_minutes`) has elapsed since the last check:

1. **Delivered on time** — delivery evidence present with `sent_at` inside the window →
   no Signal; ledger row `outcome: on_time`. Sweep-resolve any prior firing Signal for
   this action (condition cleared).
2. **(a) Didn't run** — no delivery evidence AND scheduler-state shows no fire in the
   window (no launchd run, no `lastRunAtMs` advance, no log growth) → Signal
   `app_delivery_missed`, `details.diagnosis = "did_not_run"`.
3. **(b) Ran, didn't deliver** — scheduler-state shows a fire (or an explicit
   `*_FAILED:` line / OC `lastRunStatus != ok`) but delivery evidence is absent → Signal
   `app_delivery_missed`, `details.diagnosis = "ran_undelivered"`.
4. **(c) Cannot determine** — any probe required for 2/3 failed (EACCES on the workspace,
   `sudo -n` denied, `jobs-state.json` unparseable, plist missing its TZ so the window
   itself is ambiguous) → Signal `app_delivery_unmeasurable`. Per
   [principle-tri-state-status.md](principle-tri-state-status.md) this is its own state —
   the monitor must never coerce a failed probe into "looks fine", and must never raise a
   false `did_not_run` it can't evidence. The distinguish-tooling-failure rule (PR #1579
   pattern): classify the probe's own failure separately from the finding.

**Late delivery** (evidence appears after the window, e.g. post-heal or after host wake) is
not a fourth state: the underlying `app_delivery_missed` Signal resolves, with the actual
delivery time recorded in `details.recovery` (§9). The ledger row records
`outcome: late, delivered_at: …`.

**Refinement (2026-06-11, the OC-2026.6 masking fix —
[spec-gallery-delivery-convention-2026-06-11.md](spec-gallery-delivery-convention-2026-06-11.md) §4):**
undeclared delivery evidence is no longer reported as a probe *error*; it is the
`delivered_declared = False` evidence state, and `LastExitStatus` from the existing
launchctl probe joins classification (normalized from launchd's wait-status
encoding — exit N prints as N·256 — and ignored while the job has a live PID,
since it then belongs to a previous run). Ordering after the on-time/late checks:
a fire in the window with a non-zero exit ⇒ **(b)** `ran_undelivered`,
`suspected_cause: script_error` (exit status in `details.last_exit`) — regardless of
declared evidence, since the script died before it could produce proof; a clean run
with evidence undeclared ⇒ **(c)** unmeasurable (unchanged outcome, reached honestly);
no fire with healthy probes ⇒ **(a)** `did_not_run` even when evidence is undeclared.
`script_error` misses are not heal-eligible (a kickstart re-runs the same crashing
script; the fix is the app, not the scheduler).

### 6.3 — Edge cases the classifier must handle

- **Host asleep through the window.** launchd coalesces missed `StartCalendarInterval`
  jobs on wake. If a `host_slept` Signal (platform-expansion 8.2) overlaps the window:
  suppress heal (the run is already queued by launchd), keep honesty — if the delivery
  lands late, the recovery message says *"the computer was asleep"* rather than implying
  an app fault. If the host never wakes within the window + sleep allowance, the miss
  fires with `details.suspected_cause = "host_asleep"`.
- **Gateway down for that bot.** "Ran, didn't deliver" is the expected shape when the
  bot's gateway is dead — and heal.py already owns gateway restarts. The monitor must not
  rerun an app into a dead gateway: if an active gateway-down Signal exists for the bot,
  link it via `observe(caused_by_signal_id=…)`, defer the rerun until the gateway Signal
  resolves (single deferred attempt, same window), and let the message name the real
  cause.
- **DST / timezone.** Windows are computed in the job's own TZ (plist
  `EnvironmentVariables.TZ`, falling back to host TZ). The classic 7:00 job on a
  spring-forward morning is a known launchd quirk — `details.suspected_cause = "dst"`
  when the window straddles a transition; do not heal-rerun (launchd's own behavior is
  the authority), do report honestly.
- **First install / never-yet-run.** An action installed mid-window or today has no
  history; no Signal until its first full window has elapsed.
- **Operator-disabled jobs.** `launchctl print` shows disabled state → the action is
  deliberately off. Not a miss; ledger `outcome: disabled`. (An operator who disables the
  briefing has opted out; alerting would be noise.)

### 6.4 — Interface note for U2.3 (`event_triggers[]`)

Event-triggered actions ("when X happens, tell me") have no schedule, hence no window, and
are invisible to this monitor by construction. The contract shape that *is* reusable when
the v7 slicing session lands `event_triggers[]`: the `delivery_contract.evidence` block
(§5) — "what file/line proves the user got the output" — is trigger-agnostic. This spec
reserves the block name; it does not design event-trigger monitoring.

### 6.5 — Delivery ledger (shared evidence with U0)

Every classified window appends one JSONL row to
`{shared_dir}/delivery_monitor/ledger/<YYYY-MM-DD>.jsonl`:

```jsonc
{"ts": "...", "bot_id": "...", "app_id": "...", "action_id": "...",
 "window_start": "...", "window_end": "...",
 "outcome": "on_time" | "late" | "missed" | "unmeasurable" | "disabled" | "unmonitorable",
 "diagnosis": "did_not_run" | "ran_undelivered" | null,
 "delivered_at": "... | null", "healed": true | false}
```

This is the ground truth U0.1's "proactive deliveries per week" metric reads, and it makes
the tri-state coverage visible: U0's Value view can show *"% of scheduled deliveries
measurable"* alongside the delivery rate, per the tri-state principle's "surfaces show
measurability" clause. Retention: 90 days, pruned by the existing daily retention job
pattern.

## 7 — Signal design

| Field | Value |
|---|---|
| `producer` | `delivery_monitor` |
| `type` | `app_delivery_missed` (states a+b, distinguished by `details.diagnosis`) · `app_delivery_unmeasurable` (state c) |
| `signature` | `delivery_monitor:{type}:{bot_id}:{app_id}:{action_id}` — one active Signal per action, not per window; repeat misses bump `observation_count` (find-or-create dedup via `signals.store.observe()`) |
| `scope` / `bot_id` | `bot` / the owning bot |
| `flavor` | `activity` |
| `severity` | `app_delivery_missed`: `warn` on first miss, escalated to `alert` by the monitor when the same action misses ≥2 consecutive windows or heal fails (§8). `app_delivery_unmeasurable`: `info`, escalated to `warn` after 3 consecutive unmeasurable windows (one broken probe shouldn't page anyone; a chronically blind monitor must). |
| `category` | `platform` (the `PRODUCER_CATEGORY_DEFAULT` fallback; see Open Question 6 on a future "apps" category) |
| `details` | `{app_name, action_id, schedule_human, window_start, window_end, diagnosis, suspected_cause, heal: {attempted, action, result}, recovery: {delivered_at, summary} | null, probe_errors: [...]}` |
| `caused_by_signal_id` | set when a gateway-down / host-slept Signal explains the miss (§6.3) |
| Resolution | delivery evidence for a subsequent (or healed) window → `sweep_resolve(producer="delivery_monitor", kept_signatures=…)` at the end of each monitor tick, the standard sweep-monitor pattern. `details.recovery` is written before resolving so the recovery message can tell the truth (§9). |

Why one Signal per action rather than per window: the operator-meaningful condition is
"this app's delivery is unreliable", which dedups naturally by action; per-window Signals
would flood the Alerts page after a weekend outage. The per-window record lives in the
ledger (§6.5).

## 8 — Heal path

**Policy: one attempt per missed window, gated on declared safety, honest regardless of
outcome.**

| Situation | Heal action | Precondition |
|---|---|---|
| launchd job loaded but didn't fire / run failed | `sudo /bin/launchctl kickstart system/<label>` (no `-k` — these are one-shot jobs, not daemons to restart) | `delivery_contract.heal == "rerun"` |
| plist file exists but label not loaded (the "deliberately broken" proof case; also post-migration drift) | `sudo /bin/launchctl bootstrap system <plist>` then kickstart | same, plus plist passes `plutil -lint` |
| OpenClaw cron run failed/skipped | trigger a run via the OC runtime's cron interface where the installed version supports it; otherwise report-only | `heal == "rerun"`; see Open Question 2 |
| `ran_undelivered` with active gateway-down Signal | defer to heal.py's gateway restart; single rerun after the gateway Signal resolves | §6.3 |
| heartbeat-instruction actions, undeclared-heal apps, DST/asleep cases | report-only | — |

Safety reasoning, stated explicitly because force-running user-facing apps is the riskiest
part of this spec:

- `heal: "rerun"` is an **assertion by the app author** that a forced run is safe: the app
  either enforces one-delivery-per-window itself (Morning Briefing v2's run-file
  idempotency — a kickstart after a successful run is a no-op) or is harmless to repeat.
  The monitor never infers this. Default is `none`. Apps with external side effects
  (anything that sends email, writes to third-party systems) should not declare it; the
  canary-for-one-file-edits instinct applies — the first contract backfills are
  message-only apps.
- **No retry loops.** One heal per window. If the rerun produces no delivery evidence
  within `heal_wait` (default 10 minutes), the Signal escalates to `alert` and the message
  says the restart didn't work. Subprocess-hang house rule: a heal that doesn't work gets
  reported, not re-tried harder.
- **Grants.** The kickstart/bootstrap calls require additions to the evolve sudoers file —
  rendered by `_render_evolve_sudoers()` in `setup_wizard.py` (single source of truth),
  full binary paths, validated with `visudo -c`. The monitor probes its own grant with
  `sudo -n` first; a missing grant makes heal unavailable and is itself reported tri-state
  (the monitor says "couldn't attempt the restart", not nothing) — `exists()`-lies and
  silent-degradation rules both apply.
- Every heal attempt and outcome is recorded in `details.heal` and the ledger — the
  generator (§12) needs the heal track record to propose durable fixes.

## 9 — Honest reporting

### 9.1 — The message-path decision

The notifier (`signal_notifier.py`) has Security-Bot-style flap suppression: fires younger
than ~240 s are debounced, and **recovery messages are only pushed for previously-announced
Signals** ("the fire-pushed gate prevents orphan recovery messages"). For most producers
that is correct — but here it inverts the product stance: a miss healed in 3 minutes would
be *completely silent*, and the user who got a 7:25 briefing scheduled for 7:00 gets no
explanation. That is structurally the "fixed itself, silently" experience, minus the lying.

| Option | Mechanics | Verdict |
|---|---|---|
| **M1 — notifier as-is** | fast heals silent; slow ones get ⚠️ fire + 🟢 resolve | Rejected: violates the core honesty stance for exactly the most common (fast-heal) case. |
| **M2 — per-event "announce recovery even if fire was unannounced"** | a catalog-event flag (e.g. `announce_unannounced_resolve`); resolve push renders from `details.recovery`. Fast heal ⇒ exactly **one 🟢 message**; long outage ⇒ ⚠️/🔴 fire then 🟢 resolve. | **Recommended.** Small, per-event opt-in (no orphan-resolve noise for other producers), keeps everything on the Signal-store path and the Alerts page. |
| **M3 — monitor direct-dispatches its own messages** | add `delivery_monitor` to `_DIRECT_DISPATCH_PRODUCERS` + schema `excluded_producers` stock default | Rejected: forks the message path, loses notifier debounce/cooldown, and re-creates the double-message / drift bug class the deny-list model exists to prevent. |

With M2, the notifier's existing debounce doubles as the heal window: misses that heal
inside it produce a single truthful 🟢; misses that outlast it escalate normally.

### 9.2 — Operator copy (primary surface — Plex test applies)

No "Signal", "producer", "launchd", "kickstart", file paths, or label strings in any of
these. One emoji per header, ≤10 lines, explicit action-or-none, per
[operator-message-style.md](operator-message-style.md). `{app}` renders as the app's
display name, `{bot}` as the bot's name.

**Missed and healed (the flagship message — fires as the 🟢 resolve, M2 path):**

```
🟢 {app} — late today, now delivered
{bot}'s 7:00 {app} didn't go out on time. Evolve restarted it,
and it was delivered at 7:40.
No action needed.
```

**Missed, heal failed (or no heal declared), fired after debounce:**

```
🔴 {app} didn't arrive
{bot}'s 7:00 {app} didn't go out this morning, and an automatic
restart didn't fix it.
Check {bot}'s Apps page for details.
```

**Ran but didn't deliver (gateway cause known):**

```
⚠️ {app} ran but the message didn't reach you
{bot} prepared today's {app}, but its messaging connection was down
when it tried to send. Evolve is restarting the connection and will
retry once it's back.
No action needed yet — you'll get a follow-up either way.
```

**Cannot determine (after 3 consecutive unmeasurable windows):**

```
⚠️ Can't confirm {app} is being delivered
Evolve couldn't check {bot}'s last three scheduled deliveries — the
records it needs aren't readable. {app} may still be arriving
normally.
Check the Alerts page for details.
```

The follow-up promise in the third message is binding: the deferred-rerun path (§8) ends
in either the 🟢 recovery or the 🔴 failure message — never silence.

### 9.3 — Noise discipline

- On-time deliveries: **no message, ever** (the no-all-clear rule). The weekly value
  digest (U0's open recommendation) is where "21 of 21 briefings on time" belongs.
- One active Signal per action (§7) + notifier cooldown bounds repeat-miss messaging;
  consecutive misses escalate severity rather than re-paging.
- `unmeasurable` reaches chat only after persistence (§7); single broken probes live on
  the Alerts page.

## 10 — Producer registration checklist (the silent-routing bug class)

The roadmap text (and prior incident memory) describes this bug class in terms of the
`_DEFAULT_PRODUCERS` allowlist. **That model was inverted on 2026-06-09**: signal_notifier
now routes *every* producer to chat by default, with a small deny-list
(`_DIRECT_DISPATCH_PRODUCERS`) for producers that already call `dispatcher.send()`
themselves. The bug class survives in new forms — a producer wrongly added to the
deny-list, a missing catalog mapping degrading subscription gating, and the documented
unenforced sync between the deny-list and its schema stock default. Exact steps for this
producer:

1. **Emit canonically.** `signals.store.observe(producer="delivery_monitor", …)` +
   `sweep_resolve()` per tick (§7). No direct Signal-JSON writes, no `dispatcher.send()`.
2. **Do NOT touch the deny-list.** `delivery_monitor` must not appear in
   `_DIRECT_DISPATCH_PRODUCERS` (`signal_notifier.py`) nor in the
   `alerts.signal_notifier.excluded_producers` `stock_default`
   (`config_sandbox/schema.py`) — adding it is the modern equivalent of the old
   allowlist-miss: Alerts page lights up, chat stays silent.
3. **Category routing.** Add `"delivery_monitor": "platform"` to
   `PRODUCER_CATEGORY_DEFAULT` (`packages/analyzer/schema/signal.py`) so the Alerts page
   tabs it deliberately rather than by fallback.
4. **Catalog events** (`alerts/catalog.py`): `system.app_delivery_missed` and
   `system.app_delivery_unmeasurable`, `producer_source="delivery_monitor"`,
   `body_template` starting with an approved emoji — `tests/test_alerts_catalog.py`
   enforces the emoji and key-prefix invariants. This is what gives the operator a
   per-event mute for delivery alerts without silencing the whole producer.
5. **Notifier mapping.** Extend `_catalog_event_for_signal()` in `signal_notifier.py` to
   map the producer's types to those catalog keys (unmapped Signals fall back to
   source-level gating only), and implement the M2 `announce_unannounced_resolve` flag on
   the missed-delivery event (§9.1).
6. **Test pins.** Add `delivery_monitor` to `_EXPECTED_PRODUCERS` in
   `tests/test_alerts_catalog.py`; add a notifier test in the
   `test_brand_new_producer_reaches_chat` family asserting the fast-heal single-🟢 path.
7. **Close the sync gap while here.** No test currently asserts
   `_DIRECT_DISPATCH_PRODUCERS == excluded_producers.stock_default` (the comment in
   `schema.py` demands it manually). Ship that one-assert test in this work — it converts
   the remaining drift class from convention to CI.
8. **Generator subscription (Phase 2, §12).** `subscribes_to: [app_delivery_missed]` in
   the consuming generator's charter + `tools/bump_charter_fingerprints.py` on deploy.

## 11 — Boundary with existing machinery

The organizing rule: **audits own the static contract and chronic state; the delivery
monitor owns per-window outcomes; heal.py owns processes.**

| Component | Keeps | Cedes to delivery_monitor |
|---|---|---|
| Tier-2 structural audit | install/registration assertions; `openclaw_cron_error` / `_skipped` / `_delivery_failure` as *chronic last-run-state* findings on its 6-hour cadence; new assertions validating `delivery_contract{}` shape and that declared evidence paths appear in `interface_contract.data_files` | window-level timeliness ("today's 7:00 run by 7:30") — Tier-2 must not grow per-window checks |
| Tier-3 semantic audit | manifest-vs-code coherence, incl. whether the code really writes the declared run file | all runtime observation |
| cron_alert | days-level dead-man switch for *infra* crons the operator hand-lists | app-backed crons: when a watched cron maps to a monitored scheduled action, cron_alert skips it (one-line guard) so a miss never double-pages. Eventual absorption is Open Question 5. |
| heal.py | gateway/process restarts | app reruns. Coordination is one-directional via the Signal link (§6.3); neither calls the other. |
| verify daemon | proposal verification (unrelated; the gallery manifests' "parsed by the verify daemon" wording should be corrected to name this monitor in the backfill pass) | — |
| launchd_python_signal wrapper | positive in-run signal emission | absence detection (a never-fired wrapper is exactly what this monitor exists to notice) |

Charter immutability note: none of the above moves code in v1 except the cron_alert skip
guard and the notifier flag; both are small, separately testable patches.

## 12 — Remediation proposals (generator, Phase 2)

Per signals-precede-proposals, the monitor never proposes. A thin
**`delivery_reliability` generator** (Phase 2, after the monitor has a few weeks of
ledger) subscribes to `app_delivery_missed` and proposes only on chronic patterns the
single-attempt heal can't fix — e.g. "this action missed 4 of the last 14 windows, heal
recovered 4/4; the job's schedule races the host backup window — propose moving it",
or "heal failed 3 times with the same probe error — propose an Investigation". It reads
the Signal store and the delivery ledger, not bot state (investigate-before-propose
toolkit applies). Designing its proposal taxonomy is deferred to its own session; the
contract this spec fixes is: `motivating_signals[]` will point at the
`app_delivery_missed` Signals, so every proposal traces to a condition the operator
already saw.

## 13 — Rollout and proof artifact

1. **PR 1 — contract + monitor + Signals** (detection only, heal stubbed off): manifest
   v21 block, monitor daemon + JobSpec install, ledger, registration steps 1–4 + 6–7,
   Tier-2 contract assertions, gallery backfill for the four message-only apps.
2. **PR 2 — heal + recovery messaging:** sudoers grants, kickstart/bootstrap paths,
   deferred-rerun-on-gateway logic, notifier M2 flag + 🟢 rendering (step 5), copy per
   §9.2.
3. **Soak** on the live pod ≥1 week with heal enabled only for Morning Briefing v2
   (canary-an-affected-bot rule), then enable for the remaining backfilled apps.

**Proof artifact (roadmap U2 gate):** on the live pod, deliberately break the briefing —
`launchctl bootout` its label so the plist exists but nothing is loaded — then, within one
monitor cycle of the missed window: the `app_delivery_missed` Signal fires with
`diagnosis: did_not_run`, heal re-bootstraps + kickstarts, the run file appears, the
Signal resolves, and the operator's channel shows the single 🟢 *"late today, now
delivered — delivered at HH:MM"* message. Written up as a decision-doc transcript (the
Atlas-session pattern), alongside a second negative case (gateway down →
`ran_undelivered` → deferred rerun → follow-up message) to prove the tri-state and the
follow-up promise.

## 14 — Testing

- **Classifier unit tests:** synthetic workspace + launchd-state fixtures for each row of
  the §6.2 matrix, including every probe-failure → `unmeasurable` path (the tri-state
  anti-pattern greps — no `except: return ok`).
- **Window math:** calendar vs interval schedules, TZ from plist, DST transition days,
  host-asleep overlap.
- **Heal gating:** no contract ⇒ no rerun; gateway-down ⇒ deferred; one-attempt
  enforcement across monitor restarts (state.json).
- **Messaging:** notifier tests for single-🟢 fast heal (M2), escalation copy, catalog
  emoji/key invariants, `_EXPECTED_PRODUCERS`, and the new deny-list↔stock-default sync
  test.
- **E2E:** the audit_poller OC-cron e2e pattern reused — fixture bot, broken job,
  assert Signal + ledger + message rendering.
- No multiprocessing-spawn in tests; subprocess + file barriers (house rule).

## 15 — Open questions (for design sync)

1. **Default grace window.** 30 min recommended (matches the gallery briefing's own
   success criterion). Is that right for sub-daily interval jobs, where one missed
   15-minute tick may be noise? Proposed: interval jobs default to `interval + grace` and
   only alert on **two** consecutive missed ticks.
2. **OC-cron forced run.** Does the currently-deployed OpenClaw expose a "run job now"
   CLI/API? If not, OC-cron actions are report-only until it does — acceptable for v1?
3. **`unmeasurable` escalation threshold.** 3 consecutive windows to chat (§7) — too slow
   for a daily briefing (3 days blind)? Alternative: 2 windows for daily-or-slower
   schedules, 3 for sub-daily.
4. **Severity floor for the first miss.** `warn` recommended (it becomes a 🟢-only story
   when healed fast under M2); is there appetite for `alert` on first miss for apps the
   operator marks critical?
5. **cron_alert end-state.** Keep indefinitely for infra crons, or fold into this monitor
   once it grows a non-app job list? (Leaning: keep; its audience is infra, not users.)
6. **Alerts-page category.** `platform` for v1, or introduce an `apps` category now? The
   Category literal is closed; adding one touches the Alerts page tabs — proposed as a
   separate small PR if wanted.
7. **M2 flag semantics.** Confirm the notifier owner is comfortable with per-catalog-event
   `announce_unannounced_resolve`, and that resolve-rendering from `details.recovery` fits
   the existing `_render_resolve` shape.
8. **Member-bot audience.** Misses on a member bot's app: message the pod operator only
   (v1 recommendation), or also the bot's own user? Member bots' users have no dashboard
   (bot-message-audience constraint), which argues for operator-only until the per-bot
   sysadmin-audience work lands.

## 16 — References

- [roadmap-user-value-2026-06-10.md](roadmap-user-value-2026-06-10.md) §3 (field findings), §5 U2, thesis T2
- [principle-tri-state-status.md](principle-tri-state-status.md) · [principle-signals-precede-proposals.md](principle-signals-precede-proposals.md) · [principle-plex-test.md](principle-plex-test.md)
- [operator-message-style.md](operator-message-style.md) — header set incl. the 🟢 recovery convention
- [spec-alerts-signal-store-2026-05-07.md](spec-alerts-signal-store-2026-05-07.md) · [spec-signal-subscriber-2026-05-31.md](spec-signal-subscriber-2026-05-31.md)
- [spec-launchd-python-signal-2026-06-03.md](spec-launchd-python-signal-2026-06-03.md) — the positive-signal sibling mechanism
- `packages/analyzer/app_audit_structural.py`, `packages/analyzer/app_audit_tier3.py` — the audit tiers this monitor bounds against
- `packages/analyzer/cron_alert.py`, `packages/analyzer/heal.py` — adjacent watchdogs
- `packages/admin/evolve_admin/alerts/signal_notifier.py` — deny-list routing + debounce/recovery state machine
- `packages/admin/evolve_admin/applications/manifest.py` (schema v20 `scheduled_actions`), `applications/cron_manager.py`
- `gallery/morning-briefing/p-a9a74bf7.json` — the model evidence producer (`memory/briefing-runs/`)
- PR #1579 — distinguish-tooling-failure reference implementation
