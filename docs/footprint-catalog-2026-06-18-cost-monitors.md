# Footprint catalog — F-1c: cost / monitoring footprint

**Date:** 2026-06-18 · **Aspect:** `META:footprint` · **Slice:** F-1c (cost / monitors)
**Parent spec:** [docs/spec-footprint-2026-06-18.md](spec-footprint-2026-06-18.md)
**Sibling slices:** F-1a deploy/privilege · F-1b runtime/hot-path · F-1d config-mutation/appliers

This catalogs **every monitor, signal producer, scanner, audit, generator, and cost-breaker** —
the question this slice resolves: *which Evolve "monitoring" is cheap read-only observation, and
which spends tokens?* "Just monitoring" can still bill the operator, so the cost posture of each
item is made explicit. Each item is tagged on the [four footprint dimensions](spec-footprint-2026-06-18.md)
(**Mutation** = changes how OC behaves · **Runtime** = intercepts the hot path · **Cost** = spends
tokens · **Privilege** = daemons/sudoers/ACLs) and its current toggle-state.

Everything here is grounded in code at the cited `file:line`. The headline finding: **the entire
signal-emission layer is effectively $0** — every documented signal producer is pure
Python/filesystem/subprocess/regex, consuming telemetry already on disk. Token spend is
concentrated in **four narrow, gated surfaces** (app scanner, tier-3 app audit, observation-tuple
extraction, and three gated-Haiku generators), plus the **cost breakers themselves**, which are $0
to run but are a *footprint of a different kind*: they **interrupt or throttle the bot** when they fire.

**Cost-posture legend** (notes column): `$0 read-only` · `occasional-LLM` (LLM only on a gated
condition) · `sweep-LLM` (LLM per item across a sweep) · `per-turn/per-session-LLM`.

---

## 1. Signal producers / monitors — the daily-driver observation layer

All producers below call `signals.store.observe()` ([store.py:479](../packages/analyzer/signals/store.py))
and/or `sweep_resolve()` ([store.py:826](../packages/analyzer/signals/store.py)). **Every one is
$0 read-only on the signal-emission path** — none import `anthropic`/`openai` or call
`bot_tool`/`openclaw_headless`/`.messages.create`. Schedules are from the `install_*` infra-job
definitions in [deploy.py](../packages/admin/evolve_admin/deploy.py).

| Touch / subsystem | What it does | Dimensions | Current toggle-state | Code refs (file:line) | Perf/cost/bug notes |
|---|---|---|---|---|---|
| **pod_report** | Daily pod operational triage (audit findings, gateways, anomalies) | Runtime (daemon) | Gated by per-bot launchd `ai.evolve.{bot}.pod-report-daily` (default on; daily) | observe [pod_report.py:1541](../packages/analyzer/pod_report.py); sweep :1556; sched [deploy.py:9176](../packages/admin/evolve_admin/deploy.py) | **$0 read-only**. Reads on-disk findings; no model call. Uses [pod_report_baseline.py](../packages/analyzer/pod_report_baseline.py) (pure math, 30-day rolling mean/stddev). |
| **audit** | Security/config/identity audit runner | Runtime (daemon) | Gated by launchd `ai.evolve.{bot}.audit` (default on; **every 15 min**) | observe [audit.py:3182](../packages/analyzer/audit.py); sweep :3203; sched [deploy.py:9353](../packages/admin/evolve_admin/deploy.py) | **$0 read-only**. The `anthropic` string hits (audit.py:2098-99) are API-key-leak detection regexes, not LLM calls. Highest-frequency producer (15 min). |
| **cost_watchdog** | Cost-antipattern monitor: 7 detectors (daily_spend_high, cost_spike, session_quality, automation_dominance, cron_wakes_agent, cron_overactive, context_bloat) | Cost-observe, Runtime (daemon) | Gated by launchd `ai.evolve.{bot}.cost_watchdog` (default on; hourly, jitter 300s) | observe [cost_watchdog.py:3653](../packages/analyzer/cost_watchdog.py); sweeps :3754/:3769; detectors :882/:956/:1450; sched [deploy.py:9851](../packages/admin/evolve_admin/deploy.py) | **$0 read-only** — header line states "Reads on-disk telemetry only (no LLM)" ([cost_watchdog.py:2](../packages/analyzer/cost_watchdog.py)). Conservative thresholds (daily_spend_usd=3.0, context_bloat_kb=30). Feeds the breaker/cap paths indirectly. |
| **exec_outcome_watchdog** | Tool-error pattern detection (tool_error_burst, exec_denied, approval_timeout) | Runtime (daemon) | Gated by launchd `ai.openclaw.evolve.outcome.{bot}` (default on) | observe [exec_outcome_watchdog.py:1011](../packages/analyzer/exec_outcome_watchdog.py); sweep :1058; install [deploy.py:8868](../packages/admin/evolve_admin/deploy.py) | **$0 read-only**. Gateway-log/turn regex only. |
| **sysadmin_watchdog** | Platform inspection (gateway health, plugins, launchd, ACLs) | Privilege-observe | Gated (generator observe; sweep + signal-subscriber) | observe [generators/sysadmin_watchdog/observe.py:223](../packages/analyzer/generators/sysadmin_watchdog/observe.py) | **$0 read-only**. Pure platform inspection. |
| **evolve_watchdog** | Dual-writes WatchdogEvent → Signal store | Runtime | Gated (called by heal/watchdog_alerts emitters) | observe [generators/evolve_watchdog/events.py:103](../packages/analyzer/generators/evolve_watchdog/events.py) | **$0 read-only**. JSONL + Signal I/O. |
| **host_health** | Host CPU/mem/disk (psutil) + firewall/sleep state | Privilege-observe, Runtime (daemon) | Gated by host-health launchd (default on) | observe [host_health.py:280](../packages/admin/evolve_admin/host_health.py); sweep :345 | **$0 read-only**. subprocess sysctl/socketfilterfw only. |
| **error_reporter** (`reporter.py`) | error_spike Signals from admin-server log tail | Runtime | Unconditional (admin-server internal) | observe [reporter.py:279](../packages/admin/evolve_admin/reporter.py) | **$0 read-only**. Log fingerprinting. |
| **integration_probe** (Slack) | Slack auth/config reconciliation → Signals | Privilege-observe | Gated (integration probe / doctor) | observe [integrations/slack/probe.py:155](../packages/analyzer/integrations/slack/probe.py) | **$0 read-only**. Slack SDK (API), not LLM. |
| **pod_health** (`pod_health_runner.py`) | 1-min gateway liveness via state machine | Runtime (daemon) | Gated by launchd `ai.evolve.{bot}.pod-health` (default on; **every 60s**) | delegates to `health.run_pod_health_signals_only()`; sched [deploy.py:9221](../packages/admin/evolve_admin/deploy.py) | **$0 read-only**. No LLM imports. Highest-frequency (60s) but trivially cheap. |
| **security_warden** | Workspace / prompt-injection scanning → Signals + Proposals | Cost (gated), Privilege-observe | Gated (generator observe; sweep + signal-subscriber); LLM verifier gated + fails open | observe [generators/security_warden/observe.py:223](../packages/analyzer/generators/security_warden/observe.py) ($0); LLM [scanners/llm_verifier.py:168](../packages/analyzer/generators/security_warden/scanners/llm_verifier.py) (`claude-haiku-4-5`) | **HYBRID**: signal path $0; the *proposal* path **optionally** calls Haiku to score prompt-injection confidence — `occasional-LLM`, only when the regex stage matches ([llm_verifier.py:160](../packages/analyzer/generators/security_warden/scanners/llm_verifier.py)), fails open to regex-only. ~50-100 tokens/verified injection. |
| **test_runner** | (app-test failure signals) | — | **RETIRED 2026-06-08** | [alerts/catalog.py:1422](../packages/analyzer/alerts/catalog.py) "system.test_runner_failures retired" | No spend. App-test surface killed (see [[project_app_test_surface_killed_2026_06_08]]). Listed in CLAUDE.md but no longer live. |

### Additional $0 producers (beyond the CLAUDE.md list)

~28 more monitors/audits/signals call `observe()`/`sweep_resolve()` and are **all $0 read-only** on
the signal path (pure Python / FS / subprocess / regex / non-LLM HTTP). Each is gated by its own
per-bot or pod-wide launchd job. Frequencies from [deploy.py](../packages/admin/evolve_admin/deploy.py):

| Producer | Freq | observe@ | | Producer | Freq | observe@ |
|---|---|---|---|---|---|---|
| spend_alert.py | 5 min | :457 | | home_artifacts_monitor.py | hourly | :548 |
| delivery_monitor.py | 5 min | :2393 | | deploy_drift_monitor.py | hourly | :290 |
| session_economics.py | hourly | :519 | | bot_recovery_monitor.py | hourly | :248 |
| embedding_monitor.py | hourly | :619 | | code_quality_monitor.py | daily | :529 |
| oc_substrate_monitor.py | hourly | :384 | | install_integrity_monitor.py | daily | :495 |
| pod_perms_drift_monitor.py | hourly | :218 | | monitor_coverage.py | daily | :403 |
| stuck_proposal_monitor.py | hourly | :234 | | agent_bypass_audit.py | daily | :916 |
| alerts_loop_monitor.py | hourly | :189 | | monitor_gmail_integration_health.py | 30 min | :422 |
| reconcile_audit.py | hourly | — | | digest_source_audit.py | — | :352 |
| backup_signal.py | hourly | :705 | | backup_audit_signal.py | hourly | :314 |
| local_backup_signal.py | hourly | :243 | | cascade/audit_runner.py | hourly | :314 |
| content_scan/scanner.py | — | :484 | | applications/scanner.py (`_emit_compliance_signals`) | — | :6129 |

Notes: `content_scan/scanner.py` has **zero LLM references** (grep-confirmed). `applications/scanner.py`'s
**signal-producing** path (`scan_compliance_all` → `_emit_compliance_signals`) consumes pre-computed
structural `issues` and is **$0** — distinct from its app-*discovery* path which is costed (§2).
`backup_signal.py` uses the GitHub API (HTTP), not an LLM. `spend_alert.py` (5-min burst/cap detector)
reads `turns-{date}.jsonl` only — but it is also the **auto-trip** for the L1 cost breaker (§4).

---

## 2. App scanner & app audit pipeline — the LLM-costed inventory layer

This is where most monitoring token-spend actually lives. Two **distinct LLM invocation paths**:
the **scanner** calls the Anthropic API *directly* (urllib → api.anthropic.com, keyed off the bot's
`auth-profiles.json`), so its cost is invisible to the OpenClaw turn ledger; the **tier-3 audit**
dispatches via `openclaw agent --local` subprocess, so its cost routes through OC billing and is
recoverable from `turns-*.jsonl`.

| Touch / subsystem | What it does | Dimensions | Current toggle-state | Code refs (file:line) | Perf/cost/bug notes |
|---|---|---|---|---|---|
| **App scanner — discovery** | LLM clusters a bot's workspace inventory into application candidates | Cost | Gated (on-demand only; **no recurring schedule**) | [applications/scanner.py:1116](../packages/admin/evolve_admin/applications/scanner.py) (`_resolve_tier("tier3")`), call :1262, :1689 | **occasional-LLM**: 1 `tier3`/Haiku call per scan (whole workspace, one prompt, max_tokens 4096). Triggered by CLI `application scan`, `POST /api/applications/scan`, or evo `action.scan.run` — **never auto-recurs**. Per-bot fcntl lock; `min_confidence` filter; aborts on missing key. |
| **App scanner — purpose/fit classifier** | Classifies each NEW manifest goal-app vs capability/skill | Cost | Gated (only fires when `use_llm and classifier_api_key`) | [scanner.py:1307](../packages/admin/evolve_admin/applications/scanner.py) (`_CLASSIFIER_TIER="tier2"`), loop :3287, reconcile :3686 | **sweep-LLM (bounded)**: 1 `tier2`/Sonnet call per *new* app in the scan. Fail → inert "application" default, retried next scan. |
| **App scanner — heartbeat enrichment** | Optional LLM pass relating heartbeat sections to apps | Cost | Gated (`if api_key:`; deterministic fallback) | [scanner.py:2544](../packages/admin/evolve_admin/applications/scanner.py), fallback :2021 | **occasional-LLM**: ≤1 `tier3` call, 90s timeout; pure-Python `_candidate_relates_to_app` fallback when off/failed. |
| **Tier-2 structural audit** | Pure-Python reality checks of manifest claims (files exist, sha, cron/launchd cross-check, orphan installs, coherence Pass A/C1) | Privilege-observe, Runtime (daemon) | Gated by per-bot launchd `ai.openclaw.evolve.audit-runner.{bot}` (default on; **every 6h**, `StartInterval=21600`) | docstring [app_audit_structural.py:1](../packages/analyzer/app_audit_structural.py); run_tier2 [app_audit_runner.py:822](../packages/analyzer/app_audit_runner.py); daemon [deploy.py:8713](../packages/admin/evolve_admin/deploy.py) | **$0** — "No LLM, no network, no admin-server roundtrip." subprocess only to local `crontab`/`launchctl`/`openclaw cron list`/git. Sweeps every manifest each run. |
| **Tier-3 semantic audit** (Stage 3a Discovery + 3b Triage) | LLM reads manifest claims + actual code, emits divergence observations, triages each → dismiss/auto_fix/propose | Cost, Runtime (daemon) | Gated by per-bot launchd `ai.openclaw.evolve.audit-runner-t3.{bot}` (default on; hourly poll, `run_at_load=False`, jitter 1800s) — but **cadence-gated so most ticks no-op** | tier3 [app_audit_tier3.py:986](../packages/analyzer/app_audit_tier3.py)/:1042/:1077; run_tier3 [app_audit_runner.py:1018](../packages/analyzer/app_audit_runner.py); daemon [deploy.py:8792](../packages/admin/evolve_admin/deploy.py) | **sweep-LLM (heavily gated)**: 2 `openclaw agent` LLM dispatches (3a + 3b) per *audited, due* app, at the bot's default agent model (`power` for full-tier bots; **first audit pinned to `standard`**, ~4-5× cheaper). Default `audit_cadence=monthly` → most hourly ticks are no-ops. See gate stack below. |
| **Auto-fix executor** | Applies whitelisted safe transformations from Stage 3b | Mutation | Gated (calibration mode default ON demotes all auto_fix→propose) | [app_audit_executor.py](../packages/analyzer/app_audit_executor.py); cap [app_audit_runner.py:1190](../packages/analyzer/app_audit_runner.py) | **$0** (pure transform). `max_auto_fix_per_run=3`; 4 whitelisted kinds; calibration (default on) means nothing is applied automatically. |
| **Audit scheduler** (admin-side poller) | Drains/polls bot-side audit results | Runtime (daemon) | Gated by launchd `ai.evolve.evolve.audit-scheduler` (default on; hourly) | [applications/audit_scheduler.py](../packages/admin/evolve_admin/applications/audit_scheduler.py) | **$0**. Pure Python poller. |

**Tier-3 cost gate stack** (all in [app_audit_runner.py](../packages/analyzer/app_audit_runner.py)) — the load-bearing controls that keep the hourly daemon from sweeping every app every hour:
- **Cadence gating** `_is_tier3_due` honors per-app/pod `audit_cadence` (default `monthly`; `never` short-circuits) — :300/:1080.
- **First-audit deferral (D)** never-audited app <7d old (`_FIRST_AUDIT_GRACE_DAYS`) is deferred — :352/:1093.
- **Provisioning budget (B)** first audit checks one-time ceiling + daily cost breaker via `provisioning_budget.evaluate` — :394/:1121.
- **First-audit model pin (C)** first audit resolves `standard` not the bot's `power` rung — :442/:1151.
- **Per-run ceilings** `max_tokens_per_audit=100_000`, `max_proposals_per_run=5`, `max_auto_fix_per_run=3`; per-tick bail at `max_tokens*10` — :236/:1327.
- **Calibration mode (default ON)** demotes every auto_fix→propose — :1183.

**The "Tier-3 Sonnet bleed" incident** ([[project_tier3_audit_bleed_2026_05_20]]) — root cause + fixes in [app_audit_tier3.py:554-683](../packages/analyzer/app_audit_tier3.py) (`_dispatch_via_oc_full`): `subprocess.run(timeout=)` only SIGKILLed the immediate child while `openclaw` forked `openclaw-agent` workers that kept running and finished their already-billed call. Fixes: `start_new_session=True` + `os.killpg` whole group (`_kill_process_group` :718); `_recover_cost_from_turn_observer` scans `turns-<today>.jsonl` filtering `channel=="unknown"` (:744); timeouts cut 600/300s → 180s (`_DISCOVERY/_TRIAGE_TIMEOUT_S` :81); `_MESSAGE_MAX_CHARS=200_000` refusal (:87); fresh per-dispatch UUID session-id (:619). Forensics: `docs/forensic-team_bot_a-apply-bleed-2026-05-21.md`.

---

## 3. Generators (RSI) & observation tuples — mostly $0, four narrow LLM surfaces

The generator runner is a **15-min poll** (launchd `ai.openclaw.evolve.better`, `StartInterval=900`,
runs as `evolve`; [plist:21](../packages/admin/evolve_admin/plists/ai.openclaw.evolve.better.plist)),
but each generator only `observe()`s when its own cadence (`hourly`/`daily`/`weekly`) elapses
(`_is_due` [generator_runner.py:46](../packages/analyzer/generator_runner.py)). **27 of ~28 runner
generators are $0 pure-Python** — they consume Signals that monitors already wrote, then template
Proposals. "Pure-Python by default, LLM is escalation" ([[feedback_rsi_low_cost_preference]]) is
confirmed: the only `client.messages.create` call sites in `generators/` are three gated Haiku calls.

| Touch / subsystem | What it does | Dimensions | Current toggle-state | Code refs (file:line) | Perf/cost/bug notes |
|---|---|---|---|---|---|
| **generator_runner sweep** | Polls all generators; runs those whose cadence elapsed | Runtime (daemon) | Gated by launchd `ai.openclaw.evolve.better` (default on; **15 min** poll) + `.refresh-urgent` WatchPath | [generator_runner.py:1108](../packages/analyzer/generator_runner.py); sched [plist:21](../packages/admin/evolve_admin/plists/ai.openclaw.evolve.better.plist) | The poll is $0; cost is whatever the due generators incur (almost always $0). |
| **security_warden** generator | Prompt-injection / workspace scan | Cost (gated) | Gated; hourly cadence | factory [generator_runner.py:298](../packages/analyzer/generator_runner.py); LLM [llm_verifier.py:168](../packages/analyzer/generators/security_warden/scanners/llm_verifier.py) | **occasional-LLM**: Haiku only when regex stage matches; fail-open to regex-only with no key. |
| **model_discovery** generator | Places newly-discovered models into role rungs | Cost (gated) | Gated; daily cadence; default fit-classifier injected None in tests | gate [generator_runner.py:196](../packages/analyzer/generator_runner.py); LLM [fit_llm.py:275](../packages/analyzer/generators/model_discovery/fit_llm.py) | **occasional-LLM**: Haiku-tier fit classifier **only when a new model needs placement** (0 discovered ⇒ 0 calls). Deterministic verdict is authoritative; LLM can't override real-price fit ([observe.py:399](../packages/analyzer/generators/model_discovery/observe.py)). Fail-open. |
| **user_profile_inferrer** generator | Extracts user-profile facts from sessions | Cost (gated), Privilege | Gated; **per_session** (bot-side session_end hook, NOT central runner); `honors_dnt: true`, `emits_proposals: false` | LLM [extractor.py:293](../packages/analyzer/generators/user_profile_inferrer/extractor.py) (`claude-haiku-4-5`) | **per-session-LLM**: Haiku on the **bot's own creds**. DNT short-circuits *before* any LLM call. Runs inside each bot, not the central sweep. |
| **All other ~24 generators** | budget_hawk, efficiency_hawk, cost_spike, cost_root_cause_correlator, bloat_investigator, exec_outcome_investigator, gateway_diagnostician, cache_ttl_tuner, session_quality, manifest_quality, workspace_inventory, workspace_security, auth_drift_filler, cron_caps_filler, bot_config_integrity, app_birth_detector, app_permission_drift, app_permission_review, app_suggester, persona_tuner, engagement_amplifier, pod_capability_lift, autonomy_promoter, sysadmin_watchdog | Runtime | Gated by cadence; default on | e.g. budget_hawk "no LLM" [observe.py:6](../packages/analyzer/generators/budget_hawk/observe.py); app_suggester "v1 is pure Python — no LLM" [__init__.py:9](../packages/analyzer/generators/app_suggester/__init__.py) | **$0 read-only**. Consume Signals / cost-ledger / observation-window files; template Proposals. `profile_inferrer/` and `weight_inferrer/` dirs are **empty/dead** (no charter). `test_gate_backfill`, `test_failure_responder`, `plugin_curator`, `primary_model_floor_advisor` were **deleted** ([generator_runner.py:1065](../packages/analyzer/generator_runner.py)). |
| **signal-subscriber dispatch** | Event-driven generator fan-out on firing Signal | Runtime (daemon) | Gated by launchd `ai.evolve.evolve.signal-subscriber` (KeepAlive; 1 Hz poll of `firing/`) | [signals/subscriber.py:264](../packages/analyzer/signals/subscriber.py); ledger guard :122 | **$0**: only **3** generators subscribe (`autonomy_promoter`, `engagement_amplifier`, `pod_capability_lift`) — all pure-Python. The LLM-costed generators do **not** subscribe, so the event path adds **latency, not cost**. Per-(gen, signal_id) ledger prevents re-dispatch. |
| **observation tuples** (noun×verb×mood×engagement) | LLM extracts tuples from session summaries | Cost, Privilege | Gated by launchd `ai.openclaw.evolve.tuples` (default on; **daily 01:30**, pod-wide single job) | extractor [observations/llm_extractor.py:152](../packages/analyzer/observations/llm_extractor.py) (`claude-haiku-4-5`); writer [extract_tuples.py:207](../packages/analyzer/observations/extract_tuples.py); sched [deploy.py:8876](../packages/admin/evolve_admin/deploy.py) | **sweep-LLM (bounded, NOT per-turn)**: 1 Haiku call (max_tokens 600) per *changed, non-trivial* **session** summary — extracted daily, not per turn. Guards: `DEFAULT_MAX_SESSIONS_PER_RUN=50` hard ceiling ([extract_tuples.py:56](../packages/analyzer/observations/extract_tuples.py)); `source_hash` dedup makes re-runs free; trivial sessions skipped; fixed verb/mood vocab. Worst case ≈ (#non-trivial sessions/day pod-wide) Haiku calls. |

---

## 4. Cost breakers / caps — $0 to run, but a *footprint of interruption*

These are not token-spenders (they read on-disk telemetry / flags before the LLM call). Their
footprint is the **opposite**: when they fire, they **interrupt, throttle, or downgrade the bot**.
That is a real Mutation/Runtime footprint an operator may want to dial — a breaker that pauses crons
or downgrades a model is "Evolve altering how OpenClaw behaves." There are **two distinct breaker
trees** — `packages/analyzer/breakers/` is the *proposal-risk* classifier (L1-L6), **not** a cost
breaker; the cost-interrupt tree is below.

| Touch / subsystem | What it does | Dimensions | Current toggle-state | Code refs (file:line) | Perf/cost/bug notes |
|---|---|---|---|---|---|
| **L1 cost breaker (daily_cap)** | On trip: removes `heartbeat.every` from openclaw.json (stashed for restore), kickstarts gateway, narrows exec-approvals to read-only allowlist so user turns still answer | **Mutation**, Runtime, Privilege | Gated: enforcement ON when a cap is set; cap **None by default** (graduated new-bot default applies) | [breakers_enforce.py:474-560](../packages/admin/evolve_admin/breakers_enforce.py); trip [spend_alert.py:991](../packages/analyzer/spend_alert.py) (`_trip_breaker_for_cost_cap`) | **$0 to run** (file edits + launchctl). **INTERRUPTS background work**: disables heartbeats + auto-crons; user-driven turns survive (gateway stays up). 24h TTL auto-reset. Highest-disruption interrupter short of full suspend. |
| **spend_alert.py daemon** | Burst detector + daily threshold + hard-cap auto-trip; reads `turns-{date}.jsonl` | Cost-observe → triggers Mutation | Gated by launchd (default on; **every 5 min**, `StartInterval=300`) | header [spend_alert.py:1](../packages/analyzer/spend_alert.py); install [deploy.py:9024](../packages/admin/evolve_admin/deploy.py) | **$0 read-only** itself; its hard-cap check is what *trips* the L1 breaker above. |
| **Runaway-rate hard cap** (ModelRouter) | Per-session tripwire: if rolling-window cost > `dollarsPerWindow` within `windowMinutes`, forces `fast`/tier3 for the rest of the session regardless of consent | **Mutation**, Runtime | **ON by default** (`enabled !== false`); defaults `dollarsPerWindow=20.0`, `windowMinutes=5` | [ModelRouter.ts:981-997](../packages/plugin/src/observer/ModelRouter.ts); default-true :1540; precedence :2701 | **$0** (in-memory cost tracking precedes the LLM call). **THROTTLES** (forced model downgrade, not a kill). Catches stuck loops. Cross-ref F-1b runtime catalog. |
| **Spend-cap safety net** (ModelRouter + spend_caps.py) | Reads file-backed `isSpendCapActive` flag; if active forces `fast` for all sessions | **Mutation**, Runtime | Gated (ON when flag present); action selectable | flag read [ModelRouter.ts:47-59](../packages/plugin/src/observer/ModelRouter.ts); enforce [spend_caps.py:383-451](../packages/analyzer/spend_caps.py) | **$0**. **TIERED INTERRUPT**: action ∈ `alert-only` / `downgrade-tier` / `pause-crons` (launchctl bootout cron labels) / `suspend-bot` (full gateway suspend). |
| **Heartbeat dangerous-combo preflight gate** | Rejects the wasteful combo `lightContext=false AND every>=1h` (never cache-hits) before writing openclaw.json | Mutation (blocks a write) | Gated (always runs unless `force=True`, which emits a paper-trail Signal) | [cost_profiles.py:459-510](../packages/admin/evolve_admin/cost_profiles.py) | **$0**. Prevents a *future* cost footprint; doesn't interrupt a running bot. Cross-ref [[feedback_heartbeat_iso_light_immutable]]. |
| **cascade dangerous-combo detector** | Flags background-trigger + tier1 + cascade-decided + huge-context pattern | Cost-observe | Gated (advisory) | [cascade/anomaly_detector.py:17](../packages/analyzer/cascade/anomaly_detector.py); [cascade/audit_runner.py:129](../packages/analyzer/cascade/audit_runner.py) | **$0**. Advisory operator Signal, no interrupt. |
| **cascade anomaly_detector** (budget/anomaly) | Origin-aware anomaly classification vs rolling baseline (asymmetric multipliers by origin) | Cost-observe | **Phase 2 advisory-only** (computes verdicts, no live Signals/interrupt yet) | [cascade/anomaly_detector.py:1](../packages/analyzer/cascade/anomaly_detector.py) | **$0** pure function over spans. Not yet enforcing. |
| **cascade pressure_watchdog** (budget pacing) | Pod-wide pressure (concurrent tier1, escalations/15min, tier1 spend/hr) → `pressure_flags.json`; CascadeController freezes autonomous tier1 escalations when tripped | **Mutation**, Runtime | **Phase 2 shadow mode** (written, not yet consulted); 60s poll | [cascade/pressure_watchdog.py:1](../packages/analyzer/cascade/pressure_watchdog.py) | **$0**. When live: **THROTTLES** (escalation freeze); explicit user Power picks still honored. Dead-watchdog → fail-closed. |

**Cap config storage:** `network.json::bots.<bot>.budget.per_bot_daily_hard_usd` (L1 cap; legacy
`daily_cap_usd` still read at [spend_alert.py:853](../packages/analyzer/spend_alert.py)) +
`thresholds.*`. Runaway cap in `tiers.json::runawayRateCap`. **All caps are per-bot, per-day**;
per-app caps are abuse knobs only ([[feedback_per_app_vs_per_bot_cost_unit]]). The memory note about
a "haiku sentinel" ([[project_cost_alerting_blackout_2026_05_20]], OC#84825) is **not** an always-on
Haiku monitor — it is a *model-pin workaround* pinning one bot's primary model down to Haiku
([config_sandbox/schema.py:1160](../packages/admin/evolve_admin/config_sandbox/schema.py)); no
separate always-on Haiku watcher process spends tokens.

**Disruption ranking** (when fired): `suspend-bot` (gateway down) > L1 breaker (heartbeats+crons off,
user turns survive) > `pause-crons` > runaway/spend-cap model downgrade (throttle) > pressure-watchdog
escalation freeze > advisory Signals (no interrupt).

---

## Truly free observe-only (the Passive-mode floor)

These spend **$0** and only *read* — they define what a "Passive / dashboard mode" posture can keep
running with zero token cost. An operator on Passive gets the entire legibility/inventory/alert
layer for free:

- **Every signal producer / monitor in §1** — pod_report, pod_report_baseline, audit, cost_watchdog,
  exec_outcome_watchdog, sysadmin_watchdog, evolve_watchdog, host_health, error_reporter,
  integration_probe, pod_health — **plus all ~28 additional monitors/audits/signals**. None import
  `anthropic`/`openai`; all read on-disk telemetry / subprocess / regex.
- **Tier-2 structural app audit** (every 6h) — pure-Python manifest reality checks, $0.
- **The cost_watchdog 7-detector cost-antipattern monitor** — ironically the *cost* monitor is itself
  $0; it reads telemetry to *detect* spend without spending.
- **~24 of ~28 RSI generators** — all the pure-Python investigators/fillers/hawks that consume
  Signals and template Proposals.
- **The signal-subscriber event dispatch** — only fans out to the 3 pure-Python subscribers; $0.
- **All cost breakers/caps when not tripped** — they read flags/telemetry before the LLM call; $0 to
  run (but see the interruption caveat below).

The Passive floor is therefore **large and genuinely free**: signals, alerts, host/pod health,
structural audit, cost-antipattern detection, and most proposals are all observe-only. This is the
strongest evidence for the spec's "Evolve = legibility + inventory + managed updater at minimal
footprint" Passive level — almost the entire monitoring surface already qualifies.

**Caveat — $0-to-run ≠ zero-footprint.** The cost breakers (§4) are $0 to run but **interrupt or
throttle the bot when they fire** (Mutation/Runtime). A coherent Passive posture must decide whether
the *interrupting* breakers (L1 daily-cap heartbeat-disable, runaway downgrade, pressure freeze) are
"observe-only safety" or "active mutation" — per the spec guardrail, a posture that silently leaves a
breaker half-armed is worse than a clear decision either way.

## Costed monitors — candidates to gate / sample

These spend tokens. They are the surfaces a posture dial should be able to turn **off or sample** in
Passive, leave **on** in Standard/Managed. Ranked by realistic spend:

1. **Tier-3 semantic app audit** ([app_audit_tier3.py](../packages/analyzer/app_audit_tier3.py)) —
   the heaviest, 2 `openclaw agent` LLM dispatches per due app at the bot's `power` model. Already
   the most gated (cadence/eligibility/budget/calibration), and the historical bleed source. **Passive
   candidate:** disable entirely (Tier-2 structural still gives $0 manifest reality checks); **Standard:**
   monthly cadence, calibration-on (current default); **Managed:** faster cadence + auto-fix.
2. **App scanner discovery + classifier** ([applications/scanner.py](../packages/admin/evolve_admin/applications/scanner.py)) —
   on-demand only today, so no surprise recurring bill, but a `tier3` discovery call + per-new-app
   `tier2` classifier. **Passive candidate:** structural-only scan (skip the LLM clustering/classify
   passes — the deterministic fallbacks already exist for heartbeat enrichment).
3. **Observation-tuple extraction** ([observations/llm_extractor.py](../packages/analyzer/observations/llm_extractor.py)) —
   daily Haiku, ≤50 sessions/run pod-wide, dedup-gated. Cheap per-call but runs unconditionally daily.
   **Passive candidate:** off or sampled (tuples feed persona/engagement tuning, which is a
   Managed-tier concern anyway); **Standard:** keep (cheap, dedup-gated).
4. **security_warden Haiku verifier** ([llm_verifier.py:168](../packages/analyzer/generators/security_warden/scanners/llm_verifier.py)) —
   occasional, regex-gated, fails open. **Note the security floor:** disabling the *verifier* drops to
   regex-only injection detection, not zero detection — co-own the floor decision with `edr`/security
   (spec boundary).
5. **model_discovery fit classifier** ([fit_llm.py:275](../packages/analyzer/generators/model_discovery/fit_llm.py)) —
   occasional, only on a new model; negligible. **Passive candidate:** drop to deterministic-verdict-only
   (the LLM already can't override real-price fit).
6. **user_profile_inferrer** ([extractor.py:293](../packages/analyzer/generators/user_profile_inferrer/extractor.py)) —
   per-session, bot's own creds, DNT-gated. Already honors DNT; **Passive candidate:** off (it's a
   personalization/Managed concern, not legibility).

**Routing (per spec):** implementation of any of these toggles → the owning aspect — app
scanner/audit cost gating → `apps`; generator/observation cost → `rsi`; security_warden floor →
`reports`+`edr`; the cost-breaker interruption-vs-observe classification → `model-tiers`+`edr`. This
slice (F-1c) owns only the catalog + the cost-posture classification feeding the F-3 dial.

**Net:** the monitoring layer is **already overwhelmingly $0**. A Passive posture is cheap to reach —
the entire signal/alert/health/structural-audit surface is observe-only today. The token spend is
confined to a short, well-gated list (tier-3 audit, app scanner, observation tuples, three gated-Haiku
generators), each with a deterministic/structural fallback that *already exists in code*. The harder
F-3 design question is not the token-spenders (they gate cleanly) but the **cost breakers**: they are
$0 to run yet mutate bot behavior when they fire, so they straddle the observe/active line and need an
explicit posture decision rather than a naive on/off.
