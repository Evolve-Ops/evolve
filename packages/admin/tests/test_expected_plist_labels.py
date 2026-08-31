"""Regression tests for deploy.expected_plist_labels.

Pins the contract that the redundant `ai.openclaw.evolve.heal` daemon stays
out of the expected-labels set. Both heal daemons running concurrently caused
two heal probes per 5-min cycle on different offsets — close-together probes
triggered an openclaw 1006 connection-cleanup race on evolve specifically →
recurring kill cycle.

Verified empirically (2026-04-29 03:19Z–04:19Z): with both heal daemons
running, evolve had +2 down events in 30 min. After bootout of the redundant
daemon, +0 events in 30 min (gateway PID stayed stable the entire window).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy as _deploy_mod  # noqa: E402
from evolve_admin.deploy import (  # noqa: E402
    expected_plist_labels,
    find_orphaned_plists,
    per_bot_evolve_plist_labels,
)


def test_canonical_heal_daemon_in_expected_labels():
    """The canonical heal daemon (ai.evolve.evolve.heal) must be in the
    expected set so it's not flagged as orphaned."""
    network = {"members": ["team_bot_a", "admin_bot", "evolve"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.evolve.evolve.heal" in labels


def test_redundant_heal_daemon_not_in_expected_labels():
    """The redundant heal daemon (ai.openclaw.evolve.heal) must NOT be in the
    expected set. If it appears here, it gets installed via install_staged_plists
    AND find_orphaned_plists won't flag the existing live install for removal —
    re-introducing the kill cycle on every deploy.
    """
    network = {"members": ["team_bot_a", "admin_bot", "evolve"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.openclaw.evolve.heal" not in labels, (
        "ai.openclaw.evolve.heal in expected_plist_labels would re-introduce "
        "the redundant heal daemon (root cause of evolve restart cycle)"
    )


def test_other_evolve_level_jobs_still_present():
    """Sanity: removing the redundant heal didn't accidentally remove other
    evolve-level jobs that share the same expected-labels block."""
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    for required in (
        "ai.openclaw.evolve.measure",
        "ai.openclaw.evolve.better",
        "ai.evolve.evolve.heal",
        "ai.evolve.evolve.audit",
        "ai.evolve.evolve.verify",
        "ai.evolve.evolve.admin-ui",
    ):
        assert required in labels, f"required label {required!r} missing"


def test_manifest_reflex_runner_in_expected_labels():
    """The manifest-reflex runner is a pod-wide infra job; must be in the
    expected set so find_orphaned_plists doesn't flag it for removal on
    every deploy."""
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.openclaw.evolve.manifest-reflex-runner" in labels


def test_app_posture_review_in_expected_labels():
    """The app-posture-review weekly job is pod-wide infra; must be in
    the expected set so find_orphaned_plists doesn't flag its plist for
    removal on every deploy."""
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.openclaw.evolve.app-posture-review" in labels


def test_embedding_monitor_in_expected_labels():
    """The embedding_monitor daemon (added 2026-05-09) is what surfaces
    Security_bot-class incidents — quota-exhausted or revoked credentials silently
    shadowing the embedding fallback chain. Must be in the expected set so
    find_orphaned_plists doesn't strip it on the next deploy after install."""
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.evolve.evolve.embedding_monitor" in labels


def test_audit_runner_labels_in_expected_set():
    """Per-bot audit-runner plists (Tier 2 + Tier 3) must be in the expected
    set. They are installed by _install_launchd_audit_runner[_tier3] during
    deploy_bot; omitting them from per_bot_evolve_plist_labels causes the
    orphan-sweeper to delete them every upgrade, which then reinstalls them,
    creating an infinite delete/reinstall loop."""
    network = {"members": ["team_bot_a", "evolve"], "bots": {}}
    labels = expected_plist_labels(network)
    for bot_id in ("team_bot_a", "evolve"):
        assert f"ai.openclaw.evolve.audit-runner.{bot_id}" in labels
        assert f"ai.openclaw.evolve.audit-runner-t3.{bot_id}" in labels


def test_proposal_synthesizer_in_expected_labels():
    """The proposal_synthesizer daemon (every 6h) is what drains
    ``candidates/synthesizing/`` and runs the LLM synthesis pass over
    substrate aggregates. Spec:
    internal/spec-proposal-synthesizer-2026-05-10.md §7. Must be in the
    expected set so find_orphaned_plists doesn't strip it on the next
    deploy."""
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.evolve.evolve.proposal_synthesizer" in labels


def test_log_cap_in_expected_labels():
    """The log-cap daemon (daily 03:45) rotates the three flat-file logs
    that have no in-process rotation (audit.log, better_engine.log,
    audit-warns.jsonl). Must be in the expected set so the orphan-sweeper
    doesn't strip the plist on the next deploy — without it the disk
    fillup risk that drove the 2026-06-01 incident reopens."""
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.evolve.evolve.log-cap" in labels


# ─── 2026-05-26 orphan-sweep audit ─────────────────────────────────────────
# Six labels were being installed but missing from expected_plist_labels,
# so find_orphaned_plists flagged them for deletion on the next upgrade.
# Caught on the test pod when the Admin UI orphan banner listed every
# per-bot backup plist alongside 5 pod-wide ones. The per-bot backup slice
# is covered above (PR #1611); the tests below pin the 5 pod-wide labels
# that PR #1622 added to expected_plist_labels.


def test_per_bot_backup_label_in_expected_set():
    """The per-bot nightly git-backup daemon (`ai.evolve.<bot>.backup`)
    is installed by ``_install_launchd_backup`` during ``deploy_bot``.
    It MUST be in ``per_bot_evolve_plist_labels`` (and therefore the
    expected set) so the upgrade orphan-sweeper doesn't delete every
    bot's backup plist on the next run — which would silently disable
    every bot's nightly git backup.

    Regression: as of 2026-05-26 the Maintenance → System orphan
    banner listed all 7 ``ai.evolve.<bot>.backup.plist`` files for
    removal because the label was missing from the source-of-truth set.
    """
    members = ["team_bot_a", "team_bot_c", "personal_bot", "admin_bot", "security_bot", "team_bot_b", "evolve"]
    network = {"members": members, "bots": {}}
    labels = expected_plist_labels(network)
    for bot_id in members:
        backup_label = f"ai.evolve.{bot_id}.backup"
        assert backup_label in labels, (
            f"{backup_label!r} missing from expected_plist_labels — the "
            "upgrade orphan-sweeper will delete this bot's backup plist"
        )
        # Also verify the per-bot source-of-truth list directly.
        assert backup_label in per_bot_evolve_plist_labels(bot_id)


def test_find_orphaned_plists_does_not_flag_per_bot_backup(tmp_path, monkeypatch):
    """``find_orphaned_plists`` scans LAUNCHD_DIR and returns anything
    not in ``expected_plist_labels``. With the backup label in the
    expected set, a ``/Library/LaunchDaemons/ai.evolve.<bot>.backup.plist``
    for a network member must NOT appear in the orphan list.

    Pairs with ``test_per_bot_backup_label_in_expected_set`` — that
    test pins the contract; this one exercises the actual sweep that
    would have deleted the file.
    """
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    members = ["team_bot_a", "personal_bot", "evolve"]
    for bot_id in members:
        (launchd / f"ai.evolve.{bot_id}.backup.plist").write_text("<plist/>")
    # Synthetic orphan: a label nothing in the codebase installs.
    # Confirms the sweep still flags real orphans (negative control).
    # We can't use a recently-retired real label here because PR #1622
    # added the previously-orphaned labels (digest-flush, usage-logger,
    # alerts_loop_monitor, etc.) to the expected set — they're no longer
    # orphans, so they can't serve as negative controls anymore.
    (launchd / "ai.evolve.evolve.retired-fake-daemon.plist").write_text("<plist/>")

    monkeypatch.setattr(_deploy_mod, "LAUNCHD_DIR", launchd)

    network = {"members": members, "bots": {}, "sharedDir": str(tmp_path)}
    orphans = find_orphaned_plists(network)
    orphan_stems = {p.stem for p in orphans}

    for bot_id in members:
        assert f"ai.evolve.{bot_id}.backup" not in orphan_stems, (
            f"per-bot backup plist for {bot_id} flagged as orphan — "
            "upgrade would delete it and silently disable nightly backups"
        )
    # Negative control: a genuinely orphaned plist still gets flagged.
    assert "ai.evolve.evolve.retired-fake-daemon" in orphan_stems


def test_digest_flush_in_expected_labels():
    """``ai.evolve.evolve.digest-flush`` is installed by
    digest_dispatcher.install_launchd() during install-infra-jobs. Drives
    operator alert-digest delivery (Phase G of
    spec-alert-subscriptions-2026-05-10.md)."""
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.evolve.evolve.digest-flush" in labels


def test_security_cve_scan_finalize_in_expected_labels():
    """``ai.evolve.evolve.security-cve-scan-finalize`` is installed by
    _install_launchd_cve_scan_finalize("evolve") during install-infra-jobs.
    Daily 09:10 PT CVE-scan finalizer — applies installed-version +
    baseline + idempotency filters to the LLM candidate JSON and dispatches
    via the security channel."""
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.evolve.evolve.security-cve-scan-finalize" in labels


def test_usage_jobs_in_expected_labels():
    """Both daily app-usage sweeps are installed by
    _install_launchd_usage_jobs("evolve") during install-infra-jobs:
    ``usage-logger`` (manifest-mtime footprint — the fallback signal) and
    ``usage-by-app`` (per-app rollup over turn annotations — the primary
    one, AL-1.3). Dropping either from the expected set means the next
    orphan-sweep deletes its plist and the per-app usage surfaces freeze."""
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.evolve.evolve.usage-logger" in labels
    assert "ai.evolve.evolve.usage-by-app" in labels


def test_alerts_loop_monitor_in_expected_labels():
    """``ai.openclaw.evolve.alerts_loop_monitor`` is installed by
    _install_launchd_alerts_loop_monitor during install-infra-jobs.
    Hourly Signal producer for dispatcher-log loop patterns — the early
    warning when alerting infra itself wedges."""
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.openclaw.evolve.alerts_loop_monitor" in labels


def test_cascade_audit_runner_in_expected_labels():
    """``ai.openclaw.evolve.cascade_audit_runner`` is installed by
    _install_launchd_cascade_audit_runner during install-infra-jobs.

    Hourly bridge from cascade telemetry spans into the pod's standard
    alerting layer (Signals) and Phase 4 calibration layer (per-day
    labels jsonl). Three Signal types under producer ``cascade_audit``:
    cascade_anomaly_*, dangerous_combo, runaway_rate_tripped.

    If this label is missing from expected_plist_labels, the next
    upgrade orphan-sweep deletes the daemon — every cascade Signal
    stops firing and the Phase 4 tuner stops receiving labels.

    Spec: internal/spec-tier-cascade-2026-05-26.md § audit layer.
    """
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.openclaw.evolve.cascade_audit_runner" in labels


def test_pod_perms_drift_monitor_in_expected_labels():
    """``ai.openclaw.evolve.pod_perms_drift_monitor`` is installed by
    _install_launchd_pod_perms_drift_monitor during install-infra-jobs.

    Hourly check_only pass over the perm contract that ensure_pod_perms
    enforces at deploy time. Catches the dir-owner drift class where a
    per-bot daemon (running as the bot user) is the first writer to a
    shared dir, so the dir gets created with bot-user ownership. With
    sticky 1777, only the dir owner can rename foreign files — so
    cross-user admin-server operations (dismissing a proposal owned by
    a different bot daemon, etc.) fail with EACCES until the next
    deploy. This daemon closes that gap by emitting a Signal when drift
    accumulates so the operator runs ``sudo evolve-admin
    ensure-pod-perms`` before the next deploy.

    If this label is missing from expected_plist_labels, the orphan
    sweeper deletes it on the next deploy and the drift class becomes
    invisible again between deploys."""
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.openclaw.evolve.pod_perms_drift_monitor" in labels


def test_cascade_pressure_watchdog_in_expected_labels():
    """``ai.openclaw.evolve.cascade_pressure_watchdog`` is installed by
    _install_launchd_cascade_pressure_watchdog during install-infra-jobs.

    60-second heartbeat daemon that reads cascade telemetry spans + the
    per-bot ``tier1_active.json`` in-process counters, writes the
    pod-wide ``pressure_flags.json``. CascadeController consults the
    flags at decision time to throttle escalation under pod pressure
    (escalation storms, tier1-concurrency cap, spend bursts).

    If this label is missing from expected_plist_labels, the next
    upgrade orphan-sweep deletes it — the watchdog goes silent, the
    pressure_flags.json file stops updating, and CascadeController
    treats the stale heartbeat as a dead watchdog → conservative
    fallback. That's the safe degradation path but it defeats the
    point of the daemon.

    Spec: internal/spec-tier-cascade-2026-05-26.md § pressure watchdog.
    """
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.openclaw.evolve.cascade_pressure_watchdog" in labels


def test_gmail_integration_health_in_expected_labels():
    """``ai.openclaw.evolve.gmail_integration_health`` is installed by
    _install_launchd_gmail_integration_health during install-infra-jobs.

    30-minute per-bot Google API probe — fires a signature-deduped Signal
    per (bot, failure_category) covering 401 (DwD unauthorized), 403
    (scope), 404 (subject), 5xx (transient). Auto-resolves on next clean
    probe. Without this label in the expected set the next upgrade
    orphan-sweep deletes the daemon and the wizard's remediation copy
    stops surfacing live for failing bots.

    Spec: internal/spec-google-integration-paths-2026-05-30.md §8 (PR δ).
    """
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.openclaw.evolve.gmail_integration_health" in labels


def test_oc_substrate_monitor_in_expected_labels():
    """``ai.openclaw.evolve.oc_substrate_monitor`` is installed by
    _install_launchd_oc_substrate_monitor during install-infra-jobs.

    Hourly freshness Signal producer for OC's auto-updater LaunchAgent
    state file and the usage-collector LaunchAgent's daily rollup. Both
    live outside the ``ai.{evolve,openclaw}.evolve.*`` namespace
    monitor_coverage walks, so silences historically only surfaced via
    the pod-admin-side openclaw-watchdog. If this label drops out of the
    expected set the upgrade orphan-sweep deletes the daemon and
    substrate silences go undetected again.
    """
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.openclaw.evolve.oc_substrate_monitor" in labels


def test_home_artifacts_monitor_in_expected_labels():
    """``ai.openclaw.evolve.home_artifacts_monitor`` is installed by
    _install_launchd_home_artifacts_monitor during install-infra-jobs.

    Hourly per-bot workspace large/exec-file scan + macOS LaunchServices
    Quarantine DB read — replaces the retired pod-admin-side
    openclaw-watchdog's ``check_large_files_and_executables`` and
    ``check_quarantine_log`` checks. If this label drops out of the
    expected set the next upgrade orphan-sweep deletes it and the
    user-account-compromise canary goes silent.
    """
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.openclaw.evolve.home_artifacts_monitor" in labels


def test_tuples_extractor_in_expected_labels():
    """``ai.openclaw.evolve.tuples`` is installed by _install_launchd_tuples
    during install-infra-jobs. Daily 01:30 L3 tuple-extraction into
    observations/<bot>/ — generators in better_engine_refresh consume
    these tuples via observations.access."""
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.openclaw.evolve.tuples" in labels


def test_autonomy_limits_in_expected_labels():
    """``ai.evolve.evolve.autonomy-limits`` is installed on every deploy by
    ``_install_launchd_autonomy_limits("evolve")`` (deploy_infra_jobs). Every
    5 min it evaluates rung-3 daily caps against the bot-side outward-action
    ledger, pauses capped integrations for the rest of the UTC day, and runs
    the auto-demotion reflex. Spec:
    internal/spec-autonomy-ladder-2026-06-10.md §1.3 + §3.3 (Phase B).

    The label was absent from the expected set from 2026-06-11 (#2679, the
    installer's own PR) to 2026-08-23, so ``find_orphaned_plists`` classified
    the freshly-installed plist as an orphan and ``remove_orphaned_plists``
    deleted it on the next ``evolve-admin upgrade``, leaving the daemon dead
    until something re-ran ``install_evolve_infra_jobs`` — which a bot deploy
    does not do (only ``evolve-admin install-infra-jobs``, or a repo-puller
    pull touching ``_INFRA_INSTALL_PATHS``), plus a spurious orphan banner on
    the Versions page. This is the
    exact bug class the structural meta-test below exists to catch at PR
    time; it never fired because the meta-test was itself quarantined
    behind a stale reason (see #3768)."""
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = expected_plist_labels(network)
    assert "ai.evolve.evolve.autonomy-limits" in labels


def test_opik_companion_label_is_presence_gated(tmp_path, monkeypatch):
    """``ai.evolve.opik`` must be in the expected set exactly when its plist is
    on disk — spared from the sweeper on pods that opted in, and not demanded
    on pods that did not.

    ``install_opik_companion`` installs it straight through the Scheduler seam
    (opt-in: ``evolve-admin install-infra-jobs --with-opik``), bypassing
    ``_install_launchd`` entirely, so the structural meta-test below cannot see
    it. Until 2026-08-23 the label was in no gate at all, while matching both
    the macOS ``ai.evolve.*.plist`` glob and Linux's ``_is_evolve_owned_label``
    and carrying no feature gate — so every Opik pod lost the daemon on its
    next ``evolve-admin upgrade``. Found by the review pass on the
    autonomy-limits repair; same bug class, one seam over.

    The presence gate (not an unconditional entry) is what keeps
    ``health._check_launchd`` from reporting a permanent "Plist not found" on
    the majority of pods that never opted in — the same shape as the
    per-bot iMessage poller."""
    monkeypatch.setattr(_deploy_mod, "LAUNCHD_DIR", tmp_path)
    network = {"members": ["evolve"], "bots": {}}

    assert _deploy_mod.OPIK_LAUNCHD_LABEL not in expected_plist_labels(network), (
        "opik label demanded on a pod that never installed it — "
        "health._check_launchd would report a permanent missing plist"
    )

    (tmp_path / f"{_deploy_mod.OPIK_LAUNCHD_LABEL}.plist").write_text("<plist/>")
    assert _deploy_mod.OPIK_LAUNCHD_LABEL in expected_plist_labels(network), (
        "opik label missing while its plist is installed — "
        "remove_orphaned_plists deletes it on the next upgrade"
    )


# ─── Meta-test: structural drift guard ──────────────────────────────────────
#
# The 2026-05-26 orphan-sweep incident (PR #1622 / #1611) found that 12
# actively-installed daemons were missing from `expected_plist_labels`, so
# the upgrade orphan-sweeper would have deleted them. Per-label regression
# tests above pin each one we know about — but the underlying bug class is
# structural: any future `_install_launchd(label=…)` callsite added without
# a matching entry in `expected_plist_labels` (or `per_bot_evolve_plist_labels`)
# reproduces the same drift.
#
# This meta-test closes the loop by walking deploy.py's AST and asserting
# every install-callsite label is reachable from the expected set. New
# daemons fail this test at PR time rather than at orphan-banner time.


# Modules whose AST Pass 1 walks for `_install_launchd(label=…)` callsites.
# `deploy.py` holds the bulk; `analyzer_monitor_jobs.py` imports
# `deploy._install_launchd` and installs 11 more daemons from its own file, so
# walking only deploy.py left those 11 undefended (a 12th monitor added there
# without an `ANALYZER_MONITOR_PLIST_LABELS` entry matches the sweeper's
# `ai.openclaw.evolve.*.plist` glob and is deleted on the next upgrade).
# If a new module starts calling `_install_launchd`, add it here.
#
# Keys are package-QUALIFIED (`<package>/<path>`), matching Pass 3's census
# keys. They were bare `deploy.py`-style names until the census started walking
# `packages/analyzer` too: an `_install_launchd` call there produced the
# instruction "add 'foo.py' to _WALKED_SOURCES", and following it wedged four
# tests, because the resolver only ever looked under `evolve_admin/`. A
# remediation the maintainer cannot carry out is worse than no message at all.
_PACKAGE_ROOTS: dict[str, Path] = {
    "evolve_admin": _ADMIN_DIR / "evolve_admin",
    "analyzer": _ADMIN_DIR.parent / "analyzer",
}
_WALKED_SOURCES: tuple[str, ...] = (
    "evolve_admin/deploy.py",
    "evolve_admin/analyzer_monitor_jobs.py",
)


def _module_path(module_key: str) -> Path:
    """Resolve a package-qualified module key (``<package>/<path>``) to a file."""
    prefix, _, rel = module_key.partition("/")
    if prefix not in _PACKAGE_ROOTS:
        pytest.fail(
            f"unknown package prefix in module key {module_key!r}; "
            f"known prefixes: {sorted(_PACKAGE_ROOTS)}"
        )
    return _PACKAGE_ROOTS[prefix] / rel

# External installers that own their own `install_launchd()` entry point
# (i.e., not via deploy._install_launchd) plus the module-level constant
# that holds their label. If you add another external installer, add it here
# so the meta-test still covers the full installed surface.
_EXTERNAL_INSTALLERS: tuple[tuple[str, str], ...] = (
    ("evolve_admin.repo_puller", "REPO_PULLER_LABEL"),
    ("evolve_admin.alerts.digest_dispatcher", "DIGEST_LABEL"),
    ("evolve_admin.applications.audit_scheduler", "SCHEDULER_LABEL"),
    # ALPHA-1 (#3768). The NOTE that shipped with this entry said it added no
    # coverage because its only consumer was quarantined and crashing; that is
    # no longer true — the consumer is repaired and de-quarantined, so this
    # entry is live and Pass 2 resolves it.
    ("evolve_admin.app_discovery", "APP_DISCOVERY_SWEEP_LABEL"),
    # Own `install_launchd()`, reached from pairing setup rather than deploy.py.
    ("evolve_admin.pairing.auto_approver", "SWEEP_LABEL"),
    # Installs via `get_scheduler().install` from its own module.
    ("evolve_admin.mcp_service", "LABEL"),
)


# A loop target bound to a non-string value (the `minute` int in the
# usage-jobs loop). Binding a sentinel rather than leaving the name out keeps
# "bound to something we cannot interpolate" distinct from "not a loop target
# at all" — the latter is what lets `{bot_id}` survive as a placeholder, and
# a `bot_id` loop target holding a non-string must NOT take that path.
_NON_STRING_BINDING = object()

# A `partial(_install_launchd, **kw)` whose splat could be carrying `label`.
# Distinct from "binds no label" (which defers the label to the eventual call,
# a resolvable shape) — this one is reported and fails loudly.
_SPLAT_LABEL = object()


def _names_bound_by(node: "object") -> set[str]:
    """Every name ``node`` binds, for any binding statement or expression.

    Deliberately exhaustive rather than a roster of the shapes seen today: a
    rebinding this misses is a label silently expanded to a value that is never
    installed, which is the failure this whole file guards against. Walking the
    target subtree (rather than matching ``ast.Name`` directly) is what covers
    tuple- and starred-unpacking.
    """
    import ast

    def _targets(*nodes: "object") -> set[str]:
        out: set[str] = set()
        for n in nodes:
            if n is None:
                continue
            for sub in ast.walk(n):  # type: ignore[arg-type]
                if isinstance(sub, ast.Name):
                    out.add(sub.id)
        return out

    if isinstance(node, ast.Assign):
        return _targets(*node.targets)
    if isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)):
        return _targets(node.target)
    if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        return _targets(node.target)
    if isinstance(node, ast.withitem):
        return _targets(node.optional_vars)
    if isinstance(node, ast.ExceptHandler):
        return {node.name} if node.name else set()
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {(a.asname or a.name).split(".")[0] for a in node.names}
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return set(node.names)
    if isinstance(node, ast.Delete):
        return _targets(*node.targets)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = node.args
        params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        if args.vararg:
            params.append(args.vararg)
        if args.kwarg:
            params.append(args.kwarg)
        return {node.name, *(a.arg for a in params)}
    if isinstance(node, ast.Lambda):
        args = node.args
        params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        if args.vararg:
            params.append(args.vararg)
        if args.kwarg:
            params.append(args.kwarg)
        return {a.arg for a in params}
    if isinstance(node, ast.ClassDef):
        return {node.name}
    return set()


def _body_breaks_or_rebinds(for_node: "object", names: set[str]) -> bool:
    """True when ``for_node``'s body makes its unrolled bindings unsound.

    Two conditions: a ``break``/``continue`` that binds to THIS loop, or any
    rebinding of one of its targets.

    A ``break`` inside a NESTED loop binds to that loop and cannot change this
    one's iterations, so its body is opaque here — descending into it would
    refuse a perfectly sound unroll and red the gate on a legitimate installer.
    A nested loop's ``else:`` clause is ours again, so that IS descended into.
    Rebindings count wherever they appear, nested loops included: they execute
    in this scope.
    """
    import ast

    def _breaks_this_loop(body: list) -> bool:
        stack: list = list(body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.Break, ast.Continue)):
                return True
            if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                # The nested loop's body swallows break/continue; its orelse
                # does not.
                stack.extend(node.orelse)
                continue
            if isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef),
            ):
                continue  # a different frame entirely
            stack.extend(ast.iter_child_nodes(node))
        return False

    if _breaks_this_loop(list(for_node.body)):  # type: ignore[union-attr]
        return True
    for stmt in for_node.body:  # type: ignore[union-attr]
        for sub in ast.walk(stmt):
            if _names_bound_by(sub) & names:
                return True
    return False



def _collect_install_launchd_labels(
    source: str = "evolve_admin/deploy.py",
) -> list[tuple[int, str, str]]:
    """Walk ``<source>``'s AST and return every
    ``_install_launchd(label=…)`` label seen.

    Each entry is ``(lineno, label_or_template, kind)`` where ``kind`` is
    one of ``"literal"`` / ``"f-string"`` / ``"constant"`` / ``"unresolved"``.

    ``{bot_id}`` is the ONE placeholder that survives into the returned
    template — the caller expands it against a candidate bot list. The only
    other name resolvable inside a label f-string is an enclosing ``for``
    loop's target, when that loop iterates a literal tuple/list (the
    ``_install_launchd_usage_jobs`` shape: ``for label, script, minute in
    (("usage-logger", …), …)``); such a loop yields ONE result row per
    iteration, so the two usage sweeps are checked as the two concrete labels
    they actually install. There is deliberately NO module-constant fallback
    inside an f-string — see ``_resolve_label``. A bare ``label=SOME_CONST``
    (not an f-string) IS resolved from the module constants, since no local
    can shadow the value the walker reads there.

    The callee is matched as a bare ``_install_launchd(...)``, an
    attribute-form ``deploy._install_launchd(...)``, a name assigned or
    imported-as an alias of either (``_il = _install_launchd``, transitively),
    and a ``partial()`` / ``functools.partial()`` over any of those. A partial
    that binds ``label`` resolves at the ``partial()`` itself; one that does
    not is simply another alias, since it shifts no positional argument.

    ``"unresolved"`` is the loud-failure path: any AST shape we don't know
    how to handle is reported with its dump so the meta-test fails until
    someone teaches the walker about the new pattern. Quietly skipping
    unrecognized shapes would silently re-open the drift bug class — which
    is why a positional label argument, a ``**kwargs`` splat, an
    attribute-form call, an alias the walker cannot pin to one binding, and a
    reference to the installer that escapes into code this walker does not
    model are all reported rather than passed over.

    Two limits are accepted rather than modelled, because closing them needs
    real cross-scope dataflow. Neither hides a daemon:

    * An alias called through something this walker does not resolve — another
      module (``deploy._il(label=…)``), an attribute (``self._il(…)``), a
      container — does not yield its label. It is not silent, though: an alias
      binding is exempt from the escaping-reference rule ONLY when the alias is
      consumed here (a bare-name call, handed on to another alias binding, or
      handed to a ``partial()``), so a binding whose consumer this walker
      cannot see is itself reported. That exemption being unconditional was a
      silent hole through the first revision of this rule, and reading the
      gate off the non-ambiguous subset left a narrower one behind it.
      "Consumed" is scope-blind, like everything else here: ANY bare
      ``_il(...)`` call in the module marks ``_il`` consumed, so a class-body
      alias reached only through ``self._il(…)`` is quiet if some unrelated
      module-level ``_il(...)`` call also exists.
    * A labelled ``partial`` that is ALWAYS called with an override still
      asserts its own baked label. That over-reports rather than
      under-reports, and the fix at such a callsite is one line.
    """
    return _collect_labels_from_source(_module_path(source).read_text())


def _collect_labels_from_source(src_text: str) -> list[tuple[int, str, str]]:
    """The walker proper, over source TEXT rather than a repo file.

    Split out so the walker's own contract can be tested against synthetic
    sources (see ``test_walker_shape_table``). Without that seam every
    hardening rule is unpinned: it could be deleted and the suite would stay
    green, because the real ``deploy.py`` happens to use none of the shapes
    each rule exists to catch.
    """
    import ast

    tree = ast.parse(src_text)

    # Resolve module-level `LABEL = "ai.evolve…"` assignments so
    # `label=LABEL_NAME` references can be looked up.
    module_constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                module_constants[tgt.id] = node.value.value

    # Parent links, so a call node can find the `for` loops it sits inside.
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    # ── Aliases of the installer ────────────────────────────────────────────
    # `_il = _install_launchd; _il(label=…)` and `_p = partial(_install_launchd,
    # label=…)` both walked straight past the callee match below, producing ZERO
    # rows with no `unresolved` marker — the exact silence this walker's
    # docstring promises it never emits.
    #
    # Resolution is deliberately shallow, and stays on the safe side of the
    # trade the rest of this file makes: a name bound by a plain assignment (or
    # an `import … as`) to the installer, to another such name, or to a
    # `partial()` over one. Anything richer — a name that is sometimes the
    # installer and sometimes not, a reference handed to another function or
    # stashed in a container — is REPORTED as unresolved rather than guessed
    # at. The alternative framing, "report every call whose callee cannot be
    # determined", would red this blocking gate on the thousands of ordinary
    # calls in deploy.py; the rule here fires only on code that actually names
    # the installer, of which there is none today.
    INSTALLER = "_install_launchd"

    def _is_installer_ref(node: object, names: set[str]) -> bool:
        """True when ``node`` names the installer: `_il` / `deploy._install_launchd`."""
        return (isinstance(node, ast.Name) and node.id in names) or (
            isinstance(node, ast.Attribute) and node.attr == INSTALLER
        )

    def _is_partial_call(node: ast.Call) -> bool:
        """`partial(...)` / `functools.partial(...)`, matched by NAME.

        Matching the spelling rather than the imported symbol is the same
        shallow-by-design choice as everywhere else here. A partial reached
        under some other spelling (`p = functools.partial; p(_install_launchd, …)`)
        is not missed — it falls through to the escaping-reference rule below,
        which reports the installer reference it hands over.
        """
        f = node.func
        return (isinstance(f, ast.Name) and f.id == "partial") or (
            isinstance(f, ast.Attribute) and f.attr == "partial"
        )

    def _partial_bound_label(node: ast.Call) -> object:
        """The label a ``partial(installer, …)`` has ALREADY bound.

        Returns the label expression, ``_SPLAT_LABEL`` when a ``**kwargs``
        splat could be carrying one, or ``None`` when the partial binds no
        label — in which case the label still comes from the eventual call and
        the partial's alias behaves exactly like the installer itself.
        """
        for kw in node.keywords:
            if kw.arg == "label":
                return kw.value
        if any(kw.arg is None for kw in node.keywords):
            return _SPLAT_LABEL
        # `args[0]` is the installer; `label` is its first parameter, so the
        # partial's first OWN positional is the label.
        return node.args[1] if len(node.args) > 1 else None

    # Names that behave exactly like `_install_launchd` at a callsite, and
    # names holding a partial that has already bound its label (whose calls
    # matter only when they OVERRIDE that label — `partial(f, label="a")(label="b")`
    # installs "b").
    aliases: set[str] = {INSTALLER}
    bound_partials: set[str] = set()
    # Assignment / import nodes that legitimately bind an alias, so the
    # ambiguity scan below does not count them against it.
    alias_binders: set[int] = set()
    # `partial()` calls whose result lands in a plain name, so a labelless one
    # is covered by its alias's calls rather than reported as unfollowable.
    assigned_partials: set[int] = set()

    changed = True
    while changed:  # fixpoint: `_a = _install_launchd` then `_b = _a`
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for imported in node.names:
                    if imported.asname and imported.name.split(".")[-1] == INSTALLER:
                        alias_binders.add(id(node))
                        if imported.asname not in aliases:
                            aliases.add(imported.asname)
                            changed = True
                continue
            if not (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                continue
            name = node.targets[0].id
            value = node.value
            target: set[str] | None = None
            if _is_installer_ref(value, aliases):
                target = aliases
            elif isinstance(value, ast.Name) and value.id in bound_partials:
                target = bound_partials
            elif (
                isinstance(value, ast.Call)
                and _is_partial_call(value)
                and value.args
                and _is_installer_ref(value.args[0], aliases)
            ):
                assigned_partials.add(id(value))
                target = (
                    aliases
                    if _partial_bound_label(value) is None
                    else bound_partials
                )
            if target is None:
                continue
            alias_binders.add(id(node))
            if name not in target:
                target.add(name)
                changed = True

    # A tracked name that is ALSO bound by something other than those bindings
    # is not soundly resolvable: this walker has no scope model, so it cannot
    # say which binding is in force at a given call. Resolving the label anyway
    # would demand a phantom daemon that `health._check_launchd` would then
    # hunt for forever — the same hazard the guard-`if` refusal exists for.
    ambiguous_aliases: set[str] = set()
    tracked_names = (aliases | bound_partials) - {INSTALLER}
    for node in ast.walk(tree):
        if id(node) in alias_binders:
            continue
        ambiguous_aliases |= _names_bound_by(node) & tracked_names

    def _static_iterations(for_node: ast.For) -> list[dict[str, object]] | None:
        """Unroll `for <names> in (<literal>, …):` into per-iteration name
        bindings, or return None when the loop isn't statically knowable.

        EVERY target name is bound on every iteration — a non-string value
        binds :data:`_NON_STRING_BINDING`, never nothing. An unbound name
        would fall through to the module-constant lookup and silently
        mis-expand the label (a loop target shadows module scope).
        """
        it = for_node.iter
        if not isinstance(it, (ast.Tuple, ast.List)):
            return None
        target = for_node.target
        if isinstance(target, ast.Name):
            names = [target.id]
        elif isinstance(target, ast.Tuple) and all(
            isinstance(e, ast.Name) for e in target.elts
        ):
            names = [e.id for e in target.elts]  # type: ignore[union-attr]
        else:
            return None

        # Unrolling assumes every iteration reaches the callsite with the
        # target still bound to that iteration's value. `break` / `continue`
        # make some iterations not reach it, and ANY rebinding of a target
        # inside the body makes the value at the callsite something else
        # entirely — either way the unrolled labels are not the installed
        # ones. Refuse to unroll; the label then resolves to "unresolved"
        # and fails loudly.
        if _body_breaks_or_rebinds(for_node, set(names)):
            return None

        iterations: list[dict[str, object]] = []
        # An empty literal iterable yields NO iterations, so the callsite emits
        # no rows. That is correct, not a silent skip: the loop body never runs,
        # so no daemon is installed and there is nothing to check.
        for elt in it.elts:
            if len(names) == 1:
                values: list[ast.expr] = [elt]
            elif isinstance(elt, (ast.Tuple, ast.List)):
                values = list(elt.elts)
            else:
                return None
            if len(values) != len(names):
                return None
            binding: dict[str, object] = {}
            for name, value in zip(names, values):
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    binding[name] = value.value
                else:
                    binding[name] = _NON_STRING_BINDING
            iterations.append(binding)
        return iterations

    def _loop_bindings(node: ast.AST) -> list[dict[str, object]]:
        """Every combination of loop-variable bindings in force at `node`.

        Returns ``[{}]`` when no enclosing loop is statically unrollable —
        i.e. the label is expanded exactly once, with no extra names bound.

        Only a loop whose BODY contains the call counts. A call in a
        ``for … else:`` clause runs once, after the loop, with the target at
        its last value — unrolling it would invent labels that are never
        installed and demand they be added to the expected set.
        """
        chain: list[ast.For] = []
        prev: ast.AST = node
        cur = parents.get(node)
        # Once a conditional lies between the callsite and a loop, that loop's
        # iterations no longer all reach the call — `for n in ("real",
        # "phantom"): if n == "real": _install_launchd(...)` would otherwise
        # emit a `phantom` label that is never installed, and the only way to
        # green the gate would be to add that phantom to expected_plist_labels,
        # where health._check_launchd would then hunt for a plist that cannot
        # exist. Refuse to unroll from that point outward; the label resolves
        # to "unresolved" and fails loudly instead.
        conditional_seen = False
        while cur is not None:
            if isinstance(cur, (ast.If, ast.Try, ast.While, ast.IfExp)) or (
                hasattr(ast, "Match") and isinstance(cur, ast.Match)
            ):
                conditional_seen = True
            if (
                isinstance(cur, ast.For)
                and not conditional_seen
                and any(prev is stmt for stmt in cur.body)
            ):
                chain.append(cur)
            prev = cur
            cur = parents.get(cur)
        chain.reverse()  # outermost first, so inner loops win on name clash

        combos: list[dict[str, object]] = [{}]
        for for_node in chain:
            iterations = _static_iterations(for_node)
            if iterations is None:
                continue
            combos = [{**combo, **b} for combo in combos for b in iterations]
        return combos

    results: list[tuple[int, str, str]] = []
    seen: set[tuple[int, str, str]] = set()

    def _emit(entry: tuple[int, str, str]) -> None:
        if entry not in seen:
            seen.add(entry)
            results.append(entry)

    def _resolve_label(node: ast.Call, v: ast.expr) -> None:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            _emit((node.lineno, v.value, "literal"))
        elif isinstance(v, ast.JoinedStr):
            for bindings in _loop_bindings(node):
                parts: list[str] = []
                ok = True
                for piece in v.values:
                    if isinstance(piece, ast.Constant):
                        parts.append(str(piece.value))
                        continue
                    if not (
                        isinstance(piece, ast.FormattedValue)
                        and isinstance(piece.value, ast.Name)
                        # A conversion (`!r`) or a format spec changes the
                        # rendered text, so a label carrying one is not
                        # something this walker can claim to have resolved.
                        and piece.conversion in (-1, None)
                        and piece.format_spec is None
                    ):
                        ok = False
                        break
                    name = piece.value.id
                    bound = bindings.get(name, None)
                    if isinstance(bound, str):
                        # Loop bindings shadow module scope, as at runtime.
                        parts.append(bound)
                    elif name == "bot_id" and bound is None:
                        # The one placeholder the caller expands. Checked AFTER
                        # loop bindings so a loop target named `bot_id` resolves
                        # to its real values instead of degrading to the weak
                        # any-of-candidates check.
                        parts.append("{bot_id}")
                    else:
                        # Deliberately NO module-constant fallback here. This
                        # walker has no scope model, so a function-local name
                        # that shadows a module-level constant would resolve to
                        # the MODULE value and expand the label to something
                        # never installed. Unresolved-and-loud is the only safe
                        # answer; no callsite needs the fallback today.
                        ok = False
                        break
                if ok:
                    _emit((node.lineno, "".join(parts), "f-string"))
                else:
                    _emit((
                        node.lineno,
                        f"<UNRESOLVED-FSTRING:{ast.dump(v)[:80]}>",
                        "unresolved",
                    ))
        elif isinstance(v, ast.Name) and v.id in module_constants:
            _emit((node.lineno, module_constants[v.id], "constant"))
        else:
            _emit((node.lineno, f"<UNRESOLVED:{ast.dump(v)[:80]}>", "unresolved"))

    def _resolve_installer_call(node: ast.Call) -> None:
        """Pull the label out of a call that reaches ``_install_launchd``.

        Shared by the direct/alias callsites and by a `partial()` alias that
        bound no label of its own — in the latter the partial supplied no
        positional either, so the call's own arguments line up with the
        installer's parameters exactly as a direct call's do.
        """
        keyword_args = {kw.arg: kw.value for kw in node.keywords}
        if "label" in keyword_args:
            _resolve_label(node, keyword_args["label"])
        elif node.args:
            # `label` is _install_launchd's first positional parameter.
            _resolve_label(node, node.args[0])
        elif any(kw.arg is None for kw in node.keywords):
            # `**kwargs` splat — the label is not statically visible.
            _emit((node.lineno, "<UNRESOLVED:**kwargs splat>", "unresolved"))
        else:
            _emit((node.lineno, "<UNRESOLVED:no label argument found>", "unresolved"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        # `partial(_install_launchd, …)` / `functools.partial(…)`. The label it
        # binds here is the one that gets installed unless a later call
        # overrides it, so resolve it at the partial rather than chasing the
        # object around.
        if (
            _is_partial_call(node)
            and node.args
            and _is_installer_ref(node.args[0], aliases | bound_partials)
        ):
            installer_arg = node.args[0]
            if (
                isinstance(installer_arg, ast.Name)
                and installer_arg.id in ambiguous_aliases
            ):
                # Same refusal as a direct call on an unpinnable alias: the
                # partial may not be over the installer at all, and baking its
                # label in would assert a daemon nothing installs.
                _emit((
                    node.lineno,
                    f"<UNRESOLVED:{installer_arg.id!r} is bound to the installer "
                    f"in one place and to something else in another>",
                    "unresolved",
                ))
                continue
            bound = _partial_bound_label(node)
            if bound is _SPLAT_LABEL:
                _emit((node.lineno, "<UNRESOLVED:**kwargs splat>", "unresolved"))
            elif bound is not None:
                _resolve_label(node, bound)
            elif id(node) not in assigned_partials:
                # Binds no label AND does not land in a plain name, so there is
                # no alias whose calls could supply one. Where it goes is
                # exactly the dataflow this walker refuses to model.
                _emit((
                    node.lineno,
                    "<UNRESOLVED:partial over the installer binds no label "
                    "and is not assigned to a name>",
                    "unresolved",
                ))
            continue

        # `_install_launchd(...)`, `deploy._install_launchd(...)`, and any
        # resolved alias of either.
        if isinstance(func, ast.Name) and func.id in ambiguous_aliases:
            _emit((
                node.lineno,
                f"<UNRESOLVED:{func.id!r} is bound to the installer in one place "
                f"and to something else in another>",
                "unresolved",
            ))
        elif _is_installer_ref(func, aliases):
            _resolve_installer_call(node)
        elif isinstance(func, ast.Name) and func.id in bound_partials:
            # The partial already bound a label and emitted it above; only an
            # explicit override at the call adds a label, and only a splat can
            # hide one.
            keyword_args = {kw.arg: kw.value for kw in node.keywords}
            if "label" in keyword_args:
                _resolve_label(node, keyword_args["label"])
            elif any(kw.arg is None for kw in node.keywords):
                _emit((node.lineno, "<UNRESOLVED:**kwargs splat>", "unresolved"))

    # ── Escaping references ─────────────────────────────────────────────────
    # Every reference to the installer (or to a resolved alias of it) that is
    # NOT a call, a `partial()` over it, or an alias binding hands the function
    # somewhere this walker cannot follow: `_dispatch(_install_launchd)`,
    # `HANDLERS["x"] = _install_launchd`, `return _install_launchd`. Silence
    # there is the same defect as the alias hole itself, so say so instead.
    #
    # `bound_partials` are deliberately NOT tracked here: their label is
    # already emitted at the `partial()`, so where the object travels does not
    # hide a daemon. `aliases` (which includes a labelless partial's name) is
    # exactly the set whose references could still be carrying a label nobody
    # has seen.
    escape_tracked = aliases - ambiguous_aliases
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _is_installer_ref(node.func, escape_tracked):
                exempt.add(id(node.func))
            if (
                _is_partial_call(node)
                and node.args
                and _is_installer_ref(node.args[0], escape_tracked)
            ):
                exempt.add(id(node.args[0]))
        elif id(node) in alias_binders and isinstance(node, ast.Assign):
            exempt.add(id(node.value))
    # An alias binding is only exempt because the alias itself is checked
    # somewhere. When it is NOT — no bare-name call in this module, and not
    # handed on to another alias binding — exempting the binding is the whole
    # silence this rule exists to remove: `_il = _install_launchd` in deploy.py
    # called as `deploy._il(label=…)` from analyzer_monitor_jobs.py resolves in
    # NEITHER module, and the daemon is swept. Resolving that needs cross-module
    # symbol dataflow; reporting the binding does not, and is the loud answer.
    consumed_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                consumed_aliases.add(node.func.id)
            if _is_partial_call(node) and node.args and isinstance(node.args[0], ast.Name):
                consumed_aliases.add(node.args[0].id)
        elif (
            id(node) in alias_binders
            and isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Name)
        ):
            consumed_aliases.add(node.value.id)
    for node in ast.walk(tree):
        if id(node) not in alias_binders:
            continue
        if isinstance(node, ast.Assign):
            bound_names = [node.targets[0].id]
        else:  # Import / ImportFrom
            bound_names = [
                a.asname
                for a in node.names
                if a.asname and a.name.split(".")[-1] == INSTALLER
            ]
        for bound_name in bound_names:
            # `aliases`, NOT `escape_tracked` (which excludes the ambiguous
            # ones). An alias that is ambiguous AND never locally called would
            # otherwise fall between the two rules: the ambiguity refusal only
            # fires at a callsite, and there is no callsite. `_il` bound in a
            # try/except ImportError fallback and called from another module
            # reached the original silent end state through that narrower door.
            # NOT `aliases | bound_partials`: a labelled partial legitimately
            # needs no consumer, since its label is already emitted.
            if bound_name not in aliases or bound_name in consumed_aliases:
                continue
            _emit((
                node.lineno,
                f"<UNRESOLVED:alias {bound_name!r} of the installer is bound here "
                f"but never called in this module>",
                "unresolved",
            ))
    for node in ast.walk(tree):
        if not _is_installer_ref(node, escape_tracked):
            continue
        if id(node) in exempt or not isinstance(
            getattr(node, "ctx", None), ast.Load
        ):
            continue
        _emit((
            node.lineno,
            f"<UNRESOLVED:installer reference escapes: {ast.dump(node)[:60]}>",
            "unresolved",
        ))

    return results


def _unresolved_placeholders(label: str) -> set[str]:
    """Names still wrapped in ``{}`` after ``{bot_id}`` substitution.

    A non-empty result means the AST walker handed back a template it
    could not fully resolve — a coverage hole, reported as such rather
    than crashed on.
    """
    import re

    return set(re.findall(r"\{([^{}]*)\}", label))


def test_every_install_launchd_label_is_in_expected_set():
    """Structural drift guard: every `_install_launchd(label=…)` callsite
    in deploy.py — plus the three external `install_launchd()` modules —
    must declare a label reachable from `expected_plist_labels(network)`
    for at least one candidate bot.

    What this catches: someone adds a new daemon to deploy.py and ships
    the PR without also adding the label to `expected_plist_labels` or
    `per_bot_evolve_plist_labels`. The per-label tests above only fire
    when someone *also* remembered to write the per-label test — this
    one fires regardless. See PR #1622 for the incident that surfaced
    the bug class (12 daemons drifted before anyone noticed).

    What this DOES NOT catch: labels installed by paths that aren't in
    deploy.py and aren't in `_EXTERNAL_INSTALLERS`. If you grow a new
    external installer, add it to that tuple. The meta-test will fail
    loudly if its AST walker encounters a label shape it can't resolve.
    """
    import importlib

    # Use members that exercise both the per-bot expansion path (team_bot_a, personal_bot)
    # and the "evolve-only" hardcoded labels in the static set. Without
    # "evolve" in this list, every `ai.evolve.evolve.<thing>` literal in
    # the install code would fail to match the static set.
    candidate_bots = ["team_bot_a", "personal_bot", "evolve"]
    network = {"members": candidate_bots, "bots": {}}
    expected = expected_plist_labels(network)

    not_covered: list[str] = []

    # Pass 1: _install_launchd callsites in every module that makes them.
    for source in _WALKED_SOURCES:
        for lineno, label, kind in _collect_install_launchd_labels(source):
            where = f"  {source}:{lineno}  →  {label}  "
            if kind == "unresolved":
                not_covered.append(
                    where + "(meta-test walker needs to learn this AST shape)"
                )
                continue
            # Substitute ONLY {bot_id} — never `str.format`, which raises
            # KeyError on any other placeholder and turns a coverage gap into
            # a crash (that is how this test sat quarantined against a stale
            # "label not in expected set" reason while never reaching Pass 2).
            expanded = [label.replace("{bot_id}", b) for b in candidate_bots]
            leftover = _unresolved_placeholders(expanded[0])
            if leftover:
                not_covered.append(
                    where + f"(unresolved placeholder(s) {sorted(leftover)!r}; "
                    f"the walker could not resolve them from a module constant "
                    f"or an enclosing literal for-loop)"
                )
                continue
            candidates = expanded if "{bot_id}" in label else [label]
            if not any(c in expected for c in candidates):
                not_covered.append(
                    where + f"(none of {candidates!r} in expected_plist_labels)"
                )

    # Pass 2: external installers (modules that own their own install_launchd()).
    for module_name, const_name in _EXTERNAL_INSTALLERS:
        try:
            mod = importlib.import_module(module_name)
        except ImportError as exc:  # pragma: no cover — failed import is a test setup bug
            pytest.fail(f"could not import {module_name}: {exc}")
        if not hasattr(mod, const_name):
            not_covered.append(
                f"  {module_name}.{const_name}  →  attribute missing  "
                f"(external-installer registration is stale; update _EXTERNAL_INSTALLERS)"
            )
            continue
        label_value = getattr(mod, const_name)
        if label_value not in expected:
            not_covered.append(
                f"  {module_name}.{const_name}={label_value!r}  "
                f"(label not in expected_plist_labels)"
            )

    if not_covered:
        msg = "\n".join([
            "Install-side labels missing from expected_plist_labels:",
            "",
            *not_covered,
            "",
            "Each entry above will be flagged by find_orphaned_plists() and",
            "deleted by remove_orphaned_plists() on the next `evolve-admin",
            "upgrade`. To fix, add the label to expected_plist_labels (or",
            "to per_bot_evolve_plist_labels for templated per-bot labels).",
            "",
            "See PR #1622 for the 2026-05-26 incident this meta-test guards",
            "against — 12 daemons had drifted before the orphan banner in",
            "the Admin UI surfaced the gap.",
        ])
        raise AssertionError(msg)


def test_meta_test_walker_finds_a_known_callsite():
    """Sanity check the AST walker itself: at least one well-known install
    callsite should turn up. If this fails, the meta-test above is silently
    passing because it found nothing to check."""
    labels = _collect_install_launchd_labels()
    label_strings = {label for _, label, _ in labels}
    # `ai.evolve.{bot_id}.backup` is the per-bot backup that motivated this
    # whole investigation — easy load-bearing canary.
    assert "ai.evolve.{bot_id}.backup" in label_strings, (
        "AST walker did not find _install_launchd_backup's label — "
        "the meta-test may be silently inspecting an empty list. "
        f"Found {len(labels)} labels: {sorted(label_strings)[:5]}…"
    )


def test_meta_test_walker_unrolls_literal_for_loops():
    """The walker must resolve a label f-string that interpolates a
    *loop variable*, not just ``bot_id``.

    ``_install_launchd_usage_jobs`` installs its two daemons from one
    callsite inside ``for label, script, minute in (("usage-logger", …),
    ("usage-by-app", …)):``. Before 2026-08-23 the walker turned that into
    the template ``ai.evolve.{bot_id}.{label}`` and Pass 1 then called
    ``.format(bot_id=…)`` on it, which raised ``KeyError: 'label'`` and
    killed the whole meta-test before it ever reached Pass 2 — that crash
    is why the test sat quarantined under the (wrong) reason "new
    LaunchDaemon label not in expected set".

    Pinning the unrolled result here is what stops a future repair from
    "fixing" the crash by skipping the offending callsite: skipping it
    would drop two real, installed daemons out of the drift guard's
    coverage."""
    collected = {label for _, label, _ in _collect_install_launchd_labels()}
    assert "ai.evolve.{bot_id}.usage-logger" in collected
    assert "ai.evolve.{bot_id}.usage-by-app" in collected
    assert "ai.evolve.{bot_id}.{label}" not in collected, (
        "walker regressed to emitting an unexpanded loop-variable template"
    )


def test_meta_test_walker_covers_analyzer_monitor_jobs():
    """Pass 1 must walk ``analyzer_monitor_jobs.py``, not just ``deploy.py``.

    That module imports ``deploy._install_launchd`` and installs 11 daemons
    from its own file. Walking only deploy.py left all 11 outside the drift
    guard: a 12th monitor added there without an
    ``ANALYZER_MONITOR_PLIST_LABELS`` entry matches the orphan-sweeper's
    ``ai.openclaw.evolve.*.plist`` glob and is deleted on the next upgrade,
    with nothing firing at PR time. Pinning a known label here keeps the
    source in ``_WALKED_SOURCES``; pinning the count keeps a silently-empty
    walk (a parse that finds nothing) from reading as coverage."""
    assert "evolve_admin/analyzer_monitor_jobs.py" in _WALKED_SOURCES
    rows = _collect_install_launchd_labels("evolve_admin/analyzer_monitor_jobs.py")
    labels = {label for _, label, _ in rows}
    assert "ai.openclaw.evolve.model_liveness_monitor" in labels, (
        f"walker found no known analyzer-monitor label; got {sorted(labels)[:5]}…"
    )
    # Deliberately not pinned to today's count of 11: retiring a monitor is
    # legitimate and should not red this. The job here is only to catch a walk
    # that silently finds NOTHING (a parse or path change), which the named
    # label above already does — this is the belt to that suspenders.
    assert rows, "walker returned no rows for analyzer_monitor_jobs.py"


def test_meta_test_walker_leaves_no_unresolved_placeholder():
    """No collected label may carry a placeholder other than ``{bot_id}``.

    ``{bot_id}`` is the one name Pass 1 expands (against the candidate-bot
    list). Anything else means the walker handed back a template it could
    not resolve, which Pass 1 reports as a coverage hole. Asserting it here
    too keeps the failure attributable to the walker rather than surfacing
    as a confusing "label not in expected set" entry."""
    for source in _WALKED_SOURCES:
        for lineno, label, kind in _collect_install_launchd_labels(source):
            assert kind != "unresolved", (
                f"{source}:{lineno} produced an unresolved label shape: {label}"
            )
            leftover = _unresolved_placeholders(label.replace("{bot_id}", "evolve"))
            assert not leftover, (
                f"{source}:{lineno}  →  {label}  carries unresolved "
                f"placeholder(s) {sorted(leftover)!r}"
            )



# ─── Walker contract: every hardening rule, pinned ──────────────────────────
# The real deploy.py uses almost none of the shapes the rules below exist to
# catch, so without this table each rule could be deleted with the suite still
# green — the "asserted but never executed" class. Each case names the rule it
# pins; `expect` is the set of (label, kind) pairs the walker must produce, and
# an "unresolved" label is matched on its stable prefix (the rest is an AST
# dump). Cases expecting "unresolved" pin a rule that keeps a WRONG label out
# of the gate; cases expecting concrete labels pin a rule that keeps a real
# daemon IN it. Both directions matter — an over-eager refusal reds the
# blocking gate on a legitimate installer.

_L = "_install_launchd"
_UNRES_F = "<UNRESOLVED-FSTRING"

_WALKER_CASES: tuple[tuple[str, str, set[tuple[str, str]]], ...] = (
    ("plain literal",
     f'{_L}(label="ai.evolve.evolve.plain")',
     {("ai.evolve.evolve.plain", "literal")}),
    ("bot_id f-string",
     f'{_L}(label=f"ai.evolve.{{bot_id}}.job")',
     {("ai.evolve.{bot_id}.job", "f-string")}),
    ("bare Name from a module constant (the fallback that IS kept)",
     f'K = "ai.evolve.evolve.konst"\n{_L}(label=K)',
     {("ai.evolve.evolve.konst", "constant")}),
    ("module constant inside an f-string — no fallback, no scope model",
     f'K = "konst"\n{_L}(label=f"ai.evolve.evolve.{{K}}")',
     {(_UNRES_F, "unresolved")}),
    ("literal-tuple loop unrolls to its concrete labels",
     f'for n in ("a", "b"):\n    {_L}(label=f"ai.evolve.evolve.{{n}}")',
     {("ai.evolve.evolve.a", "f-string"), ("ai.evolve.evolve.b", "f-string")}),
    ("nested literal loops take the cross product",
     f'for x in ("a",):\n    for y in ("p", "q"):\n        {_L}(label=f"ai.{{x}}.{{y}}")',
     {("ai.a.p", "f-string"), ("ai.a.q", "f-string")}),
    ("loop target shadows bot_id — bindings win over the placeholder",
     f'for bot_id in ("alpha",):\n    {_L}(label=f"ai.evolve.{{bot_id}}.job")',
     {("ai.evolve.alpha.job", "f-string")}),
    ("non-string loop element cannot be interpolated",
     f'for n in (1, 2):\n    {_L}(label=f"ai.evolve.evolve.{{n}}")',
     {(_UNRES_F, "unresolved")}),
    ("non-static iterable is not unrolled",
     f'for n in _specs():\n    {_L}(label=f"ai.evolve.evolve.{{n}}")',
     {(_UNRES_F, "unresolved")}),
    ("loop body rebinds the target (plain assign)",
     f'for n in ("outer",):\n    n = "real"\n    {_L}(label=f"ai.evolve.evolve.{{n}}")',
     {(_UNRES_F, "unresolved")}),
    ("loop body rebinds the target by tuple unpacking",
     f'for n in ("outer",):\n    n, _o = "real", 1\n    {_L}(label=f"ai.evolve.evolve.{{n}}")',
     {(_UNRES_F, "unresolved")}),
    ("loop body rebinds the target with a walrus",
     f'for n in ("outer",):\n    if (n := "real"):\n        pass\n    {_L}(label=f"ai.evolve.evolve.{{n}}")',
     {(_UNRES_F, "unresolved")}),
    ("loop body rebinds the target via except-as",
     f'for n in ("outer",):\n    try:\n        pass\n    except Exception as n:\n        pass\n    {_L}(label=f"ai.evolve.evolve.{{n}}")',
     {(_UNRES_F, "unresolved")}),
    ("continue in this loop's own body",
     f'for n in ("a", "b"):\n    continue\n    {_L}(label=f"ai.evolve.evolve.{{n}}")',
     {(_UNRES_F, "unresolved")}),
    ("break in a NESTED loop must NOT refuse the outer unroll",
     f'for n in ("a", "b"):\n    for _j in (1, 2):\n        break\n    {_L}(label=f"ai.evolve.evolve.{{n}}")',
     {("ai.evolve.evolve.a", "f-string"), ("ai.evolve.evolve.b", "f-string")}),
    ("guard `if` between loop body and callsite yields no phantom",
     f'for n in ("real", "phantom"):\n    if n == "real":\n        {_L}(label=f"ai.evolve.evolve.{{n}}")',
     {(_UNRES_F, "unresolved")}),
    ("for-else runs once at the last value — not unrolled",
     f'for n in ("a", "b"):\n    pass\nelse:\n    {_L}(label=f"ai.evolve.evolve.{{n}}")',
     {(_UNRES_F, "unresolved")}),
    ("empty literal iterable installs nothing, so emits nothing",
     f'for n in ():\n    {_L}(label=f"ai.evolve.evolve.{{n}}")',
     set()),
    ("positional label argument",
     f'{_L}("ai.evolve.evolve.positional")',
     {("ai.evolve.evolve.positional", "literal")}),
    ("attribute-form call",
     f'deploy.{_L}(label="ai.evolve.evolve.attr")',
     {("ai.evolve.evolve.attr", "literal")}),
    ("**kwargs splat hides the label",
     f'{_L}(**kw)',
     {("<UNRESOLVED:**kwargs splat>", "unresolved")}),
    ("f-string conversion changes the rendered text",
     f'{_L}(label=f"ai.evolve.{{bot_id!r}}.job")',
     {(_UNRES_F, "unresolved")}),
    ("f-string format spec changes the rendered text",
     f'{_L}(label=f"ai.evolve.{{bot_id:>8}}.job")',
     {(_UNRES_F, "unresolved")}),
    ("non-Name interpolation",
     f'{_L}(label=f"ai.evolve.{{cfg.name}}.job")',
     {(_UNRES_F, "unresolved")}),
    ("a non-f-string, non-Name label expression is reported, not dropped",
     f'{_L}(label="ai.evolve." + suffix)',
     {("<UNRESOLVED:", "unresolved")}),

    # ── aliases of the installer ────────────────────────────────────────────
    # Each of these produced ZERO rows and NO `unresolved` marker before the
    # alias rules landed — a real daemon installed through any of them was
    # invisible to this gate and swept on the next `evolve-admin upgrade`.
    ("local alias of the installer",
     f'_il = {_L}\n_il(label="ai.evolve.evolve.aliased")',
     {("ai.evolve.evolve.aliased", "literal")}),
    ("alias chain resolves transitively",
     f'_a = {_L}\n_b = _a\n_b(label="ai.evolve.evolve.chained")',
     {("ai.evolve.evolve.chained", "literal")}),
    ("alias of the attribute form",
     f'_il = deploy.{_L}\n_il(label="ai.evolve.evolve.attralias")',
     {("ai.evolve.evolve.attralias", "literal")}),
    ("import-as alias",
     f'from .deploy import {_L} as _il\n_il(label="ai.evolve.evolve.imported")',
     {("ai.evolve.evolve.imported", "literal")}),
    ("a plain import is not itself an alias, and is not an escape",
     f'from .deploy import {_L}\n{_L}(label="ai.evolve.evolve.ok")',
     {("ai.evolve.evolve.ok", "literal")}),
    ("an alias callsite gets the full f-string/loop treatment",
     f'_il = {_L}\nfor n in ("a", "b"):\n    _il(label=f"ai.evolve.evolve.{{n}}")',
     {("ai.evolve.evolve.a", "f-string"), ("ai.evolve.evolve.b", "f-string")}),
    ("an alias callsite resolves a positional label",
     f'_il = {_L}\n_il("ai.evolve.evolve.pos")',
     {("ai.evolve.evolve.pos", "literal")}),
    ("an alias bound to something else elsewhere cannot be pinned",
     f'_il = {_L}\ndef f():\n    _il = other\n_il(label="ai.evolve.evolve.amb")',
     {("<UNRESOLVED:", "unresolved")}),

    # ── functools.partial over the installer ────────────────────────────────
    ("partial binding the label resolves at the partial",
     f'_p = partial({_L}, label="ai.evolve.evolve.plabeled")\n_p(user="evolve")',
     {("ai.evolve.evolve.plabeled", "literal")}),
    ("functools.partial attribute spelling",
     f'_p = functools.partial({_L}, label="ai.evolve.evolve.pattr")',
     {("ai.evolve.evolve.pattr", "literal")}),
    ("partial binding the label positionally",
     f'_p = partial({_L}, "ai.evolve.evolve.ppos")',
     {("ai.evolve.evolve.ppos", "literal")}),
    ("a partial binding no label is just another alias",
     f'_p = partial({_L}, user="evolve")\n_p(label="ai.evolve.evolve.plater")',
     {("ai.evolve.evolve.plater", "literal")}),
    # The branch that makes a labelless partial an ALIAS rather than a
    # label-bound one. Only a positional exposes it: such a partial shifts no
    # positional argument, so the call's first one is still `label` — routing
    # it to the label-bound branch instead would emit nothing at all here.
    ("a labelless partial passes positionals through to `label`",
     f'_p = partial({_L}, user="evolve")\n_p("ai.evolve.evolve.ppassthrough")',
     {("ai.evolve.evolve.ppassthrough", "literal")}),
    ("a call may override the partial's baked label — both are asserted",
     f'_p = partial({_L}, label="ai.evolve.evolve.base")\n_p(label="ai.evolve.evolve.override")',
     {("ai.evolve.evolve.base", "literal"),
      ("ai.evolve.evolve.override", "literal")}),
    ("a labelless partial that lands nowhere followable",
     f'reg(partial({_L}, user="evolve"))',
     {("<UNRESOLVED:", "unresolved")}),
    ("a **kwargs splat into the partial could be carrying the label",
     f'_p = partial({_L}, **kw)',
     {("<UNRESOLVED:", "unresolved")}),

    # ── references that escape the shapes above ─────────────────────────────
    ("installer handed to another function",
     f'reg({_L})',
     {("<UNRESOLVED:", "unresolved")}),
    ("installer stashed in a container",
     f'H = {{}}\nH["x"] = {_L}',
     {("<UNRESOLVED:", "unresolved")}),
    ("installer returned from a factory",
     f'def f():\n    return {_L}',
     {("<UNRESOLVED:", "unresolved")}),
    ("attribute-form reference that is not a call",
     f'reg(deploy.{_L})',
     {("<UNRESOLVED:", "unresolved")}),
    ("partial reached under a spelling the walker does not match",
     f'p = functools.partial\np({_L}, label="ai.evolve.evolve.x")',
     {("<UNRESOLVED:", "unresolved")}),

    # ── an alias nothing in this module consumes ────────────────────────────
    # Exempting an alias BINDING from the escape rule is only sound because the
    # alias is checked somewhere else. These are the shapes where it is not —
    # each was silent (zero rows, no marker) until the consumed-alias rule.
    ("an alias bound but never called in this module",
     f'_il = {_L}',
     {("<UNRESOLVED:", "unresolved")}),
    ("a class-scope alias reached through an attribute",
     f'class K:\n    _il = {_L}\n\n    def m(self):\n        self._il(label="ai.evolve.evolve.cls")',
     {("<UNRESOLVED:", "unresolved")}),
    ("an alias exported for another module to call",
     f'_install_analyzer_job = {_L}',
     {("<UNRESOLVED:", "unresolved")}),
    ("an import-as alias nothing here calls",
     f'from .deploy import {_L} as _il',
     {("<UNRESOLVED:", "unresolved")}),
    ("a labelless partial assigned but never called",
     f'_p = partial({_L}, user="evolve")',
     {("<UNRESOLVED:", "unresolved")}),
    # The counter-direction: a link in a resolved chain is consumed by the next
    # binding, so it must NOT be reported. An over-eager rule here reds the gate
    # on every legitimate alias chain.
    ("a mid-chain alias is consumed by the next binding, not reported",
     f'_a = {_L}\n_b = _a\n_b(label="ai.evolve.evolve.midchain")',
     {("ai.evolve.evolve.midchain", "literal")}),
    # `ast.walk` is breadth-first, so a binding nested DEEPER than its consumer
    # is only picked up on a second iteration. Every other chain case sits at
    # one depth in source order and resolves in a single pass, which left the
    # fixpoint free to delete.
    ("an alias chain whose first link is nested deeper than its consumer",
     f'def _setup():\n    _a = {_L}\n_b = _a\n_b(label="ai.evolve.evolve.fixpoint")',
     {("ai.evolve.evolve.fixpoint", "literal")}),
    ("an alias consumed by a partial is not an unconsumed binding",
     f'_il = {_L}\n_p = partial(_il, label="ai.evolve.evolve.viaalias")',
     {("ai.evolve.evolve.viaalias", "literal")}),
    # An alias that is ambiguous AND never called locally falls between the two
    # rules unless the unconsumed-report gate reads `aliases` rather than
    # `escape_tracked`: the ambiguity refusal only fires at a callsite, and
    # there is none. Reached the original silent end state through that door.
    ("an ambiguous alias that is never called locally is still reported",
     f'try:\n    from .deploy import {_L} as _il\nexcept ImportError:\n    _il = None',
     {("<UNRESOLVED:", "unresolved")}),
    ("an ambiguous alias rebound and never called",
     f'_il = {_L}\n_il = other',
     {("<UNRESOLVED:", "unresolved")}),
    # The counter-direction for that same gate: a LABELLED partial legitimately
    # needs no consumer, because its label is already emitted at the partial.
    # Widening the gate to `aliases | bound_partials` reds this.
    ("a labelled partial needs no consumer to be resolved",
     f'_p = partial({_L}, label="ai.evolve.evolve.noconsumer")',
     {("ai.evolve.evolve.noconsumer", "literal")}),
    ("an unpinnable alias handed to a partial",
     f'_il = {_L}\nif x:\n    _il = other\n_p = partial(_il, label="ai.evolve.evolve.phantom")',
     {("<UNRESOLVED:", "unresolved")}),
)


@pytest.mark.parametrize(
    "rule,src,expect", _WALKER_CASES, ids=[c[0] for c in _WALKER_CASES]
)
def test_walker_shape_table(rule, src, expect):
    """One case per walker rule, over synthetic source."""
    got = {
        (label, kind) for _, label, kind in _collect_labels_from_source(src)
    }
    prefixes = {e[0] for e in expect if e[1] == "unresolved"}
    normalized = {
        (next((pre for pre in prefixes if label.startswith(pre)), label), kind)
        if kind == "unresolved" else (label, kind)
        for label, kind in got
    }
    assert normalized == expect, f"{rule}: walker produced {sorted(got)}"


# ─── 2026-06-01 root-by-omission guard ──────────────────────────────────────
# Three pod-wide installers (defer-runner, manifest-reflex-runner,
# app-posture-review) shipped via raw-template plists that omitted the
# UserName key, so launchd booted them as root. The fix routes them
# through `_install_launchd(user="evolve", ...)`, which guarantees a
# UserName key in the rendered plist. This test pins that contract so a
# future refactor can't silently regress the three back to root by
# reverting them to ad-hoc templates without a UserName.


@pytest.mark.parametrize(
    "installer_name,expected_label",
    [
        ("_install_launchd_defer_runner", "ai.openclaw.evolve.defer-runner"),
        ("_install_launchd_manifest_reflex_runner", "ai.openclaw.evolve.manifest-reflex-runner"),
        ("_install_launchd_app_posture_review", "ai.openclaw.evolve.app-posture-review"),
    ],
)
def test_pod_wide_runner_installer_passes_user_evolve(installer_name, expected_label):
    """Each of the three pod-wide runners must route through
    ``_install_launchd(label=<label>, user="evolve", ...)``.

    Why this matters: until 2026-06-01 these three installers wrote
    their plists from raw f-string templates and omitted the
    ``UserName`` key — so launchd defaulted them to ``root`` on every
    pod. Symptoms were silent: root-owned log files in
    ``/Users/evolve/.openclaw/logs/`` and pod-wide infra reading per-bot
    state with root privileges instead of the intended evolve-via-ACL
    path. The reads "worked" only because root bypasses every ACL.

    Routing through ``_install_launchd(user="evolve", ...)`` is the
    structural fix: ``_plist_content`` unconditionally emits
    ``<key>UserName</key><string>{user}</string>``, so as long as the
    callsite passes ``user="evolve"``, the rendered plist cannot omit
    the key. This test enforces both halves of the contract.
    """
    import ast

    src = (_ADMIN_DIR / "evolve_admin" / "deploy.py").read_text()
    tree = ast.parse(src)

    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == installer_name:
            target = node
            break
    assert target is not None, f"{installer_name} not found in deploy.py"

    install_calls: list[ast.Call] = []
    for sub in ast.walk(target):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "_install_launchd"
        ):
            install_calls.append(sub)

    assert install_calls, (
        f"{installer_name} does not call _install_launchd — it would "
        "otherwise have to emit a raw plist template, which historically "
        "omitted UserName and ran the daemon as root. See "
        "test_pod_wide_runner_installer_passes_user_evolve docstring."
    )

    for call in install_calls:
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        label_node = kwargs.get("label")
        assert isinstance(label_node, ast.Constant) and label_node.value == expected_label, (
            f"{installer_name}: expected _install_launchd(label={expected_label!r}, ...) "
            f"but found label={ast.dump(label_node) if label_node else 'MISSING'}"
        )
        user_node = kwargs.get("user")
        assert isinstance(user_node, ast.Constant) and user_node.value == "evolve", (
            f"{installer_name}: _install_launchd must be called with "
            f'user="evolve" (got {ast.dump(user_node) if user_node else "MISSING"}). '
            "A missing or non-evolve user re-introduces the 2026-06-01 root-by-omission bug."
        )


def test_plist_content_emits_username_key():
    """``_plist_content`` must always render a ``UserName`` key — every
    callsite passes ``user=…`` and the helper threads it into the XML.

    This is the structural invariant the test above relies on: as long
    as a daemon is installed through ``_install_launchd`` (which calls
    ``_plist_content``), the plist cannot accidentally run as root.

    Smoke-checking the rendered XML directly is the cheapest way to
    catch a future refactor that "simplifies" ``_plist_content`` and
    drops the key.
    """
    rendered = _deploy_mod._plist_content(
        label="ai.openclaw.evolve.defer-runner",
        user="evolve",
        script_path=Path("/Users/Shared/evolve-repo/packages/analyzer/defer_runner.py"),
        schedule={"interval": 120},
    )
    assert "<key>UserName</key>" in rendered
    assert "<string>evolve</string>" in rendered


# ─── 2026-05-26 → 2026-06-01 better-engine outage guard ────────────────────
# Sibling failure mode to the root-by-omission case above. The
# `ai.openclaw.evolve.better` daemon DID emit a UserName key, but the
# value was wrong: deploy.py resolved it via `_bot_user_for(bot_id)`,
# which pre-evo-account-separation accidentally returned "evolve" for
# the primary bot and post-separation returns "evo" — the bot account
# that can't write to evolve-owned metrics/ paths. Six days of silent
# outage (cost rollups, generator cadence, compliance scan all dead)
# before anyone noticed. This test pins the fix.


def test_better_engine_plist_pinned_to_evolve_user(monkeypatch):
    """``_install_launchd_better_engine`` must hardcode ``UserName=evolve``,
    regardless of which bot triggered the deploy or what macOS account
    that bot resolves to.

    Regression guard for the 2026-05-26 → 2026-06-01 outage: the function
    used to resolve UserName via ``_bot_user_for(bot_id)``. Pre-evo-account-
    separation that accidentally returned "evolve" because every bot ran
    as the evolve macOS user. Once the primary bot got its own "evo"
    account, deploying it rewrote the installed plist with UserName=evo,
    and the daemon exited 78 (EX_CONFIG) on every 15-min trigger —
    silently knocking out cost rollups, the generator-runner cadence,
    AND the compliance scan for six days.
    """
    from evolve_admin.deploy import DeployResult, _install_launchd_better_engine
    from evolve_admin.runtime import render_launchd_plist

    captured: dict[str, object] = {}

    def _fake_install_via_seam(spec, result):
        # better-engine now installs through the Scheduler seam
        # (_install_spec_via_seam → get_scheduler().install) instead of the
        # retired _write_plist. Capture the JobSpec it would install.
        captured["spec"] = spec

    monkeypatch.setattr(_deploy_mod, "_install_spec_via_seam", _fake_install_via_seam)
    monkeypatch.setattr(_deploy_mod, "_run_sudo", lambda *a, **kw: None)
    # The function bails early if VENV_PYTHON doesn't exist on disk; point
    # it at any extant file (the test box won't have evolve-venv installed).
    monkeypatch.setattr(_deploy_mod, "VENV_PYTHON", "/bin/bash")
    # Force the bot-user resolver to return the post-separation "evo"
    # account — the very condition that broke production. If the function
    # ever reaches for the bot user again, the assertion below will catch.
    monkeypatch.setattr(_deploy_mod, "_bot_user_for", lambda bot_id, *a, **kw: "evo")

    _install_launchd_better_engine(
        "evolve", DeployResult(bot_id="evolve", success=True)
    )

    import plistlib

    spec = captured.get("spec")
    assert spec is not None, "better-engine never reached the install seam"
    assert spec.user == "evolve", (
        "better-engine JobSpec is not pinned to UserName=evolve — see the "
        f"2026-05-26 outage note above. Got: {spec.user!r}"
    )
    # HOME must also be evolve's home directory; if it points at /Users/evo
    # (or any bot's home) the daemon trips on PYTHONUSERBASE / cache writes.
    assert spec.env.get("HOME") == "/Users/evolve", (
        "better-engine JobSpec HOME is not /Users/evolve — same root cause "
        f"as the UserName drift. Got: {spec.env.get('HOME')!r}"
    )
    # The rendered plist carries the same pinning end-to-end.
    parsed = plistlib.loads(render_launchd_plist(spec).encode())
    assert parsed.get("UserName") == "evolve"


# ─── Pass 3: the install-surface census ─────────────────────────────────────
#
# Pass 1 walks `_install_launchd(label=…)` callsites in `_WALKED_SOURCES`;
# Pass 2 checks a hand-kept `_EXTERNAL_INSTALLERS` allowlist. An installer
# that reaches the Scheduler seam DIRECTLY — `get_scheduler().install(spec)`
# or `deploy._install_spec_via_seam(spec, result)` — is invisible to both.
#
# That is not hypothetical. `ai.evolve.opik` (`install_opik_companion`,
# opt-in via `evolve-admin install-infra-jobs --with-opik`) installed through
# the seam, matched the macOS `ai.evolve.*.plist` glob AND Linux's
# `_is_evolve_owned_label`, was in no gate, and was therefore deleted on the
# next `evolve-admin upgrade` for every pod that opted in. #3772 pinned that
# one label; it did not close the class.
#
# WHY A CENSUS + DECLARATION RATHER THAN A DEEPER WALKER.  Extending the AST
# walker to resolve the label out of a seam callsite was the other candidate.
# It does not survive contact with the real callsites: not one of them passes
# a label expression AT the call. They pass a local built earlier in the
# function (`plist_label = OPIK_LAUNCHD_LABEL`), a helper's return value
# (`per_bot_gateway_plist_label(bot_id)`), or a JobSpec built by a factory
# whose `label` parameter is bound from the ENCLOSING function's parameter
# (`_install_spec_via_seam(_admin_ui_jobspec(label), result)` where
# `label = f"ai.evolve.{user}.admin-ui"` and `user` is an argument). Resolving
# those needs local dataflow plus interprocedural parameter binding — a scope
# model the walker deliberately does not have (#3772 REMOVED a module-constant
# fallback inside f-strings for exactly this reason: no scope model means a
# function-local shadow resolves to the wrong value and greens the gate on a
# daemon that then gets swept). A confidently-wrong label is worse here than
# no label at all. And `install_scheduled_jobspec` takes its label from a
# caller-supplied JobSpec by design, so no static analysis can ever resolve it.
#
# So: the STRUCTURE is discovered automatically (a new seam callsite anywhere
# under the two packages fails at PR time with nothing for a human to have
# foreseen), and the LABEL is declared — but a declaration alone would just be
# paperwork, so each one is made to do work three ways:
#
#   1. `("const", …)` / `("fn", …)` rows are resolved LIVE from the module, so
#      they cannot rot: rename the constant and Pass 3 reds.
#   2. `("template", …)` rows are cross-checked against the enclosing
#      function's own source, so a declared template that drifts from the code
#      reds (`test_seam_installer_templates_match_their_source`).
#   3. Every resolved label is run through the ACTUAL sweeper predicate with
#      its artifact on disk (`test_registered_seam_labels_survive_the_orphan_sweeper`).

import ast as _ast  # noqa: E402
import re as _re  # noqa: E402

# (display prefix, package root). Both are censused: `_install_launchd` and
# the Scheduler seam are both importable from either package, and a daemon
# installed from `packages/analyzer` would be swept exactly the same way.
_CENSUS_ROOTS: tuple[tuple[str, Path], ...] = tuple(_PACKAGE_ROOTS.items())

# deploy.py's two seam wrappers. A call to either is a daemon install.
_SEAM_WRAPPERS = frozenset({"_install_spec_via_seam", "_install_job_ensuring_restart"})
# The seam accessors. `<x> = get_scheduler()` makes `<x>.install(...)` a
# daemon install too — that is how digest_dispatcher / audit_scheduler /
# auto_approver / install_helpers all reach it.
_SCHEDULER_FACTORIES = frozenset({"get_scheduler", "get_launchd_scheduler"})
# launchctl verbs that REGISTER a service definition. A daemon can also be
# materialised without `Scheduler.install()` at all — write the plist with
# `sudo /bin/cp`, then `sched.raw("bootstrap", "system", <path>)` — and that
# style is live here (`install_launchd_system_daemon`, `cli.migrate_jobs`, and
# the macOS branches of `mcp_service.install` and `_provision_evo_oc`, whose
# rows existed only because their LINUX branches happen to call the seam). It
# is the same class: the label lands in /Library/LaunchDaemons and the sweeper
# deletes it if nothing expects it. `bootout`, `print`, `kickstart`, `list` and
# `unload` register nothing and are deliberately absent — flagging them would
# bury the gate in heal-path noise.
_REGISTER_VERBS = frozenset({"bootstrap", "load"})
# In-tree `*args` forwarders onto `Scheduler.raw`. Matching only `<x>.raw(...)`
# left the class one indirection deep: `_scheduler_launchctl("bootstrap",
# "system", path)` registers a daemon exactly as surely, and three such
# forwarders already carry live register-verb calls. Named rather than inferred
# — a forwarder is a `def f(*args): return sched.raw(*args)` whose literal verb
# lives at the CALL site, so there is nothing to resolve, only a name to know.
_LAUNCHCTL_FORWARDERS = frozenset({
    "_scheduler_launchctl",   # deploy.py
    "_launchctl",             # service.py, runtime/scheduler.py
    "_launchctl_n",           # recovery.py
})


def _census_install_sites(src_text: str) -> list[tuple[int, str, str, str]]:
    """Every daemon-install callsite in ``src_text``.

    Returns ``(lineno, enclosing_function, kind, detail)`` rows, where ``kind``
    is one of:

    ``"launchd"``
        A ``_install_launchd(...)`` call (bare or attribute form). Pass 1
        resolves its label — but only for modules in ``_WALKED_SOURCES``, so
        this row exists to catch a THIRD module starting to call it.
    ``"seam"``
        A call that reaches ``Scheduler.install()``: one of
        ``_SEAM_WRAPPERS``, ``get_scheduler().install(...)``, or
        ``<name>.install(...)`` where ``<name>`` is assigned from a scheduler
        factory anywhere in the module.
    Also emitted for ``Scheduler.install`` reached as a VALUE rather than a
        direct call — an alias, a ``functools.partial``, or
        ``getattr(sched, "install")`` — since each of those installs a daemon
        just as surely.
    ``"raw-register"``
        A ``<sched>.raw("bootstrap"|"load", …)`` call — the closing step of a
        daemon materialised by writing its plist directly rather than through
        ``Scheduler.install()``. Registered from the same table.
    ``"unclassified"``
        Any other ``<recv>.install(...)``. Deliberately reported rather than
        skipped: the whole failure mode this file guards is an install path no
        gate can see, and a receiver we cannot prove is not a scheduler is
        exactly that. ``detail`` carries the rendered receiver so a human can
        classify it in one line.

    Split from file IO so the census's own rules can be driven from synthetic
    sources — see ``test_census_shape_table``. Without that seam every rule
    below is free to delete with the suite still green, which is the finding
    that dominated #3772's review.
    """
    tree = _ast.parse(src_text)

    parents: dict[_ast.AST, _ast.AST] = {}
    for node in _ast.walk(tree):
        for child in _ast.iter_child_nodes(node):
            parents[child] = node

    def _enclosing_function(node: _ast.AST) -> str:
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                return cur.name
            cur = parents.get(cur)
        return "<module>"

    # Names bound to a scheduler ANYWHERE in the module, at any scope. Scope-
    # blind on purpose: over-collecting here can only widen what counts as a
    # seam install (more rows to register), never hide one.
    scheduler_names: set[str] = set()
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Assign):
            continue
        val = node.value
        if not (
            isinstance(val, _ast.Call)
            and isinstance(val.func, _ast.Name)
            and val.func.id in _SCHEDULER_FACTORIES
        ):
            continue
        for tgt in node.targets:
            if isinstance(tgt, _ast.Name):
                scheduler_names.add(tgt.id)

    def _render(expr: _ast.expr) -> str:
        if isinstance(expr, _ast.Name):
            return expr.id
        if isinstance(expr, _ast.Attribute):
            return f"{_render(expr.value)}.{expr.attr}"
        if isinstance(expr, _ast.Call):
            return f"{_render(expr.func)}()"
        return type(expr).__name__

    def _is_scheduler_expr(expr: _ast.expr) -> bool:
        """True when ``expr`` evaluates to the injected Scheduler."""
        return (
            isinstance(expr, _ast.Call)
            and isinstance(expr.func, _ast.Name)
            and expr.func.id in _SCHEDULER_FACTORIES
        ) or (isinstance(expr, _ast.Name) and expr.id in scheduler_names)

    rows: list[tuple[int, str, str, str]] = []

    # `Scheduler.install` reached WITHOUT calling the attribute directly:
    # `inst = get_scheduler().install; inst(spec)`, `functools.partial(
    # sched.install, spec)()`, `getattr(get_scheduler(), "install")(spec)`. All
    # three install a daemon, and all three were invisible while the only rule
    # was "a Call whose func is an `install` Attribute" — a fail-OPEN hole in a
    # gate whose whole posture is loud-on-unknown. The `getattr` idiom is
    # already in-tree on this very object (`deploy.py:5331` reaches for
    # `getattr(get_scheduler(), "_launchctl", None)`), so this is a live style,
    # not an exotic one.
    #
    # Scoped to a receiver that classifies as a scheduler. A bare `.install`
    # reference on anything else is a data field — `channel.install`,
    # `pkg.install`, eleven of them in this tree — and demanding an allowlist
    # row for each would be noise that teaches people to pattern-match the
    # table instead of reading it.
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute) and node.attr == "install":
            parent = parents.get(node)
            called_directly = isinstance(parent, _ast.Call) and parent.func is node
            if not called_directly and _is_scheduler_expr(node.value):
                rows.append((
                    node.lineno, _enclosing_function(node), "seam",
                    f"{_render(node.value)}.install (alias)",
                ))
        elif (
            isinstance(node, _ast.Call)
            and isinstance(node.func, _ast.Name)
            and node.func.id == "getattr"
            and len(node.args) > 1
            and isinstance(node.args[1], _ast.Constant)
            and node.args[1].value == "install"
            and _is_scheduler_expr(node.args[0])
        ):
            rows.append((
                node.lineno, _enclosing_function(node), "seam",
                f'getattr({_render(node.args[0])}, "install")',
            ))

    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        func = node.func
        # Exact name match — `_install_launchd_backup(...)` is a DIFFERENT
        # function and must not be censused as the seam primitive.
        name = (
            func.id if isinstance(func, _ast.Name)
            else func.attr if isinstance(func, _ast.Attribute)
            else None
        )
        if name == "_install_launchd":
            rows.append((node.lineno, _enclosing_function(node), "launchd", name))
            continue
        if name in _SEAM_WRAPPERS:
            rows.append((node.lineno, _enclosing_function(node), "seam", name))
            continue
        if (
            (
                (isinstance(func, _ast.Attribute) and func.attr == "raw")
                or (isinstance(func, _ast.Name) and func.id in _LAUNCHCTL_FORWARDERS)
                or (
                    isinstance(func, _ast.Attribute)
                    and func.attr in _LAUNCHCTL_FORWARDERS
                )
            )
            and node.args
            and isinstance(node.args[0], _ast.Constant)
            and node.args[0].value in _REGISTER_VERBS
        ):
            callee = func.attr if isinstance(func, _ast.Attribute) else func.id
            rows.append((
                node.lineno, _enclosing_function(node), "raw-register",
                f"{callee}({node.args[0].value!r})",
            ))
            continue
        # Only `.install(...)` — `.restart()`, `.remove()`, a non-registering
        # `.raw()` verb, and `.artifact_path()` install nothing, and flagging
        # them would red the gate on legitimate code.
        if not (isinstance(func, _ast.Attribute) and func.attr == "install"):
            continue
        recv = func.value
        is_seam = _is_scheduler_expr(recv)
        rows.append((
            node.lineno,
            _enclosing_function(node),
            "seam" if is_seam else "unclassified",
            _render(recv),
        ))
    return rows


def _census_scanned_modules(roots=_CENSUS_ROOTS) -> list[str]:
    """Every module key the census reads. Exposed so a root that silently
    stops being walked is a FINDING: with no install sites in
    ``packages/analyzer`` today, dropping it from ``_CENSUS_ROOTS`` changed no
    census row and no assertion — half the advertised coverage was free to
    delete."""
    keys: list[str] = []
    for prefix, root in roots:
        if not root.is_dir():
            pytest.fail(
                f"census root missing: {root}. The package moved or was "
                "renamed; a census over a directory that is not there walks "
                "nothing and passes everything."
            )
        for path in sorted(root.rglob("*.py")):
            # `__pycache__` holds no source, and `tests/` is where the
            # synthetic installers in THIS file's tables would live — censusing
            # either would report fixtures as unregistered production daemons.
            if "__pycache__" in path.parts or "tests" in path.parts:
                continue
            keys.append(f"{prefix}/{path.relative_to(root)}")
    return keys


def _census_repo(roots=_CENSUS_ROOTS) -> list[tuple[str, int, str, str, str]]:
    """``_census_install_sites`` over every production module in both
    packages. Rows are ``(module_key, lineno, function, kind, detail)`` with
    ``module_key`` like ``"evolve_admin/deploy.py"``."""
    out: list[tuple[str, int, str, str, str]] = []
    for key in _census_scanned_modules(roots):
        for lineno, fn, kind, detail in _census_install_sites(
            _module_path(key).read_text()
        ):
            out.append((key, lineno, fn, kind, detail))
    return out


# The seam plumbing itself. These functions ARE the mechanism — their callers
# are the installers, and those callers are censused on their own rows.
_SEAM_PLUMBING: frozenset[tuple[str, str]] = frozenset({
    ("evolve_admin/deploy.py", "_install_job_ensuring_restart"),
    ("evolve_admin/deploy.py", "_install_spec_via_seam"),
    # `_install_launchd` reaches the seam via `_install_job_ensuring_restart`.
    # Its own label argument is Pass 1's job.
    ("evolve_admin/deploy.py", "_install_launchd"),
    # The seam's own launchd adapter: `LaunchdScheduler.install` / `.enable`
    # ARE `Scheduler.install()`, reached from the inside.
    ("analyzer/runtime/scheduler.py", "install"),
    ("analyzer/runtime/scheduler.py", "enable"),
})

# Every seam-install callsite, keyed by (module, enclosing function), with the
# label it installs. Three resolvable forms plus one exemption:
#
#   ("const", "<module>:<NAME>")  read live via getattr — cannot rot.
#   ("fn",    "<module>:<name>")  called live with each candidate bot id.
#   ("template", ("<label>", "<binding>"))
#                                 declared here; `{bot_id}` is expanded, and the
#                                 declaration is cross-checked against the RHS
#                                 of `<binding> = ...` in the installer, so it
#                                 cannot drift. Naming the BINDING is what makes
#                                 the check mean anything: matching against any
#                                 label-shaped string in the function let a
#                                 second label in the body launder a wrong
#                                 declaration to green -- and an installer that
#                                 sweeps a legacy label alongside its own is a
#                                 shape already in the tree
#                                 (`_install_launchd_backup`). Use this ONLY
#                                 when the label is a literal or f-string bound
#                                 to a name in the installer itself; anything
#                                 built elsewhere wants ("const"|"fn") instead.
#   ("plist-dir", "<pkg-relative dir>")
#                                 the site bootstraps every static plist shipped
#                                 in that directory; the labels are the file
#                                 stems, read LIVE off disk, so a plist added
#                                 there is checked without touching this table.
#   ("caller-supplied", "<why>")  the label arrives on a JobSpec (or a
#                                 label + plist_xml pair) the CALLER built. No
#                                 static label exists; exempt, reason recorded.
#   ("re-register", "<why>")      the site re-bootstraps an artifact already on
#                                 disk (a heal path, a cutover, a restart). It
#                                 introduces no label of its own; exempt,
#                                 reason recorded.
#
# The last two are EXEMPTIONS, and the gate does NOT verify their reasons — it
# verifies only that a live callsite still exists for the row. That residual is
# recorded rather than papered over: a future author could register a genuine
# installer as ("caller-supplied", …) and get a green gate. What the row buys
# is that the choice is visible in the diff and has to be argued for.
#
# A new seam callsite with no row here fails
# `test_every_daemon_install_site_is_registered` at PR time.
_SEAM_INSTALLERS: tuple[tuple[str, str, tuple[str, str]], ...] = (
    # ── deploy.py ───────────────────────────────────────────────────────────
    # Per-bot OpenClaw gateway. A ("fn", per_bot_gateway_plist_label) row
    # would misdescribe this one: the installer builds the label from its OWN
    # inline f-string (deploy.py:5603) and never calls the helper, so a live
    # resolution through the helper would keep passing after the two diverged.
    # The template row pins the string the installer actually uses.
    ("evolve_admin/deploy.py", "install_bot_gateway_plist",
     ("template", ("ai.openclaw.{bot_id}-gateway", "label"))),
    # Per-bot iMessage poller. Presence-gated in `expected_plist_labels`.
    ("evolve_admin/deploy.py", "install_imessage_poller",
     ("fn", "evolve_admin.deploy:_imessage_poller_plist_label")),
    # The #3772 live bug: opt-in Opik companion, presence-gated since.
    ("evolve_admin/deploy.py", "install_opik_companion",
     ("const", "evolve_admin.deploy:OPIK_LAUNCHD_LABEL")),
    ("evolve_admin/deploy.py", "_install_launchd_better_engine",
     ("template", ("ai.openclaw.evolve.better", "label"))),
    ("evolve_admin/deploy.py", "_install_launchd_signal_subscriber",
     ("template", ("ai.evolve.evolve.signal-subscriber", "label"))),
    # Pod-wide singletons. Their `user` parameter reads like a per-bot knob
    # but every production caller passes "evolve" (deploy.py:8254, 8263, 8268),
    # so the concrete label is declared rather than a `{bot_id}` template —
    # `ai.evolve.team_bot_a.admin-ui` is not a daemon that exists, and
    # demanding it in the expected set would make health._check_launchd hunt
    # for a plist that can never be there.
    ("evolve_admin/deploy.py", "_install_launchd_admin_ui",
     ("template", ("ai.evolve.evolve.admin-ui", "label"))),
    ("evolve_admin/deploy.py", "_install_launchd_mcp_bridge",
     ("template", ("ai.evolve.evolve.mcp-bridge", "label"))),
    # ── the modules that own their own install entry point ──────────────────
    # These six are also in `_EXTERNAL_INSTALLERS` (Pass 2). The duplication is
    # deliberate and cheap: Pass 2 asserts the module's advertised constant is
    # expected; Pass 3 asserts the module's actual CALLSITES are all accounted
    # for. A module that grows a SECOND seam installer with a different label
    # passes Pass 2 unchanged and fails here.
    ("evolve_admin/setup_wizard.py", "_provision_evo_oc",
     ("fn", "evolve_admin.deploy:per_bot_gateway_plist_label")),
    ("evolve_admin/app_discovery.py", "install_app_discovery_sweep",
     ("const", "evolve_admin.app_discovery:APP_DISCOVERY_SWEEP_LABEL")),
    ("evolve_admin/mcp_service.py", "install",
     ("const", "evolve_admin.mcp_service:LABEL")),
    ("evolve_admin/repo_puller.py", "install_launchd",
     ("const", "evolve_admin.repo_puller:REPO_PULLER_LABEL")),
    ("evolve_admin/alerts/digest_dispatcher.py", "install_launchd",
     ("const", "evolve_admin.alerts.digest_dispatcher:DIGEST_LABEL")),
    ("evolve_admin/applications/audit_scheduler.py", "install_launchd",
     ("const", "evolve_admin.applications.audit_scheduler:SCHEDULER_LABEL")),
    ("evolve_admin/pairing/auto_approver.py", "install_launchd",
     ("const", "evolve_admin.pairing.auto_approver:SWEEP_LABEL")),
    # ── raw-plist registration sites (no Scheduler.install() involved) ──────
    # `cli.migrate-jobs` cp's every plist shipped under evolve_admin/plists/
    # into /Library/LaunchDaemons and bootstraps it. Read off disk, so a THIRD
    # plist added there is checked against the expected set automatically.
    ("evolve_admin/cli.py", "migrate_jobs", ("plist-dir", "plists")),
    # com.evolve.* — an operator-Mac LaunchAgent, not a pod daemon. Resolved
    # live rather than exempted, so "no sweeper glob matches com.evolve" stays
    # a checked fact instead of a claim in a comment.
    ("evolve_admin/tunnel.py", "install_persistent_tunnel",
     ("const", "evolve_admin.tunnel:TUNNEL_LABEL")),
    ("evolve_admin/applications/install_helpers.py", "install_launch_agent",
     ("caller-supplied",
      "caller passes label + plist_xml; installs under the bot user's "
      "~/Library/LaunchAgents, which neither the macOS glob nor the systemd "
      "scan reads, so the orphan sweeper cannot see it either way")),
    ("evolve_admin/applications/install_helpers.py", "install_launchd_system_daemon",
     ("caller-supplied",
      "caller passes label + plist_xml. Sole production caller is "
      "install_python_signal_action, whose label comes from the app manifest "
      "-- covered by expected_plist_labels' scheduled_actions[]."
      "installed_artifact scan, same as install_scheduled_jobspec")),
    ("evolve_admin/bot_templates/cli_integration.py", "bootstrap",
     ("caller-supplied",
      "template-install materializer; labels come from the embedded app "
      "template and are spared by expected_plist_labels' "
      "template_installed_labels() consultation")),
    ("evolve_admin/health.py", "_execute_fix",
     ("re-register",
      "heal path: re-bootstraps a plist already on disk whose job was booted "
      "out. The label is one health already knows about")),
    ("evolve_admin/mcp_service.py", "start",
     ("re-register",
      "re-bootstraps the LABEL plist that mcp_service.install wrote; that "
      "label is registered above")),
    ("evolve_admin/setup_wizard.py", "_evo_cutover_bootstrap",
     ("re-register",
      "evo cutover: re-bootstraps the caller-supplied plist path after the "
      "UserName rewrite. Introduces no label of its own")),
    ("analyzer/oc_cli.py", "oc_gateway_restart",
     ("re-register",
      "gateway restart: bootstraps the plist it just discovered on disk when "
      "kickstart finds the job unloaded")),
    # Reached through a `*args` launchctl forwarder rather than `.raw` directly.
    # `com.evolve.admin` is the operator-Mac admin LaunchAgent -- the same shape
    # as the tunnel row above, and it went unregistered in the first revision
    # purely because the census could not see one indirection.
    ("evolve_admin/service.py", "install",
     ("const", "evolve_admin.service:LABEL")),
    ("evolve_admin/recovery.py", "_bootstrap_gateway",
     ("re-register",
      "resume-all: re-bootstraps an existing gateway plist it just located")),
    ("evolve_admin/deploy.py", "install_staged_plists",
     ("caller-supplied",
      "bootstraps whatever *.plist an operator staged under "
      "{shared_dir}/plists/. Currently DEAD -- imported at cli.py:72 and never "
      "called -- but it is the widest member of this class, so it carries a "
      "row rather than an absence: re-wiring it must not be invisible")),
    # ── generic materializer ────────────────────────────────────────────────
    ("evolve_admin/applications/install_helpers.py", "install_scheduled_jobspec",
     ("caller-supplied",
      "gallery/app scheduled-action materializer: the JobSpec (and its label) "
      "comes from the app manifest via the caller. `expected_plist_labels` "
      "covers these through the scheduled_actions[].installed_artifact scan, "
      "not through a label declared in code.")),
)

# `<recv>.install(...)` sites whose receiver is NOT a scheduler. Each is
# spelled out rather than inferred: an inference rule that mis-classifies
# fails OPEN (a real installer silently dropped), which is the exact shape of
# the bug this file exists for. Four short rows beat a clever heuristic.
_NON_SCHEDULER_INSTALL_SITES: tuple[tuple[str, str, str, str], ...] = (
    ("evolve_admin/gallery_verify/pipeline.py", "verify_one_app", "api",
     "gallery-verify HTTP client — POSTs the app install endpoint"),
    ("evolve_admin/web/routes_mcp.py", "api_mcp_install", "_mcp",
     "module alias for evolve_admin.mcp_service; its own seam call is censused"),
    ("evolve_admin/setup_wizard.py", "run_fresh_wizard", "_mcp",
     "module alias for evolve_admin.mcp_service; its own seam call is censused"),
    ("evolve_admin/web/server.py", "api_service_install", "_svc",
     "module alias for evolve_admin.service; com.evolve.* LaunchAgent, which "
     "no orphan-sweeper glob matches"),
)


def _registration_findings(
    rows: list[tuple[str, int, str, str, str]],
    *,
    seam_installers: tuple = _SEAM_INSTALLERS,
    non_scheduler_sites: tuple = _NON_SCHEDULER_INSTALL_SITES,
    plumbing: frozenset = _SEAM_PLUMBING,
    walked_sources: tuple = _WALKED_SOURCES,
) -> list[str]:
    """Reconcile census ``rows`` against the registration tables.

    Split from the repo walk (and parameterised on the tables) so every rule
    below can be driven from synthetic input — see
    ``test_registration_rule_table``. Without that seam the reverse checks in
    particular are free to delete with the suite still green: the real tables
    are in sync, so nothing exercises the stale-row path. That is the
    asserted-but-never-executed class #3772's review turned on its own
    hardening, and it applies here identically.
    """
    unregistered: list[str] = []
    registered = {(m, f) for m, f, _ in seam_installers}
    non_scheduler = {(m, f, r) for m, f, r, _ in non_scheduler_sites}
    walked = set(walked_sources)

    for module_key, lineno, fn, kind, detail in rows:
        where = f"  {module_key}:{lineno}  in {fn}()  "
        if kind == "launchd":
            if module_key not in walked:
                unregistered.append(
                    where + "calls _install_launchd but its module is not in "
                    "_WALKED_SOURCES, so Pass 1 never resolves its label. Add "
                    f"{module_key!r} to _WALKED_SOURCES."
                )
        elif kind in ("seam", "raw-register"):
            if (module_key, fn) in plumbing:
                continue
            if (module_key, fn) not in registered:
                how = (
                    f"registers a service definition via {detail}"
                    if kind == "raw-register"
                    else f"reaches Scheduler.install() via {detail!r}"
                )
                unregistered.append(
                    where + how + " but has no _SEAM_INSTALLERS row. Add one "
                    "naming the label it installs (see that table's header for "
                    "the forms), or -- if it only re-registers an artifact "
                    "already on disk -- a ('re-register', <why>) row."
                )
        else:  # unclassified
            if (module_key, fn, detail) not in non_scheduler:
                unregistered.append(
                    where + f"calls {detail}.install(...) and the census cannot "
                    f"prove {detail!r} is not a Scheduler. If it installs a "
                    "daemon, add a _SEAM_INSTALLERS row; if it does not, add a "
                    "_NON_SCHEDULER_INSTALL_SITES row with the reason."
                )

    # A registration for a callsite that no longer exists is dead weight that
    # reads as coverage — the `_EXTERNAL_INSTALLERS` staleness hole, one table
    # over. Both directions, so the tables track the code.
    live_seam = {
        (m, f) for m, _l, f, k, _d in rows if k in ("seam", "raw-register")
    } - plumbing
    for module_key, fn, _src in seam_installers:
        if (module_key, fn) not in live_seam:
            unregistered.append(
                f"  _SEAM_INSTALLERS row {module_key}:{fn}() no longer has a "
                "seam-install callsite — the installer moved or was retired. "
                "Delete the row (or repoint it) so the table isn't read as "
                "coverage it no longer provides."
            )
    live_unclassified = {(m, f, d) for m, _l, f, k, d in rows if k == "unclassified"}
    for module_key, fn, recv, _why in non_scheduler_sites:
        if (module_key, fn, recv) not in live_unclassified:
            unregistered.append(
                f"  _NON_SCHEDULER_INSTALL_SITES row {module_key}:{fn}() "
                f"receiver {recv!r} no longer exists — delete the row."
            )
    return unregistered


def test_every_daemon_install_site_is_registered():
    """Structural census: every daemon-install callsite in either package must
    be accounted for by a gate.

    This is the gap #3772 left open. Pass 1 sees `_install_launchd(label=…)`
    in `_WALKED_SOURCES`; Pass 2 sees a hand-listed set of modules. An
    installer that calls `get_scheduler().install(...)` or
    `_install_spec_via_seam(...)` from a module in neither list is invisible
    to both — which is how `ai.evolve.opik` was deleted on every upgrade of
    every pod that opted in, for as long as the feature existed.

    Registration is not the point; DISCOVERY is. A human cannot be relied on
    to remember a gate exists, so the gate finds the callsite itself and
    refuses to pass until it is classified. What the classification then buys
    is checked by the two tests below.
    """
    unregistered = _registration_findings(_census_repo())
    if unregistered:
        raise AssertionError("\n".join([
            "Daemon-install callsites that no gate can see:",
            "",
            *unregistered,
            "",
            "A label no gate knows about is classified an orphan by",
            "find_orphaned_plists() and DELETED by remove_orphaned_plists() on",
            "the next `evolve-admin upgrade` — the daemon installs, then",
            "silently vanishes. `upgrade` sweeps and then loops deploy_bot,",
            "which does NOT re-run install_evolve_infra_jobs, so the daemon",
            "stays gone until an infra-jobs run happens by. See #3772's",
            "`ai.evolve.opik` for the live instance of this exact gap.",
        ]))


def _resolve_seam_labels(
    source: tuple[str, str], candidate_bots: list[str]
) -> list[str]:
    """Concrete labels for one ``_SEAM_INSTALLERS`` label source.

    ``("const"|"fn", …)`` are resolved LIVE out of the module, so a rename
    reds this rather than leaving a stale string behind. ``("template", …)``
    is expanded over ``candidate_bots``. ``("caller-supplied", …)`` yields
    nothing — see its row's recorded reason.
    """
    import importlib

    form, payload = source
    if form in ("caller-supplied", "re-register"):
        return []
    if form == "plist-dir":
        plist_dir = _PACKAGE_ROOTS["evolve_admin"] / payload
        if not plist_dir.is_dir():
            pytest.fail(
                f"_SEAM_INSTALLERS ('plist-dir', {payload!r}): {plist_dir} is "
                "not a directory — the shipped-plist location moved"
            )
        stems = sorted(f.stem for f in plist_dir.glob("*.plist"))
        if not stems:
            pytest.fail(
                f"_SEAM_INSTALLERS ('plist-dir', {payload!r}): no plists under "
                f"{plist_dir}; an empty read would pass the sweeper check "
                "while checking nothing"
            )
        return stems
    if form == "template":
        declared = payload[0]
        if "{bot_id}" in declared:
            return [declared.replace("{bot_id}", b) for b in candidate_bots]
        return [declared]
    module_name, _, attr = payload.partition(":")
    try:
        mod = importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover — a setup bug, reported as one
        pytest.fail(f"_SEAM_INSTALLERS: could not import {module_name}: {exc}")
    if not hasattr(mod, attr):
        pytest.fail(
            f"_SEAM_INSTALLERS: {module_name}.{attr} is gone — the declaration "
            "is stale; repoint it at the constant/helper the installer now uses."
        )
    target = getattr(mod, attr)
    if form == "const":
        assert isinstance(target, str), (
            f"_SEAM_INSTALLERS: {payload} is not a str label (got {type(target)})"
        )
        return [target]
    if form == "fn":
        assert callable(target), f"_SEAM_INSTALLERS: {payload} is not callable"
        return [target(bot_id) for bot_id in candidate_bots]
    raise AssertionError(f"unknown _SEAM_INSTALLERS form {form!r}")  # pragma: no cover


def _would_be_swept(label: str, network: dict) -> bool:
    """True iff the orphan sweeper would delete ``label`` on ``network``.

    Mirrors the real predicate rather than restating "is it in
    `expected_plist_labels`". Two reasons that matters:

    * `_is_evolve_owned_label` is the Linux ownership gate and a strict
      SUPERSET of the two macOS globs, so one call covers both platforms.
    * The keep-set is `_orphan_keep_set`, not `expected_plist_labels`: it
      widens by every configured bot's gateway. `expected_plist_labels` lists
      only the PRIMARY gateway on purpose (pod_health must not fail on a
      momentarily-absent member gateway), so checking the raw expected set
      would red this gate on `install_bot_gateway_plist` — a legitimate
      installer whose daemons the sweeper never touches.
    """
    from evolve_admin import orphan_sweep

    if not orphan_sweep._is_evolve_owned_label(label):
        return False
    return label not in orphan_sweep._orphan_keep_set(network)


def test_registered_seam_labels_survive_the_orphan_sweeper(tmp_path, monkeypatch):
    """Every label a seam installer installs must survive the sweep.

    Checked under the condition that makes the bug bite: the daemon IS
    installed. `expected_plist_labels` presence-gates two labels
    (`ai.evolve.opik`, the per-bot iMessage poller) on their artifact being on
    disk — precisely so a pod that never opted in doesn't get a permanent
    "Plist not found" from `health._check_launchd`. Staging each label's plist
    in a temp `LAUNCHD_DIR` asks the real question ("if this daemon exists,
    does upgrade delete it?") and needs no per-label special case: an
    unconditional label and a presence-gated one both answer correctly.
    """
    candidate_bots = ["team_bot_a", "personal_bot", "evolve"]
    network = {
        "members": candidate_bots,
        "bots": {b: {} for b in candidate_bots},
        "sharedDir": str(tmp_path / "shared"),
    }
    launchd_dir = tmp_path / "LaunchDaemons"
    launchd_dir.mkdir()
    monkeypatch.setattr(_deploy_mod, "LAUNCHD_DIR", launchd_dir)

    swept: list[str] = []
    for module_key, fn, source in _SEAM_INSTALLERS:
        for label in _resolve_seam_labels(source, candidate_bots):
            # Stage this label's artifact, so presence-gated entries are
            # evaluated in the state where losing them is a real outage.
            plist = launchd_dir / f"{label}.plist"
            plist.write_text("<plist/>")
            try:
                if _would_be_swept(label, network):
                    swept.append(
                        f"  {module_key}:{fn}()  →  {label}  "
                        "(installed, Evolve-owned, and NOT in the sweeper's "
                        "keep-set)"
                    )
            finally:
                plist.unlink()

    # Every staged artifact is removed again. Asserted, not assumed: the
    # `finally: plist.unlink()` above was deletable with the suite green, and a
    # leaked plist silently widens `expected_plist_labels` for every label
    # checked after it — the gate would then be grading its own leftovers.
    assert not list(launchd_dir.iterdir()), (
        "staged plists leaked: "
        f"{sorted(f.name for f in launchd_dir.iterdir())}"
    )

    if swept:
        raise AssertionError("\n".join([
            "Seam-installed daemons the orphan sweeper would delete:",
            "",
            *swept,
            "",
            "Add each label to expected_plist_labels (presence-gate it if the",
            "daemon is opt-in, mirroring OPIK_LAUNCHD_LABEL — an unconditional",
            "entry makes health._check_launchd report a permanent 'Plist not",
            "found' on every pod that never opted in).",
        ]))


def _label_skeletons_in_function(
    src_text: str, func_name: str, binding: str
) -> set[str] | None:
    """Skeletons of the values assigned to ``binding`` inside ``func_name``.

    Every ``{…}`` interpolation is flattened to ``{}``. Returns ``None`` when
    the function isn't found — reported by the caller as a stale declaration
    rather than silently passing.

    Scoped to ONE binding rather than every string in the body. The unscoped
    version accepted a declaration matching ANY label-shaped string in the
    function, so an installer carrying a second label — a ``legacy_label`` it
    boots out, the shape ``_install_launchd_backup`` already has — could declare
    the wrong one, stay green, and install the other. That is the #3772 outage
    with a gate sitting on top of it.

    Collects string literals, f-string skeletons, and module-level string
    constants assigned to that name, from ``Assign``, ``AnnAssign``,
    ``NamedExpr`` and tuple-unpacking targets alike. Deliberately does NOT
    follow calls: a label that comes from elsewhere should be declared
    ``("const", …)`` or ``("fn", …)``, which resolve live and cannot drift.

    Assignments inside a NESTED function are skipped — they execute in another
    frame, so a helper that returns a legacy name says nothing about what this
    function installs, and counting it let that legacy name stand in as the
    declaration.

    The caller requires EVERY collected skeleton to match the declaration, not
    merely one of them. Any-of let a second assignment to the same binding — a
    reassignment after a legacy bootout, or an if/else that picks a label —
    keep the gate green on the branch that was declared while the other one
    shipped. That is the realistic drift path, not an attack: a template row
    written correctly today, plus a legacy branch added six months later.
    """
    tree = _ast.parse(src_text)
    module_constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, _ast.Assign) and isinstance(node.value, _ast.Constant) \
                and isinstance(node.value.value, str):
            for tgt in node.targets:
                if isinstance(tgt, _ast.Name):
                    module_constants[tgt.id] = node.value.value

    target: _ast.AST | None = None
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) \
                and node.name == func_name:
            target = node
            break
    if target is None:
        return None

    def _skeleton_of(value: _ast.expr) -> "str | None":
        if isinstance(value, _ast.Constant) and isinstance(value.value, str):
            return value.value
        if isinstance(value, _ast.JoinedStr):
            return "".join(
                str(piece.value) if isinstance(piece, _ast.Constant) else "{}"
                for piece in value.values
            )
        if isinstance(value, _ast.Name) and value.id in module_constants:
            return module_constants[value.id]
        return None

    def _own_frame_nodes(fn_node: _ast.AST):
        """Walk ``fn_node``'s body without descending into nested frames."""
        stack = list(_ast.iter_child_nodes(fn_node))
        while stack:
            node = stack.pop()
            if isinstance(
                node,
                (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.Lambda, _ast.ClassDef),
            ):
                continue
            yield node
            stack.extend(_ast.iter_child_nodes(node))

    out: set[str] = set()
    for node in _own_frame_nodes(target):
        if isinstance(node, _ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (_ast.AnnAssign, _ast.NamedExpr)):
            targets, value = [node.target], node.value
        else:
            continue
        if value is None:
            continue  # a bare `label: str` annotation binds nothing
        # A tuple target unpacks positionally: `label, legacy = a, b`.
        for tgt in targets:
            if isinstance(tgt, _ast.Name) and tgt.id == binding:
                skeleton = _skeleton_of(value)
                if skeleton is not None:
                    out.add(skeleton)
            elif isinstance(tgt, (_ast.Tuple, _ast.List)) and isinstance(
                value, (_ast.Tuple, _ast.List)
            ) and len(tgt.elts) == len(value.elts):
                for sub_t, sub_v in zip(tgt.elts, value.elts):
                    if isinstance(sub_t, _ast.Name) and sub_t.id == binding:
                        skeleton = _skeleton_of(sub_v)
                        if skeleton is not None:
                            out.add(skeleton)
    return out


# A skeleton whose literal (non-placeholder) text is shorter than this is not
# specific enough to confirm anything: `f"{label}"` flattens to `{}`, whose
# pattern would match every label ever declared and make the cross-check
# vacuous. Eight characters is past `ai.evolve.`-less noise and well under the
# shortest real label stem.
_MIN_SKELETON_LITERAL_CHARS = 8


def _skeleton_pattern(skeleton: str) -> "_re.Pattern[str] | None":
    """Compile a ``{}``-flattened skeleton into a whole-string matcher.

    Each ``{}`` becomes ``.*`` — the declaration says ``{bot_id}`` where the
    source says ``{user}``, and either may be a concrete value, so the
    placeholder position is the one part that must stay loose. The LITERAL
    segments are what carry the check: rename ``admin-ui`` to ``adminui`` in
    the source and the declared label stops matching.

    Returns ``None`` for a skeleton with too little literal text to be
    evidence of anything.
    """
    chunks = skeleton.split("{}")
    if sum(len(c) for c in chunks) < _MIN_SKELETON_LITERAL_CHARS:
        return None
    return _re.compile("".join([
        "^", ".*".join(_re.escape(c) for c in chunks), "$",
    ]))


def _binding_reaches_install(src_text: str, func_name: str, binding: str) -> bool:
    """True when ``binding`` flows into the arguments of a daemon-install call
    inside ``func_name``.

    The binding-scoped skeleton check proves the DECLARATION matches what that
    name holds; this proves the name is the one handed to the installer.
    Without it a row could name any local that happens to hold a label-shaped
    string — the same laundering, one step removed.

    Local bindings are followed transitively, because the common shape builds
    the JobSpec well before installing it::

        label = "ai.evolve.evolve.thing"
        spec = _thing_jobspec(label, user)
        _install_spec_via_seam(spec, result)

    Only assignment RHSs are followed, so a name that merely appears elsewhere
    in the body (a log line, a bootout of a legacy label) does not count.
    """
    tree = _ast.parse(src_text)
    # The caller reaches this only after `_label_skeletons_in_function` found
    # the function and returned a non-empty set, so a missing target here is
    # unreachable — no `if target is None` guard, which would be dead code
    # asserting nothing.
    target = next(
        n for n in _ast.walk(tree)
        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
        and n.name == func_name
    )

    assigned: dict[str, list[_ast.expr]] = {}
    for node in _ast.walk(target):
        if isinstance(node, _ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, _ast.Name):
                    assigned.setdefault(tgt.id, []).append(node.value)

    def _names_in(expr: _ast.expr) -> set[str]:
        return {x.id for x in _ast.walk(expr) if isinstance(x, _ast.Name)}

    reachable: set[str] = set()
    install_lines = {
        lineno for lineno, fn, kind, _d in _census_install_sites(src_text)
        if fn == func_name and kind in ("seam", "launchd")
    }
    for call in _ast.walk(target):
        if isinstance(call, _ast.Call) and call.lineno in install_lines:
            for arg in [*call.args, *(kw.value for kw in call.keywords)]:
                reachable |= _names_in(arg)

    frontier = set(reachable)
    while frontier:
        nxt: set[str] = set()
        for name in frontier:
            for rhs in assigned.get(name, ()):
                for found in _names_in(rhs) - reachable:
                    reachable.add(found)
                    nxt.add(found)
        frontier = nxt
    return binding in reachable


def _template_drift_findings(seam_installers, source_of) -> list[str]:
    """Cross-check every ``("template", …)`` row against its installer's source.

    ``source_of(module_key)`` returns that module's text — injected so the rules
    below can be driven from synthetic sources (``test_template_drift_table``)
    rather than only from a repo that is, by construction, already in sync.

    Only ``("template", …)`` rows are checked: ``("const", …)`` and
    ``("fn", …)`` read the live module and cannot drift, and
    ``("caller-supplied", …)`` has no label to compare.

    Three things must hold for a template row: the named binding is assigned a
    literal or f-string in that function; the declaration matches one of those
    assignments; and the binding actually flows into the install call.
    """
    problems: list[str] = []
    for module_key, fn, (form, payload) in seam_installers:
        if form != "template":
            continue
        declared, binding = payload
        src_text = source_of(module_key)
        skeletons = _label_skeletons_in_function(src_text, fn, binding)
        if skeletons is None:
            problems.append(
                f"  {module_key}:{fn}() — function not found; the "
                "_SEAM_INSTALLERS row is stale"
            )
            continue
        if not skeletons:
            problems.append(
                f"  {module_key}:{fn}() names binding {binding!r}, but nothing "
                "in that function assigns it a literal or f-string. Either the "
                "binding was renamed, or the label now comes from elsewhere — "
                "in which case switch the row to ('const', …) or ('fn', …)."
            )
            continue
        # EVERY assignment must match, not merely one. See
        # `_label_skeletons_in_function` — any-of let a second assignment to the
        # same name keep the gate green on the declared branch while the other
        # label shipped.
        unmatched = [
            sk for sk in sorted(skeletons)
            if not (
                (pat := _skeleton_pattern(sk)) is not None and pat.match(declared)
            )
        ]
        if unmatched:
            problems.append(
                f"  {module_key}:{fn}() declares {declared!r}, but {binding!r} is "
                f"also assigned {unmatched!r} in that function. Every assignment "
                "to the declared binding must render the declared label — a "
                "second one is a label that ships undeclared."
            )
            continue
        if not _binding_reaches_install(src_text, fn, binding):
            problems.append(
                f"  {module_key}:{fn}() declares binding {binding!r}, but that "
                "name never reaches the install call's arguments — the gate "
                "would be checking a label the installer does not use."
            )
    return problems


def test_seam_installer_templates_match_their_source():
    """A ``("template", …)`` declaration must still match the installer's code.

    This is what stops the table from rotting into paperwork. ``("const", …)``
    and ``("fn", …)`` rows read the live module, so they cannot drift; a
    template is a copy, and a copy goes stale silently. Rename
    ``f"ai.evolve.{user}.admin-ui"`` to ``…adminui`` and without this check the
    gate happily verifies the OLD label — which is in the expected set — while
    the new one gets swept on the next upgrade. Exactly the bug, wearing the
    gate as camouflage.

    Comparison is on the placeholder-flattened skeleton, since the declaration
    says ``{bot_id}`` where the source says ``{user}``: both become ``{}``.
    """
    problems = _template_drift_findings(
        _SEAM_INSTALLERS,
        lambda module_key: _module_path(module_key).read_text(),
    )
    if problems:
        raise AssertionError("\n".join([
            "_SEAM_INSTALLERS template declarations that drifted from the code:",
            "",
            *problems,
            "",
            "Update the declaration to the label the installer actually",
            "installs, and make sure that label is in expected_plist_labels.",
            "If the label is no longer a literal in the installer, switch the",
            "row to ('const', …) or ('fn', …) — those resolve live.",
        ]))


# ─── Census contract: every rule, pinned ────────────────────────────────────
# Same posture as `test_walker_shape_table`. Cases expecting a row pin a rule
# that keeps a real installer IN the census; cases expecting no row pin a rule
# that keeps a legitimate call OUT — an over-eager census reds a required CI
# gate on code that installs nothing.

_CENSUS_CASES: tuple[tuple[str, str, set[tuple[str, str, str]]], ...] = (
    ("inline get_scheduler().install is a seam install",
     "def f():\n    get_scheduler().install(spec)",
     {("f", "seam", "get_scheduler()")}),
    ("get_launchd_scheduler().install is a seam install too",
     "def f():\n    get_launchd_scheduler().install(spec)",
     {("f", "seam", "get_launchd_scheduler()")}),
    ("a name bound to a scheduler carries through to .install",
     "def f():\n    sched = get_scheduler()\n    sched.install(spec)",
     {("f", "seam", "sched")}),
    ("the binding is found even when it is in another function",
     "def g():\n    sched = get_scheduler()\ndef f(sched):\n    sched.install(spec)",
     {("f", "seam", "sched")}),
    ("_install_spec_via_seam is a seam install",
     "def f():\n    _install_spec_via_seam(spec, result)",
     {("f", "seam", "_install_spec_via_seam")}),
    ("_install_job_ensuring_restart is a seam install",
     "def f():\n    ok, err = _install_job_ensuring_restart(spec)",
     {("f", "seam", "_install_job_ensuring_restart")}),
    ("attribute form of a seam wrapper (the cross-module import shape)",
     "def f():\n    deploy._install_job_ensuring_restart(spec)",
     {("f", "seam", "_install_job_ensuring_restart")}),
    ("_install_launchd is its own kind — Pass 1 owns its label",
     'def f():\n    _install_launchd(label="ai.evolve.evolve.x", user="evolve")',
     {("f", "launchd", "_install_launchd")}),
    ("attribute form of _install_launchd (analyzer_monitor_jobs' shape)",
     'def f():\n    deploy._install_launchd(label="ai.evolve.evolve.x")',
     {("f", "launchd", "_install_launchd")}),
    ("a same-prefix helper is NOT the seam primitive",
     'def f():\n    _install_launchd_backup("evolve", result)',
     set()),
    ("an unknown receiver is reported, never dropped",
     "def f(api):\n    api.install(pkg, bot)",
     {("f", "unclassified", "api")}),
    ("a non-install verb on a scheduler installs nothing",
     "def f():\n    sched = get_scheduler()\n    sched.restart(label)\n"
     "    sched.remove(label)\n    sched.raw('bootout', label)\n"
     "    sched.artifact_path(label)",
     set()),
    ("a bare function call named install is not a receiver call",
     "def f():\n    install(spec)",
     set()),
    ("the innermost enclosing def is the attribution",
     "def outer():\n    def inner():\n        get_scheduler().install(spec)",
     {("inner", "seam", "get_scheduler()")}),
    ("a module-level seam call is attributed to <module>",
     "get_scheduler().install(spec)",
     {("<module>", "seam", "get_scheduler()")}),
    ("a dotted receiver renders in full for classification",
     "def f():\n    self._sched.install(spec)",
     {("f", "unclassified", "self._sched")}),
    # ── Scheduler.install reached as a VALUE, not a direct call ─────────────
    ("an aliased scheduler install is a seam install",
     "def f():\n    inst = get_scheduler().install\n    inst(spec)",
     {("f", "seam", "get_scheduler().install (alias)")}),
    ("a partial over a scheduler install is a seam install",
     "def f():\n    sched = get_scheduler()\n"
     "    functools.partial(sched.install, spec)()",
     {("f", "seam", "sched.install (alias)")}),
    ("getattr(scheduler, 'install') is a seam install",
     'def f():\n    getattr(get_scheduler(), "install")(spec)',
     {("f", "seam", 'getattr(get_scheduler(), "install")')}),
    ("a direct call is NOT double-counted as an alias",
     "def f():\n    get_scheduler().install(spec)",
     {("f", "seam", "get_scheduler()")}),
    # The over-eager direction: `.install` on anything that is not a scheduler
    # is a data field, and there are eleven such references in this tree.
    ("a bare .install on a non-scheduler is not a census row",
     "def f(channel):\n    if channel.install:\n        pass",
     set()),
    ("getattr(non-scheduler, 'install') is not a census row",
     'def f(api):\n    getattr(api, "install")(spec)',
     set()),
)


@pytest.mark.parametrize(
    "rule,src,expect", _CENSUS_CASES, ids=[c[0] for c in _CENSUS_CASES]
)
def test_census_shape_table(rule, src, expect):
    """One case per census rule, over synthetic source."""
    got = {(fn, kind, detail) for _l, fn, kind, detail in _census_install_sites(src)}
    assert got == expect, f"{rule}: census produced {sorted(got)}"


def test_census_finds_the_known_seam_callsites():
    """Sanity: the repo census must actually find the seam callsites we know
    about. Without this a path or parse change turns the census into an empty
    list, and an empty census passes every gate above."""
    rows = _census_repo()
    assert rows, "install-surface census returned nothing"
    seam = {(m, f) for m, _l, f, k, _d in rows if k == "seam"}
    # The #3772 live bug, and the shape that motivated this whole pass.
    assert ("evolve_admin/deploy.py", "install_opik_companion") in seam
    # A seam callsite in a module deploy.py does not own.
    assert ("evolve_admin/repo_puller.py", "install_launchd") in seam
    launchd = {m for m, _l, _f, k, _d in rows if k == "launchd"}
    assert launchd == set(_WALKED_SOURCES), (
        "the set of modules calling _install_launchd drifted from "
        f"_WALKED_SOURCES: {sorted(launchd)}"
    )


# ─── Registration + resolution contract, pinned ─────────────────────────────
# The census rules above are pinned by `test_census_shape_table`; these pin
# what happens to a census row AFTERWARDS. Gutting each rule below left the
# suite green before these tests existed — the real tables are in sync and
# every real label resolves, so nothing exercised the failure paths. Same
# finding as #3772's review, one pass over.

_REG_ROW_SEAM = ("evolve_admin/x.py", 10, "installer", "seam", "get_scheduler()")
_REG_ROW_LAUNCHD = ("evolve_admin/x.py", 10, "installer", "launchd", "_install_launchd")
_REG_ROW_UNCLASSIFIED = ("evolve_admin/x.py", 10, "installer", "unclassified", "thing")
_REG_TABLE = (
    ("evolve_admin/x.py", "installer",
     ("template", ("ai.evolve.evolve.x", "label"))),
)
_REG_NONSCHED = (("evolve_admin/x.py", "installer", "thing", "why"),)
_EMPTY: tuple = ()

_REGISTRATION_CASES: tuple[tuple[str, dict, int, str], ...] = (
    ("a registered seam callsite is clean",
     dict(rows=[_REG_ROW_SEAM], seam_installers=_REG_TABLE,
          non_scheduler_sites=_EMPTY, walked_sources=()),
     0, ""),
    ("an unregistered seam callsite is reported",
     dict(rows=[_REG_ROW_SEAM], seam_installers=_EMPTY,
          non_scheduler_sites=_EMPTY, walked_sources=()),
     1, "no _SEAM_INSTALLERS row"),
    ("seam plumbing is exempt without a table row",
     dict(rows=[_REG_ROW_SEAM], seam_installers=_EMPTY,
          non_scheduler_sites=_EMPTY, walked_sources=(),
          plumbing=frozenset({("evolve_admin/x.py", "installer")})),
     0, ""),
    ("_install_launchd outside _WALKED_SOURCES is reported",
     dict(rows=[_REG_ROW_LAUNCHD], seam_installers=_EMPTY,
          non_scheduler_sites=_EMPTY, walked_sources=()),
     1, "not in _WALKED_SOURCES"),
    ("_install_launchd inside _WALKED_SOURCES is clean",
     dict(rows=[_REG_ROW_LAUNCHD], seam_installers=_EMPTY,
          non_scheduler_sites=_EMPTY, walked_sources=("evolve_admin/x.py",)),
     0, ""),
    ("an unclassified receiver is reported",
     dict(rows=[_REG_ROW_UNCLASSIFIED], seam_installers=_EMPTY,
          non_scheduler_sites=_EMPTY, walked_sources=()),
     1, "cannot prove"),
    ("an allowlisted non-scheduler receiver is clean",
     dict(rows=[_REG_ROW_UNCLASSIFIED], seam_installers=_EMPTY,
          non_scheduler_sites=_REG_NONSCHED, walked_sources=()),
     0, ""),
    # ── the reverse direction: a table row whose callsite is gone ───────────
    ("a _SEAM_INSTALLERS row with no live callsite is reported",
     dict(rows=[], seam_installers=_REG_TABLE,
          non_scheduler_sites=_EMPTY, walked_sources=()),
     1, "no longer has a seam-install callsite"),
    ("a _SEAM_INSTALLERS row whose callsite became plumbing is reported",
     dict(rows=[_REG_ROW_SEAM], seam_installers=_REG_TABLE,
          non_scheduler_sites=_EMPTY, walked_sources=(),
          plumbing=frozenset({("evolve_admin/x.py", "installer")})),
     1, "no longer has a seam-install callsite"),
    ("a _NON_SCHEDULER_INSTALL_SITES row with no live callsite is reported",
     dict(rows=[], seam_installers=_EMPTY,
          non_scheduler_sites=_REG_NONSCHED, walked_sources=()),
     1, "no longer exists"),
    ("a raw-register row gets the raw-register remediation, not the seam one",
     dict(rows=[("evolve_admin/x.py", 10, "installer", "raw-register",
                 "raw('bootstrap')")],
          seam_installers=_EMPTY, non_scheduler_sites=_EMPTY, walked_sources=()),
     1, "registers a service definition via raw('bootstrap')"),
    ("a seam row gets the seam remediation, not the raw-register one",
     dict(rows=[_REG_ROW_SEAM], seam_installers=_EMPTY,
          non_scheduler_sites=_EMPTY, walked_sources=()),
     1, "reaches Scheduler.install()"),
    ("a raw-register callsite satisfies a _SEAM_INSTALLERS row",
     dict(rows=[("evolve_admin/x.py", 10, "installer", "raw-register",
                 "raw('bootstrap')")],
          seam_installers=_REG_TABLE, non_scheduler_sites=_EMPTY,
          walked_sources=()),
     0, ""),
    ("a _NON_SCHEDULER row whose receiver was renamed is reported",
     dict(rows=[("evolve_admin/x.py", 10, "installer", "unclassified", "other")],
          seam_installers=_EMPTY, non_scheduler_sites=_REG_NONSCHED,
          walked_sources=()),
     2, "no longer exists"),
)


@pytest.mark.parametrize(
    "rule,kwargs,count,fragment",
    _REGISTRATION_CASES,
    ids=[c[0] for c in _REGISTRATION_CASES],
)
def test_registration_rule_table(rule, kwargs, count, fragment):
    """One case per registration rule, over synthetic census rows + tables."""
    rows = kwargs.pop("rows")
    findings = _registration_findings(rows, **kwargs)
    assert len(findings) == count, f"{rule}: got {findings}"
    if fragment:
        assert any(fragment in f for f in findings), f"{rule}: got {findings}"


_SKELETON_CASES: tuple[tuple[str, str, str, bool], ...] = (
    ("a literal skeleton matches itself",
     "ai.evolve.evolve.better", "ai.evolve.evolve.better", True),
    ("a placeholder is loose, so a concrete value matches",
     "ai.evolve.{}.admin-ui", "ai.evolve.evolve.admin-ui", True),
    ("a placeholder is loose in the other direction too",
     "ai.evolve.{}.admin-ui", "ai.evolve.{bot_id}.admin-ui", True),
    ("a renamed literal segment stops matching — the anti-rot bite",
     "ai.evolve.{}.adminui", "ai.evolve.evolve.admin-ui", False),
    ("a longer label is not matched by a shorter skeleton",
     "ai.evolve.evolve.better", "ai.evolve.evolve.better-engine", False),
    ("the match is anchored at the start",
     "evolve.{}.admin-ui", "ai.evolve.evolve.admin-ui", False),
    ("dots are escaped, not treated as wildcards",
     "ai.evolve.evolve.x", "aixevolvexevolvexx", False),
)


@pytest.mark.parametrize(
    "rule,skeleton,label,matches",
    _SKELETON_CASES,
    ids=[c[0] for c in _SKELETON_CASES],
)
def test_skeleton_pattern_table(rule, skeleton, label, matches):
    pattern = _skeleton_pattern(skeleton)
    assert pattern is not None, f"{rule}: skeleton was rejected as too vague"
    assert bool(pattern.match(label)) is matches, rule


@pytest.mark.parametrize("skeleton", ["{}", "{}{}", "ai.{}", "{}.x"])
def test_skeleton_pattern_rejects_a_vague_skeleton(skeleton):
    """A skeleton with almost no literal text is evidence of nothing.

    ``f"{label}"`` flattens to ``{}``, whose pattern matches every label ever
    declared. Accepting it would make
    ``test_seam_installer_templates_match_their_source`` pass for ANY
    declaration in a function that formats a label into a log line — which is
    every one of them. The cross-check would then be pure decoration.
    """
    assert _skeleton_pattern(skeleton) is None


def _sweep_network(tmp_path, monkeypatch) -> dict:
    bots = ["team_bot_a", "evolve"]
    launchd_dir = tmp_path / "LaunchDaemons"
    launchd_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(_deploy_mod, "LAUNCHD_DIR", launchd_dir)
    return {
        "members": bots,
        "bots": {b: {} for b in bots},
        "sharedDir": str(tmp_path / "shared"),
    }


_SWEEP_CASES: tuple[tuple[str, str, bool], ...] = (
    ("an unexpected ai.evolve.* label is swept",
     "ai.evolve.evolve.nobody-declared-me", True),
    ("an unexpected ai.openclaw.evolve.* label is swept",
     "ai.openclaw.evolve.nobody-declared-me", True),
    ("an expected label is spared",
     "ai.evolve.evolve.heal", False),
    # The ownership gate. Without it every non-Evolve unit on the box reads as
    # swept, and `_would_be_swept` stops describing the sweeper at all.
    ("a non-Evolve label is never swept, expected or not",
     "com.evolve.mcp-bridge", False),
    ("a third-party label is never swept",
     "org.nginx.nginx", False),
    # The keep-set widening. `expected_plist_labels` lists only the PRIMARY
    # gateway; `_orphan_keep_set` adds every configured bot's. Checking the
    # raw expected set would red the gate on install_bot_gateway_plist.
    ("a member bot's gateway is spared by the keep-set widening",
     "ai.openclaw.team_bot_a-gateway", False),
    ("a retired bot's gateway is swept",
     "ai.openclaw.ghost_bot-gateway", True),
)


@pytest.mark.parametrize(
    "rule,label,swept", _SWEEP_CASES, ids=[c[0] for c in _SWEEP_CASES]
)
def test_would_be_swept_table(rule, label, swept, tmp_path, monkeypatch):
    """``_would_be_swept`` must mirror the sweeper, not approximate it."""
    network = _sweep_network(tmp_path, monkeypatch)
    assert _would_be_swept(label, network) is swept, rule


def test_resolve_seam_labels_const_form_reads_the_live_module():
    labels = _resolve_seam_labels(
        ("const", "evolve_admin.deploy:OPIK_LAUNCHD_LABEL"), ["evolve"]
    )
    assert labels == [_deploy_mod.OPIK_LAUNCHD_LABEL]


def test_resolve_seam_labels_fn_form_expands_over_candidate_bots():
    """The ``("fn", …)`` form must actually CALL the helper for every candidate.

    Returning nothing here is the silent-skip shape: the sweeper check then
    iterates an empty list and passes, so `install_bot_gateway_plist` and
    `install_imessage_poller` would both be unguarded while the table still
    reads as covering them.
    """
    labels = _resolve_seam_labels(
        ("fn", "evolve_admin.deploy:per_bot_gateway_plist_label"),
        ["alpha", "beta"],
    )
    assert labels == ["ai.openclaw.alpha-gateway", "ai.openclaw.beta-gateway"]


def test_resolve_seam_labels_template_form_expands_bot_id():
    assert _resolve_seam_labels(
        ("template", ("ai.evolve.{bot_id}.x", "label")), ["a", "b"]
    ) == ["ai.evolve.a.x", "ai.evolve.b.x"]
    assert _resolve_seam_labels(
        ("template", ("ai.evolve.evolve.x", "label")), ["a"]
    ) == ["ai.evolve.evolve.x"]


def test_resolve_seam_labels_caller_supplied_form_yields_nothing():
    assert _resolve_seam_labels(("caller-supplied", "manifest-driven"), ["a"]) == []


@pytest.mark.parametrize("form", ["const", "fn"])
def test_resolve_seam_labels_fails_loudly_on_a_stale_declaration(form):
    """A row pointing at a renamed constant/helper must FAIL, not resolve to
    nothing. `getattr` on a missing name is the one way a live-resolution row
    can rot, and a silent skip there re-opens the whole gap."""
    with pytest.raises(pytest.fail.Exception, match="is gone"):
        _resolve_seam_labels((form, "evolve_admin.deploy:NO_SUCH_NAME"), ["evolve"])


# ─── Template cross-check contract, pinned ──────────────────────────────────
# The repo's own tables are in sync by construction, so nothing in the tree
# drives the drift path. These synthetic sources do — both directions.

_M = "evolve_admin/x.py"


def _one(form: str, payload):
    return ((_M, "installer", (form, payload)),)


_DRIFT_CASES: tuple[tuple[str, tuple, str, int, str], ...] = (
    ("a literal bound to the named binding matches its declaration",
     _one("template", ("ai.evolve.evolve.thing", "label")),
     'def installer():\n    label = "ai.evolve.evolve.thing"\n    get_scheduler().install(mk(label))',
     0, ""),
    ("an f-string label matches a concrete declaration",
     _one("template", ("ai.evolve.evolve.admin-ui", "label")),
     'def installer(user):\n    label = f"ai.evolve.{user}.admin-ui"\n    get_scheduler().install(mk(label))',
     0, ""),
    ("an f-string label matches a {bot_id} declaration",
     _one("template", ("ai.evolve.{bot_id}.admin-ui", "label")),
     'def installer(user):\n    label = f"ai.evolve.{user}.admin-ui"\n    get_scheduler().install(mk(label))',
     0, ""),
    ("a module constant assigned to the binding counts as source",
     _one("template", ("ai.evolve.evolve.konst", "label")),
     'K = "ai.evolve.evolve.konst"\ndef installer():\n    label = K\n    get_scheduler().install(mk(label))',
     0, ""),
    # The value must sit on a DIFFERENT line from the call: `install_lines`
    # matches by line number, so a same-line `spec=mk(label)` is found by the
    # positional scan too and the keyword rule goes unexercised.
    ("the binding reaches the install as a KEYWORD argument",
     _one("template", ("ai.evolve.evolve.thing", "label")),
     'def installer(result):\n    label = "ai.evolve.evolve.thing"\n'
     '    spec = mk(label)\n    _install_spec_via_seam(\n'
     '        spec=spec,\n        result=result)',
     0, ""),
    ("the binding reaches the install through TWO intermediate locals",
     _one("template", ("ai.evolve.evolve.thing", "label")),
     'def installer(result):\n    label = "ai.evolve.evolve.thing"\n'
     '    args = pack(label)\n    spec = build(args)\n'
     '    _install_spec_via_seam(spec, result)',
     0, ""),
    ("the binding reaches the install through an intermediate spec local",
     _one("template", ("ai.evolve.evolve.thing", "label")),
     'def installer(result):\n    label = "ai.evolve.evolve.thing"\n'
     '    spec = build(label, 1)\n    unrelated = 2\n'
     '    _install_spec_via_seam(spec, result)',
     0, ""),
    # ── the drift directions ────────────────────────────────────────────────
    ("a renamed label in the source is caught — the whole point",
     _one("template", ("ai.evolve.evolve.admin-ui", "label")),
     'def installer(user):\n    label = f"ai.evolve.{user}.adminui"\n    get_scheduler().install(mk(label))',
     1, "is also assigned"),
    ("a SECOND label in the function cannot launder a wrong declaration",
     _one("template", ("ai.evolve.evolve.heal", "label")),
     'def installer(result):\n    legacy_label = "ai.evolve.evolve.heal"\n'
     '    get_scheduler().remove(legacy_label)\n'
     '    label = "ai.evolve.evolve.canary"\n'
     '    _install_spec_via_seam(mk(label), result)',
     1, "is also assigned"),
    ("a binding never assigned a literal is caught",
     _one("template", ("ai.evolve.evolve.thing", "label")),
     'def installer():\n    label = build_the_label()\n    get_scheduler().install(mk(label))',
     1, "nothing in that function assigns"),
    ("a bare interpolation is not evidence — the vacuous-skeleton path",
     _one("template", ("ai.evolve.evolve.thing", "label")),
     'def installer(x):\n    label = f"{x}"\n    get_scheduler().install(mk(label))',
     1, "is also assigned"),
    ("a binding that never reaches the install is caught",
     _one("template", ("ai.evolve.evolve.thing", "label")),
     'def installer(spec):\n    label = "ai.evolve.evolve.thing"\n'
     '    log(label)\n    get_scheduler().install(spec)',
     1, "never reaches the install"),
    # ── NEW-B: laundering variants that survived the any-of match ──────────
    ("a REASSIGNMENT after the declared one is caught",
     _one("template", ("ai.evolve.evolve.heal", "label")),
     'def installer(result):\n    label = "ai.evolve.evolve.heal"\n'
     '    get_scheduler().remove(label)\n'
     '    label = "ai.evolve.evolve.canary"\n'
     '    _install_spec_via_seam(mk(label), result)',
     1, "is also assigned"),
    ("an if/else that picks between two labels is caught",
     _one("template", ("ai.evolve.evolve.heal", "label")),
     'def installer(result, legacy):\n    if legacy:\n'
     '        label = "ai.evolve.evolve.heal"\n    else:\n'
     '        label = "ai.evolve.evolve.canary"\n'
     '    _install_spec_via_seam(mk(label), result)',
     1, "is also assigned"),
    ("an inner helper's unrelated binding of the same name does not red a "
     "correct row",
     _one("template", ("ai.evolve.evolve.heal", "label")),
     'def installer(result):\n    label = "ai.evolve.evolve.heal"\n'
     '    def _fmt(row):\n        label = "row=%s"\n        return label % row\n'
     '    log(_fmt(1))\n    _install_spec_via_seam(mk(label), result)',
     0, ""),
    ("a nested def's binding of the same name does not count as evidence",
     _one("template", ("ai.evolve.evolve.heal", "label")),
     'def installer(result):\n    def _legacy():\n'
     '        label = "ai.evolve.evolve.heal"\n        return label\n'
     '    _uninstall(_legacy())\n'
     '    label = "ai.evolve.evolve.canary"\n'
     '    _install_spec_via_seam(mk(label), result)',
     1, "is also assigned"),
    # ── NEW-C: binding forms a template row must be able to express ────────
    ("an annotated assignment binds the label",
     _one("template", ("ai.evolve.evolve.thing", "label")),
     'def installer():\n    label: str = "ai.evolve.evolve.thing"\n'
     '    get_scheduler().install(mk(label))',
     0, ""),
    ("a walrus binds the label",
     _one("template", ("ai.evolve.evolve.thing", "label")),
     'def installer():\n    if (label := "ai.evolve.evolve.thing"):\n'
     '        get_scheduler().install(mk(label))',
     0, ""),
    ("a tuple unpack binds the label positionally",
     _one("template", ("ai.evolve.evolve.thing", "label")),
     'def installer(result):\n'
     '    label, user = "ai.evolve.evolve.thing", "evolve"\n'
     '    _install_spec_via_seam(mk(label, user), result)',
     0, ""),
    ("a bare annotation binds nothing and is reported as such",
     _one("template", ("ai.evolve.evolve.thing", "label")),
     'def installer(spec):\n    label: str\n'
     '    get_scheduler().install(spec)',
     1, "nothing in that function assigns"),
    ("a renamed installer function is caught, not skipped",
     _one("template", ("ai.evolve.evolve.thing", "label")),
     'def installer_v2():\n    label = "ai.evolve.evolve.thing"\n    get_scheduler().install(mk(label))',
     1, "function not found"),
    ("an async installer is found, not reported as missing",
     _one("template", ("ai.evolve.evolve.thing", "label")),
     'async def installer():\n    label = "ai.evolve.evolve.thing"\n    get_scheduler().install(mk(label))',
     0, ""),
    # The non-template forms resolve live; the cross-check must leave them
    # alone even when their payload looks nothing like the source.
    ("a ('const', ...) row is not cross-checked",
     _one("const", "evolve_admin.deploy:WHATEVER"), 'def installer():\n    pass',
     0, ""),
    ("a ('fn', ...) row is not cross-checked",
     _one("fn", "evolve_admin.deploy:whatever"), 'def installer():\n    pass',
     0, ""),
    ("a ('caller-supplied', ...) row is not cross-checked",
     _one("caller-supplied", "manifest-driven"), 'def installer():\n    pass',
     0, ""),
)


@pytest.mark.parametrize(
    "rule,table,src,count,fragment",
    _DRIFT_CASES,
    ids=[c[0] for c in _DRIFT_CASES],
)
def test_template_drift_table(rule, table, src, count, fragment):
    """One case per template-cross-check rule, over synthetic source."""
    findings = _template_drift_findings(table, lambda _key: src)
    assert len(findings) == count, f"{rule}: got {findings}"
    if fragment:
        assert any(fragment in f for f in findings), f"{rule}: got {findings}"


# ─── The rules an in-sync repo cannot exercise ──────────────────────────────
# Every rule below was deletable with the whole suite green: the real tables
# agree with the real code, every real label resolves, and both census roots
# hold well-formed modules — so nothing in the tree ever drove the failure
# path. Third round of the same finding (#3772, then the extraction pass, then
# this), which is itself the argument for making each one executable rather
# than trusting the next reader to notice.


def test_census_walks_both_package_roots():
    """Both roots in ``_CENSUS_ROOTS`` must actually be read.

    ``packages/analyzer`` has no install site today, so deleting it from
    ``_CENSUS_ROOTS`` changed no census row and no assertion — half the
    advertised coverage was free to delete. Asserting on the SCANNED module
    list (not on rows) is what makes an empty-but-walked root distinguishable
    from a root that is not walked at all.
    """
    scanned = _census_scanned_modules()
    by_prefix = {key.split("/", 1)[0] for key in scanned}
    assert by_prefix == set(_PACKAGE_ROOTS), (
        f"census walked {sorted(by_prefix)}, expected {sorted(_PACKAGE_ROOTS)}"
    )
    for prefix in _PACKAGE_ROOTS:
        assert sum(1 for k in scanned if k.startswith(f"{prefix}/")) > 10, (
            f"census found almost nothing under {prefix}/ — a path change "
            "would read as coverage"
        )
    assert "analyzer/oc_cli.py" in scanned


def test_census_skips_pycache_and_test_trees():
    """`tests/` holds this file's own synthetic installers; censusing it would
    report fixtures as unregistered production daemons. `__pycache__` holds no
    source at all."""
    scanned = _census_scanned_modules()
    assert not [k for k in scanned if "__pycache__" in k or "/tests/" in k], (
        "census walked a cache or test tree"
    )


def test_census_fails_loudly_on_a_missing_root():
    """A root that moved must FAIL, not walk nothing and pass everything."""
    with pytest.raises(pytest.fail.Exception, match="census root missing"):
        _census_scanned_modules((("ghost", _ADMIN_DIR / "no-such-package"),))


def test_resolve_seam_labels_rejects_a_non_string_const():
    """A ``("const", …)`` row pointing at something that is not a label must
    fail rather than hand a non-str down to the sweeper predicate, where
    ``label.startswith`` would raise an AttributeError nobody can read."""
    with pytest.raises(AssertionError, match="not a str label"):
        _resolve_seam_labels(
            ("const", "evolve_admin.deploy:expected_plist_labels"), ["evolve"]
        )


def test_resolve_seam_labels_rejects_a_non_callable_fn():
    with pytest.raises(AssertionError, match="not callable"):
        _resolve_seam_labels(
            ("fn", "evolve_admin.deploy:OPIK_LAUNCHD_LABEL"), ["evolve"]
        )


def test_resolve_seam_labels_plist_dir_reads_the_shipped_plists():
    """The ``("plist-dir", …)`` form must read real stems off disk — an empty
    or missing directory has to fail, not resolve to nothing and pass."""
    labels = _resolve_seam_labels(("plist-dir", "plists"), ["evolve"])
    assert "ai.openclaw.evolve.better" in labels
    assert all(not lb.endswith(".plist") for lb in labels)
    with pytest.raises(pytest.fail.Exception, match="not a directory"):
        _resolve_seam_labels(("plist-dir", "no-such-dir"), ["evolve"])
    with pytest.raises(pytest.fail.Exception, match="no plists under"):
        _resolve_seam_labels(("plist-dir", "web"), ["evolve"])


def test_resolve_seam_labels_re_register_form_yields_nothing():
    assert _resolve_seam_labels(("re-register", "heal path"), ["a"]) == []


_ASYNC_CENSUS_CASES: tuple[tuple[str, str, set], ...] = (
    ("an async installer is attributed to its own def, not <module>",
     "async def installer():\n    get_scheduler().install(spec)",
     {("installer", "seam", "get_scheduler()")}),
    ("an async def nested in a sync def still wins attribution",
     "def outer():\n    async def inner():\n        get_scheduler().install(spec)",
     {("inner", "seam", "get_scheduler()")}),
)


@pytest.mark.parametrize(
    "rule,src,expect", _ASYNC_CENSUS_CASES, ids=[c[0] for c in _ASYNC_CENSUS_CASES]
)
def test_census_attributes_async_functions(rule, src, expect):
    """``AsyncFunctionDef`` in the attribution walk was deletable: nothing in
    either package installs a daemon from an async function today, so every
    such row would silently become ``<module>`` and its registration would
    never match."""
    got = {(fn, kind, detail) for _l, fn, kind, detail in _census_install_sites(src)}
    assert got == expect, f"{rule}: census produced {sorted(got)}"


def test_census_renders_an_exotic_receiver_rather_than_crashing():
    """``_render``'s fallback. A receiver that is neither Name, Attribute nor
    Call still has to produce a printable row: the message is the whole
    remediation, and a census that raises here takes the gate down instead of
    asking for a classification."""
    rows = _census_install_sites('def f():\n    ("a" + b).install(spec)')
    assert rows == [(2, "f", "unclassified", "BinOp")]


def test_would_be_swept_is_never_narrower_than_the_macos_sweeper(
    tmp_path, monkeypatch
):
    """``_would_be_swept`` uses Linux's predicate on both platforms. That is
    only safe because two widenings cancel, and nothing pinned the cancellation.

    macOS keeps ``expected_plist_labels`` and scans two globs; Linux keeps
    ``_orphan_keep_set`` (expected PLUS every configured bot's gateway) and
    scans ``_is_evolve_owned_label`` (those two globs PLUS the gateway shape).
    The extra keep-set members are exactly the extra scanned shape, so the
    difference is nil today. If ``_orphan_keep_set`` ever widens past gateways,
    this gate goes SILENTLY narrow on macOS — a label the macOS sweeper deletes
    would stop being reported. Assert the containment directly rather than
    trusting the arithmetic to stay balanced.
    """
    network = _sweep_network(tmp_path, monkeypatch)
    expected = _deploy_mod.expected_plist_labels(network)
    probes = [
        "ai.evolve.evolve.heal",
        "ai.evolve.evolve.nobody-declared-me",
        "ai.openclaw.evolve.better",
        "ai.openclaw.evolve.nobody-declared-me",
        "ai.evolve.team_bot_a.backup",
        "ai.evolve.ghost_bot.backup",
        "ai.openclaw.team_bot_a-gateway",
        "ai.openclaw.ghost_bot-gateway",
        "com.evolve.tunnel",
        "org.nginx.nginx",
    ]
    exercised = 0
    for label in probes:
        macos_scans = label.startswith("ai.openclaw.evolve.") or label.startswith(
            "ai.evolve."
        )
        macos_would_delete = macos_scans and label not in expected
        if macos_would_delete:
            exercised += 1
            assert _would_be_swept(label, network), (
                f"{label!r}: the macOS sweeper deletes it, but _would_be_swept "
                "says otherwise — the gate has gone narrow on macOS"
            )
    # Without this the test is vacuity-prone: every assertion sits inside the
    # branch, so a probe list of labels the macOS sweeper happens to spare
    # passes while checking nothing.
    assert exercised >= 3, (
        f"only {exercised} probe(s) reached the macOS-would-delete branch; "
        "the containment is not actually being tested"
    )


def test_module_path_rejects_an_unknown_package_prefix():
    """``_module_path`` must refuse a key whose package it does not know.

    Unexercised today because both prefixes resolve — but the day a third
    package root lands, a key typed against the old two-root world would
    otherwise resolve to ``<analyzer-or-admin>/<whatever>`` and read as a
    missing file rather than a missing root."""
    with pytest.raises(pytest.fail.Exception, match="unknown package prefix"):
        _module_path("gallery/forge.py")
