"""analyzer_monitor_jobs — launchd/systemd installers for the pod-wide analyzer
Signal-producing monitors.

Extracted from ``deploy.py`` to keep that hot-hazard file under its 4.1a
no-growth cap (``tools/file-size-baseline.txt``). These installers are thin
wrappers over ``deploy._install_launchd`` that pin each monitor's label,
script, schedule, and args; grouping them here keeps deploy.py focused on the
orchestration and gives the analyzer monitors one obvious home.

All run as the ``evolve`` user, pod-wide (no per-bot suffix). The shared
``deploy`` symbols (``_install_launchd``, ``ANALYZER_DIR``, the canonical
shared-dir / network paths) are imported lazily inside each function so this
module carries no import-time dependency on deploy (and vice versa — deploy
imports the installer lazily at its call site, so there is no import cycle).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .deploy import DeployResult


# Launchd labels for every monitor this module installs. Lives here rather than
# in ``deploy.expected_plist_labels`` so adding a monitor is ONE edit next to its
# installer — deploy.py splices this in via ``_analyzer_monitor_labels()``. A
# label missing from this tuple gets swept as an orphan plist on the next deploy.
ANALYZER_MONITOR_PLIST_LABELS: tuple[str, ...] = (
    "ai.openclaw.evolve.capability_gap_monitor",     # daily 03:15: walks per-bot ObservationTuples, clusters by noun, emits app_suggester_gap Signals where the conversational theme has no installed app coverage AND the bot's AGENTS.md doesn't contradict. Pure Python, no LLM. Closes the producer side of the app_suggester grounding contract — without it, app_suggester is silent. Installed by analyzer_monitor_jobs.install_capability_gap_monitor. Spec: docs/spec-rsi-proposal-eligibility-2026-06-05.md §"Phase 2".
    "ai.openclaw.evolve.engagement_amplifier_monitor", # daily 03:30: walks per-bot ObservationTuples, clusters by (noun, verb), applies engagement + mood + recurrence gates + AGENTS.md objective check, emits engagement_amplification_opportunity Signals for working patterns worth deepening. Companion to capability_gap_monitor (gaps vs. amplifications). Consumed by the engagement_amplifier generator. Pure Python, no LLM. Installed by analyzer_monitor_jobs.install_engagement_amplifier_monitor. Spec: docs/spec-rsi-proposal-eligibility-2026-06-05.md §"Phase 2".
    "ai.openclaw.evolve.evo_path_probe_monitor",     # every 30 min: black-box end-to-end probe of the "evo" keyword path (gateway plugin → admin /api/evo/dispatch) over BOTH the cookieless TCP loopback (faithful EvoDispatchClient replica — catches the device-auth 401 class, #3257) AND the admin-daemon unix socket (catches the ENOENT/EACCES path/perm break class, #3160/#3223). Fires ONE pod-wide Signal per transport (producer=evo_path_probe, type=evo_path_down) when broken; sweep_resolves on recovery. Pure Python, no LLM. Watched by monitor_coverage's producer-liveness layer. Installed by analyzer_monitor_jobs.install_evo_path_probe_monitor. Spec: docs/spec-reports-2026-06-12.md.
    "ai.openclaw.evolve.model_liveness_monitor",  # daily 04:40: model liveness probe (auto-upgrade Phase 2, spec-model-auto-upgrade-2026-07-30 §Liveness guard). One REAL agent turn per routed model (each bot's role-resolved models[0] targets, deduped pod-wide, capped at model_liveness.MAX_PROBES_PER_RUN) through the same gateway dispatch path user messages ride — sudo -u <bot> openclaw agent --json --model <target>, via oc_cli. Catches the #3489 class: a provider block dispatching to the wrong transport 401s like key expiry while `models list` stays green, leaving rungs silently dead fleet-wide. Fires model_dispatch_failed per dead/rerouted target; sweeps ONLY targets this run probed ok (unverified probes and unprobed targets — spot-checks, cap-dropped, broken-bot — never sweep; fail-safe). COSTS REAL TOKENS — each probe pays the bot's full system-prefix input (~10-27k tokens/turn) as a 1.25x-rate CACHE WRITE that never gets read back (fresh session id per probe + ~24h cadence vs a 5-min cache TTL), billed to the probing bot; the one LLM-dispatching monitor; cadence stays daily. MEASURED on evolve-vps Aug 2026: ~$0.89/run across 4 resolved targets, of which claude-opus-4-8 alone is ~$0.75 (84%) — ~$27/month, not the "low-single-digit dollars/month" this comment used to claim. Read+dispatch only, never mutates config. Watched by monitor_coverage's producer-liveness layer. Installed by analyzer_monitor_jobs.install_model_liveness_monitor.
    "ai.openclaw.evolve.vocabulary_expander_monitor",  # weekly Sun 03:45: Layer 2 plumbing for runtime vocab expansion. Walks per-bot ObservationTuples, identifies unrecognized nouns (not in static + dynamic vocab), writes a preflight report when the opt-in flag (network.json::rsi.vocabulary_expansion.enabled) is true AND the unrecognized-noun count clears the threshold. NO LLM in this monitor — preflight only. PR γ will wire the actual per-bot LLM call. Default off → zero cost when off. Installed by analyzer_monitor_jobs.install_vocabulary_expander_monitor. Spec: docs/spec-rsi-proposal-eligibility-2026-06-05.md §"Phase 2 / Layer 2".
    "ai.openclaw.evolve.autogen_volume_monitor",  # daily 04:50: forward-discipline disk-output backstop. Walks {shared_dir}/* and each bot's ~/.openclaw/workspace/evolve/* and fires an autogen_volume_exceeded Signal (sweep_resolves on drain) for any auto-gen dir past its per-dir file/byte budget (seeded from the footprint audit, tunable via network.json::footprint.autogen_volume). Reactive complement to the author-time declaration lint — catches the existing-producer leak class (the 537 MB audit_outbox/_ingested bloat). Pure Python, no LLM. Watched by monitor_coverage's producer-liveness layer. Installed by analyzer_monitor_jobs.install_autogen_volume_monitor. Spec: docs/spec-footprint-2026-06-18.md + docs/footprint-disk-output-audit-2026-06-28.md.
    "ai.openclaw.evolve.roster_allowlist_drift_monitor",  # hourly: out-of-band group-allowlist drift safety net (R1a PR3). Compares each bot's live on-disk effective group allowlist (channels.<ch> group list, via roster_resolver) against the admin-recorded expected-state baseline (roster_baseline, written by the group-allowlist approve/revoke routes; monitor seeds once on first sight). Fires roster_group_allowlist_changed per drifted (bot, channel) — added senders = the risk (can spend the bot's tokens) — plus roster_group_allowlist_unreadable per bot whose openclaw.json can't be read (a blind tick must not read as clean). Detection-only; enforcement stays OpenClaw's fail-closed gate. Pure Python, no LLM. Watched by monitor_coverage's producer-liveness layer. Installed by analyzer_monitor_jobs.install_roster_allowlist_drift_monitor. Spec: docs/spec-users-meta-2026-06-15.md §"Deferred to a follow-up (PR3)".
    "ai.openclaw.evolve.bootstrap_size_monitor",  # hourly: per-bot OC-ingested bootstrap file sizes (AGENTS.md, SOUL.md, TOOLS.md, IDENTITY.md, USER.md, HEARTBEAT.md, BOOTSTRAP.md, MEMORY.md — context_health.OC_BOOTSTRAP_INGESTED) vs the bot's effective agents.defaults.bootstrapMaxChars / bootstrapTotalMaxChars from its openclaw.json (OC defaults 12000/60000 when absent). Fires bootstrap_truncation_risk per bot (warn ≥85% of a cap, alert ≥100% — OC is truncating and the file's tail is silently dead in every turn's context; the 2026-08-01 primary-bot AGENTS.md incident), plus bootstrap_size_unreadable per bot whose config/workspace can't be read even via sudo-cat (a blind tick must not read as clean). Reads via the evolve ACL with sudo /bin/cat fallback; handles the primary bot's evo account via evolve_config.bot_home. Report-only. Pure Python, no LLM. Watched by monitor_coverage's producer-liveness layer. Installed by analyzer_monitor_jobs.install_bootstrap_size_monitor. Spec: docs/spec-context-observability-2026-07-30.md §Implementation contract.
    "ai.openclaw.evolve.orphaned_bot_account_monitor",  # daily 04:20: host accounts that carry an Evolve-provisioned OpenClaw install (workspace/evolve, workspace/manifests, or workspace/evolve-backup under ~/.openclaw) but back no member of the network.json roster — a decommissioned bot whose account, home, and credentials outlived it. Roster side resolves through evolve_config.get_bot_user, so a bot running under a different account name (team_bot_b→shared_account_b, primary evolve→evo on the reference pod) is never mis-reported as an orphan, and an operator's personal OpenClaw install (no Evolve markers) is never mis-reported as a bot. Fires orphaned_bot_account per account carrying the VALUE-FREE credential inventory from bot_credential_inventory (openclaw.json secret-shaped fields by dotted path + length, ~/.ssh private keys, credential dirs, the per-agent auth store) plus a revoke destination for each, and orphaned_bot_account_unreadable when the account list or one account's install can't be read (a blind tick must not read as clean — and a failed enumeration skips the sweep entirely rather than mass-resolving every live orphan). REPORT-ONLY — never deletes an account, a home, or a credential; delete-bot stays an explicit operator decision and channel-token revocation is a platform action Evolve cannot perform. NOT covered by audit_machine's user-account baseline: that check frames the same account as "🔴 CRITICAL: New user account(s) detected", which reads as a false positive (the 2026-08-02 ledger finding sat unactioned for seven weeks). Pure Python, no LLM. Watched by monitor_coverage's producer-liveness layer. Installed by analyzer_monitor_jobs.install_orphaned_bot_account_monitor. Design: docs/design-bot-account-lifecycle-2026-08-02.md.
    "ai.openclaw.evolve.roster_coherence_monitor",  # hourly: roster<->OpenClaw store-coherence reconcile (M1-B3). Per bot, harvests every identity the openclaw.json names across every allowlist-capable channel_registry channel (DM allowFrom, groupAllowFrom, and nested guild/channel `users` lists — Discord's guilds.<gid>.users included) and reports any that no Evolve roster source knows: the canonical roster_resolver join, the per-bot overlay (identities/blocked/ignored), and network.json's primary_user + pod admins. Fires roster_oc_identity_unknown per (bot, channel) carrying the unknown ids, plus roster_coherence_unreadable per bot whose config OR roster can't be read (a blind tick must not read as clean). REPORT-ONLY — never writes a roster, an openclaw.json, or network.json. NOT covered by roster_allowlist_drift_monitor: that one seeds its baseline from the live config on first sight, so a pre-existing identity is adopted as expected and stays permanently invisible. Pure Python, no LLM. Watched by monitor_coverage's producer-liveness layer. Installed by analyzer_monitor_jobs.install_roster_coherence_monitor. Spec: docs/spec-users-meta-2026-06-15.md §"M1 — Multi-platform messaging per bot" finding 3 / bite B3.
    "ai.openclaw.evolve.model_swap_watch",  # daily 05:05: post-swap behavior check on every model-rung change recorded in {shared_dir}/model_swaps.jsonl (model_swap_ledger, written by the admin tier-write path). Per (bot, tier) swap older than SETTLE_DAYS, splits the bot's turn annotations at the swap instant and compares the TERSE-REPLY RATE (share of turns at/below 40 output tokens — a proxy for how often the bot stays quiet) on the swapped rung against the bot's OTHER rungs as an in-bot control. Fires model_swap_behavior_divergence only when the swapped rung collapses while the control holds, so a pod-wide traffic change is ignored and only a model-specific regression fires; sweep_resolves on recovery or when the swap ages out. Catches the 2026-08-14 class: a swap verified as a STRING (the id landed in the tier) but never as a BEHAVIOR — the new model stopped honoring OC's NO_REPLY sentinel and leaked its should-I-reply deliberation into four requireMention:false Slack channels for five days. Reads existing annotations only — no new instrumentation, no LLM. Watched by monitor_coverage's producer-liveness layer. Installed by analyzer_monitor_jobs.install_model_swap_watch. Design: docs/design-model-swap-behavior-guard-2026-08-19.md.
)


def install_capability_gap_monitor(result: "DeployResult") -> None:
    """Install capability_gap_monitor (daily 03:15, evolve user).

    Per Phase 2 of the RSI eligibility spec
    (``docs/spec-rsi-proposal-eligibility-2026-06-05.md``), this monitor
    closes the producer side of app_suggester's
    ``app_suggester_gap`` Signal contract. Without it, app_suggester
    is RSI-shaped but silent — its catalog_match alone sits below the
    confidence floor.

    The monitor walks per-bot ObservationTuples over the last 30 days,
    clusters by noun, maps recurring nouns → domain tags via the same
    keyword vocabulary app_suggester uses for coverage, applies the
    strength gate (≥3 distinct sessions, ≥10 engagement, ≥3 days),
    and runs an objective check against the bot's workspace AGENTS.md
    before emitting Signals. Pure Python, no LLM. Runs as the evolve
    user pod-wide; iterates network.json::bots.

    Cadence: daily 03:15 — capability patterns don't change minute-to-
    minute, and the 30-day window is read-heavy enough that we don't
    want it on the hourly cycle. The 03:15 slot puts it after the
    01:30 tuples extraction (so today's data is included) and well
    before the 09:00 daily digest produces operator-facing content.
    """
    from .deploy import ANALYZER_DIR, _CANONICAL_SHARED_DIR, _install_launchd
    _install_launchd(
        label="ai.openclaw.evolve.capability_gap_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "capability_gap_monitor.py",
        schedule={"Hour": 3, "Minute": 15},
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR)],
        run_at_load=False,
        jitter_seconds=120,
    )


def install_engagement_amplifier_monitor(result: "DeployResult") -> None:
    """Install engagement_amplifier_monitor (daily 03:30, evolve user).

    Second Phase 2 RSI substrate producer per
    ``docs/spec-rsi-proposal-eligibility-2026-06-05.md``. Companion to
    the capability_gap_monitor:

      - capability_gap_monitor (03:15): users want X, no app covers it
      - engagement_amplifier_monitor (03:30): users use X heavily, no
        formal "deepen this" mechanism

    Together they cover both directions of objective-aware RSI on
    Recommendations.

    For each bot the monitor walks ObservationTuples over the last 30
    days, clusters by (noun, verb), applies engagement + mood +
    recurrence gates, runs an objective categorization (confirmed vs.
    emergent) against the bot's workspace AGENTS.md, and emits
    ``engagement_amplification_opportunity`` Signals. Capped at 3
    Signals per bot per run (top engagement_total).

    Consumer: ``engagement_amplifier`` generator emits Phase A
    operator-first Proposals on surface=improvement.

    Cadence: daily 03:30 — same rationale as capability_gap_monitor
    (patterns don't change minute-to-minute, 30-day window is
    read-heavy). 15 minutes after the gap monitor so they don't
    compete for the same tuple cache.
    """
    from .deploy import ANALYZER_DIR, _CANONICAL_SHARED_DIR, _install_launchd
    _install_launchd(
        label="ai.openclaw.evolve.engagement_amplifier_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "engagement_amplifier_monitor.py",
        schedule={"Hour": 3, "Minute": 30},
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR)],
        run_at_load=False,
        jitter_seconds=120,
    )


def install_evo_path_probe_monitor(result: "DeployResult") -> None:
    """Install evo_path_probe_monitor (every 30 min, evolve user).

    Synthetic end-to-end probe for the "evo" keyword path — the gateway
    plugin → admin ``/api/evo/dispatch`` call that a user typing ``evo help``
    rides. That path has broken pod-wide more than once (device-auth 401 on
    the plugin's cookieless TCP RPC, #3257; admin-daemon.sock path/perm
    breaks, #3160/#3223) and EVERY time was caught by a human, never a
    monitor. This probe replicates the plugin's cookieless request over both
    the TCP loopback and the admin-daemon unix socket and fires a pod-wide
    Signal (producer ``evo_path_probe``, type ``evo_path_down``) when it
    breaks; ``sweep_resolve`` clears it on recovery. Pure Python, no LLM.

    Runs as the evolve user (no per-bot suffix — the gate/transport is
    pod-wide, so one probe covers the pod). 30-min cadence balances fast
    detection of a user-facing outage against cost (two tiny loopback calls
    per run); jitter keeps it off the other 30-min jobs' tick.

    ``run_at_load=False`` deliberately: firing on every (re)load would land
    the probe in the deploy window when the admin-ui daemon may itself be
    mid-restart, producing a transient false RED (ECONNREFUSED). The explicit
    post-deploy Gate-2 check is served instead by invoking the probe with
    ``--gate`` (exits non-zero on RED) AFTER the daemon is confirmed up, so the
    scheduled job stays flap-free.

    Spec: docs/spec-reports-2026-06-12.md (signal-producer quality + UX).
    """
    from .deploy import ANALYZER_DIR, _CANONICAL_SHARED_DIR, _install_launchd
    _install_launchd(
        label="ai.openclaw.evolve.evo_path_probe_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "evo_path_probe_monitor.py",
        schedule={"interval": 30 * 60},
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR)],
        run_at_load=False,
        jitter_seconds=60,
    )


def install_model_liveness_monitor(result: "DeployResult") -> None:
    """Install model_liveness monitor (daily 04:40, evolve user).

    Auto-upgrade Phase 2's probe, standing alone as a provider-health monitor
    (``docs/spec-model-auto-upgrade-2026-07-30.md`` §Liveness guard). One real
    agent turn per model the pod actually routes to, dispatched as the routing
    bot through the SAME gateway path a user message rides — because that is
    the path #3489 broke while the capability resolver kept reporting the
    models available. Fires ``model_dispatch_failed`` (producer
    ``model_liveness_probe``) per dead or silently-rerouted target;
    ``sweep_resolve`` clears recovered ones. Read + dispatch only — never
    mutates any config, rung, or routing (Phase 3 does not exist yet).

    THIS MONITOR COSTS REAL TOKENS — the only LLM-dispatching monitor in this
    module, and not cheap per turn: ``openclaw agent`` runs the bot's full
    embedded agent, so each probe pays the bot's whole system-prefix input
    (~10-27k tokens on this fleet), billed to the probing bot's own cost
    accounting. MEASURED on evolve-vps in August 2026: **~$0.89 per run**
    across 4 resolved targets, of which ``claude-opus-4-8`` alone is ~$0.75
    (84%) — **~$27/month** at the daily cadence. The prefix is billed as a
    cache WRITE at 1.25x input rate, and ``cache_read`` was 0 on every probe
    all month: a fresh session id per probe plus a ~24h cadence against a
    5-minute cache TTL means the cache can never be hit, so the 1.25x is paid
    every single time. Earlier revisions of this docstring said "~8 turns/day,
    roughly low-single-digit dollars/month" — that was wrong by an order of
    magnitude and is why the real cost went unexamined until darwin tripped
    its $5.00/day breaker on 2026-08-19.

    The spend is bounded by design: probes only the role-resolved
    (``models[0]``-reachable) targets deduped pod-wide, capped at
    ``model_liveness.MAX_PROBES_PER_RUN`` per run, one-line prompt / one-word
    reply / thinking off, DAILY cadence. Dropped targets are named in the run
    summary rather than silently skipped — and their outage Signals are never
    sweep-resolved by a run that did not probe them.

    04:40 sits after the nightly extraction/monitor cluster and clear of the
    09:00 digest. ``run_at_load=False`` is what this docstring used to credit
    with keeping deploys from firing a probe burst while gateways may be
    mid-restart — **it never did that**. ``run_at_load`` shapes unit CONTENT
    only, and the deploy bounce
    (``deploy._install_job_ensuring_restart``) force-RAN the job on the
    byte-identical skip path regardless, several times a day. Fixed by making
    that bounce skip timer-activated oneshot jobs
    (``runtime.scheduler.is_timer_activated_oneshot``); ``run_at_load=False``
    is still correct, but it is not the protection — the discriminator is.
    """
    from .deploy import ANALYZER_DIR, _CANONICAL_SHARED_DIR, _install_launchd
    _install_launchd(
        label="ai.openclaw.evolve.model_liveness_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "model_liveness.py",
        schedule={"Hour": 4, "Minute": 40},
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR)],
        run_at_load=False,
        jitter_seconds=120,
    )


def install_vocabulary_expander_monitor(result: "DeployResult") -> None:
    """Install the vocabulary_expander_monitor (weekly, Sunday 03:45,
    evolve user). Layer 2 plumbing for RSI vocabulary expansion per the
    Phase 2 / Layer 2 design discussion.

    The monitor checks ``network.json::rsi.vocabulary_expansion.enabled``
    (default False) and exits silently when off — zero cost. When
    enabled, it walks per-bot ObservationTuples over the last 30 days,
    identifies unrecognized nouns (not resolving via the merged
    static ∪ dynamic vocabulary), and writes a preflight report to
    ``{shared_dir}/vocabulary/preflight/<bot>.json`` describing what
    an LLM expansion call *would* look like — but does NOT make any
    LLM call itself.

    PR γ will wire the actual per-bot LLM call (using each bot's own
    credentials per the per-bot inference rule).

    Cadence: weekly because vocabulary drift is a slow phenomenon and
    a daily cycle would write redundant preflight reports. Sunday at
    03:45 puts it after the daily pattern monitors and on a day where
    no pattern producer competes for IO. Jitter ±120s.
    """
    from .deploy import (
        ANALYZER_DIR,
        _CANONICAL_NETWORK_JSON,
        _CANONICAL_SHARED_DIR,
        _install_launchd,
    )
    _install_launchd(
        label="ai.openclaw.evolve.vocabulary_expander_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "vocabulary_expander_monitor.py",
        schedule={"Weekday": 0, "Hour": 3, "Minute": 45},  # Sunday
        result=result,
        extra_args=[
            "--shared-dir", str(_CANONICAL_SHARED_DIR),
            "--network", str(_CANONICAL_NETWORK_JSON),
        ],
        run_at_load=False,
        jitter_seconds=120,
    )


def install_autogen_volume_monitor(result: "DeployResult") -> None:
    """Install autogen_volume_monitor (daily 04:50, evolve user).

    Forward-discipline backstop for the footprint disk-output audit
    (``docs/footprint-disk-output-audit-2026-06-28.md``). Walks the auto-gen
    surfaces — ``{shared_dir}/*`` and each bot's
    ``~/.openclaw/workspace/evolve/*`` — and fires an ``autogen_volume_exceeded``
    Signal for any directory past its per-dir file/byte budget, naming the dir,
    current-vs-budget, and the top sub-path contributors. ``sweep_resolve``
    clears it once the directory drops back under budget. Budgets are seeded from
    the audit and operator-tunable via ``network.json::footprint.autogen_volume``.

    Reactive complement to the author-time declaration lint: the lint stops a NEW
    write-path from shipping without a retention + consumer contract; this monitor
    catches the EXISTING producers (e.g. the 537 MB audit_outbox/_ingested leak
    that went unnoticed until a manual sweep).

    Cadence: daily — disk bloat is slow-moving, and a daily recursive walk keeps
    the read cost off the hot cycles. 04:50 puts it after the daily retention /
    log-cap / oc-log-rotate maintenance jobs (03:35–04:30) so it measures the
    post-prune steady state, not mid-prune. Pure Python, no LLM. Iterates
    ``network.json::members``. Watched by monitor_coverage's producer-liveness
    layer.
    """
    from .deploy import (
        ANALYZER_DIR,
        _CANONICAL_NETWORK_JSON,
        _CANONICAL_SHARED_DIR,
        _install_launchd,
    )
    _install_launchd(
        label="ai.openclaw.evolve.autogen_volume_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "autogen_volume_monitor.py",
        schedule={"Hour": 4, "Minute": 50},
        result=result,
        extra_args=[
            "--shared-dir", str(_CANONICAL_SHARED_DIR),
            "--network", str(_CANONICAL_NETWORK_JSON),
        ],
        run_at_load=False,
        jitter_seconds=120,
    )


def install_roster_allowlist_drift_monitor(result: "DeployResult") -> None:
    """Install roster_allowlist_drift_monitor (hourly, evolve user).

    R1a PR3 safety net (docs/spec-users-meta-2026-06-15.md §"Deferred to a
    follow-up (PR3)"). After PR2 the group allowlist the OpenClaw gateway
    enforces (``channels.<ch>`` group list) is the SAME artifact the admin
    curates via the group-allowlist approve/revoke routes — but an
    OpenClaw-CLI edit, a hand-edit, or a membership sync that changes it
    out-of-band is invisible to the operator between admin GETs. This monitor
    compares each bot's live on-disk effective group allowlist against the
    admin-recorded baseline (``evolve_admin.roster_baseline``) and fires a
    Signal per drifted ``(bot, channel)`` (added senders = the risk), plus an
    ``unreadable`` Signal per bot whose openclaw.json can't be read (so a blind
    tick never reads as clean). Detection-only — no auto-remediation; enforcement
    stays OpenClaw's fail-closed gate.

    Cadence: hourly — a config edit persists until reverted (it does not flap),
    and an hour is a tight enough window on "who can spend the bot's tokens in a
    channel." Runs as the evolve user pod-wide; iterates network.json::members.
    Watched by monitor_coverage's producer-liveness layer.
    """
    from .deploy import (
        ANALYZER_DIR,
        _CANONICAL_NETWORK_JSON,
        _install_launchd,
    )
    _install_launchd(
        label="ai.openclaw.evolve.roster_allowlist_drift_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "roster_allowlist_drift_monitor.py",
        schedule={"interval": 3600},
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=False,
        jitter_seconds=120,
    )


def install_roster_coherence_monitor(result: "DeployResult") -> None:
    """Install roster_coherence_monitor (hourly, evolve user).

    M1-B3 (docs/spec-users-meta-2026-06-15.md §"M1 — Multi-platform messaging
    per bot", finding 3). Invariant 1 says the Evolve roster is canonical and
    the bot consumes a projection of it; this monitor detects the inverse — an
    identity present in a bot's ``openclaw.json`` (DM allowlist, group
    allowlist, or a nested guild/channel member list, across every
    allowlist-capable ``channel_registry`` channel) and absent from every Evolve
    roster source. It fires a Signal per ``(bot, channel)`` carrying the unknown
    identities, plus an ``unreadable`` Signal per bot whose config or roster
    can't be read (so a blind tick never reads as clean).

    Distinct from ``roster_allowlist_drift_monitor`` and NOT covered by it: that
    monitor seeds its baseline from the live config on first sight of a bot, so
    an identity already present before it ran is adopted as expected and stays
    permanently invisible. Coherence needs no baseline — the roster is the anchor.

    Report-only: no auto-remediation, no roster writes, no config writes.

    Cadence: hourly, matching the drift sibling — a config/roster divergence
    persists until reconciled (it does not flap), and an hour is a tight enough
    window on "who can reach this bot without the admin knowing." Runs as the
    evolve user pod-wide; iterates network.json::members. Watched by
    monitor_coverage's producer-liveness layer.
    """
    from .deploy import (
        ANALYZER_DIR,
        _CANONICAL_NETWORK_JSON,
        _install_launchd,
    )
    _install_launchd(
        label="ai.openclaw.evolve.roster_coherence_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "roster_coherence_monitor.py",
        schedule={"interval": 3600},
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=False,
        # Offset from the drift sibling's 120s so the two hourly roster monitors
        # don't contend for the same openclaw.json reads on the same tick.
        jitter_seconds=300,
    )


def install_bootstrap_size_monitor(result: "DeployResult") -> None:
    """Install bootstrap_size_monitor (hourly, evolve user).

    Motivated by the 2026-08-01 live finding: the primary bot's workspace
    AGENTS.md grew to 128k chars against its effective ``bootstrapMaxChars``
    of 40,000, so OpenClaw silently truncated 18 of 22 sections — including
    every anti-confabulation rule — out of every turn's context for an
    unknown period, with only a gateway log line as witness. This monitor
    compares each bot's OC-ingested bootstrap files (the fixed
    ``context_health.OC_BOOTSTRAP_INGESTED`` set) against the bot's effective
    ``agents.defaults.bootstrapMaxChars`` / ``bootstrapTotalMaxChars`` and
    fires ``bootstrap_truncation_risk`` per bot (warn at ≥85% — OC's own
    near-limit ratio; alert at ≥100% — content silently dead), plus
    ``bootstrap_size_unreadable`` per bot whose config or workspace can't be
    read (a blind tick must not read as clean). Report-only.

    Cadence: hourly — bootstrap files grow via memory flushes and operator
    edits (slow-moving, non-flapping), and an hour bounds how long a bot can
    run with its behavioral rules silently truncated. Cheap: ≤8 small file
    reads per bot, no LLM. Runs as the evolve user pod-wide; iterates
    network.json::members. Watched by monitor_coverage's producer-liveness
    layer.
    """
    from .deploy import (
        ANALYZER_DIR,
        _CANONICAL_NETWORK_JSON,
        _install_launchd,
    )
    _install_launchd(
        label="ai.openclaw.evolve.bootstrap_size_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "bootstrap_size_monitor.py",
        schedule={"interval": 3600},
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=False,
        # Offset from the roster siblings' 120s/300s so the hourly monitors
        # that all read per-bot openclaw.json don't contend on the same tick.
        jitter_seconds=420,
    )


def install_orphaned_bot_account_monitor(result: "DeployResult") -> None:
    """Install orphaned_bot_account_monitor (daily 04:20, evolve user).

    Motivated by the 2026-08-02 live finding: ``ledger`` was retired cleanly on
    2026-06-15 (``archived-bots/ledger-2026-06-15/STATUS.json`` →
    ``retire_complete``), and seven weeks later its account still held a live
    46-char Telegram bot token, a 48-char OpenClaw gateway token, and an SSH
    keypair. Nothing reported that, because ``retire-bot`` *deliberately*
    preserves the account and home (only ``delete-bot`` removes them) and no
    monitor named the resulting condition. The one check that fired —
    ``audit_machine``'s user-account baseline — called it a NEW user account,
    which reads as a false positive and was ignored for the whole seven weeks.

    This monitor cross-references host accounts against the roster and reports
    any account carrying an Evolve-provisioned OpenClaw install that backs no
    member, together with a value-free inventory of every credential still on
    disk and where to revoke each one. Report-only: no account deletion, no
    home deletion, no credential mutation.

    Cadence: daily — the condition is created by a single operator action
    (retiring or removing a bot) and then persists indefinitely; it cannot flap
    and does not need an hourly tick. The 04:20 slot sits after the 03:15-03:45
    analyzer monitors and well before the 09:00 digest, so a new orphan is
    named in the next morning's digest. Cheap: one ``pwd.getpwall()`` plus a
    handful of stat/read probes per non-roster account, no LLM. Runs as the
    evolve user pod-wide. Watched by monitor_coverage's producer-liveness layer.
    """
    from .deploy import (
        ANALYZER_DIR,
        _CANONICAL_NETWORK_JSON,
        _install_launchd,
    )
    _install_launchd(
        label="ai.openclaw.evolve.orphaned_bot_account_monitor",
        user="evolve",
        script_path=ANALYZER_DIR / "orphaned_bot_account_monitor.py",
        schedule={"Hour": 4, "Minute": 20},
        result=result,
        extra_args=["--network", str(_CANONICAL_NETWORK_JSON)],
        run_at_load=False,
        jitter_seconds=120,
    )


def install_model_swap_watch(result: "DeployResult") -> None:
    """Install model_swap_watch (daily 05:05, evolve user).

    The outcome watcher for the model-tier write path
    (docs/design-model-swap-behavior-guard-2026-08-19.md). The admin UI's
    tier write verifies that the model *string* landed and stops there; this
    monitor is what checks the bots still BEHAVE correctly afterwards.

    For each rung change in ``{shared_dir}/model_swaps.jsonl`` it compares the
    bot's terse-reply rate on the swapped rung either side of the swap
    instant, against the bot's other rungs as an in-bot control, and fires
    ``model_swap_behavior_divergence`` when the swapped rung collapses while
    the control holds steady.

    Cadence: daily. The divergence accumulates over days and the monitor only
    judges swaps that are already past their settle window, so there is
    nothing an hourly tick would catch sooner. 05:05 puts it after the
    04:50 autogen-volume sweep and clear of the 03:15-04:40 monitor block.
    Pure Python, reads only annotations Evolve already writes — no LLM, no
    new instrumentation, no config writes. Watched by monitor_coverage's
    producer-liveness layer.
    """
    from .deploy import (
        ANALYZER_DIR,
        _CANONICAL_SHARED_DIR,
        _install_launchd,
    )
    _install_launchd(
        label="ai.openclaw.evolve.model_swap_watch",
        user="evolve",
        script_path=ANALYZER_DIR / "model_swap_watch.py",
        schedule={"Hour": 5, "Minute": 5},
        result=result,
        extra_args=["--shared-dir", str(_CANONICAL_SHARED_DIR)],
        run_at_load=False,
        jitter_seconds=120,
    )



def install_analyzer_monitor_jobs(result: "DeployResult") -> None:
    """Install every pod-wide analyzer Signal-producing monitor, in order.

    Called from ``deploy.install_evolve_infra_jobs``. Order preserved from the
    pre-extraction call sequence so jitter/cadence offsets stay as designed.
    """
    install_capability_gap_monitor(result)
    install_engagement_amplifier_monitor(result)
    install_evo_path_probe_monitor(result)
    install_model_liveness_monitor(result)
    install_vocabulary_expander_monitor(result)
    install_autogen_volume_monitor(result)
    install_roster_allowlist_drift_monitor(result)
    install_roster_coherence_monitor(result)
    install_bootstrap_size_monitor(result)
    install_orphaned_bot_account_monitor(result)
    install_model_swap_watch(result)
