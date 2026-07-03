"""protection_registry — fresh-pod alert-protection posture for every operator-facing producer.

Spec: docs/spec-transient-signal-suppression-2026-06-23.md (§ "Protection by
construction").

The umbrella spec ships three composable gates that stop a freshly-provisioned
pod from spamming its operator (all under :mod:`signals`):

* ``settle_gate``   (#3186) — withhold steady-state monitor signals during the
  bring-up settle window.
* ``flap_gate``     (#3197) — dwell N≥2 cycles for self-healing flaps (acl/perms).
* ``maturity_gate`` (#3204) — "warming up / insufficient data" for history-
  dependent producers with no accumulated history yet.

Those gates are powerful but **opt-in per producer** — nothing forces a producer
to adopt the right one, so the next new monitor can reintroduce exactly the
fresh-pod spam they just fixed. This registry closes that gap: it enumerates
*every* operator-facing Signal / summary producer and the fresh-pod protection
**posture** it declares. ``tools/signal-protection-lint`` statically maps every
``signals.store.observe(...)`` call site to its producer and FAILS if that
producer is not declared here — so a new producer cannot ship without an
explicit, reviewable answer to "how do you behave on a fresh/immature pod?".

This is enforcement of a **declared** posture, NOT a non-``none`` one. A
genuinely-critical, must-page-immediately condition (gateway down, daemon not
loaded, sudoers invalid, security floor) correctly declares :data:`NONE` —
*with* a required ``justification`` string. The point is that the decision is
made on purpose and visible in version control, not that every alert is gated.
**Do not over-gate a critical alert to satisfy the lint.**

----

The four postures
-----------------

* :data:`SETTLE`   — gated by the bring-up settle window (``settle_gate``).
* :data:`FLAP`     — dwell hysteresis for transient-prone conditions (``flap_gate``).
* :data:`MATURITY` — no-data / pod-maturity gate (``maturity_gate``).
* :data:`NONE`     — deliberately always pages. Requires ``justification``.

A producer may declare several non-``none`` postures (e.g. ``settle`` + ``flap``,
which ``pod_perms_drift`` does). ``none`` is exclusive — it means "no gate", so
it cannot be combined with a gate.

The ``gap`` flag
----------------

A ``none`` entry is one of two things, and the flag records which:

* ``gap=False`` — *deliberate* none. The condition is critical / per-event /
  advisory-with-dedup and SHOULD page (or is harmless) on a fresh pod. This is
  the expected, correct state for most ``none`` producers.
* ``gap=True``  — a *known coverage gap*: a steady-state / history-dependent
  producer that can false-fire on a fresh or immature pod and SHOULD eventually
  adopt a gate, but has not been wired yet. Backfilling surfaced these; this PR
  registers them at their **current** posture (``none``) and records the gap
  rather than re-wiring behavior here (boundary: reports owns the contract, the
  owning subsystem owns the wiring). ``tools/signal-protection-lint --gaps``
  lists them; each is a follow-up candidate.

Both ``gap`` values still require a ``justification`` — the lint only checks that
a ``none`` is *explained*, never that it is a gate.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Posture constants ────────────────────────────────────────────────────────
SETTLE = "settle"
FLAP = "flap"
MATURITY = "maturity"
NONE = "none"

VALID_POSTURES = frozenset({SETTLE, FLAP, MATURITY, NONE})

# Producer "kind" — purely descriptive (helps an operator read the table); the
# lint does not branch on it.
KIND_SIGNAL = "signal"          # writes Signal JSON via signals.store.observe
KIND_SUMMARY = "summary"        # operator-facing Markdown digest via the dispatcher
KIND_DISPATCH = "dispatch"      # pages the operator directly (no Signal of its own)
KIND_RELAY = "relay"            # re-delivers already-gated Signals (protection is upstream)
KIND_MIRROR = "mirror"          # mirrors another producer's events into Signals
KIND_INFRA = "infra"            # the emit/dispatch plumbing itself


@dataclass(frozen=True)
class ProducerProtection:
    """One operator-facing producer and its declared fresh-pod posture.

    ``postures``    — subset of {SETTLE, FLAP, MATURITY, NONE}. NONE is exclusive.
    ``justification`` — required iff NONE is declared; explains the decision.
    ``kind``        — descriptive classification (see ``KIND_*``).
    ``emits_from``  — repo-relative source files that carry this producer's
                      ``signals.store.observe(...)`` call site(s). The lint's
                      file-coverage check claims every file in the union of all
                      entries' ``emits_from``; a producer that pages only through
                      the dispatcher (a summary / dispatch / relay) has no observe
                      site and leaves this empty.
    ``gap``         — see module docstring. Only meaningful when NONE is declared.
    ``note``        — optional free-text (the follow-up pointer for a gap, etc.).
    """

    producer: str
    postures: tuple[str, ...]
    kind: str
    justification: str = ""
    emits_from: tuple[str, ...] = ()
    gap: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        # Light structural validation so a malformed entry fails loudly at import
        # rather than silently passing the lint. The richer policy checks
        # (justification-required, etc.) live in `consistency_errors()` so the
        # lint can report them all at once instead of dying on the first.
        if not self.postures:
            raise ValueError(f"{self.producer}: postures must be non-empty")
        for p in self.postures:
            if p not in VALID_POSTURES:
                raise ValueError(f"{self.producer}: unknown posture {p!r}")

    @property
    def is_none(self) -> bool:
        return NONE in self.postures

    @property
    def is_gated(self) -> bool:
        return not self.is_none


def _e(*args, **kwargs) -> tuple[str, ProducerProtection]:
    p = ProducerProtection(*args, **kwargs)
    return p.producer, p


# ── The registry ─────────────────────────────────────────────────────────────
# Backfilled from the live producer wiring (2026-06-23): the Signal producers
# enumerated by signals.producer_severity.PRODUCER_SEVERITY and reconciled
# against the dispatcher source map (alerts.dispatcher._DEFAULT_SOURCE_CATEGORY)
# for the summary/dispatch surfaces. `tools/signal-protection-lint` keeps this in
# lock-step with the code (a new observe() site or a new dispatcher source that
# is absent here fails the lint).
PROTECTIONS: dict[str, ProducerProtection] = dict(
    [
        # ── Gated today (the three mechanisms are wired) ─────────────────────
        _e(
            "audit",
            (SETTLE, FLAP),
            KIND_SIGNAL,
            emits_from=(
                "packages/analyzer/audit.py",
                "packages/admin/evolve_admin/web/routes_oc.py",
            ),
            note="settle_gate.should_withhold in audit.py (#3186); flap_gate dwell "
            "for the benign perm/mode/acl finding family (_is_flap_prone_perm_"
            "finding, D2) so a re-clamp flap dwells N≥2 cycles — CRITICAL "
            "credential exposure stays must-page-now. The on-demand re-run in "
            "routes_oc.py shares the same producer.",
        ),
        _e(
            "infra_audit",
            (SETTLE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/applications/audit_poller.py",),
            note="settle_gate via applications/infra_audit._withhold_during_bringup.",
        ),
        _e(
            "pod_perms_drift",
            (SETTLE, FLAP),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/pod_perms_drift_monitor.py",),
            note="settle (withhold while unsettled) then flap (dwell once settled) (#3197).",
        ),
        _e(
            "sysadmin_watchdog",
            (SETTLE, FLAP),
            KIND_SIGNAL,
            emits_from=(
                "packages/analyzer/generator_runner.py",
                "packages/analyzer/generators/evolve_watchdog/events.py",
            ),
            note="ACL-drift detector applies settle+flap "
            "(generators/sysadmin_watchdog/detectors/platform/acl.py, #3197); "
            "emits via generator observe_signals + the watchdog→Signal mirror.",
        ),
        _e(
            "weekly_review",
            (MATURITY,),
            KIND_SUMMARY,
            note="maturity_gate (#3204): renders 🌱 Warming up instead of 🔴 At Risk "
            "until 14d + ≥1 funnel data point. Dispatched summary, no observe site.",
        ),
        # ── Critical / must-page-now — deliberate NONE ───────────────────────
        _e(
            "security_warden",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/generators/security_warden/observe.py",),
            justification="Security floor. Multi-user exec=full / no-admin privilege "
            "exposure must page the moment it is detected, including on a fresh pod — "
            "delaying a real privilege-exposure finding is the worst failure here.",
        ),
        _e(
            "heal",
            (NONE,),
            KIND_DISPATCH,
            justification="Gateway down / restart-failed / config-drift are "
            "user-affecting outages; heal pages directly via the dispatcher every "
            "5-min tick. A fresh pod whose gateway will not come up must page now.",
        ),
        _e(
            "host_health",
            (NONE,),
            KIND_SIGNAL,
            emits_from=(
                "packages/admin/evolve_admin/host_health.py",
                "packages/analyzer/delivery_monitor.py",
            ),
            justification="CPU/disk/memory exhaustion on the pod host — the host "
            "itself is in trouble and everything else is downstream. Real on a "
            "fresh pod as much as a mature one; never delay.",
        ),
        _e(
            "pod_health",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/health.py",),
            gap=True,
            justification="Pod-wide health (launchd daemons loaded, port binds, "
            "ACLs). A genuinely-down daemon must page immediately, but the bring-up "
            "transients (ACL not yet re-asserted, gateway still starting) are exactly "
            "the settle-window class.",
            note="GAP: candidate for a per-check settle gate on the bring-up-transient "
            "checks (daemon-not-loaded stays NONE). Owner: platform/edr. Do not "
            "blanket-settle — the critical checks must stay immediate.",
        ),
        _e(
            "breakers_runner",
            (NONE,),
            KIND_DISPATCH,
            justification="L1 cost-breaker trips are operator-critical; every trip "
            "must reach the operator immediately. The producer is already silent on "
            "single trips (recurrent gate), so each emit is alert-grade.",
        ),
        _e(
            "send_surface_probe",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/ocadmin.py",),
            justification="Post-OC-upgrade end-to-end delivery proof. It only fires "
            "when the just-installed OpenClaw broke the path every user-facing send "
            "goes through — a pod-wide outage that must page now.",
        ),
        _e(
            "evo_path_probe",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/evo_path_probe_monitor.py",),
            justification="Black-box end-to-end proof of the 'evo' keyword path "
            "(gateway plugin → admin /api/evo/dispatch). It only fires when that "
            "pod-wide path is broken — every user typing `evo …` gets generic "
            "OpenClaw help instead of the evo surface. A pod-wide outage that must "
            "page now; no settle/flap window (the path is up or it is down).",
        ),
        _e(
            "openclaw_config_validator",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/openclaw_config_validator.py",),
            justification="A bot's openclaw.json no longer validates — the gateway "
            "will crash-loop on next reload (silent bot in Chat). User-affecting; "
            "page immediately.",
        ),
        _e(
            "repo_puller",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/repo_puller.py",),
            justification="Deploy checkout cannot fast-forward — every daemon runs "
            "stale code until cleared. Page now; this is a structural deploy wedge, "
            "not a bring-up transient.",
        ),
        _e(
            "repo_puller_sudoers",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/repo_puller.py",),
            justification="Repo-puller's own sudoers grant is missing/invalid — the "
            "puller can't operate. Infrastructure-critical; page immediately.",
        ),
        _e(
            "release_manager",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/release_manager.py",),
            justification="Canary/soak/promote/rollback outcomes for the release "
            "pointer — operator must see a failed promotion or an auto-rollback now.",
        ),
        # ── Watchdog mirror + the generator/emit plumbing ────────────────────
        _e(
            "evolve_watchdog",
            (NONE,),
            KIND_MIRROR,
            emits_from=("packages/analyzer/generators/evolve_watchdog/events.py",),
            justification="Mirrors WatchdogEvents (proposal-volume / rejection-rate / "
            "calibration drift) into Signals. These are RSI-meta deviations measured "
            "over accumulated history; on a fresh pod the watchdog itself has nothing "
            "to deviate from, so it does not fire — protection is structural, not a gate.",
        ),
        _e(
            "test_runner",
            (NONE,),
            KIND_MIRROR,
            emits_from=("packages/analyzer/generators/evolve_watchdog/events.py",),
            justification="Weekly test-failure pattern mirrored from the watchdog. "
            "Persisting real failures must surface; the weekly cadence + content-hash "
            "dedup already prevent fresh-pod spam.",
        ),
        _e(
            "registry",
            (NONE,),
            KIND_INFRA,
            emits_from=("packages/analyzer/generator_runner.py",),
            justification="generator_load_error meta-signal — a generator module "
            "failed to import (the 18-day phantom-import crash class). Must page; a "
            "broken generator silently starves the RSI pipeline.",
        ),
        # ── Generators that emit Signals via observe_signals (→ generator_runner) ─
        _e(
            "model_discovery",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/generator_runner.py",),
            justification="Advisory 'a new model line exists' (info/activity). "
            "find-or-create dedup caps it to one ping; nothing is broken, so it is "
            "harmless on a fresh pod. The degraded-listing override is a real fault "
            "that should surface immediately.",
        ),
        _e(
            "value_baseline",
            (NONE,),
            KIND_SIGNAL,
            emits_from=(
                "packages/analyzer/value_baseline.py",
                "packages/analyzer/generator_runner.py",
            ),
            gap=True,
            justification="Per-bot utilization baseline (bot_underused, info). The "
            "pod-scope coverage signal ('most of the fleet unmeasurable') overrides "
            "to warn — and on a fresh pod nothing is measurable yet, which is the "
            "maturity-gate failure mode in miniature.",
            note="GAP: the pod-coverage warn override is a maturity-gate candidate "
            "(immature pod ⇒ 'warming up', not 'recording pipeline broken'). "
            "Owner: rsi / user-value.",
        ),
        _e(
            "budget_hawk",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/generator_runner.py",),
            justification="Budget-pacing generator signals. Cost overruns are real "
            "the moment they happen; per-bot dedup prevents repeat pages.",
        ),
        # ── Steady-state config/posture monitors — NONE today, gap-flagged ───
        _e(
            "deploy_drift_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/deploy_drift_monitor.py",),
            gap=True,
            justification="Bots running an older Evolve commit than the admin server. "
            "On a fresh pod the fleet is mid-deploy, so 'behind' is expected and "
            "transient until the first sweep completes.",
            note="GAP: settle-gate candidate (drift during bring-up is expected). "
            "Owner: deploy.",
        ),
        _e(
            "plugin_schema_skew",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/plugin_schema_skew.py",),
            gap=True,
            justification="The deployed/staged plugin manifest the gateways load lags "
            "the repo source schema, so the materializer is omitting/stripping a key "
            "(e.g. repoRoot) to keep config reloads valid. Sibling of "
            "deploy_drift_monitor (deployed artifact behind admin code); during a "
            "deploy/update window the lag is expected and self-corrects on the next "
            "deploy once the staged plugin catches up. warn/maintenance (digest, not a "
            "hard page).",
            note="GAP: settle-gate candidate (skew during a deploy window is expected). "
            "Owner: deploy.",
        ),
        _e(
            "install_integrity_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/install_integrity_monitor.py",),
            gap=True,
            justification="Bot install integrity (ownership, /evolve/status liveness, "
            "validator pass). On a fresh pod bots are still being provisioned, so "
            "incomplete-install findings are bring-up transients.",
            note="GAP: settle-gate candidate. Owner: deploy.",
        ),
        _e(
            "permission_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/permissions/monitor.py",),
            gap=True,
            justification="Per-bot permission posture (drift, dangerous combos). "
            "Dangerous-combo / denylist findings are real and must page; baseline "
            "drift on a freshly-deployed config is bring-up noise.",
            note="GAP: per-finding settle gate on the drift class (keep dangerous "
            "combos immediate). Owner: edr.",
        ),
        _e(
            "plugin_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/plugins/monitor.py",),
            gap=True,
            justification="Plugin enable/disable + allowlist drift. 'Denied plugin "
            "enabled' / 'unauthorized source' are real; plain drift on a fresh deploy "
            "is bring-up noise.",
            note="GAP: per-finding settle gate on the drift class. Owner: edr/skills.",
        ),
        _e(
            "roster_allowlist_drift",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/roster_allowlist_drift_monitor.py",),
            gap=True,
            justification="Out-of-band group-allowlist drift (R1a PR3). The drift type "
            "(roster_group_allowlist_changed) self-quiets on a fresh pod by "
            "construction: the monitor SEEDS the baseline from the live config on "
            "first sight of a bot and only fires on a LATER divergence, so bring-up "
            "never reads as drift. The sibling unreadable type "
            "(roster_group_allowlist_unreadable) CAN fire during bring-up while bots "
            "are still being provisioned (openclaw.json not landed / ACL not yet "
            "re-asserted) — the same not-yet-ready shape as content_scan / "
            "integration_probe. A genuine out-of-band widening is real the moment it "
            "happens (added senders can spend the bot's tokens), so it must surface.",
            note="GAP: settle-gate candidate for the unreadable class only "
            "(a real widening stays immediate). Owner: users/edr.",
        ),
        _e(
            "hook_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/hooks/monitor.py",),
            gap=True,
            justification="Webhook ingress + transformsDir state. Silent-inbound-"
            "surface findings are real; baseline drift during bring-up is transient.",
            note="GAP: per-finding settle gate on the drift class. Owner: edr.",
        ),
        _e(
            "mcp_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/mcp_admin/monitor.py",),
            gap=True,
            justification="MCP server connectivity / credentials / drift. Self-mutate "
            "/ credential-invalid are real; connectivity blips and drift during "
            "bring-up are transient.",
            note="GAP: settle-gate candidate for the connectivity/drift class. "
            "Owner: edr/skills.",
        ),
        _e(
            "content_scan",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/content_scan/scanner.py",),
            gap=True,
            justification="Content-scan misses on bot config files. Two distinct "
            "types from this one producer: content_scan_file_disappeared (file "
            "genuinely gone, alert) and content_scan_file_unreadable (evolve can't "
            "read it — a transient permission/ACL flap, warn/info). On a fresh pod "
            "config files are still landing and ACLs still settling, so both are "
            "bring-up transients.",
            note="GAP: settle-gate candidate. Owner: apps. The unreadable split "
            "(warn/info, digest-default) is the producer-side flap-collapse the "
            "settle gate would complement.",
        ),
        _e(
            "app_manifest_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/permissions/app_manifest_monitor.py",),
            gap=True,
            justification="App-manifest posture monitor. App manifests are scanned/"
            "minted during bring-up, so structural findings can be transient on a "
            "fresh pod.",
            note="GAP: settle-gate candidate. Owner: apps.",
        ),
        _e(
            "app_structural_verifier",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/applications/audit_poller.py",),
            gap=True,
            justification="Static app-manifest defect verifier. Permission-sensitive "
            "misses must surface; structural defects on still-materializing manifests "
            "during bring-up are transient.",
            note="GAP: settle-gate candidate (shares audit_poller with infra_audit, "
            "which is already gated). Owner: apps.",
        ),
        _e(
            "oc_substrate_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/oc_substrate_monitor.py",),
            gap=True,
            justification="OpenClaw substrate (plugins/skills/MCP install) integrity. "
            "Substrate is installed during bring-up, so missing-substrate findings "
            "can be transient on a fresh pod.",
            note="GAP: settle-gate candidate. Owner: skills.",
        ),
        _e(
            "pod_report",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/pod_report.py",),
            gap=True,
            justification="Daily pod report's broken-bucket lines (Pod silent, "
            "metrics_outage, gateway down). Real outages must surface; 'metrics "
            "outage' / 'pod silent' on a 1-day-old pod with no history yet is the "
            "maturity-gate failure mode.",
            note="GAP: maturity-gate candidate for the no-history buckets (gateway-"
            "down stays immediate). Owner: reports.",
        ),
        # ── Cost / spend — deliberate NONE (real same-day intervention) ──────
        _e(
            "cost_watchdog",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/cost_watchdog.py",),
            justification="Cost spikes / heartbeat-on-primary-model / shell-only-wake "
            "crons are real spend waste. dedup_key embeds {today}; a fresh pod that "
            "is already burning budget should page.",
        ),
        _e(
            "session_cost_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/cost_watchdog.py",),
            justification="Per-session cost attribution (sibling to cost_watchdog). "
            "Same-day spend signal; page when it crosses threshold.",
        ),
        _e(
            "spend_alert",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/spend_alert.py",),
            justification="Intraday cost bursts needing same-day operator "
            "intervention. Real on day one; per-bot dedup prevents repeats.",
        ),
        _e(
            "cost_profiles",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/cost_profiles.py",),
            justification="Per-bot cost-profile drift. Spend-shaped; surfaces a real "
            "overrun, not a bring-up transient.",
        ),
        _e(
            "provisioning_budget",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/provisioning_budget.py",),
            justification="Provisioning-phase budget guard. Its whole job is to catch "
            "runaway spend *during* bring-up, so it must page on a fresh pod by design.",
        ),
        _e(
            "forge_cost_guard",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/applications/forge_cost_guard.py",),
            justification="Forge-job cost ceiling. Per-job; a job that blows its "
            "budget must surface immediately regardless of pod age.",
        ),
        _e(
            "anthropic_admin_ingest",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/anthropic_admin_ingest.py",),
            justification="Admin-API ingest discrepancy (our ledger ≠ Anthropic's "
            "total). Content-hashed dedup; a real reconciliation gap should surface.",
        ),
        # ── Per-event / per-job producers — deliberate NONE ──────────────────
        _e(
            "forge_engine",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/applications/forge_engine.py",),
            justification="Per-forge-job outcomes; job IDs are unique per job, so each "
            "is a distinct one-shot event, not a recurring fresh-pod condition.",
        ),
        _e(
            "compliance_scan",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/applications/scanner.py",),
            justification="App-compliance scan findings. Per-finding, content-hashed; "
            "a genuine compliance miss should surface immediately.",
        ),
        _e(
            "error_reporter",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/reporter.py",),
            justification="Uncaught-exception reporter for the admin/daemon stack. A "
            "crash is a crash on day one; per-fingerprint dedup prevents repeats.",
        ),
        _e(
            "cron_exit_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/cron_exit_monitor.py",),
            justification="A managed launchd job exited non-zero. The job won't retry "
            "until its next window, so the broken state persists — surface it.",
        ),
        _e(
            "openclaw_overrides_expiry",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/openclaw_overrides_expiry.py",),
            justification="An operator's sandbox override expired and was removed. "
            "Per-entry; the operator may want to recreate it. Not bring-up noise.",
        ),
        _e(
            "oc_deps_probe",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/ocadmin.py",),
            justification="Post-upgrade OpenClaw dependency-surface probe. Per-version; "
            "fires when an OC release breaks a surface Evolve depends on — surface it.",
        ),
        _e(
            "oc_upgrade_runtime_notes_reminder",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/ocadmin.py",),
            justification="Post-upgrade reminder to read OC runtime notes. Per-version "
            "one-shot; not a recurring fresh-pod condition.",
        ),
        _e(
            "integration_probe",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/web/routes_oc.py",),
            gap=True,
            justification="Per-integration connectivity check. Credential-invalid / "
            "hard-down must page; on a fresh pod integrations are not yet connected, "
            "so 'down' / 'stale workspace' is expected until the operator wires them.",
            note="GAP: settle-gate candidate for the soft (degraded/stale) findings "
            "(credential-invalid stays immediate). Owner: reports/apps.",
        ),
        _e(
            "slack_config_probe",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/admin/evolve_admin/integrations/slack/probe.py",),
            gap=True,
            justification="Slack-side config-drift probe. Dangerous categories must "
            "page; baseline drift before the operator has finished wiring Slack on a "
            "fresh pod is expected.",
            note="GAP: settle-gate candidate for the drift class. Owner: edr.",
        ),
        _e(
            "auth_drift_filler",
            (NONE,),
            KIND_SIGNAL,
            emits_from=(
                "packages/analyzer/generators/auth_drift_filler/signal_proposals.py",
            ),
            gap=True,
            justification="config_intent-aware auth-config drift detector. Dangerous "
            "drift must page; on a fresh pod auth profiles are still being seeded, so "
            "benign drift is a bring-up transient.",
            note="GAP: settle-gate candidate for the benign-drift class. Owner: edr.",
        ),
        _e(
            "oc_cli",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/signals/cli_misinvocation.py",),
            justification="An evolve caller invoked the openclaw CLI with the wrong "
            "shape (upstream rename the caller missed). A silently-broken call site "
            "is broken on day one; surface it.",
        ),
        _e(
            "agent_bypass_audit",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/agent_bypass_audit.py",),
            justification="Behavioral-integrity audit (agent freelancing past a "
            "script's controls). A real bypass should surface; not bring-up-prone.",
        ),
        _e(
            "app_script_failure_audit",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/app_script_failure_audit.py",),
            justification="Agent-invoked app scripts that FAIL at runtime. A genuine "
            "runtime failure should surface; per-script dedup prevents repeats.",
        ),
        _e(
            "exec_outcome_watchdog",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/exec_outcome_watchdog.py",),
            justification="Per-bot exec-outcome attribution after policy enforcement. "
            "Drives the exec-outcome investigator; per-bot, not fresh-pod-prone.",
        ),
        _e(
            "delivery_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/delivery_monitor.py",),
            gap=True,
            justification="A scheduled user-facing app missed its delivery window. "
            "Repeated misses are real; the first windows on a fresh pod (before any "
            "schedule has run) are unmeasurable, not missed.",
            note="GAP: the app_delivery_unmeasurable class is a maturity-gate "
            "candidate (no windows yet ⇒ warming up). Owner: apps/user-value.",
        ),
        _e(
            "bot_log_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/bot_log_signal.py",),
            justification="Gateway error-log patterns (MAX auth failure, target "
            "invalid, repeated delivery failures). Operator action required; a real "
            "log error is real on day one.",
        ),
        _e(
            "alerts_loop_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/alerts_loop_monitor.py",),
            justification="Dispatcher-log loop patterns (FAILED sends, same-message "
            "thrash). Either alerts aren't arriving or a fix is being skipped — "
            "operator action either way; surface it.",
        ),
        _e(
            "embedding_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/embedding_monitor.py",),
            justification="Embedding 429 storms / quota_exceeded — the bot is degraded "
            "(memory search on local fallback). A real degradation should surface.",
        ),
        _e(
            "stuck_proposal_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/stuck_proposal_monitor.py",),
            justification="Proposals stuck pending past the staleness window. A real "
            "backlog should surface; on a fresh pod there are no proposals to be "
            "stuck, so it self-quiets without a gate.",
        ),
        _e(
            "home_artifacts_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/home_artifacts_monitor.py",),
            justification="Stray artifacts in bot home dirs. A real stray file should "
            "surface; per-path dedup prevents repeats.",
        ),
        _e(
            "autogen_volume_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/autogen_volume_monitor.py",),
            justification="An auto-generated directory grew past its file/byte budget "
            "(the 537 MB audit_outbox/_ingested leak class). Disk volume accrues over "
            "time, so a fresh pod's dirs are near-empty and structurally below budget — "
            "it self-quiets without a gate. A genuine over-budget directory is a real "
            "disk-hygiene task that should surface; per-surface signature + "
            "sweep_resolve dedups to one Signal per dir that clears when it drains.",
        ),
        _e(
            "monitor_gmail_integration_health",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/monitor_gmail_integration_health.py",),
            gap=True,
            justification="Gmail integration health. A real degradation should page; "
            "on a fresh pod Gmail is not yet connected, so 'unhealthy' is expected "
            "until the operator wires it.",
            note="GAP: settle-gate candidate for the not-yet-connected case. "
            "Owner: apps.",
        ),
        _e(
            "digest_source_audit",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/digest_source_audit.py",),
            justification="Audits the digest-source wiring (a source mis-routed or "
            "silently dropped). A real wiring fault should surface.",
        ),
        _e(
            "reconcile_audit",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/reconcile_audit.py",),
            justification="Reconciliation audit between stores. A real divergence "
            "should surface; content-hashed dedup prevents repeats.",
        ),
        # ── Advisory / telemetry — deliberate NONE (info-tier, dedup-capped) ─
        _e(
            "session_economics",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/session_economics.py",),
            justification="Cache-health / engagement telemetry (info/activity) that "
            "feeds the cache_ttl_tuner generator. Not operator-actionable on its own; "
            "the actionable layer is the Proposal it feeds. Harmless on a fresh pod.",
        ),
        _e(
            "monitor_coverage",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/monitor_coverage.py",),
            justification="Meta-signal about which monitors have been firing "
            "(producer-silence detector). Advisory; the silent monitor itself carries "
            "the urgency. Its whole job is to notice gaps, so it must run on day one.",
        ),
        _e(
            "cascade_audit",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/cascade/audit_runner.py",),
            justification="Tier-cascade telemetry roll-up for calibration (info/"
            "activity). Advisory; the per-decision signals carry any urgency.",
        ),
        _e(
            "code_quality_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/code_quality_monitor.py",),
            justification="Lint/format-check style findings (info). Advisory, not "
            "user-affecting; harmless on a fresh pod.",
        ),
        _e(
            "capability_gap_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/capability_gap_monitor.py",),
            justification="Per-(bot, capability) gap observations feeding the RSI "
            "recommendation pipeline. Advisory; find-or-create dedup caps it.",
        ),
        _e(
            "engagement_amplifier_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/engagement_amplifier_monitor.py",),
            justification="Per-bot engagement observations feeding the RSI pipeline. "
            "Advisory; find-or-create dedup caps it.",
        ),
        _e(
            "bot_recovery_monitor",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/bot_recovery_monitor.py",),
            justification="Bot self-recoveries from a previously-firing error (info). "
            "The operator-visible chip is already green; the signal is the history "
            "trail. Harmless on a fresh pod.",
        ),
        _e(
            "backup_signal",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/backup_signal.py",),
            justification="Nightly workspace backups bouncing for a bot — real "
            "data-loss exposure. Escalates per-emit after 7+ consecutive misses; a "
            "fresh pod has no backup history to be bouncing, so it self-quiets.",
        ),
        _e(
            "backup_audit_signal",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/backup_audit_signal.py",),
            justification="Advisory notes about backup-repo contents (info). Operator "
            "may want to know; not actionable in the moment, harmless on a fresh pod.",
        ),
        _e(
            "local_backup_signal",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/local_backup_signal.py",),
            justification="Time-Machine coverage advisories (info). FYI context, not "
            "a fire; harmless on a fresh pod.",
        ),
        _e(
            "oc_surface_drift",
            (NONE,),
            KIND_SIGNAL,
            emits_from=("packages/analyzer/update_watcher.py",),
            justification="OpenClaw surface drift between OC releases (info; warn when "
            "a depended-on surface changed). Per-version; a real Evolve-needs-updating "
            "task should surface.",
        ),
        # ── Producers that page only through the dispatcher (no observe site) ─
        # Reconciled against alerts.dispatcher._DEFAULT_SOURCE_CATEGORY. These
        # have no signals.store.observe() call of their own, so emits_from is
        # empty and the lint's file-coverage check does not apply to them — the
        # name-reconciliation check (against the dispatcher source map) keeps
        # them honest.
        _e(
            "weekly_bot_trends",
            (NONE,),
            KIND_SUMMARY,
            gap=True,
            justification="Sunday per-bot 7-day trend digest. History-dependent like "
            "its sibling weekly_review; on a fresh pod the 7-day window is empty.",
            note="GAP: maturity-gate candidate (sibling to weekly_review's #3204 "
            "gate). Owner: reports.",
        ),
        _e(
            "signal_notifier",
            (NONE,),
            KIND_RELAY,
            justification="Relays already-written firing Signals to Telegram. The "
            "fresh-pod protection was applied upstream at the Signal producer "
            "(settle/flap/maturity gate the observe() call); the relay only forwards "
            "what survived. STATE_TRACKED dedup prevents repeat sends.",
        ),
        _e(
            "report",
            (NONE,),
            KIND_DISPATCH,
            justification="Ad-hoc operator-invoked analyzer report. The operator "
            "asked for it, so it is never unsolicited fresh-pod spam.",
        ),
        _e(
            "digest_dispatcher",
            (NONE,),
            KIND_INFRA,
            justification="Flushes the bundled daily/weekly digest. A relay of "
            "already-gated items; per-flush-date dedup. No condition of its own.",
        ),
        _e(
            "alert_rate_breaker",
            (NONE,),
            KIND_INFRA,
            justification="The global outbound rate breaker's own '🌊 Alert storm' "
            "notice/heartbeat (alerts.rate_breaker). It is the meta-notice that "
            "announces the backstop is batching a flood into the digest — exempt "
            "from the breaker itself (it can't be capped by the cap it announces). "
            "The breaker owns cadence (one per episode + hourly heartbeat), so no "
            "fresh-pod gate applies; on a quiet pod it never fires.",
        ),
        _e(
            "upstream_issues_watcher",
            (NONE,),
            KIND_DISPATCH,
            justification="Activity on upstream issues we filed (info; opt-in at the "
            "subscription layer). Per-comment; a fresh pod has filed no issues, so it "
            "self-quiets without a gate.",
        ),
        _e(
            "app_install",
            (NONE,),
            KIND_DISPATCH,
            justification="Post-wrap app-install outcomes (pack-install failure, "
            "briefing auto-activation). Per-job one-shot; a real install failure "
            "should surface immediately.",
        ),
        _e(
            "soak_send_probe",
            (NONE,),
            KIND_DISPATCH,
            justification="Canary soak send-surface probe outcome. Per-event "
            "one-shot canary gate (dispatcher category PER_EVENT_UNIQUE): a "
            "broken send surface (REGRESSION) must fail the soak and surface "
            "immediately before the fleet moves. #3206 made the success case "
            "silent (proof preserved), so only a failure pages. Reserved "
            "default-OFF source — a deliberate none, not a fresh-pod coverage "
            "gap. Owner: deploy.",
        ),
        _e(
            "security_cve_scan",
            (NONE,),
            KIND_DISPATCH,
            justification="Daily CVE-scan findings. Security floor: a real CVE must "
            "page. Content-hashed dedup gives one announcement per distinct "
            "finding-set per day.",
        ),
        _e(
            "cron_alert",
            (NONE,),
            KIND_DISPATCH,
            justification="Stalled-cron alert; persists until the operator restarts "
            "the job. STATE_PERSISTS 24h floor prevents repeat pages.",
        ),
        _e(
            "review",
            (NONE,),
            KIND_DISPATCH,
            justification="Per-proposal review outcome (rejected/reviewed/quarantined). "
            "Per-proposal dedup_key; a fresh pod has no proposals, so it self-quiets.",
        ),
        _e(
            "analyze",
            (NONE,),
            KIND_DISPATCH,
            justification="Proposal-ready batch announcement. Per-batch dedup_key; a "
            "fresh pod has nothing to analyze yet, so it self-quiets.",
        ),
        _e(
            "apply",
            (NONE,),
            KIND_DISPATCH,
            justification="Per-proposal apply result (success/rollback/fail). "
            "Per-proposal dedup_key; a real apply outcome should surface.",
        ),
        _e(
            "outcome",
            (NONE,),
            KIND_DISPATCH,
            justification="7-day 'did this help?' check-in. Per-outcome dedup_key; a "
            "fresh pod has no applied proposals to check in on.",
        ),
        _e(
            "validate",
            (NONE,),
            KIND_DISPATCH,
            justification="Forge/manifest validation result. Per-proposal dedup_key; "
            "a real validation failure should surface.",
        ),
        _e(
            "cost",
            (NONE,),
            KIND_DISPATCH,
            justification="Key-rotation-overdue (spend duplicate retired). "
            "Content-hashed dedup_key over the stale-bots set; a real overdue "
            "rotation should surface.",
        ),
        _e(
            "update_watcher",
            (NONE,),
            KIND_DISPATCH,
            justification="OpenClaw / evolve-repo / plugin update notifications. "
            "Per-version dedup_key; a real available update should surface. (The "
            "oc_surface_drift Signal it also emits is registered separately.)",
        ),
        # ── Advisory generator-status + reflex producers (no direct observe) ─
        _e(
            "proposal_synthesizer",
            (NONE,),
            KIND_INFRA,
            justification="Proposal-volume / synthesizer status (info/activity). The "
            "proposals themselves are the actionable layer, not this status signal. "
            "Advisory; harmless on a fresh pod.",
        ),
        _e(
            "manifest_reflex_runner",
            (NONE,),
            KIND_SIGNAL,
            justification="Fires when a bot builds a new app mid-session (info). "
            "Advisory — the operator should see what got built, but it is not a "
            "failure; the manifest is reviewed via the App page.",
        ),
        _e(
            "manifest_reflex_scanner",
            (NONE,),
            KIND_SIGNAL,
            justification="Scanner side of the manifest-reflex pair (info). Advisory; "
            "harmless on a fresh pod.",
        ),
        _e(
            "openclaw_posture",
            (NONE,),
            KIND_SIGNAL,
            justification="Periodic bot security-posture snapshot (info umbrella; "
            "specific findings escalate per-emit). Advisory; the specific findings "
            "carry any urgency.",
        ),
        _e(
            "skill_audit",
            (NONE,),
            KIND_SIGNAL,
            gap=True,
            justification="Per-app skill audit. A real skill defect should surface; "
            "on a fresh pod skills are still being installed, so missing-skill "
            "findings can be bring-up transients.",
            note="GAP: settle-gate candidate. Owner: skills.",
        ),
        _e(
            "provider_audit",
            (NONE,),
            KIND_SIGNAL,
            gap=True,
            justification="Per-app provider/model audit. A real misconfiguration "
            "should surface; on a fresh pod providers are still being wired, so "
            "findings can be bring-up transients.",
            note="GAP: settle-gate candidate. Owner: model-tiers.",
        ),
        _e(
            "app_audit_investigation",
            (NONE,),
            KIND_SIGNAL,
            justification="LLM-backed app-audit investigation outcome. Per-app, "
            "investigation-gated; a real finding should surface.",
        ),
        _e(
            "cascade_telemetry",
            (NONE,),
            KIND_SIGNAL,
            justification="Per-session tier-cascade telemetry roll-up (advisory). The "
            "cascade_audit summary carries any urgency; harmless on a fresh pod.",
        ),
        _e(
            "gateway",
            (NONE,),
            KIND_DISPATCH,
            justification="Gateway-plugin-emitted operational signal. Gateway-down is "
            "user-affecting and must page immediately, on a fresh pod or not.",
        ),
    ]
)


# ── Query / validation API (used by tools/signal-protection-lint + tests) ────
def get(producer: str) -> ProducerProtection | None:
    """Return the registered protection for ``producer`` (or None)."""
    return PROTECTIONS.get(producer)


def is_registered(producer: str) -> bool:
    return producer in PROTECTIONS


def claimed_files() -> set[str]:
    """Repo-relative source files claimed by some producer's ``emits_from``.

    The lint's file-coverage check: any file with a ``signals.store.observe(...)``
    call that is NOT in this set hosts an undeclared producer.
    """
    out: set[str] = set()
    for entry in PROTECTIONS.values():
        out.update(entry.emits_from)
    return out


def gaps() -> list[ProducerProtection]:
    """Producers registered at posture NONE but flagged as a coverage gap."""
    return [e for e in PROTECTIONS.values() if e.gap]


def entry_errors(entry: ProducerProtection) -> list[str]:
    """Policy violations for a single entry (the per-entry rule the lint enforces).

    * A NONE producer MUST carry a non-empty ``justification`` (the escape hatch
      is "explain it", never "gate it").
    * NONE is exclusive — it cannot be combined with a gate posture.
    * ``gap=True`` is only meaningful for a NONE producer.
    """
    name = entry.producer
    errors: list[str] = []
    if entry.is_none:
        if len(entry.postures) != 1:
            errors.append(
                f"{name}: NONE is exclusive — cannot combine with a gate "
                f"(got {list(entry.postures)})"
            )
        if not entry.justification.strip():
            errors.append(
                f"{name}: posture 'none' requires a justification (why does this "
                f"producer deliberately always page on a fresh pod?)"
            )
    elif entry.gap:
        errors.append(f"{name}: gap=True is only meaningful for a 'none' producer")
    return errors


def consistency_errors() -> list[str]:
    """Registry-internal policy violations (the lint blocks on any of these)."""
    errors: list[str] = []
    for name, e in PROTECTIONS.items():
        if name != e.producer:
            errors.append(f"{name}: key does not match entry.producer ({e.producer!r})")
        errors.extend(entry_errors(e))
    return errors


# NOTE: deliberately NOT raised at import. The committed table is kept consistent
# by ``tools/signal-protection-lint`` (rule 2) and ``test_registry_is_internally_
# consistent`` — and importing-to-report is exactly what the lint does, so a hard
# raise here would make the lint crash on a malformed table instead of reporting
# it cleanly.
