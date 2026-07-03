# Footprint Catalog — F-1a: Deploy / Privilege surface (2026-06-18)

**Aspect:** `META:footprint` · **Slice:** F-1a (deploy/privilege) · **Spec:** [docs/spec-footprint-2026-06-18.md](spec-footprint-2026-06-18.md)

This is one slice of the F-1 footprint audit (see the spec's backlog): the
**persistent, privileged surface** that Evolve installs on the *host* and the
OpenClaw install at setup/deploy time. It catalogs every LaunchDaemon, every
sudoers grant, every macOS ACL, the managed deploy checkout + repo-puller, the
`evo` account, and the OC safe-upgrade path. Sibling slices (runtime/gateway,
cost/monitors, config-mutation/appliers) live in their own catalog files.

Every row is tagged on the four footprint dimensions from the spec:

- **M** — *Mutation*: changes config/state the bot loads or how OC behaves.
- **R** — *Runtime*: runs in / intercepts the bot's turn loop (hot path).
- **C** — *Cost*: spends tokens (LLM work).
- **P** — *Privilege*: daemons / sudoers / ACLs / accounts / managed checkout.

This slice is overwhelmingly **P**, with **R**/**C** flagged where a daemon does
real recurring work. "Current toggle-state" is `Unconditional` (installed on every
deploy/bootstrap, no off switch) or `Gated by <mechanism> (default <on/off>)`.

Grounding note: the authoritative daemon registry is two functions in
[deploy.py](../packages/admin/evolve_admin/deploy.py) —
`per_bot_evolve_plist_labels` ([deploy.py:365](../packages/admin/evolve_admin/deploy.py:365),
per-bot set) and `expected_plist_labels` ([deploy.py:419](../packages/admin/evolve_admin/deploy.py:419),
which inlines the pod-wide `evolve`-user set at
[deploy.py:575–668](../packages/admin/evolve_admin/deploy.py:575)). Both are the
named single-source-of-truth so deploy / upgrade / retire cannot drift. Cadence
and gating cites point at the per-daemon installer functions.

---

## 1. Always-on / long-running daemons (KeepAlive)

These run continuously as a service, not on a timer.

| Touch / subsystem | What it does | Dimensions | Current toggle-state | Code refs | Perf/cost/bug notes |
|---|---|---|---|---|---|
| `ai.evolve.evolve.admin-ui` | The admin web server (`server.py`); runs as `evolve`. The whole control plane. | P, (R) | **Unconditional** | [deploy.py:620](../packages/admin/evolve_admin/deploy.py:620) | Reads/audits the pod; no LLM generation in the hot path. The surface everything else hangs off. |
| `ai.evolve.evolve.mcp-bridge` | MCP tunnel (port 5051) so admin-laptop Claude Desktop can reach the mini over Tailscale. Converted 2026-05-30 from a per-user LaunchAgent to a system LaunchDaemon. | P | **Gated** — skipped when `network.json::mcp_bridge` disabled (default **on**) | [deploy.py:621](../packages/admin/evolve_admin/deploy.py:621), gate at [deploy.py:8099](../packages/admin/evolve_admin/deploy.py:8099) | Opens a network listener; only useful if the operator drives the pod from a laptop. |
| `ai.evolve.evolve.repo-puller` | `git pull --ff-only` of `/Users/Shared/evolve-repo` every 15 min + post-pull hooks. See §5. | P, M | **Unconditional** | [deploy.py:622](../packages/admin/evolve_admin/deploy.py:622), [repo_puller.py:54](../packages/admin/evolve_admin/repo_puller.py:54) | The auto-update engine; rebuilds plugin + kickstarts gateways on diff (so it indirectly mutates the bot runtime). |
| `ai.evolve.evolve.signal-subscriber` | Watches `{shared}/signals/firing/` at 1 Hz; on a Signal whose type matches a generator's `subscribes_to`, dispatches that generator within ~5 s. | P, R, **C** | **Unconditional** | [deploy.py:654](../packages/admin/evolve_admin/deploy.py:654), label [deploy.py:10656](../packages/admin/evolve_admin/deploy.py:10656) | Dispatch fires generators → **LLM cost** on the event path, not just the daily sweep. Disable via `launchctl bootout`; daily `generator_runner` sweep is the backstop. |

---

## 2. Fast-cadence pod-wide daemons (≤2 min)

Run as the `evolve` user, pod-wide. Frequent enough to matter for steady-state load.

| Touch / subsystem | What it does | Dimensions | Current toggle-state | Code refs | Perf/cost/bug notes |
|---|---|---|---|---|---|
| `ai.evolve.evolve.pod-health` | 1-min gateway-liveness Signal emitter. | P, R | **Unconditional** | [deploy.py:603](../packages/admin/evolve_admin/deploy.py:603) | Pure Python; reads gateway state every 60 s. |
| `ai.evolve.evolve.signal-notifier` | 1-min Signal-transition → alert dispatcher. | P | **Gated** by `alerts.signal_notifier.enabled` (default **off**) | [deploy.py:604](../packages/admin/evolve_admin/deploy.py:604) | Daemon present but quiet until the toggle is on. |
| `ai.openclaw.evolve.defer-runner` | Continuity Engine v2 defer-queue executor, every 2 min. | P, R | **Unconditional** | [deploy.py:584](../packages/admin/evolve_admin/deploy.py:584), [deploy.py:8929](../packages/admin/evolve_admin/deploy.py:8929) | Pure Python; fires queued deferred actions. |
| `ai.openclaw.evolve.manifest-reflex-runner` | Manifest-reflex queue executor, every 60 s; materializes ApplicationManifests. | P, R, M | **Unconditional** | [deploy.py:585](../packages/admin/evolve_admin/deploy.py:585) | Can write manifest artifacts into bot workspaces (Mutation downstream). |
| `ai.openclaw.evolve.cascade_pressure_watchdog` | 60 s (no jitter): reads tier-cascade telemetry → writes `{shared}/cascade/pressure_flags.json`. | P, R | **Unconditional** (no-op on empty telemetry) | [deploy.py:644](../packages/admin/evolve_admin/deploy.py:644) | Safe day-one; CascadeController reads the flags at decision time. |
| `ai.evolve.evolve.pairing-sweep` | 30 s sweep: auto-approves pod-admin / primary-owner / auto_admit-channel pending pairings. | P, M | **Unconditional** | [deploy.py:624](../packages/admin/evolve_admin/deploy.py:624) | Writes pairing credentials (per-bot, sudo) — Mutation of OC pairing state. Never auto-approves a blocked pairing. |

---

## 3. Per-bot Evolve daemons

Installed **per member bot** during `deploy_bot`, run as the **bot user**. Source of
truth: `per_bot_evolve_plist_labels` ([deploy.py:365–409](../packages/admin/evolve_admin/deploy.py:365)).
One independent daemon per bot, so the host count scales linearly with the roster.

| Touch / subsystem | What it does | Dimensions | Current toggle-state | Code refs | Perf/cost/bug notes |
|---|---|---|---|---|---|
| `ai.openclaw.evolve.apply.{bot}` | Applier — realizes approved proposals into the bot's config. | P, **M** | **Unconditional** (per bot) | [deploy.py:380](../packages/admin/evolve_admin/deploy.py:380) | The L1/L2 mutation arm; writes `openclaw.json` etc. |
| `ai.openclaw.evolve.cost-converter.{bot}` | Converts OC turn records → cost_event JSONL (replaces the silent `llm_output` hook path). | P | **Unconditional** (per bot) | [deploy.py:388](../packages/admin/evolve_admin/deploy.py:388) | Pure Python. |
| `ai.openclaw.evolve.audit-runner.{bot}` | App-audit Tier 2 (6 h structural). | P, **C** | **Unconditional** (per bot) | [deploy.py:391](../packages/admin/evolve_admin/deploy.py:391) | LLM-capable audit. |
| `ai.openclaw.evolve.audit-runner-t3.{bot}` | App-audit Tier 3 (hourly cadence). | P, **C** | **Unconditional** (per bot) | [deploy.py:392](../packages/admin/evolve_admin/deploy.py:392) | Tier-3 has a documented Sonnet-bleed history (see memory). |
| `ai.openclaw.evolve.doctor-pass.{bot}` | Nightly `openclaw doctor --fix` at 03:17 + jitter. | P, **M** | **Unconditional** (per bot) | [deploy.py:400](../packages/admin/evolve_admin/deploy.py:400) | Runs OC's own fixer against the bot — mutates OC state. |
| `ai.evolve.{bot}.backup` | Nightly git-backup at 02:00, one daemon per bot. | P | **Unconditional** (per bot) | [deploy.py:408](../packages/admin/evolve_admin/deploy.py:408) | Pure git; pushes to backup remote via distributed SSH key (§4). |

(Per-bot gateway daemon `ai.openclaw.{bot}-gateway` is OC's own "bot is running"
daemon, [deploy.py:412](../packages/admin/evolve_admin/deploy.py:412) — Evolve
manages/kickstarts it but does not own its existence.)

---

## 4. Periodic pod-wide daemons (hourly / daily / weekly)

Run as the `evolve` user (a handful self-gate to a configured hour). Almost all are
pure-Python Signal producers — **P** with no Cost — but they are *unconditional host
daemons*, which is the footprint that matters here. Grouped; all enumerated in
`expected_plist_labels` ([deploy.py:575–668](../packages/admin/evolve_admin/deploy.py:575)).

| Touch / subsystem | What it does | Dimensions | Current toggle-state | Code refs | Perf/cost/bug notes |
|---|---|---|---|---|---|
| `ai.openclaw.evolve.measure` | Daily metrics for all bots. | P | **Unconditional** | [deploy.py:631](../packages/admin/evolve_admin/deploy.py:631) | Pure Python. |
| `ai.openclaw.evolve.better` | Better Engine 15-min refresh + WatchPaths urgent trigger; pinned to `evolve` user. | P, **C** | **Unconditional** | [deploy.py:632](../packages/admin/evolve_admin/deploy.py:632) | LLM generator runs (cost-capped per generator-runner substantiveness gate). |
| `ai.evolve.evolve.proposal_synthesizer` | Every 6 h: LLM synthesis over the candidate queue. | P, **C** | **Unconditional** | [deploy.py:615](../packages/admin/evolve_admin/deploy.py:615) | LLM-heavy (Sonnet; per-run cap). |
| `ai.openclaw.evolve.expansion.{bot}` / `.evolve` | Proactive app-expansion generator (monthly self-gate). | P, **C** | **Unconditional** | [deploy.py:588](../packages/admin/evolve_admin/deploy.py:588) | LLM app recommendations. |
| `ai.openclaw.evolve.capability_gap_monitor` | Daily 03:15: noun-cluster coverage gaps → `app_suggester_gap` Signals. | P | **Unconditional** | [deploy.py:656](../packages/admin/evolve_admin/deploy.py:656) | Pure Python (no LLM). |
| `ai.openclaw.evolve.engagement_amplifier_monitor` | Daily 03:30: high-engagement deepening opportunities. | P | **Unconditional** | [deploy.py:657](../packages/admin/evolve_admin/deploy.py:657) | Pure Python. |
| `ai.openclaw.evolve.vocabulary_expander_monitor` | Weekly Sun 03:45: unrecognized-noun preflight report. | P | **Gated** by `rsi.vocabulary_expansion.enabled` (default **off** → zero cost) | [deploy.py:658](../packages/admin/evolve_admin/deploy.py:658) | Daemon installed unconditionally; the *work* is gated. LLM expansion deferred to a later PR. |
| `ai.openclaw.evolve.analyze.{bot}` / `.evolve` | Weekly behavior analysis. | P, **C** | **Unconditional** | [deploy.py:577](../packages/admin/evolve_admin/deploy.py:577) | LLM analysis. |
| `ai.openclaw.evolve.outcome.{bot}` / `.evolve` | Daily outcome tallying. | P | **Unconditional** | [deploy.py:583](../packages/admin/evolve_admin/deploy.py:583) | Pure Python scoring. |
| `ai.openclaw.evolve.tuples` | Daily 01:30: L3 observation-tuple extraction. | P | **Unconditional** | [deploy.py:641](../packages/admin/evolve_admin/deploy.py:641) | Pure Python aggregation. |
| `ai.openclaw.evolve.app-posture-review` | Weekly per-bot app-posture snapshot; injects markdown into bot systemAppend. | P, **M** | **Unconditional** | [deploy.py:586](../packages/admin/evolve_admin/deploy.py:586) | Writes into the bot's prompt surface (Mutation). |
| `ai.openclaw.evolve.slack-signals.{bot}` / `.evolve` | Daily 03:00: Slack events → Signals. | P | **Unconditional** | [deploy.py:587](../packages/admin/evolve_admin/deploy.py:587) | Slack API reads; pure Python. |
| `ai.evolve.evolve.spend-alert` | 5-min spend alerting. | P | **Unconditional** | [deploy.py:589](../packages/admin/evolve_admin/deploy.py:589) | Pure Python; reads turn JSONL. |
| `ai.evolve.evolve.cron-alert` | Hourly cron-exit alerting. | P | **Unconditional** | [deploy.py:590](../packages/admin/evolve_admin/deploy.py:590) | Pure Python. |
| `ai.evolve.evolve.cost_watchdog` | Hourly cost watchdog. | P | **Unconditional** | [deploy.py:616](../packages/admin/evolve_admin/deploy.py:616) | Pure Python. |
| `ai.evolve.evolve.session_economics` | Hourly cache-health + engagement-gap Signals. | P | **Unconditional** | [deploy.py:617](../packages/admin/evolve_admin/deploy.py:617) | Pure Python. |
| `ai.evolve.evolve.embedding_monitor` | Embedding-store health monitor. | P | **Unconditional** | [deploy.py:618](../packages/admin/evolve_admin/deploy.py:618) | Pure Python. |
| `ai.evolve.evolve.verify` | 5-min bot integrity checker. | P, R | **Unconditional** | [deploy.py:619](../packages/admin/evolve_admin/deploy.py:619) | Structural verification; no LLM. |
| `ai.evolve.evolve.heal` | 5-min gateway self-heal + auto-restart. | P, R | **Unconditional** | [deploy.py:602](../packages/admin/evolve_admin/deploy.py:602) | Restarts failed gateways; the redundant `ai.openclaw.evolve.heal` was removed (double-probe race, [deploy.py:659](../packages/admin/evolve_admin/deploy.py:659)). |
| `ai.evolve.evolve.audit` | 15-min security audit. | P | **Unconditional** | [deploy.py:605](../packages/admin/evolve_admin/deploy.py:605) | Pure Python audit framework. |
| `ai.evolve.evolve.update-watcher` | Daily 09:30: npm + git release polling. | P | **Unconditional** | [deploy.py:606](../packages/admin/evolve_admin/deploy.py:606) | HTTP polls; pure Python. Feeds the OC-version banner (§6). |
| `ai.evolve.evolve.anthropic-admin-ingest` | Daily 04:15: snapshot Anthropic cost-report + audit logs. | P | **Unconditional** | [deploy.py:607](../packages/admin/evolve_admin/deploy.py:607) | Anthropic Admin API reads. |
| `ai.evolve.evolve.retention` | Daily: signals/watchdog/proposals retention prune. | P | **Unconditional** | [deploy.py:608](../packages/admin/evolve_admin/deploy.py:608) | File cleanup. |
| `ai.evolve.evolve.log-cap` | Daily 03:35: cap flat-file logs by size. | P | **Unconditional** | [deploy.py:609](../packages/admin/evolve_admin/deploy.py:609) | File rotation. |
| `ai.evolve.evolve.oc-log-rotate` | Daily 04:30: truncate bot-owned gateway.log/.err.log (needs sudo). | P, **M** | **Unconditional** | [deploy.py:610](../packages/admin/evolve_admin/deploy.py:610) | Touches bot-owned files via `sudo /bin/truncate` (see §7 grant). |
| `ai.evolve.evolve.openclaw-overrides-expiry` | Daily 04:00: enforce `expires_at` on per-bot OC overrides. | P, **M** | **Unconditional** | [deploy.py:611](../packages/admin/evolve_admin/deploy.py:611) | Reverts lapsed overrides — Mutation of OC config. |
| `ai.evolve.evolve.proposal-auto-resolve` | Daily 03:45: archive proposals whose motivating signals cleared. | P | **Unconditional** | [deploy.py:612](../packages/admin/evolve_admin/deploy.py:612) | Pure Python sweep. |
| `ai.evolve.evolve.breakers-audit` | 5-min: write audit summary/recommendation onto active breaker trips. | P | **Unconditional** | [deploy.py:613](../packages/admin/evolve_admin/deploy.py:613) | Pure Python. |
| `ai.evolve.evolve.breakers-runner` | 10-min observe-only activity-shape detector. | P, (M) | **Gated** — acts on trips only when `breakers.auto_trip_enabled` (default **off**); observe-only otherwise | [deploy.py:614](../packages/admin/evolve_admin/deploy.py:614) | When enabled it can pause integrations (Mutation). Daemon itself unconditional. |
| `ai.evolve.evolve.pod-report-daily` | Hourly self-gating daily pod report. | P | **Unconditional** | [deploy.py:591](../packages/admin/evolve_admin/deploy.py:591) | Pure Python; merged the retired per-bot `report` daemon. |
| `ai.evolve.evolve.weekly-review` / `weekly-bot-trends` | Sunday 7-day digest + trend analysis. | P | **Unconditional** | [deploy.py:594](../packages/admin/evolve_admin/deploy.py:594) | Pure Python. |
| `ai.evolve.evolve.usage-logger` | Daily usage aggregator → Usage tab + cost reconciliation. | P | **Unconditional** | [deploy.py:629](../packages/admin/evolve_admin/deploy.py:629) | Pure Python. |
| `ai.evolve.evolve.security-cve-scan-finalize` | Daily 09:10: finalize LLM CVE candidates + dispatch. | P | **Unconditional** | [deploy.py:628](../packages/admin/evolve_admin/deploy.py:628) | Reads pre-generated JSON; no new LLM. |
| `ai.evolve.evolve.audit-scheduler` | Hourly tick: infra-audit cadence + per-bot audit-outbox drain + cron-exit sweep. | P | **Unconditional** | [deploy.py:623](../packages/admin/evolve_admin/deploy.py:623) | Renamed 2026-06-08 from app-test-scheduler. |
| `ai.evolve.evolve.digest-flush` | Hourly tick; alert-digest dispatcher self-gates to digest hour. | P | **Unconditional** (dark until operator picks a digest frequency) | [deploy.py:627](../packages/admin/evolve_admin/deploy.py:627) | Pure Python; queue empty by default. |
| `ai.evolve.evolve.reconcile-audit` | Daily 04:30: scheduled_actions[] drift vs gallery. | P | **Unconditional** | [deploy.py:651](../packages/admin/evolve_admin/deploy.py:651) | Pure Python. |
| `ai.evolve.evolve.digest-source-audit` | Daily 04:35: RSS/source-health (≥3 consecutive failures). | P | **Unconditional** | [deploy.py:652](../packages/admin/evolve_admin/deploy.py:652) | Pure Python. |
| `ai.evolve.evolve.agent-bypass-audit` | Daily 04:40: detect agent-freelance bypass on at-risk apps. | P | **Unconditional** | [deploy.py:653](../packages/admin/evolve_admin/deploy.py:653) | Reads session transcripts via the evolve ACL. |
| `ai.evolve.evolve.delivery-monitor` | 5-min: per-window delivery outcomes for scheduled user-facing apps. | P, (M) | **Unconditional** | [deploy.py:655](../packages/admin/evolve_admin/deploy.py:655) | Detection mostly; §8 heal can kickstart a missed-window job (canary-gated). |
| `ai.openclaw.evolve.deploy_drift_monitor` | Hourly: Signal when bots lag admin code. | P | **Unconditional** | [deploy.py:633](../packages/admin/evolve_admin/deploy.py:633) | Pure Python. |
| `ai.openclaw.evolve.bot_recovery_monitor` | Hourly: track heal-recovered alerts. | P | **Unconditional** | [deploy.py:634](../packages/admin/evolve_admin/deploy.py:634) | Pure Python. |
| `ai.openclaw.evolve.stuck_proposal_monitor` | Hourly: flag approved proposals sitting >7 d. | P | **Unconditional** | [deploy.py:635](../packages/admin/evolve_admin/deploy.py:635) | Pure Python. |
| `ai.openclaw.evolve.backup_signal` / `local_backup_signal` / `backup_audit_signal` | Hourly backup Signal producers + audit. | P | **Unconditional** | [deploy.py:636–638](../packages/admin/evolve_admin/deploy.py:636) | Pure Python. |
| `ai.openclaw.evolve.local_backup_excluder` | Hourly: sync ephemeral classification → tmutil exclusions. | P, **M** | **Gated** by `backup.tm_exclusion_sync` (opt-in, default **off**) | [deploy.py:639](../packages/admin/evolve_admin/deploy.py:639) | Mutates Time Machine exclusions on the host. |
| `ai.openclaw.evolve.alerts_loop_monitor` | Hourly: detect alert-delivery loops. | P | **Unconditional** | [deploy.py:640](../packages/admin/evolve_admin/deploy.py:640) | Pure Python. |
| `ai.openclaw.evolve.monitor_coverage` | Daily SELF-AUDIT: Signal when any Evolve monitor's log goes silent. | P | **Unconditional** | [deploy.py:642](../packages/admin/evolve_admin/deploy.py:642) | The "auditor of the auditors." |
| `ai.openclaw.evolve.install_integrity_monitor` | Daily: wizard-verification gauntlet (ownership/agent/channels). | P | **Unconditional** | [deploy.py:643](../packages/admin/evolve_admin/deploy.py:643) | Pure Python. |
| `ai.openclaw.evolve.cascade_audit_runner` | Hourly: cascade telemetry → Signals + calibration labels. | P | **Unconditional** (no-op on empty spans) | [deploy.py:645](../packages/admin/evolve_admin/deploy.py:645) | Pure Python. |
| `ai.openclaw.evolve.pod_perms_drift_monitor` | Hourly: `ensure_pod_perms(check_only=True)` → Signal on ownership/ACL drift. | P | **Unconditional** | [deploy.py:646](../packages/admin/evolve_admin/deploy.py:646) | Read-only stat checks; watches the very ACL surface in §8. |
| `ai.openclaw.evolve.gmail_integration_health` | 30-min: per-bot Google API probe → Signals. | P | **Unconditional** | [deploy.py:647](../packages/admin/evolve_admin/deploy.py:647) | HTTP probes. |
| `ai.openclaw.evolve.oc_substrate_monitor` | Hourly: freshness Signal for OC auto-updater + usage-collector state. | P | **Unconditional** | [deploy.py:648](../packages/admin/evolve_admin/deploy.py:648) | Pure Python. |
| `ai.openclaw.evolve.home_artifacts_monitor` | Hourly: per-bot workspace large/exec-file + macOS Quarantine-DB check. | P | **Unconditional** | [deploy.py:649](../packages/admin/evolve_admin/deploy.py:649) | Uses narrow `sudo /bin/cp` for the Quarantine DB (§7). |
| `ai.openclaw.evolve.code_quality_monitor` | Daily: repo-process KPIs (revert rate, fix-heavy scopes). | P | **Unconditional** | [deploy.py:650](../packages/admin/evolve_admin/deploy.py:650) | Reads the deploy checkout's git history. |
| `ai.evolve.evolve.upstream-issues-watcher` | 15-min: GitHub upstream-issue polling for maintainer replies. | P, **C** | **Gated** by `install.json::features.upstream_issues_watcher` (default **off**; plist absent when off) | [deploy.py:625](../packages/admin/evolve_admin/deploy.py:625), [FEATURE_GATED_PLIST_LABELS](../packages/admin/evolve_admin/deploy.py:682) | Dev-tier; needs `sudo -u evolve gh auth`. |
| `ai.evolve.evolve.inbound-issues-watcher` | 15-min: GitHub inbox polling + LLM triage. | P, **C** | **Gated** by `install.json::features.inbound_issues_watcher` (default **off**) | [deploy.py:626](../packages/admin/evolve_admin/deploy.py:626) | LLM triage on new issues. |

> The two `*-issues-watcher` labels are the **only feature-gated plists**
> (`FEATURE_GATED_PLIST_LABELS`, [deploy.py:682](../packages/admin/evolve_admin/deploy.py:682)):
> their labels stay in `expected_plist_labels` so the orphan-sweeper doesn't delete
> the plist when the feature flips off mid-uptime — but the plist is only *on disk*
> when the feature is on.

---

## 5. Managed deploy checkout + repo-puller

| Touch / subsystem | What it does | Dimensions | Current toggle-state | Code refs | Perf/cost/bug notes |
|---|---|---|---|---|---|
| Managed checkout `/Users/Shared/evolve-repo` | The deploy checkout every daemon loads from; treated as read-only, owned by `evolve`. | P | **Unconditional** | [repo_puller.py:54](../packages/admin/evolve_admin/repo_puller.py:54), [CLAUDE.md](../CLAUDE.md) | A second full clone of the repo on the host; the substrate for auto-update. |
| `git pull --ff-only` every 15 min | Fast-forwards the checkout to origin/main (direct mode). `--ff-only` so it never overwrites local commits. | P, M | **Unconditional** | [repo_puller.py:330](../packages/admin/evolve_admin/repo_puller.py:330), [repo_puller.py:988](../packages/admin/evolve_admin/repo_puller.py:988) | Auto-update is on by default; the mutation it lands flows to every bot. |
| Release pointer / `evolve-stable` tag (canary mode) | When `pod.release.mode=canary`, the fleet checkout follows `release.json::stable` (Gate-1 static checks → canary soak → promote), not origin tip. Candidates gate in worktrees under `/Users/Shared/evolve-staging/`. Out-of-band `git reset` is repaired back to the pointer. | P, M | **Gated** by `pod.release.mode` (`network.json` / `EVOLVE_RELEASE_MODE`; default **direct**) | [release_manager.py:6–42](../packages/admin/evolve_admin/release_manager.py:6), [release_manager.py:146](../packages/admin/evolve_admin/release_manager.py:146) | *Reduces* footprint risk (gated rollout) at the cost of extra staging worktrees on disk. Live pod runs canary. |
| Untracked-file quarantine | On a pull blocked by untracked files: delete-if-identical-to-origin, else move to `/Users/Shared/evolve-quarantine/<utc>/`; retry. | P | **Unconditional** (fires only on conflict) | [repo_puller.py:255–349](../packages/admin/evolve_admin/repo_puller.py:255), [repo_puller.py:58](../packages/admin/evolve_admin/repo_puller.py:58) | Recovery for accidental writes into the deploy checkout. |
| Post-pull hooks | On a pull that advances HEAD: rebuild+restage plugin (if `packages/plugin/` changed) → kickstart all bot gateways (2 s stagger) → re-run infra-jobs (if deploy.py/audit_scheduler.py changed) → bump charter fingerprints → sudoers drift check → `pip install -e` (if pyproject changed) → lagging-bot redeploy. | P, M, R | **Unconditional** (each hook path-gated by the diff) | [repo_puller.py:1106–1318](../packages/admin/evolve_admin/repo_puller.py:1106) | This is how a pull **indirectly mutates the bot runtime** — plugin restage + gateway kickstart restarts every bot. Auto-restart disableable via `EVOLVE_PULLER_AUTO_RESTART=0` ([repo_puller.py:670](../packages/admin/evolve_admin/repo_puller.py:670)). Hooks are best-effort: a failed hook does not roll back the pull. |
| Sudoers drift detection (not install) | Puller detects when `/etc/sudoers.d/evolve` lags the rendered template and fires a Signal. It **cannot install sudoers itself** (Option B, #2759 — prevents self-grant escalation). | P | **Unconditional** (detect-only) | [repo_puller.py:1249–1296](../packages/admin/evolve_admin/repo_puller.py:1249) | Operator applies via `sudo evolve-admin refresh-sudoers`. See §6. |

---

## 6. sudoers grants

Two files, both rendered in [setup_wizard.py](../packages/admin/evolve_admin/setup_wizard.py)
and validated with `visudo -c` before install. **All grants are `NOPASSWD`.**
**Refresh is manual by design** — grants are dormant until the operator runs
`sudo evolve-admin refresh-sudoers`; the `evolve` user cannot rewrite sudoers itself
(it fires a `sudoers-refresh-failed` Signal instead).

| Touch / subsystem | What it does | Dimensions | Current toggle-state | Code refs | Perf/cost/bug notes |
|---|---|---|---|---|---|
| `/etc/sudoers.d/evolve` | The `evolve` service-user grant set: ~22 sections, the bulk of Evolve's root reach. Categories below. | P | **Unconditional** (content); **manual** apply | renderer [setup_wizard.py:2062](../packages/admin/evolve_admin/setup_wizard.py:2062), writer [setup_wizard.py:3133](../packages/admin/evolve_admin/setup_wizard.py:3133) | Single source of truth; `cli.py` imports the renderer, doesn't copy it. |
| `/etc/sudoers.d/evolve-admin` | Per-bot grants for the human operator (`pod-admin`) to `sudo` as each bot user: read `openclaw.json` / `auth-profiles.json`, `crontab -l`, gateway restart verbs. | P | **Unconditional** (dynamic per roster); **manual** apply | renderer [setup_wizard.py:1931](../packages/admin/evolve_admin/setup_wizard.py:1931), writer [setup_wizard.py:1988](../packages/admin/evolve_admin/setup_wizard.py:1988) | CLI-use only; regenerated as bots are added. |
| — *Read bot config/logs/credentials* | `/bin/cat` of `openclaw.json`, `auth-profiles.json`, gateway logs, integration-discovery probe files (gws, dropbox, workspace creds, `.env`, gh hosts, ssh listing). Belt-and-suspenders behind the ACL reads. | P | **Unconditional** | [setup_wizard.py:2172–2268](../packages/admin/evolve_admin/setup_wizard.py:2172) | Reads tokens; the ACL (§8) is the primary path, sudo `cat` the fallback. |
| — *Write bot config* | `/tmp`-staged `/bin/cp` + `chown` + `chmod 600/644` for `openclaw.json`, `auth-profiles.json`, `evolve-tiers.json`, pairing creds, GWS creds, device-scope files, `exec-approvals(.preview).json`, `cron/jobs.json`, skills config. | P, **M** | **Unconditional** | [setup_wizard.py:2273–3097](../packages/admin/evolve_admin/setup_wizard.py:2273) | The privileged half of every OC-config mutation. Secrets enforced 0600. |
| — *Write bot workspace docs* | `/bin/cp` of AGENTS.md, SOUL.md, MEMORY.md, README.md, POD_CONDUCT.md, procedures, manifests, INSTALLED_APPS.md, pod_config.json, `.git/config`. | P, **M** | **Unconditional** | [setup_wizard.py:2823–2958](../packages/admin/evolve_admin/setup_wizard.py:2823) | Writes the bot's prompt/identity surface. |
| — *Manage gateways & Evolve daemons* | `launchctl` (macOS) / `systemctl` (Linux) list/kickstart/bootstrap/bootout for `ai.openclaw.*` and `ai.evolve.*`; write/rm plists. | P | **Unconditional** | [setup_wizard.py:2438–2802](../packages/admin/evolve_admin/setup_wizard.py:2438) | How Evolve starts/stops the whole fleet. |
| — *Provision/retire bot accounts* | macOS `dscl -create/-delete /Users/*` + `createhomedir` + `rm -rf /Users/*`; Linux `useradd`/`userdel`/`groupadd`. | P | **Unconditional** | [setup_wizard.py:2528–2549](../packages/admin/evolve_admin/setup_wizard.py:2528) | Evolve can create and delete macOS user accounts. High-privilege. |
| — *Set ACLs* | macOS `chmod +a` / `chmod -N`; Linux `setfacl` on `.openclaw`, workspace/evolve, credentials, shared stores. | P | **Unconditional** | [setup_wizard.py:2560–2750](../packages/admin/evolve_admin/setup_wizard.py:2560) | The grants behind §8. |
| — *Write shared state* | `network.json`, proposals/feedback dirs, plists, plugin dist to `/Users/Shared/evolve-plugin`, repo chown/chmod for plugin build. | P, M | **Unconditional** | [setup_wizard.py:2791–3040](../packages/admin/evolve_admin/setup_wizard.py:2791) | Plugin restage is how gateway code is mutated. |
| — *Run CLIs as bot users* | `SETENV` grant for the discovered `openclaw` binary + analyzer tools (`oc_model.py`, `oc_keys.py`, `application_scanner.py`, `app_audit_runner.py`, `backup.py --commit-baseline-local`). | P, (C) | **Conditional** — openclaw grant only emitted if the binary is discovered ([_find_openclaw_path](../packages/admin/evolve_admin/setup_wizard.py:2039)) | [setup_wizard.py:2405–2434](../packages/admin/evolve_admin/setup_wizard.py:2405) | Lets `evolve` run OC + scanners as any bot. |
| — *Process control* | `/usr/sbin/lsof` listener probe; `/bin/kill -9 -<pgid>` for orphaned OC children. | P | **Unconditional** | [setup_wizard.py:3048–3056](../packages/admin/evolve_admin/setup_wizard.py:3048) | Kill grant is broad (any process-group as root). |

---

## 7. macOS ACLs (and per-daemon sudo touches)

`set_evolve_read_acl(bot_id)` ([deploy.py:1261](../packages/admin/evolve_admin/deploy.py:1261))
runs on every `deploy_bot` ([deploy.py:6192](../packages/admin/evolve_admin/deploy.py:6192))
and every `ensure_pod_perms` ([deploy.py:5349](../packages/admin/evolve_admin/deploy.py:5349)).

| Touch / subsystem | What it does | Dimensions | Current toggle-state | Code refs | Perf/cost/bug notes |
|---|---|---|---|---|---|
| Read ACL on `/Users/{bot}/.openclaw/` | Inherited read (`read,readattr,readextattr,readsecurity` + dir/file inherit) for the `evolve` user across the whole tree. Makes `Path.read_text()` work without sudo. | P | **Unconditional** | [deploy.py:1282](../packages/admin/evolve_admin/deploy.py:1282) | The reason the admin server can read every bot's config. |
| ACL **exceptions** (stripped) | `credentials/` forced 0700 no-ACL; `profiles/*.md` forced 0600 no-ACL — `evolve` cannot read bot API keys or private profile bodies. | P | **Unconditional** | [deploy.py:1284–1301](../packages/admin/evolve_admin/deploy.py:1284) | A deliberate privilege *floor* — the few things `evolve` is blocked from. |
| Write ACL on `workspace/evolve/`, `manifests/`, `evolve-backup/`, workspace root | Inherited write/list/delete for `evolve` so manifests + scan-status write without sudo. | P, (M) | **Unconditional** | [deploy.py:1235–1239](../packages/admin/evolve_admin/deploy.py:1235), [deploy.py:1303–1383](../packages/admin/evolve_admin/deploy.py:1303) | Enables direct writes into bot workspaces. |
| Evo write ACL on shared stores | `read,write,delete,append` + inherit for the `evo` user on `{shared}/{proposals,signals,keystore,config_intents}/`; ownership normalized to `evolve:wheel` first. | P | **Gated** — applied only if the `evo` account exists; no-op on pre-separation pods | [deploy.py:4482–4517](../packages/admin/evolve_admin/deploy.py:4482), [deploy.py:4934–4999](../packages/admin/evolve_admin/deploy.py:4934) | Lets evo's MCP tools `os.replace` proposals/signals written by `evolve`. |
| Quarantine-DB sudo snapshot | `home_artifacts_monitor` copies the macOS Quarantine DB via `sudo /bin/cp`. | P | **Unconditional** (daemon-driven) | [setup_wizard.py:2268](../packages/admin/evolve_admin/setup_wizard.py:2268) | Narrow per-daemon sudo touch. |
| Gateway-log truncate sudo | `oc-log-rotate` truncates bot-owned gateway logs via `sudo /bin/truncate`. | P, (M) | **Unconditional** (daemon-driven) | [setup_wizard.py:2196–2197](../packages/admin/evolve_admin/setup_wizard.py:2196) | Narrow per-daemon sudo touch. |

---

## 8. The `evo` macOS account

| Touch / subsystem | What it does | Dimensions | Current toggle-state | Code refs | Perf/cost/bug notes |
|---|---|---|---|---|---|
| `evo` account creation | `_provision_evo_account()` creates a real macOS user `evo` (dscl+createhomedir / useradd) with a standard `.openclaw/` tree, owned `evo:staff`, `credentials/` 0700. Grants the `evolve` user read ACL on the tree. | P | **Gated** — fresh-install wizard only; idempotent + **non-fatal** (warns and continues if it fails) | [setup_wizard.py:3327–3432](../packages/admin/evolve_admin/setup_wizard.py:3327) | A whole extra OS account. Unprivileged (no sudoers grants); reach is its own home + world-readable paths + the shared ACL above. Spec: [docs/spec-evo-account-separation-2026-05-25.md](spec-evo-account-separation-2026-05-25.md). |

---

## 9. OpenClaw safe-upgrade — does Evolve upgrade the operator's OpenClaw?

**No.** Evolve **detects** an available OC upgrade (`update-watcher`, §4) and offers a
**read-only six-gate preflight** ("would upgrading break Evolve?"), but never runs
the upgrade itself.

| Touch / subsystem | What it does | Dimensions | Current toggle-state | Code refs | Perf/cost/bug notes |
|---|---|---|---|---|---|
| Safe-upgrade preflight | Read-only gates (node-version, stub-install, config-references, user-launchagents, plist-paths, port-owners). Only side effect: writes a report to `{shared}/safe-upgrade/reports/` (keep 20). | P | **On-demand** — UI button (`POST /api/oc/safe-upgrade/check`) or `evolve-admin oc safe-upgrade`; never automatic | [safe_upgrade.py:2](../packages/admin/evolve_admin/safe_upgrade.py:2), [safe_upgrade.py:44–45](../packages/admin/evolve_admin/safe_upgrade.py:44) | The decision to apply stays with the operator. Spec: [docs/spec-safe-upgrade-2026-05-02.md](spec-safe-upgrade-2026-05-02.md). |
| `oc upgrade` (manual) | Runs the preflight, then `sudo npm install -g openclaw@<target>` + gateway restart. Blocks on a failed preflight unless `--force`. | P, M | **Manual only** — operator must run it | `ocadmin.py` (`oc_upgrade`) | Evolve never triggers this; no daemon calls it. |

---

## Candidates to make dialable

For a low-footprint ("Passive / dashboard mode") operator, the unconditional items
below are the largest reducible surface. Ordered roughly by footprint-per-benefit.

1. **The cost-spending generator/audit daemons** (`better`, `proposal_synthesizer`,
   `expansion`, `analyze`, `audit-runner` T2/T3, `signal-subscriber` dispatch). All
   **Unconditional** and all spend tokens. A passive posture would stop generating
   proposals and stop costed audits. *Breaks:* the RSI proposal pipeline and app-audit
   findings go quiet — Evolve becomes inventory + alerts only. This is the single
   biggest **Cost** lever and matches the spec's "Passive" definition directly.

2. **Auto-mutation daemons** (`apply.{bot}`, `doctor-pass`, `openclaw-overrides-expiry`,
   `app-posture-review`, `manifest-reflex-runner`, `pairing-sweep`). All
   **Unconditional**, all write OC config or the bot's prompt surface. A passive
   posture would suggest-don't-apply (proposals stay pending; doctor-pass off). *Breaks:*
   nothing auto-heals or auto-applies; the operator must approve every change. Aligns
   with "Standard = suggest, don't auto-apply."

3. **Auto-update on by default** (repo-puller `git pull --ff-only` every 15 min + the
   gateway-kickstart post-pull hook). **Unconditional.** A low-footprint operator may
   want *manual* updates (pull on demand), or at least to suppress the automatic
   plugin-restage + fleet kickstart that restarts every bot. The `pod.release.mode=canary`
   gate already *de-risks* this but doesn't make it opt-out. *Breaks:* the pod stops
   self-updating; security/bugfix lag becomes the operator's responsibility. Tension with
   the "managed updater" benefit Evolve advertises even in passive mode — so the dial
   should likely keep *pulling* but gate the *auto-kickstart/restage*.

4. **The ~30 pure-Python pod-wide monitors** (§4). Individually cheap, but ~64 total
   host daemons is itself a footprint (launchd entries, log files, the
   `monitor_coverage` self-audit watching them all). These are **P-only, no Cost** — the
   weakest case for a per-daemon toggle, but a posture dial could collapse the
   *non-essential* producers (trend digests, code-quality KPIs, engagement amplifier)
   while keeping the safety-floor ones (heal, pod-health, verify, audit). *Breaks:*
   reduced observability; some Alerts/Reports surfaces go empty. Coordinate the
   safety-floor cut with `edr`/security per the spec boundary.

5. **The broad sudoers grants** — account create/delete (`dscl`/`useradd`), `kill -9
   -<pgid>`, recursive chown of the repo. All **Unconditional**. A passive operator who
   never adds/removes bots via the UI doesn't need the provisioning grants live. *Breaks:*
   add-bot/retire-bot from the UI; manual operator steps required instead. Lower priority
   (grants are dormant capability, not running cost) but the highest *privilege* surface.

6. **The `evo` second account** — already **Gated** (fresh-install opt-in, non-fatal),
   so it's a good model for the posture contract rather than a cut candidate. Worth
   citing as the precedent: a footprint item that is created only when its benefit
   (gateway/store isolation) is wanted.

**Already gated (no work needed; the posture dial should surface, not re-implement):**
`mcp-bridge` (network.json), `signal-notifier` (alerts.signal_notifier.enabled, default
off), `vocabulary_expander` work (rsi.vocabulary_expansion.enabled, default off),
`breakers-runner` enforcement (breakers.auto_trip_enabled, default off),
`local_backup_excluder` (backup.tm_exclusion_sync, default off), both `*-issues-watcher`
(install.json features, default off), `pod.release.mode` (canary vs direct), and the
`evo` account. These are the existing per-subsystem toggles the F-3 posture-dial design
should aggregate under coherent posture levels.
