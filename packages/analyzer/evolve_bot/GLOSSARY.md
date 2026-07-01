## Pod glossary — what evo's terms actually mean

*Generated from `packages/analyzer/evolve_bot/glossary.yaml`. Do not hand-edit this section — regenerate via `evolve-admin gen-evo-glossary` and edit the yaml instead. Pod-specific overrides live in `network.json::evo_glossary_overrides`.*

This section is the taught reference for evo's domain. When the operator asks about a *chip*, a *signal*, or a *proposal*, your answer should match how it actually works, not a plausible sound-alike interpretation.

### Tile chips (Dashboard pills)

Source: `analyzer/tile_metrics.py`. Tool: `pod_state.bots` returns them per bot in the `tile_chips` field.

**Member-bot chips:**

- **`gateway_down`** (critical) — "gateway down"
  Fires when: Bot's gateway process is not running OR unreachable (per status/{bot}.json written by heal.py every ~5m).
  **Urgency: act.** Bot can't process turns. Check pod_state.host for daemon state; operator should restart from Maintenance → Status.

- **`stale_heartbeat`** (depends) — "stale heartbeat"
  Fires when: Critical: bot's status is 'offline'. Warn: status is 'active' but last metric file is >1 day old.
  **Urgency: depends.** Critical variant: act — bot's heartbeat probe is failing. Warn variant: observe; usually a day-rollover blip but can signal a developing outage.

- **`version_drift`** (warn) — "version drift"
  Fires when: bot_data.evolve_synced is False
  **Urgency: defer.** Bot still works but is missing recent fixes/features. Operator runs deploy at convenience.

- **`unexpected_billing`** (warn) — "unexpected billing"
  Fires when: sum of unexpected_billing_turns over 7d is >0
  **Urgency: defer.** Anomalous billing pattern (fallback behaviors, retry loops). Investigate via Cost tab, not urgent.

- **`high_correction`** (warn) — "pushback"
  Fires when: >10% of turns in the last 7d (≥20 turns) had the *user's incoming message* match a correction phrase aimed at the bot's previous reply ("no, i meant", "that's not what i", "you misunderstood", "not quite", "try again", "that's wrong", "incorrect", "you said you would", "you didn't", "still not", "that didn't work"). Plain substring match on lowercased text in packages/plugin/src/observer/TierClassifier.ts; the bot's own output is not inspected.
  **Urgency: defer.** The user is pushing back on the bot's prior turns often enough to clear the 10% threshold — the signal is user-initiated, not bot self-revision or model hedging. Likely causes: soul / prompt mismatch, model under-tier for the work, scope mismatch (user asks things outside the bot's apparent capability), or task-type drift since the soul was tuned. Defer; review recent session transcripts on the Sessions page (filter by corrections) when convenient to spot the dominant pattern.

- **`scan_needed`** (warn) — "scan needed"
  Fires when: applications/<bot>/.scan-status.json is missing OR >30 days old AND apps.total > 0
  **Urgency: defer.** Low priority unless apps.total > 5 — stale manifest hampers app audit but doesn't break anything. Operator runs scan from Applications tab.

- **`cost_spike`** (warn) — "cost spike"
  Fires when: current 7d cost > 2× prior 7d AND > $5 floor
  **Urgency: depends.** Act if unexplained (loop / misconfigured cron / genuine usage change). Defer if you know the cause.

- **`security_critical`** (critical) — "N critical security"
  Fires when: Security audit found N critical findings for this bot (cached output of /api/security/audit).
  **Urgency: act.** Usually means exposed credentials, dangerous privilege grants, or policy violations. Fetch pod_state.audit(bot_id=...) for details.


**Primary-bot chips (appended when role=primary):**

- **`repo_puller_stale`** (warn) — "repo-puller stale"
  Fires when: Repo-puller daemon hasn't fetched in >20m (mtime of evolve-repo/.git/FETCH_HEAD).
  **Urgency: depends.** Investigate — check launchctl state for ai.evolve.evolve.repo-puller. Pod isn't getting new code.

- **`infra_daemon_down`** (critical) — "infra daemon down"
  Fires when: One or more of (repo-puller, heal, verify) not loaded in launchctl.
  **Urgency: act.** Pod monitoring is degraded. Act now.

- **`acl_drift`** (warn) — "ACL drift"
  Fires when: `evolve` user has lost read ACL on one or more member bots' .openclaw/ dirs.
  **Urgency: depends.** Investigate. Evolve can't monitor those bots; operator may need to re-run the setup wizard's ACL step.

- **`disk_high`** (depends) — "disk high"
  Fires when: Disk holding shared_dir is ≥90% (warn) or ≥95% (critical).
  **Urgency: depends.** Act at critical (95%+). Defer at warn (90–94%) but plan cleanup.


### Signal producers (the Alerts page sources)

Source: `signals.store.observe(producer=...)`. The Alerts page filters by producer. Tool: `pod_state.signals.firing(producer=...)`.

- **`cost_watchdog`** — cost and automation telemetry hourly
  Signal types: `daily_spend_high`, `automation_dominance`, `cron_wakes_agent`, `cron_overactive`, `context_bloat`, `session_token_outlier`
  Sweep-resolves: yes
  Most are tuning suggestions; act on session_token_outlier (runaway sessions are serious) and cost_spike-style daily_spend_high.

- **`bot_log_monitor`** — per-bot gateway error logs for known failure patterns
  Signal types: `max_auth_failure`, `discord_target_invalid`, `tool_delivery_failing`
  Sweep-resolves: yes
  Act on max_auth_failure and discord/target_invalid; delivery_failing usually needs cross-bot debug.

- **`deploy_drift_monitor`** — per-bot deployed version vs admin code version
  Signal types: `deploy_drift`
  Sweep-resolves: yes
  Bundled with next deploy; operator runs install-infra-jobs at convenience.

- **`oc_cli`** — openclaw CLI invocations from evolve callers (inline guard in oc_cli.oc_command)
  Signal types: `cli_misinvocation`
  Sweep-resolves: no
  Update the evolve caller's openclaw invocation to match the current upstream CLI shape (missing requiredOption / renamed flag / changed positional arity). Persistent until the caller ships a fix; re-fires weekly while broken.

- **`bot_recovery_monitor`** — recovery from offline events
  Signal types: `bot_recovered`
  Sweep-resolves: yes
  Informational; no action needed.

- **`embedding_monitor`** — embedding provider health (gateway.err.log tail, hourly)
  Signal types: `provider_failing`, `rate_limit_storm`
  Sweep-resolves: no
  Act on auth_failed (rotate key); inform on rate-limit storms (provider degraded; operator can't fix from pod side).

- **`session_economics`** — prompt-cache and bot-engagement patterns daily
  Signal types: `cache_invalidation_elevated`, `cache_hit_rate_low`, `bot_unused`
  Sweep-resolves: yes
  Most feed into proposals (cache_ttl_tuner, efficiency_hawk); defer to the proposal flow unless directly debugging.

- **`permission_monitor`** — approvals, cron, openclaw config drift vs baseline hourly
  Signal types: `perm_config_drift`, `perm_config_dangerous_combo`, `perm_approvals_denylist_match`, `perm_approvals_volume_warn`, `perm_approvals_volume_alarm`, `perm_cron_uncapped_agent_turn`, `perm_cron_denylist_match`, `perm_cron_added_silently`, … (1 more)
  Sweep-resolves: yes
  Act on _denylist_match and _dangerous_combo (policy violations); defer on _volume_warn (tuning opportunity).

- **`plugin_monitor`** — plugin inventory diff vs pod baseline hourly
  Signal types: `plugin_missing_required`, `plugin_denied_present`, `plugin_load_path_unexpected`, `plugin_install_source_unauthorized`, `plugin_unexpected_enabled`, `plugin_unexpected_disabled`, `plugin_allow_list_missing`, `plugin_allow_list_drift`, … (3 more)
  Sweep-resolves: yes
  Act on _missing_required and _denied_present (policy violations); others are baseline-tuning opportunities.

- **`hook_monitor`** — hook/webhook config drift vs baseline hourly
  Signal types: `hook_webhook_unexpected_enabled`, `hook_webhook_mapping_changed`, `hook_webhook_transforms_dir_drift`, `hook_plugin_policy_silent_disable`, `hook_plugin_policy_unexpected`, `hook_command_gate_enabled`, `hook_openclaw_config_missing`
  Sweep-resolves: yes
  Act on _webhook_unexpected_enabled and _plugin_policy_silent_disable (potential security drift).

- **`alerts_loop_monitor`** — alerts dispatcher log for failures and repeat-loops hourly
  Signal types: `dispatcher_failures`, `alert_repeat_loop`
  Sweep-resolves: yes
  dispatcher_failures: check Discord/Telegram credentials. alert_repeat_loop: resolve the underlying condition (approve or revert the triggering proposal).

- **`audit`** — OC security audit (every 15 min)
  Signal types: `varies — per-finding signals from the audit catalog`
  Sweep-resolves: yes
  Severity drives urgency. Fetch pod_state.audit(bot_id=...) for finding-level detail when the operator asks.

- **`integration_probe`** — per-bot integration health (Telegram / Slack / Google OAuth / etc.)
  Signal types: `varies per integration probe`
  Sweep-resolves: yes
  Failure means a specific integration is broken end-to-end. Bot can't deliver to that channel until fixed.

- **`error_reporter`** — surfaced exceptions from analyzer + admin daemons
  Signal types: `varies per error class`
  Sweep-resolves: no
  Critical errors warrant immediate operator attention; recurring warnings are usually tuning opportunities.

- **`gateway`** — gateway-level events (restarts, failures, port conflicts)
  Signal types: `gateway_instability`, `gateway_restart`, `etc.`
  Sweep-resolves: yes
  gateway_instability blocks bot operation; gateway_diagnostician proposes the heal config fix.

- **`host_health`** — pod host CPU / memory / disk / load
  Signal types: `host_cpu_high`, `host_memory_high`, `host_disk_high`, `host_load_high`
  Sweep-resolves: yes
  Severity drives urgency. disk_high is the most actionable.

- **`manifest_reflex_runner`** — app-manifest reflex execution outcomes
  Signal types: `varies`
  Sweep-resolves: yes
  Tracks reflex-runner behavior; defer unless investigating an app-specific issue.

- **`manifest_reflex_scanner`** — app-manifest reflex scanner outcomes
  Signal types: `varies`
  Sweep-resolves: yes
  Same posture as manifest_reflex_runner.

- **`pod_health`** — pod-wide health rollup
  Signal types: `varies per dimension`
  Sweep-resolves: yes
  Severity drives urgency. Often the umbrella for several finer-grained producers.

- **`pod_report`** — pod-state rollup (daily report)
  Signal types: `varies`
  Sweep-resolves: yes
  Daily-report producer; most signals here are informational summaries.

- **`registry`** — generator-registry health (charter drift, fingerprint mismatch)
  Signal types: `registry_drift`, `charter_fingerprint_mismatch`
  Sweep-resolves: yes
  A drifted generator can produce stale or wrong proposals; act now.

- **`security_warden`** — security posture checks (also a proposal generator)
  Signal types: `multi_user_no_pod_admins`, `multi_user_no_primary_recorded`, `multi_user_exec_full_unscoped`, `openclaw_version_below_floor`
  Sweep-resolves: yes
  Security drift is critical; act now.

- **`sysadmin_watchdog`** — platform-level failures (also a proposal generator)
  Signal types: `varies`
  Sweep-resolves: yes
  Act on critical findings (gateway down, daemon missing); defer on tuning-class signals.

- **`evolve_watchdog`** — RSI meta-health (also a proposal generator)
  Signal types: `varies`
  Sweep-resolves: yes
  Evolve correcting itself is cascading risk; act now.

- **`test_runner`** — app-test execution results
  Signal types: `test_failed`, `test_passed_after_fail`
  Sweep-resolves: yes
  test_failed blocks app readiness — paired with test_failure_responder proposal generator for the diagnosis.

- **`budget_hawk`** — pod-wide spend vs configured cap (also a proposal generator)
  Signal types: `pod_spend_near_cap`, `pod_spend_over_cap`, `billing_mode_drift`
  Sweep-resolves: yes
  Cost cascades fast — act when near cap. Same entity also runs as a proposal generator that emits TierAdjustment etc.

- **`mcp_monitor`** — MCP server configuration on each bot vs the pod allowlist
  Signal types: `mcp_server_not_in_allowlist`, `mcp_server_signature_drift`, `mcp_launcher_missing`
  Sweep-resolves: yes
  MCP drift means a bot is talking to a server the pod hasn't vetted, or a vetted server has changed its config. Act on _not_in_allowlist and _signature_drift; launcher_missing is usually a deploy-incomplete state.

- **`backup_signal`** — per-bot nightly backup run-state (hourly launchd)
  Signal types: `backup_failing`
  Sweep-resolves: yes
  Closes the May 2026 silent-failure gap (admin UI showed "✓ 65h ago" while every nightly run was bouncing). Warn at 3+ consecutive failures, escalates to alert at 7+. Operator retries via Maintenance → Backup "Backup now"; tooltip on the red badge shows the latest error.

- **`breakers_runner`** — auto-trip decisions from the activity-shape detector
  Signal types: `recurrent_breaker_trip`
  Sweep-resolves: no
  Fires only when a bot trips a second time within 48h on the same breaker type — pattern suggests a root cause that needs investigation rather than another reset. Trip is already in effect; this signal is the "look at why" prompt.

- **`compliance_scan`** — per-bot manifest + workspace compliance (daily scan)
  Signal types: `stale`, `test_failing`, `missing_required_field`, `validation_error`, `unregistered_script`, `unregistered_cron`, `misplaced_secret`
  Sweep-resolves: yes
  Feeds the manifest_quality, workspace_inventory, and workspace_security generators (which emit Investigation proposals). Act on misplaced_secret (credential leak risk); defer on stale / test_failing / unregistered_* (operator triages via the generated proposals).

- **`spend_alert`** — intraday spend bursts + spend_alert's own self-failure (every 5m)
  Signal types: `cost_burst`, `self_failure_jsonl_discovery`
  Sweep-resolves: no
  cost_burst: bot spent past the burst threshold in a short window (warn at threshold, alert at 3×); investigate via Usage tab. self_failure_jsonl_discovery: spend_alert itself can't read turn JSONL for some bots — the alerter is blind for them until fixed (check evolve user's read access to /Users/Shared/evolve/{bot}/turns/).

- **`stuck_proposal_monitor`** — proposals sitting in approved/ past a threshold (default 7 days)
  Signal types: `stuck_proposal`
  Sweep-resolves: yes
  Catches the 2026-05-20 forensics failure mode (approved proposal stuck for a month because apply.py's idempotency check fired against a stale apply-results entry). Pod-scoped; one signal lists all stuck proposals. Operator either re-applies, archives, or unblocks the apply pipeline.

- **`anthropic_admin_ingest`** — daily reconciliation of local cost ledger against Anthropic admin API
  Signal types: `cost_diverges_from_anthropic`
  Sweep-resolves: yes
  Fires when the pod's local cost ledger disagrees with Anthropic's org-level cost report for the same day by more than the divergence threshold (default 10%). Usually a ledger / ingest gap worth investigating but not an emergency. No-ops cleanly when the admin key isn't configured.

- **`app_structural_verifier`** — tier-2 structural audit findings from each bot's audit outbox
  Signal types: `varies — per-assertion finding ids from the audit catalog`
  Sweep-resolves: yes
  Structural-audit findings from the per-bot audit_runner. Severity depends on the assertion; outbox sweep-resolves cleared findings after each tier-2 run summary. Producer key is shared across bots — sweep_resolve doesn't take a bot filter.

- **`infra_audit`** — infrastructure audit run health (sudoers, launchd, ACLs)
  Signal types: `infra_audit_run_failed`
  Sweep-resolves: yes
  Infra audits route findings directly into Proposals, not Signals — the one Signal type fires when the audit itself broke, meaning no infra Proposals will land until it's fixed. Pod-scoped, alert severity.

- **`openclaw_posture`** — OpenClaw posture-doctor findings per bot (Phase 0.5)
  Signal types: `varies — per-rule lowercased code (e.g. exec_default_allow`, `oc_version_below_floor)`
  Sweep-resolves: yes
  Posture-doctor findings cover OC version floor, exec defaults, channel surface gaps, etc. Severity per rule drives urgency. sweep_resolve handles findings that no longer fire on the next run.

- **`slack_config_probe`** — per-bot Slack integration health (continuous reconciler)
  Signal types: `silent_blackhole`, `silent_blackhole_unverified`, `oc_default_drift`, `bot_invited_unwatched`
  Sweep-resolves: yes
  silent_blackhole (alert): bot is in a member channel but the allowlist excludes it — messages disappear with no reply. Act. oc_default_drift: visibleReplies setting unexpected. Act. bot_invited_unwatched: bot was invited but no entry exists yet (operator finalizes the wire-up). INFO findings don't emit Signals.


### Proposal generators (the Recommendations page sources)

Source: `analyzer/generators/<id>/charter.yaml`. The Recommendations page filters by generator. Tool: `pod_state.proposals.pending`.

- **`app_birth_detector`** (weekly, audience=bot_owner) — orphan script+data clusters that look like an undeclared app
  Proposes: BuildApp to formalize the cluster
  Hygiene improvement; bot is already using the app informally.

- **`app_suggester`** (weekly, audience=pod_operator) — bot's existing apps + a curated catalog of common capabilities
  Proposes: Investigation suggesting one uncovered catalog category per bot
  Exploratory "consider installing X" prompts (pure Python, no LLM). Long dwell time — does not auto-resolve on silence; the operator dismisses or accepts. Arbiter dedup/cooldown handles "don't re-suggest the same category."

- **`auth_drift_filler`** (daily, audience=pod_operator) — perm_config_drift signals from permission_monitor
  Proposes: ConfigPatch per drifted field, restoring baseline value
  Permission drift is security-adjacent; act when firing — one proposal per drifted field, accept selectively.

- **`budget_hawk`** (hourly, audience=pod_operator) — cost anomalies, billing-mode issues, approaching pod cap
  Proposes: tier downgrades, investigation, config patches
  Act when near cap — cost cascades fast.

- **`cache_ttl_tuner`** (daily, audience=bot_owner) — cache_invalidation_elevated / cache_hit_rate_low from session_economics
  Proposes: which cache TTL knob to bump
  Cost optimization, not a blocker.

- **`cost_spike`** (daily, audience=pod_operator) — cost_spike Signals from cost_watchdog (week-over-week relative change)
  Proposes: Investigation so operator can compare context and decide intentional vs regression
  Distinct from budget_hawk (absolute cap). Sensor-style — proposal auto-archives when the upstream signal clears. Act if unexplained (heartbeat leak, runaway cron, accidental model upgrade); defer if you know the cause.

- **`cron_caps_filler`** (daily, audience=pod_operator) — perm_cron_uncapped_agent_turn signals from permission_monitor
  Proposes: UpsertCronJob to add maxTurns + maxBudgetUsd to uncapped agentTurn jobs
  Uncapped headless agent turns can burn unbounded budget; act when firing.

- **`efficiency_hawk`** (daily, audience=bot_owner) — high turn counts, tier misrouting, repetitive Q&A
  Proposes: tier adjustments, workflow streamlines, manifest updates
  Act on high-severity entries; real cost drag.

- **`evolve_watchdog`** (daily, audience=pod_operator) — RSI ecosystem health (noise drift, verification reliability, generator dominance)
  Proposes: throttle or pause generators
  Evolve correcting itself is cascading risk; act now if firing.

- **`gateway_diagnostician`** (daily, audience=pod_operator) — gateway_instability watchdog signals
  Proposes: specific heal config changes (timeout, retry, restart cooldown)
  Gateway instability blocks bot operation; act.

- **`manifest_quality`** (daily, audience=pod_operator) — compliance_scan Signals — stale, test_failing, missing_required_field, validation_error
  Proposes: Investigation per issue so operator can review the manifest
  Sensor-style — auto-archives when compliance_scan stops emitting the underlying Signal (operator updated last_reviewed, tests pass, field added, schema fixed). Right response depends on whether the app is still in use — operator decides.

- **`persona_tuner`** (weekly, audience=bot_owner) — mismatches between bot voice/tone and user preferences
  Proposes: AGENTS.md additions, SoulEdit for deeper recalibrations
  UX polish; defer.

- **`plugin_curator`** (hourly, audience=pod_operator) — plugin allowlist / enable / disable / config drift
  Proposes: UpdatePluginAllowDeny, EnablePluginEntry, DisablePluginEntry, UpdatePluginConfig
  Baseline compliance tuning; rarely time-sensitive.

- **`security_warden`** (hourly, audience=pod_operator) — credential exposure, excessive scope, upstream OC version drift
  Proposes: Investigation + ConfigPatch (auth/ACL hygiene)
  Security drift is critical; act when firing.

- **`session_quality`** (daily, audience=pod_operator) — session_quality Signals from cost_watchdog — sessions dominated by config/housekeeping
  Proposes: Investigation so operator can check for stuck loops, repeated auth errors, etc.
  Bot is burning sessions on maintenance overhead instead of productive output. Right response depends on what's causing it (loop, auth flap, misconfigured cron); operator context needed. Sensor-style — auto-archives when sessions normalize.

- **`sysadmin_watchdog`** (hourly, audience=pod_operator) — platform failures (gateway down, plugin missing, daemon not loaded, ACL drift)
  Proposes: ConfigPatch for ACL restoration (others surface via Signals)
  Act on critical findings (gateway/daemon down).

- **`test_failure_responder`** (weekly, audience=bot_owner) — app test_command failures
  Proposes: diagnosis Investigation
  Failed test blocks app readiness; act.

- **`test_gate_backfill`** (daily, audience=bot_owner) — legacy app manifests lacking test_command/test_cases/test_exemption_reason
  Proposes: ManifestUpdate (when pattern matches "manual cron"), else Investigation
  Hygiene; new apps already gated.

- **`user_profile_inferrer`** (per-session, audience=bot_owner) — session transcripts for user-fact extraction
  Proposes: (no proposals — operates silently)
  No proposals to act on. If operator asks "how does evolve know about me?", this is the answer: per-user profile maintained at ~/.openclaw/profiles/<user_key>.md, honors DNT.

- **`workspace_inventory`** (daily, audience=pod_operator) — compliance_scan Signals — unregistered_script, unregistered_cron
  Proposes: Investigation per orphan so operator can register a manifest or delete the file
  Workspace contains scripts/crons no manifest claims. Usually "register the missing manifest" but sometimes "delete the orphan" — only the operator knows which. Sensor-style; auto-archives when the file/cron disappears or gets registered.

- **`workspace_security`** (daily, audience=pod_operator) — misplaced_secret Signals from compliance_scan
  Proposes: Investigation per credential find so operator can review the file
  Severity skews critical — credentials in untracked workspace files are top-of-list. Investigation-shaped because the operator decides real leak vs documented sample, and remediation (rotate, redact, move) carries blast radius needing explicit approval.


### Severity → urgency cheat sheet

When evaluating any signal, audit finding, or proposal:

| Severity | Default treatment |
|----------|-------------------|
| critical | **Act now or stage a confirmable action immediately.** Don't defer; if you can't fix, escalate to operator with a concrete next step. |
| alert / warn | **Surface and propose action; ok to defer with a reason.** If the operator says "snooze it", say a duration. |
| info | **Read-only; only mention if directly asked.** Don't include in summaries unless the operator wants them. |

The Alerts page has a "show info-tier signals" toggle — info signals are deliberately hidden by default.
