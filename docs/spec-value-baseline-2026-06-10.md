# Spec: Per-Bot Value Baseline + `bot_underused` Signal (U0)

**Status:** draft for design sync · **Date:** 2026-06-10 · **Owner:** U0 spec session
**Roadmap:** [roadmap-user-value-2026-06-10.md](roadmap-user-value-2026-06-10.md) §5 (U0) — this spec owns the two questions delegated by design-sync decision #1: (a) the metric definitions and explicit exclusions, (b) the weekly "what your pod did for you" digest recommendation.
**Governing principles:** [instrument-outcomes-before-optimization](principle-instrument-outcomes-before-optimization.md) · [tri-state status](principle-tri-state-status.md) · [Plex test](principle-plex-test.md)

This is a decision doc: options, recommendations, open questions. Build follows after design sync.

---

## 1. Motivation and scope

Nothing in Evolve today answers "is this pod underused, and which bot is the idle one?" We have rich raw data (tile metrics, trigger-kind splits, app usage, ObservationTuples, app run logs) and no rollup. Every later phase of the user-value roadmap — activation (U1), delivery reliability (U2), the effectiveness layer (U3) — needs this scoreboard first, per the instrument-outcomes principle: *outcome signal first, decision layer second, optimization machinery last*. U0 is the Phase-0 outcome instrumentation for the whole usefulness track.

**In scope:**

- A per-bot **utilization baseline**: a small vector of interpretable metrics computed by pure Python over data already on disk. No new collection, no LLM calls.
- A **`bot_underused` Signal**: tri-state honest, fired through the standard Signal store, with the producer-registration steps named explicitly (a known silent-failure trap).
- **Surfacing**: bot tile block + chip, a Value view in the admin UI, and a recommendation on the weekly operator digest.
- A **proof artifact**: the live pod's bots ranked by the baseline, with one defensible `bot_underused` firing.

**Out of scope (see §10):** suggestions about *what to do* about low value (that is U3, the effectiveness layer), purpose capture (U1.1), delivery-window monitoring (U2.1), any automated consumer of the signal.

---

## 2. What counts as value — and what is explicitly excluded

This section is first-class spec content, ahead of the metric definitions, because the failure mode it guards against is worse than having no baseline at all: a baseline that rewards chattiness would push the product toward the exact noise the operator-message-style guide exists to prevent, and would mislabel healthy bots as failing.

**Definition.** For this baseline, a bot is *delivering value* when **a person chooses to use it** or **it proactively delivers something a person receives**. Both count equally. Everything else — however busy the bot looks — does not count.

### 2.1 Explicit exclusions (anti-Goodhart guardrails)

| Excluded from "value" | Why |
|---|---|
| **Turn volume / session counts / message counts** | Volume is activity, not value. A heartbeat-heavy bot can post thousands of turns while serving no one. These stay in the tile as *descriptive* numbers; no baseline metric or threshold may reward higher volume. |
| **Chattiness and engagement totals** | The field data is unambiguous: power-user bots *push, then shut up*. A bot that sends one reliable briefing a day beats a bot that chats constantly. Engagement totals from ObservationTuples are descriptive only. |
| **Breadth of use (many verbs/domains)** | A quiet bot doing **one verb reliably is a success, not churn risk** (roadmap mandate boundary). Breadth is reported descriptively in the Value view; it never appears in the `bot_underused` predicate and is never framed as a goal. |
| **Cost / spend** | Spend is not utility. A cheap bot can be invaluable; an expensive one can be pure heartbeat waste. Cost stays on the cost tile. |
| **Mood / sentiment** | A frustrated-mood cluster may be the *highest-stakes* usage the bot has (troubleshooting under pressure). Mood is U3 input, not a value metric. |
| **Background/automation activity that reaches no one** | Heartbeats, subagent spawns, internal crons with no user-facing output are excluded from "proactive delivery". Scheduled runs count only because v1 cannot yet confirm delivery (§4.2, honestly labeled; upgraded by U2.1). |
| **A composite 0–100 score** | The tile redesign deliberately killed the composite score for being uninterpretable and gameable. The baseline is a vector of named metrics; ranking for the proof artifact uses an explicit sort key (§9), not a published score. |

### 2.2 Consumption guardrail

Per instrument-outcomes: **no automated machinery may read the baseline or subscribe to `bot_underused` until the signal is validated on live data** (§9). In v1 the only consumers are the operator (tile, Value view, alert) and the proof artifact. No generator gets `subscribes_to: [bot_underused]`; the U3 effectiveness layer becomes the first machine consumer *after* validation. A shadow metric nobody has verified must not steer proposals.

---

## 3. Inputs — existing data only

All inputs already exist on disk and are readable by the `evolve` user without sudo. The baseline adds **no new collection** and makes **no LLM calls** (ObservationTuples are LLM-extracted upstream, but reading the JSONL is pure Python).

| Input | Path | What it provides | Caveats |
|---|---|---|---|
| Daily metrics | `{shared_dir}/metrics/{YYYY-MM-DD}/{bot_id}.json` (written nightly by `measure.py`) | `turn_count`, `session_count`, `app_usage`, per-day | Anchor for day-granular windows; absence of a day's file = that day is unmeasurable |
| Cost-event annotations | `{shared_dir}/annotations/{bot_id}/cost_events-{date}.jsonl` | `trigger_kind` per event: `user_turn`, `heartbeat`, `cron_app`, `subagent`, `fallback`/`unknown` | The only source that distinguishes *a person typed* from *a clock fired*. Same source `tile_metrics._classify_split` uses |
| App manifests | `{shared_dir}/applications/{bot_id}/*.json` | Installed-app inventory; `apps.used_7d/28d` via existing tile helpers | — |
| ObservationTuples | `{shared_dir}/observations/{bot_id}/{YYYY-MM-DD}.jsonl` | noun×verb×mood×engagement per session segment | Subject to per-bot DNT opt-out — absence must read as *null*, not zero (tri-state). Descriptive metrics only (§2.1) |
| App run logs | e.g. Morning Briefing's `memory/briefing-runs/YYYY-MM-DD.json` in the bot workspace | Per-run delivery metadata (`sent_at`, `channel_delivery`, composer) | **v1.1, not v1** (§4.2 option C). Path is per-bot workspace: resolve via `bot_home()` / `get_bot_user()` — never `/Users/{bot_id}/…` (bot_id ≠ macOS account name). Reads work via the existing `.openclaw/` ACL |

---

## 4. The metric set

### 4.1 Options

**Option A — minimal (2 metrics):** active-human-days + app coverage. Cheapest, but blind to the proactive-delivery half of value: a briefing-only bot with zero human turns would rank as unused, which violates §2 directly. *Rejected.*

**Option B — recommended (4 headline metrics + 1 descriptive):** the set below. Covers both halves of the value definition (human pull + proactive push), trend, and app utilization, while keeping every number explainable in one sentence.

**Option C — B plus run-log-confirmed deliveries:** replaces the scheduled-runs proxy with confirmed deliveries parsed from per-app run logs. More honest, but coverage is partial (only instrumented apps write run logs today) and the parsing per app-contract belongs with U2.1's delivery monitor. *Deferred to v1.1; the metric is designed to swap sources without renaming (§4.2).*

### 4.2 Recommended metrics (Option B)

All windows anchor on the bot's most recent metrics-file day (same `anchor_date` convention as `tile_metrics.py`; 28d = two clean 4-week buckets for the trend comparison). Every metric is tri-state: `null` means *couldn't measure*, never coerced to 0.

| Metric | Definition | Null when |
|---|---|---|
| **`active_human_days_7d` / `_28d`** | Count of days in the window with ≥ 1 `trigger_kind == "user_turn"` cost event. Day-granular **by design**: one human interaction makes the day active; ten more add nothing. This makes the metric structurally insensitive to volume (§2.1) — the quiet one-verb bot scores identically to the chatty one. | Fewer than the measurability floor of days are measurable (§4.3) |
| **`proactive_runs_7d` / `_28d`** | Count of `trigger_kind == "cron_app"` cost events — scheduled app runs. **Heartbeats and subagent events are excluded.** Honest label: this counts *runs, not confirmed deliveries* (a run that crashed before sending still counts in v1). The Value view says "scheduled app runs"; when U2.1's delivery monitor lands, the source swaps to confirmed deliveries and the label upgrades — same metric name, documented source change. | Same floor as above |
| **`app_coverage_28d`** | `apps.used_28d / apps.total` from the existing tile helpers — fraction of installed apps that ran in the window. | `apps.total == 0` (no apps installed → coverage is undefined, not 0%) |
| **`value_trend_28d`** | `(active_human_days_28d + proactive_runs_28d)` for the current 28d vs the prior 28d, reported as a signed delta. **Descriptive only** — it never gates `bot_underused` (a deliberate wind-down would otherwise look like a failure). | Fewer than the floor of measurable days in *either* bucket (needs 56d of history) |
| **`usage_breadth_28d`** *(descriptive)* | Distinct (noun × verb) cells in the bot's ObservationTuples over 28d. Shown in the Value view as context ("what kinds of work"), never in any predicate, never framed as a target (§2.1). | Observations dir absent for the window (incl. DNT opt-out) |

### 4.3 Day-level measurability (tri-state mechanics)

Each day in a window is classified independently:

- **Measurable, active/inactive** — the day's metrics file exists. If `turn_count > 0` but the day's cost-events file is absent, the day is **unmeasurable** instead (activity happened but the annotation pipeline can't say who triggered it — counting it either way would lie).
- **Unmeasurable** — no metrics file for the day, or the metrics/annotations cross-check above fails.

A window metric is computed over measurable days only and reported with its coverage: `{"value": 3, "measurable_days": 26, "window_days": 28}`. Aggregations skip null rows; nothing averages a null as zero. The Value view surfaces coverage ("26 of 28 days measurable") so a degrading pipeline is visible as a *measurement* problem, not a fake usage drop.

**Measurability floor:** a window with < 80% measurable days (e.g. < 23 of 28) renders the window's metrics null and the bot's utilization state `unmeasurable` (§6.2).

---

## 5. Computation, storage, cadence

### 5.1 Where it runs

- **Option A — post-step of the nightly measure job (recommended).** `ai.openclaw.evolve.measure` already runs daily at 01:00 as the `evolve` user and produces the exact per-day inputs the baseline consumes. The baseline runs as a step after per-bot aggregation completes. No new LaunchDaemon, no new plist (consistent with the Phase-C plist consolidation direction), and the freshest possible anchor date.
- **Option B — separate daily launchd job.** Cleaner isolation, but adds a fleet-management surface (plist via the `JobSpec` renderer, monitor-coverage registration, log rotation) for a computation that takes well under a second per bot.

A standalone CLI entrypoint exists either way: `python3 -m value_baseline --shared-dir {shared_dir}` (module at `packages/analyzer/value_baseline.py`), with `--rank` for the human-readable ranked table (proof artifact, §9) and `--bot-id` for a single-bot run. Manual runs and the nightly step share one code path.

### 5.2 Rollup file

One pod-wide file per day, written by the nightly run with the standard temp-file + rename atomic write, owned by `evolve` (no sudo, no /tmp staging — it lives under `{shared_dir}`):

```
{shared_dir}/metrics/value/{YYYY-MM-DD}.json
{
  "version": 1,
  "computed_at": "2026-06-10T01:12:03+00:00",
  "anchor_date": "2026-06-09",
  "bots": {
    "<bot_id>": {
      "active_human_days_7d":  {"value": 4,  "measurable_days": 7,  "window_days": 7},
      "active_human_days_28d": {"value": 17, "measurable_days": 27, "window_days": 28},
      "proactive_runs_7d":     {"value": 7,  "measurable_days": 7,  "window_days": 7},
      "proactive_runs_28d":    {"value": 28, "measurable_days": 27, "window_days": 28},
      "app_coverage_28d":      {"value": 0.6, "apps_total": 5, "apps_used": 3},
      "value_trend_28d":       {"value": 3,  "current": 45, "prior": 42},
      "usage_breadth_28d":     {"value": 6},
      "age_days": 212,
      "utilization_state": "active" | "underused" | "unmeasurable",
      "state_reason": "<one line, e.g. 'no human use and no scheduled app runs in 28 measurable days'>"
    }
  }
}
```

`utilization_state` is computed **once, here** — the tile chip, the Value view, and the Signal producer all read this field rather than re-implementing the predicate. One predicate, three consumers; no drift between what the chip shows and what the alert says. Retention: prune rollups older than 90 days in the same pass (matches signal-archive retention; the trend only needs 56d).

### 5.3 Staleness

The Value view and tile block display the rollup's `anchor_date` ("as of June 9") and flag a rollup older than 48h as stale — measurement degradation must be visible, not silently rendered as last week's numbers (tri-state at the surface level).

---

## 6. The `bot_underused` Signal

### 6.1 Identity

| Field | Value | Notes |
|---|---|---|
| `producer` | `value_baseline` | |
| `type` | `bot_underused` | |
| `signature` | `value_baseline:bot_underused:{bot_id}` | via `make_signature()`; find-or-create dedup means at most one active Signal per bot, ever — re-observations bump `observation_count`, they don't re-ping |
| `scope` / `bot_id` | `bot` / the bot | |
| `flavor` | `activity` | |
| `severity` | `info` (v1) | Decision below, §6.4 |
| `category` | `hygiene` | Advisory bucket; open question §11.1 proposes a future `value` category |

### 6.2 Tri-state honesty

Per [principle-tri-state-status](principle-tri-state-status.md), the producer distinguishes three states per bot and **only one of them fires**:

| State | Meaning | Behavior |
|---|---|---|
| `active` | Measured; the bot is used (human days > 0 **or** proactive runs > 0) | No signal. `sweep_resolve` archives any prior firing for this bot |
| `underused` | **Measured, no use** — the predicate in §6.3 holds | `signals.store.observe()` fires/bumps `bot_underused` |
| `unmeasurable` | **Can't measure** — below the 80% measurability floor, or the bot is younger than the age gate | **No `bot_underused` fires.** The state is recorded in the rollup and shown in the Value view as "not enough data" — an unmeasurable bot must never be reported as unused |

Fleet-level measurement failure is its own condition: if **more than half the fleet** is `unmeasurable`, the producer fires a single pod-scope `value_baseline_coverage` Signal (`warn`) — that shape means the annotation pipeline is broken, not that the bots are idle. This is the "distinguish tooling failure from findings" rule applied at the producer level; per-bot unmeasurable entries stay quiet to avoid N copies of one pipeline problem.

### 6.3 The firing predicate (v1: strict)

Fire `bot_underused` for a bot when **all** hold:

1. `age_days ≥ 28` — first-ever metrics file at least 28 days old. New bots are *onboarding*, not underused (U1's job).
2. Window measurability ≥ 80% (§4.3).
3. `active_human_days_28d == 0` — no person used it in four measured weeks.
4. `proactive_runs_28d == 0` — and it delivered nothing proactively either.

**Options considered:**

- **A — zero/zero over 28d (recommended).** Fires only on the unambiguous case: the bot does nothing for anyone. Precision over recall — per instrument-outcomes, the signal must be *defensibly right* before anything is allowed to tune thresholds. Both §2 success modes are protected by construction: the quiet one-verb bot has human days; the silent briefing bot has proactive runs. Neither can fire.
- **B — soft thresholds (≤ 2 human-days and ≤ 1 run/week).** Catches "barely used", but every threshold is an argument waiting to happen and a Goodhart surface. Deferred until A is validated and we have ranked-baseline data to calibrate against.
- **C — trend-triggered ("was active, went quiet").** Valuable ("value decaying" is roadmap language) but needs 56d of clean history and careful vacation/seasonality handling. Deferred; `value_trend_28d` is already collected so C can be evaluated offline first.

### 6.4 Severity and chat routing — decision

The chat notifier pushes every firing Signal from a non-excluded producer regardless of severity (severity affects emoji and Alerts-page default filtering, where `info` sits behind a toggle). Because the signature dedups per bot, the worst case is **one chat message per underused bot, ever** (plus one on recovery).

- **Option A — `info`, loud in chat (recommended).** One advisory ping per idle bot is squarely "worth saying" (operator-message-style §5); the find-or-create dedup makes repeat-noise structurally impossible; dismissal is a native, durable opt-out (§6.5). On the Alerts page it stays in the advisory tier, which matches its nature.
- **Option B — `info` + producer excluded from chat during burn-in.** Safer-looking, but it recreates the historical silent-producer failure mode on purpose, and the proof artifact (§9) already validates before the fleet sees anything. Rejected.
- **Option C — `warn`.** Overstates it: nothing is broken. The promotion question (after the signal proves precise: is an idle bot a `warn`?) is open question §11.2.

### 6.5 Lifecycle

- **Fire/bump:** nightly run calls `signals.store.observe()` for each `underused` bot. Existing active Signal → count bump, no new ping.
- **Recovery:** the run ends with `signals.store.sweep_resolve(producer="value_baseline", kept_signatures={underused bots})` — a bot that came back into use auto-resolves, and the notifier sends the standard 🟢 recovery message.
- **Operator opt-out:** **dismissal is the opt-out.** The store's dismissed-signature suppression (observe() bumps dismissed Signals in place, never re-creates) means "this bot is deliberately dormant" is one click, durable, and needs no config knob. Snooze covers "ask me again later" (e.g. a seasonal bot).

### 6.6 Producer registration steps — the silent-firing trap, named explicitly

Historically, new Signal producers fired silently because the chat notifier used an allowlist (`_DEFAULT_PRODUCERS`) that nobody remembered to extend. **That model has been inverted**: `alerts/signal_notifier.py` is now deny-list-by-default (`_DIRECT_DISPATCH_PRODUCERS`) — a new producer is loud automatically. The registration checklist under the current model, in full, so none of it is rediscovered at build time:

1. **`packages/analyzer/signals/producer_severity.py::PRODUCER_SEVERITY`** — add `"value_baseline": "info"` explicitly. Unmapped producers fall through to `warn`; relying on the fallback both mis-tiers the signal and hides the policy decision from review.
2. **`packages/analyzer/schema/signal.py::PRODUCER_CATEGORY_DEFAULT`** — add `"value_baseline": "hygiene"`. The fallback (`platform`) would file value advisories under platform health on the Alerts page.
3. **Chat routing — deliberately do nothing.** Do **not** add `value_baseline` to `_DIRECT_DISPATCH_PRODUCERS` or to the `alerts.signal_notifier.excluded_producers` stock default in `config_sandbox/schema.py` (either would silence it), and do **not** also call `dispatcher.send()` directly (the deny-list exists exactly because direct-dispatch + notifier double-messages). The build PR should state this no-op explicitly so a reviewer can check it.
4. **Run-coverage visibility.** Under the recommended Option-A runner (§5.1), the baseline inherits the measure job's liveness story, and the rollup file's `anchor_date` staleness flag (§5.3) is the user-visible backstop. If Option B (separate job) is chosen instead, add the job to `monitor_coverage.py::WATCHED_DAEMONS` — paired, per that file's contract, with on-host verification that the job logs a line on every successful wake.
5. **Operator-facing copy** in `observe(title=, body=)` passes the Plex test (§7.3) — title ≤ 80 chars (store clamps at 120).

---

## 7. Surfacing

### 7.1 Bot tile

The tile JSON gains a `value` block (the §5.2 per-bot entry, minus internals), and an `underused` health chip renders when `utilization_state == "underused"`:

- chip: `{"id": "underused", "severity": "warn", "horizon": "ongoing", "digest_tier": "tile_only"}` — chip severity vocabulary is warn|critical only; `tile_only` keeps it out of the daily digest (it is standing state, exactly the repeat-forever class that tier exists for).
- The chip reads `utilization_state` from the latest rollup — it must not re-implement the predicate (§5.2).

### 7.2 The Value view

- **Option A — section on the Improvements page (recommended for v1).** "Is this pod underused?" is an *Improve*-bucket question, and U3's suggestions will land on this page later — the value table is their future context. One ranked table: bot, state, active human days (28d), scheduled app runs (28d), app coverage, trend, coverage %. Sort = the proof-artifact key (§9).
- **Option B — panel on Overview.** Highest visibility, but Overview is tile-dense and the tiles already carry the per-bot block; a second rendering on the same page is redundant.
- **Option C — dedicated Value page.** Premature for five numbers per bot; revisit when U3 ideas need a home.

### 7.3 Operator copy (Plex test)

No internal vocabulary in any of this — "baseline", "signal", "producer", "tri-state", "measurable days" don't appear. Reference copy:

Chat, on fire (ℹ️):

> ℹ️ **A bot has been idle for 4 weeks**
> Nobody has used *personal-bot* since May 13, and it has no scheduled jobs delivering anything.
> If it's waiting on setup, connect it to a chat channel. If it's not needed, you can remove it.
> Dismiss this and we won't bring it up again.

Chat, on recovery (🟢): "*personal-bot* is in use again — no action needed."

Tile chip label: `Idle 4 weeks` · detail: `No one has used this bot recently and it isn't delivering anything on a schedule.`

Value view, unmeasurable state: `Not enough data to tell — usage records cover only 14 of the last 28 days.` (Plain words for the tri-state distinction; never "no use".)

---

## 8. Weekly "what your pod did for you" digest — recommendation

The spec owns this decision (roadmap §7.1). **Recommendation: yes, in-mandate — weekly, default-on with a one-click opt-out — but as its own small slice after the baseline has soaked, not inside U0.**

**Why it's in-mandate, not noise.** Operator-message-style's "silence is the default" rule explicitly carves out scheduled summaries: *"Summaries are the exception … the product, not noise."* The field data makes the case stronger: week 3 is the conversion point where a pod becomes infrastructure, and the digest is the mechanism that makes the pod's invisible work *visible* exactly during that window. A briefing that arrives daily at 7am disappears into routine; "your pod delivered 14 briefings, watched 2 inboxes, and saved you N interruptions this week" is what an operator quotes when deciding the pod is worth keeping. It is also the natural honest channel for "one of your bots did nothing this week" — the same fact `bot_underused` states, in a gentler weekly frame.

**Content contract (anti-Goodhart applies to prose too):**

- Report **delivered facts only**: briefings delivered, watchers fired, days the operator used each bot, apps that ran. Counts of *outcomes*, never turn volume, never cost framed as effort, never self-praise.
- **Honest zeros**: a bot that did nothing appears as "did nothing this week", not omitted.
- Tri-state at the prose level: if measurement was degraded, the digest says "usage records were incomplete this week" rather than presenting partial numbers as totals.
- Plex-test copy throughout; one message, weekly cadence (Monday morning, after the Sunday-night rollup).

**Why not in U0 proper:** the digest needs 2+ weeks of trustworthy rollups to have anything honest to say, and `tile_metrics.py` already anticipates a planned weekly bot-trends digest (the `horizon: "7d"` / `digest_tier` machinery). Shipping two separate weekly messages would violate the noise rule, so the value digest and the bot-trends digest must be **one message** — that consolidation is its own slice (see §12), with the baseline rollup as its data source.

---

## 9. Validation plan and proof artifact

Per instrument-outcomes, the signal must demonstrably discriminate before anything consumes it.

1. **Run the baseline on the live pod** (`python3 -m value_baseline --rank`) and capture the ranked table. Rank key (explicit, not a published score): `utilization_state` (underused last), then `active_human_days_28d`, then `proactive_runs_28d`.
2. **Hand-verify the ranking** against operator ground truth: the daily-driver bots rank top; the bot known never to have been onboarded to a channel ranks bottom and is the expected `bot_underused` firing.
3. **Positive control:** the never-onboarded bot fires, with `state_reason` quoting the evidence (0 human days, 0 scheduled runs, N measurable days).
4. **Negative controls:** (a) a low-volume but regularly-used bot does **not** fire (active-human-days catches it); (b) a zero-human-turn bot with a daily scheduled briefing does **not** fire (proactive runs catch it).
5. **Tri-state control:** with a bot's annotations directory temporarily renamed on a test run, that bot reports `unmeasurable` — not `underused` — and the fleet-coverage signal fires if pushed past 50%.
6. **Proof artifact:** the ranked table + the one firing (and its chat rendering), written up in the build PR. Only after this passes may any later phase wire a machine consumer (§2.2).

If step 3 or 4 fails, the metric definitions are wrong — fix the baseline, don't tune the predicate until it fits (the principle's "detector doesn't fire → maybe the design is wrong" clause).

---

## 10. Non-goals

- **No prescriptions.** The baseline says *whether* a bot is used, never *what to change* — that is U3, which will cite these numbers as evidence.
- **No new data collection, no LLM calls, no per-turn hooks.** Reads existing stores only.
- **No composite score** (§2.1) and no cross-pod benchmarking ("your pod vs others") — out of scope and against the privacy posture.
- **No delivery confirmation** — v1 counts scheduled runs honestly labeled as such; U2.1 owns delivery windows.
- **No automated consumers** until §9 passes.

---

## 11. Open questions

1. **A `value` Alerts-page category?** `hygiene` is the least-wrong existing bucket for `value_baseline`. If U2.1 (delivery monitor) and U3 (effectiveness) also emit user-value signals, a dedicated `Category = "value"` earns its schema bump. Proposed: revisit in the U2.1 spec; keep `hygiene` until there are ≥ 2 value producers.
2. **Severity promotion.** After the predicate proves precise over a few weeks, does `bot_underused` graduate `info → warn` (it is actionable: onboard or retire)? Leaning yes-eventually; decide on calibration data, not now.
3. **Predicate B/C activation.** When (if ever) do soft thresholds or trend-triggered firing turn on? Proposal: evaluate offline against accumulated rollups; bring findings to a later design sync rather than pre-committing.
4. **`apps.used` source fidelity.** App-usage attribution in daily metrics has known coarseness (manifest-name matching). Good enough for coverage-as-context; flag if it ever enters a predicate.
5. **Member-visible variant.** Should a bot's primary *user* (not the operator) ever see their own usage summary? Out of U0 scope; touches the per-bot sysadmin-audience question. Park until the digest ships for operators.

---

## 12. Build slices (post design-sync)

| Slice | Content | Effort |
|---|---|---|
| B1 | `packages/analyzer/value_baseline.py`: metrics + rollup file + CLI `--rank`; measure-job post-step; tests incl. tri-state day classification | S |
| B2 | `bot_underused` producer: predicate, observe/sweep_resolve, fleet-coverage signal, registration steps §6.6, copy | S |
| B3 | Surfacing: tile `value` block + chip, Improvements-page Value section (style-guide §9 compliant, both themes) | S |
| B4 | Proof artifact run on the live pod + write-up (§9) | S |
| B5 *(separate, after 2-week soak)* | Weekly digest: one consolidated message (value + 7d bot trends), default-on, opt-out | S/M |

B1–B4 are one PR-sized arc each, sequenced; B5 waits for soak per §8.
