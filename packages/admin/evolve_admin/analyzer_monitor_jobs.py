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


def install_analyzer_monitor_jobs(result: "DeployResult") -> None:
    """Install every pod-wide analyzer Signal-producing monitor, in order.

    Called from ``deploy.install_evolve_infra_jobs``. Order preserved from the
    pre-extraction call sequence so jitter/cadence offsets stay as designed.
    """
    install_capability_gap_monitor(result)
    install_engagement_amplifier_monitor(result)
    install_evo_path_probe_monitor(result)
    install_vocabulary_expander_monitor(result)
    install_autogen_volume_monitor(result)
    install_roster_allowlist_drift_monitor(result)
