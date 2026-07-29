"""Central default-severity *and* default-flavor policy for Signal producers.

The Signal store accepts three severities: ``info``, ``warn``, ``alert`` and
two flavors: ``activity`` (triage / FYI lane) and ``maintenance`` (the
"queue-and-fix" task inbox). Without coordination, each producer picks its
own defaults — which is how ``warn`` ended up being used both for
"security_warden flagged multi-user exec=full on team_bot_a" (a real concern)
and "openclaw mtime changed because brew upgraded" (cosmetic noise), and how
197/203 firing signals ended up framed as ``maintenance`` even for purely
informational items like "a new model line is available". The alerts page
conflates them.

This module is the single place where the per-producer defaults are
declared. Producers should call ``signals.store.observe`` *without* passing
``severity`` / ``flavor`` to inherit the producer default; pass an explicit
value only when a *specific finding diverges* from the producer's normal
level (e.g. ``model_discovery`` is an advisory ``activity`` producer, but its
degraded-listing signal overrides to ``maintenance`` because that one *is* a
fix-it task; ``security_warden`` is a ``maintenance`` producer, but its
purely-informational exec-posture note overrides to ``activity``).

UI policy (Phase 4 PR-2):
  - ``alert``  → always shown (critical/red)
  - ``warn``   → shown by default (error/orange)
  - ``info``   → hidden by default; toggle reveals (advisory/gray)

Flavor policy (spec-alerts-signal-store §4): ``maintenance`` is reserved for
*genuine breakage / an action the operator must queue*; advisory, telemetry,
discovery, and "nothing is broken" observations default to ``activity``. The
strong correlation in practice is **info-severity ⟹ activity flavor** — the
``test_default_flavor_matches_info_severity`` guard pins that so a future
advisory producer can't silently default into the maintenance task inbox.

The maps below are the *current policy*, not a contract. Tune freely.
"""

from __future__ import annotations

from typing import Literal

Severity = Literal["info", "warn", "alert"]
Flavor = Literal["activity", "maintenance"]


# Default severity per producer. Producers missing from the map fall
# through to DEFAULT_SEVERITY — but new producers should be added
# explicitly so the policy decision is visible in version control.
PRODUCER_SEVERITY: dict[str, Severity] = {
    # ── alert (critical) ────────────────────────────────────────────────────
    # Security findings are categorically more urgent than config drift —
    # the multi-user exec=full / no-admin combo is real privilege exposure.
    "security_warden": "alert",
    # Gateway down + restart-failed loops are user-affecting; alert tier.
    "heal": "alert",
    "sysadmin_watchdog": "alert",
    # A bot's openclaw.json no longer validates against the OC schema —
    # the gateway will crash-loop on next reload (this is exactly the
    # PR #1525 class: schema removes a key, existing on-disk configs
    # still carry it). User-affecting (silent bot in Chat), alert tier.
    "openclaw_config_validator": "alert",
    # Recurrent auto-trip = a structural problem the per-trip recovery isn't
    # catching; warrants the same urgency as the underlying outage. The
    # producer always builds these as recurrent (single trips are silent —
    # see breakers/runner.py recurrent gate), so every emit is alert-grade.
    "breakers_runner": "alert",
    # CPU / disk / memory threshold breaches on the pod host — the host
    # itself is in trouble; everything else is downstream.
    "host_health": "alert",
    # The proposal reviewer's AST security layer failed to import — the gate is
    # failing closed (force-flagging every proposal), but a degraded security
    # gate is a privilege-exposure-class fault the operator must see now.
    "review_gate": "alert",

    # ── warn (real problem, action needed) ─────────────────────────────────
    # Daily enforcer for ``expires_at`` on sandbox/overrides/<bot>.json
    # entries. Default ``warn`` covers the "expired and removed" case
    # (operator's intentional override is gone; they may want to
    # recreate it). The producer overrides per-emit to ``info`` for the
    # pre_expiry notification (advisory only). See Phase 5 of
    # docs/spec-openclaw-json-derived-artifact-2026-05-24.md.
    "openclaw_overrides_expiry": "warn",
    # Audit findings span criticality; audit.py escalates per-finding to
    # alert via per-emit override (see dispatch_findings). Default is warn.
    "audit": "warn",
    # Cost spikes + heartbeat-on-primary-model + shell-only-wake-main crons
    # are real cost waste but rarely user-breaking. Producer can escalate
    # via per-emit override when budget exceeded by ≥ 2× threshold.
    "cost_watchdog": "warn",
    # cost_burst signals from spend_alert. Default warn; the producer
    # escalates to "alert" per-emit when cost crosses
    # critical_multiplier × threshold (default 3×). Independent of
    # cost_watchdog — cost_watchdog tracks long-horizon waste, spend_alert
    # tracks intraday bursts that need same-day operator intervention.
    "spend_alert": "warn",
    # Embedding 429 storms + quota_exceeded — the bot is degraded (memory
    # search falling back to local) but session UX still works.
    "embedding_monitor": "warn",
    # pod_report's broken-bucket lines (Pod silent, metrics_outage,
    # gateway down) — warn; explicit per-emit alert override available.
    "pod_report": "warn",
    # Cache-health and bot-engagement observations. These are pure
    # telemetry that feed the cache_ttl_tuner generator (which is the
    # operator-action surface — it emits an UpdateAgentDefaults proposal
    # to flip cacheRetention, or an Investigation pointing at prompt
    # churn). The raw Signal isn't operator-actionable on its own — the
    # right response is either "approve the cache_ttl_tuner Proposal"
    # (which appears on the Proposals page) or "do nothing" (the bot's
    # usage pattern is incidental). Demoted from warn → info in the
    # 2026-06-04 quality-control pass after the operator flagged
    # cache_invalidation_elevated Signals as alert-page noise: "either
    # there's a proposed action or it's nothing concrete to do." With
    # info-tier, the observation is preserved (the cache_ttl_tuner
    # generator still consumes it) but it doesn't crowd the default
    # Alerts view; the actionable Proposal lands on Reports → Proposals
    # where it belongs. bot_unused was already explicitly info per emit;
    # the cache_* signals inherit the new info default.
    "session_economics": "info",
    # Gateway error-log patterns (MAX auth failure, Discord target invalid,
    # repeated tool delivery failures). Operator action required; warn tier.
    "bot_log_monitor": "warn",
    # Dispatcher-log loop patterns (FAILED sends, same-message thrashing).
    # Either alerts aren't arriving (silent swallow) or a structural fix is
    # being skipped in favor of recurring chat nag — operator action either
    # way; warn tier with per-emit alert escalation for severe failure rates.
    "alerts_loop_monitor": "warn",
    # Bots running an older Evolve commit than the admin server. Not an
    # outage, but the deployed pod isn't running latest code until the
    # next `evolve-admin deploy <bot>` (or install-infra-jobs) sweep.
    "deploy_drift_monitor": "warn",
    # A managed calendar job (oc-log-rotate, retention, log-cap, …) whose
    # last run exited non-zero — launchd's LastExitStatus surfaced as a
    # Signal (PR #2669 follow-up). The job won't retry until its next
    # scheduled window, so the broken state persists for a day: real
    # breakage needing operator action, but maintenance-shaped rather
    # than a user-facing outage. The cannot_escalate meta-signal (probe
    # tooling failure) rides the same default.
    "cron_exit_monitor": "warn",
    # Nightly workspace backups bouncing 3+ times for a bot. Real
    # potential data-loss exposure, not user-facing — warn tier. The
    # producer escalates per-emit to "alert" once a bot has missed 7+
    # consecutive runs (a week of bouncing = real state loss exposure
    # if restore is ever needed). See packages/analyzer/backup_signal.py.
    "backup_signal": "warn",
    # An evolve caller is invoking the openclaw CLI with the wrong shape
    # (missing requiredOption, unknown flag, etc.) — symptom of an upstream
    # rename the caller hasn't caught up to. System continues but the call
    # site is silently broken; warn so the operator sees it on the Alerts
    # page rather than buried in a log file. Background: docs/diagnosis-spend-alert-openclaw-flag-2026-05-21.md.
    "oc_cli": "warn",
    # Integration probe — dashboard's pluggable connectivity check per
    # integration. Per-emit severity escalates to alert for credential-
    # invalid / hard-down findings; warn default for the soft cases
    # (degraded, stale workspace).
    "integration_probe": "warn",
    # Pod-wide health checks (launchd daemons, port binds, file ACLs). The
    # producer overrides per-emit when a check is critical (host_health is
    # the separate alert-tier producer for host-level resource exhaustion).
    "pod_health": "warn",
    # MCP server connectivity / credentials / drift. Drift and unknown
    # servers default to warn; the producer escalates to alert per-emit
    # for "bot can self-mutate MCP/plugins config" and credential-invalid.
    "mcp_monitor": "warn",
    # Per-bot permission posture (drift, dangerous combinations, approval
    # volume above thresholds). Per-emit alert for dangerous combos +
    # denylist matches.
    "permission_monitor": "warn",
    # Plugin enable/disable + allowlist drift. Per-emit alert for "denied
    # plugin enabled" and "plugin installed from unauthorized source".
    "plugin_monitor": "warn",
    # Out-of-band changes to a bot's group allowlist (channels.<ch> group
    # list) vs the admin-recorded baseline. Added senders can spend the bot's
    # tokens in the channel — a governance exposure the operator is otherwise
    # blind to between admin GETs — but not a user-facing outage; warn. The
    # sibling "unreadable" finding (drift check blind on a bot) rides the same
    # warn default. Detection-only (R1a PR3). See
    # packages/analyzer/roster_allowlist_drift_monitor.py.
    "roster_allowlist_drift": "warn",
    # Webhook ingress + transformsDir state. Per-emit alert when ingress
    # is on but baseline says off (silent inbound surface).
    "hook_monitor": "warn",
    # Content-scan misses on bot config files. Per-emit overrides for
    # severity vary; warn is the right default ("file missing or
    # unreadable" → operator action needed but not on-fire).
    "content_scan": "warn",
    # Bot install integrity (file ownership, /evolve/status liveness,
    # validator pass). User-visible regression but recoverable via deploy.
    "install_integrity_monitor": "warn",
    # Per-bot exec outcome attribution after policy enforcement. Drives
    # the exec-outcome investigator; per-emit overrides for severity.
    "exec_outcome_watchdog": "warn",
    # WatchdogEvent → Signal mirror. Severity is carried per-event from
    # the watchdog catalog (see generators/evolve_watchdog/events.py).
    "evolve_watchdog": "warn",
    # Proposals stuck in pending past the staleness window — backlog
    # signal. Warn so it surfaces; the proposal itself is the action.
    "stuck_proposal_monitor": "warn",
    # Anthropic admin API ingest discrepancies (our ledger ≠ their total).
    # Warn — the discrepancy needs reconciliation but is not user-affecting.
    "anthropic_admin_ingest": "warn",
    # Auth-drift filler (config_intent-aware drift detector for auth
    # configs). Per-emit overrides where dangerous; warn default.
    "auth_drift_filler": "warn",
    # Slack-side config drift detector (probes bot's Slack posture vs.
    # admin's slack-policy.json). Per-emit alert for some categories.
    "slack_config_probe": "warn",
    # Static app-manifest verifier — finds structural defects in deployed
    # app manifests. Per-emit alerts for permission-sensitive misses.
    "app_structural_verifier": "warn",
    "app_manifest_monitor": "warn",
    # Agent-invoked app scripts that FAIL at runtime (the "(agent) failed"
    # exec chip leaking into chat). Sibling to agent_bypass_audit; behavioral
    # integrity is operationally important but not page-the-operator urgent,
    # so warn — the agent freelances past the script's controls but nothing
    # is on fire. Spec: docs/spec-agent-freelance-bypass-2026-06-05.md.
    "app_script_failure_audit": "warn",
    # Proactive-delivery monitor (U2.1) — a scheduled user-facing app
    # missed its delivery window. Per-emit policy per spec §7:
    # app_delivery_missed warn on first miss, escalated to alert by the
    # producer at >=2 consecutive misses; app_delivery_unmeasurable info,
    # escalated to warn after 3 consecutive blind windows. Central
    # default warn covers the headline (missed) type.
    "delivery_monitor": "warn",
    # Post-OC-upgrade send-surface verification (the 2026-06-11 P0 guard,
    # emitted from `evolve-admin menu upgrade`). It only ever fires when
    # the just-installed OpenClaw broke the path every scheduled
    # user-facing send goes through — pod-wide outage, alert tier.
    "send_surface_probe": "alert",
    # Black-box end-to-end probe of the "evo" keyword path
    # (gateway plugin → admin /api/evo/dispatch). Fires only when that
    # pod-wide path is broken — users typing `evo …` get generic OpenClaw
    # help instead of the evo surface. Pod-wide user-facing outage of a
    # shared path; alert tier (sibling to send_surface_probe).
    "evo_path_probe": "alert",
    # Infra-audit run status (visudo, ACL bootstrap, sudoers integrity).
    # Per-emit alerts for hard failures; warn default for partial-pass.
    "infra_audit": "warn",
    # repo-puller wedge — deploy checkout can't fast-forward, so every
    # daemon will run stale code until cleared. User-actionable but not
    # user-facing; warn tier. The producer still dispatches the chat
    # alert directly via alerts.dispatcher; the Signal is parallel
    # structured state for Evo / Alerts UI.
    "repo_puller": "warn",
    # Forge apply-time outcomes (audit slate S2, 2026-07-02): a Phase 4.5
    # scheduled-action materialize failure means the app shipped but its
    # schedule isn't live (silent-dead-app class) — operator action needed
    # (fix + re-forge), not an outage; warn. The producer's other type
    # (forge_install_cost_overrun) passes explicit severity per emit.
    "forge_engine": "warn",

    # ── info (advisory) ────────────────────────────────────────────────────
    # Proposal-volume and synthesizer status are informational; the
    # proposals themselves are the actionable layer, not the signals.
    "proposal_synthesizer": "info",
    # Upstream maintainer activity on issues we filed — informational by
    # default. The operator-facing urgency is "see this before forgetting
    # we filed the issue", not "drop everything." Catalog event hosts the
    # per-event subscription frequency for operators who want digest mode.
    "upstream_issues_watcher": "info",
    # OpenClaw surface drift — update_watcher diffed a candidate OC release's
    # surface (CLI/extensions/channels/config-schema) against the installed
    # one. Default info (a surface changed, but nothing Evolve depends on);
    # the producer escalates per-emit to warn when the change touches a
    # surface declared in oc_deps.OC_DEPENDENCIES (a real "Evolve needs
    # updating" task). Mirrors the model_discovery info→warn pattern.
    "oc_surface_drift": "info",
    # Bot self-recoveries from a previously-firing error condition. Tier
    # info — the operator-visible chip is already green, the Signal lives
    # for the history trail + downstream surfaces that don't read the
    # bot's status JSON directly.
    "bot_recovery_monitor": "info",
    # Backup audit signal — advisory notes about backup repo contents
    # (e.g. "carries N non-cloud paths"). Operator may want to know;
    # not actionable in the moment.
    "backup_audit_signal": "info",
    # Local backup (Time Machine) coverage advisories. The Time Machine
    # exclusion check is "FYI, these paths are excluded" — informational
    # context, not a fire. Per-emit override available for hard
    # coverage failures.
    "local_backup_signal": "info",
    # Manifest reflex — fires when a bot builds a new app mid-session.
    # Advisory: the operator should see what got built but it's not a
    # failure. The manifest itself is reviewed via the App page.
    "manifest_reflex_runner": "info",
    "manifest_reflex_scanner": "info",
    # Code-quality monitor — advisory; lints/format-check style findings
    # that aren't user-affecting.
    "code_quality_monitor": "info",
    # Monitor-coverage tracker — meta-signal about which monitors have
    # been firing. Advisory; the firing/quiet monitors themselves carry
    # the operational urgency.
    "monitor_coverage": "info",
    # Cascade audit summary — periodic telemetry roll-up of tier-cascade
    # decisions for calibration. Advisory; the per-decision Signals carry
    # any urgency.
    "cascade_audit": "info",
    # Openclaw posture summary — periodic snapshot of bot security
    # posture. Per-emit warn/alert for specific findings; the umbrella
    # producer default is info.
    "openclaw_posture": "info",
    # Model discovery — "a new model line exists; consider adopting it".
    # Advisory: the model isn't broken, it's just not in a rung. The
    # generator's Investigation Proposal is the actionable layer. Note: the
    # generator escalates per-emit to ``warn`` for its degraded-mode Signal
    # (a failed listing call), so a degraded run is louder than a discovery.
    "model_discovery": "info",
    # Per-bot utilization baseline (bot_underused). Advisory: nothing is
    # broken — the bot is idle, and the find-or-create dedup means at most
    # one ping per bot, ever. The producer escalates per-emit to ``warn``
    # for its pod-scope coverage signal (most of the fleet unmeasurable =
    # the recording pipeline is broken, not idle bots). Promotion of
    # bot_underused to warn is an open question pending calibration
    # (docs/spec-value-baseline-2026-06-10.md §6.4 / §11.2).
    "value_baseline": "info",
}

DEFAULT_SEVERITY: Severity = "warn"


def default_severity(producer: str) -> Severity:
    """Return the central default severity for a producer.

    Unmapped producers fall through to DEFAULT_SEVERITY — but missing
    entries are noise; new producers should be added to PRODUCER_SEVERITY
    explicitly so the policy is reviewable.
    """
    return PRODUCER_SEVERITY.get(producer, DEFAULT_SEVERITY)


# Producers whose *normal* output is advisory — telemetry, discovery, or a
# "nothing is broken, here's a thing you might want to know" observation.
# These default to ``activity`` (the triage lane) instead of the
# ``maintenance`` task inbox. Every producer here is also info-severity in
# PRODUCER_SEVERITY (the info ⟹ activity correlation the guard test pins);
# the override-when-divergent rule still applies per-emit (e.g.
# model_discovery's degraded signal passes ``flavor="maintenance"`` because
# *that* finding is a real fix-it task, not a discovery).
#
# Producers NOT listed here fall through to DEFAULT_FLAVOR (``maintenance``),
# which is the conservative "loud / task-inbox" default — appropriate for the
# breakage-shaped monitors (heal, audit, host_health, the structural
# verifier's genuine-defect findings, …) that make up the bulk of producers.
PRODUCER_FLAVOR: dict[str, Flavor] = {
    # Model discovery — "a new model line exists; consider adopting it".
    # Nothing is broken; the model just isn't in a rung yet. The degraded
    # and no-listing-adapter signals override to maintenance per-emit.
    "model_discovery": "activity",
    # Per-bot utilization baseline (bot_underused). Nothing is broken — the
    # bot is idle. The pod-coverage signal overrides to maintenance per-emit
    # (most of the fleet unmeasurable = the recording pipeline is broken).
    "value_baseline": "activity",
    # Cache-health / engagement telemetry that feeds the cache_ttl_tuner
    # generator. The raw observation isn't operator-actionable on its own —
    # the actionable layer is the Proposal it feeds. Advisory.
    "session_economics": "activity",
    # Meta-signal about which monitors have been firing. Advisory roll-up.
    "monitor_coverage": "activity",
    # Periodic tier-cascade telemetry roll-up for calibration. Advisory.
    "cascade_audit": "activity",
    # Proposal-volume / synthesizer status. The proposals themselves are the
    # actionable layer, not these status signals. Advisory.
    "proposal_synthesizer": "activity",
    # Upstream maintainer activity on issues we filed. "See this before
    # forgetting we filed it", not "drop everything". Advisory.
    "upstream_issues_watcher": "activity",
    # OpenClaw surface drift — "version X changed these surfaces". Advisory by
    # default; the producer overrides to maintenance per-emit when a changed
    # surface is one Evolve declares it depends on (then it IS a fix-it task).
    "oc_surface_drift": "activity",
    # Bot self-recoveries from a previously-firing error. The operator-visible
    # chip is already green; the signal is the history trail. Advisory.
    "bot_recovery_monitor": "activity",
    # Backup-repo content advisories ("carries N non-cloud paths"). FYI.
    "backup_audit_signal": "activity",
    # Time Machine coverage advisories ("these paths are excluded"). FYI.
    "local_backup_signal": "activity",
    # Mid-session app builds. The operator should see what got built, but it's
    # not a failure — the manifest is reviewed via the App page. Advisory.
    "manifest_reflex_runner": "activity",
    "manifest_reflex_scanner": "activity",
    # Lint / format-check style findings that aren't user-affecting. Advisory.
    "code_quality_monitor": "activity",
    # Periodic bot security-posture snapshot. The umbrella producer is
    # advisory; specific findings override to maintenance per-emit.
    "openclaw_posture": "activity",
}

DEFAULT_FLAVOR: Flavor = "maintenance"


def default_flavor(producer: str) -> Flavor:
    """Return the central default flavor for a producer.

    Unmapped producers fall through to DEFAULT_FLAVOR (``maintenance``) —
    the conservative, loud default for breakage-shaped monitors. Advisory
    producers must be listed in PRODUCER_FLAVOR so the "this is FYI, not a
    task" decision is visible in version control (and pinned by the
    info ⟹ activity guard test).
    """
    return PRODUCER_FLAVOR.get(producer, DEFAULT_FLAVOR)
