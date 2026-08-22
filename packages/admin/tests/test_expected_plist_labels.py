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
    docs/spec-proposal-synthesizer-2026-05-10.md §7. Must be in the
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

    Spec: docs/spec-tier-cascade-2026-05-26.md § audit layer.
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

    Spec: docs/spec-tier-cascade-2026-05-26.md § pressure watchdog.
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

    Spec: docs/spec-google-integration-paths-2026-05-30.md §8 (PR δ).
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


# External installers that own their own `install_launchd()` entry point
# (i.e., not via deploy._install_launchd) plus the module-level constant
# that holds their label. If you add another external installer, add it here
# so the meta-test still covers the full installed surface.
_EXTERNAL_INSTALLERS: tuple[tuple[str, str], ...] = (
    ("evolve_admin.repo_puller", "REPO_PULLER_LABEL"),
    ("evolve_admin.alerts.digest_dispatcher", "DIGEST_LABEL"),
    ("evolve_admin.applications.audit_scheduler", "SCHEDULER_LABEL"),
)


def _collect_install_launchd_labels() -> list[tuple[int, str, str]]:
    """Walk deploy.py's AST and return every `_install_launchd(label=…)`
    label seen.

    Each entry is ``(lineno, label_or_template, kind)`` where ``kind`` is
    one of ``"literal"`` / ``"f-string"`` / ``"constant"`` / ``"unresolved"``.
    Templates keep their ``{bot_id}`` placeholder so the caller can expand
    against a candidate bot list.

    ``"unresolved"`` is the loud-failure path: any AST shape we don't know
    how to handle is reported with its dump so the meta-test fails until
    someone teaches the walker about the new pattern. Quietly skipping
    unrecognized shapes would silently re-open the drift bug class.
    """
    import ast

    deploy_src = (_ADMIN_DIR / "evolve_admin" / "deploy.py").read_text()
    tree = ast.parse(deploy_src)

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

    results: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "_install_launchd"):
            continue
        for kw in node.keywords:
            if kw.arg != "label":
                continue
            v = kw.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                results.append((node.lineno, v.value, "literal"))
            elif isinstance(v, ast.JoinedStr):
                parts: list[str] = []
                ok = True
                for piece in v.values:
                    if isinstance(piece, ast.Constant):
                        parts.append(str(piece.value))
                    elif (
                        isinstance(piece, ast.FormattedValue)
                        and isinstance(piece.value, ast.Name)
                    ):
                        parts.append("{" + piece.value.id + "}")
                    else:
                        ok = False
                        break
                if ok:
                    results.append((node.lineno, "".join(parts), "f-string"))
                else:
                    results.append((node.lineno, f"<UNRESOLVED-FSTRING:{ast.dump(v)[:80]}>", "unresolved"))
            elif isinstance(v, ast.Name) and v.id in module_constants:
                results.append((node.lineno, module_constants[v.id], "constant"))
            else:
                results.append((node.lineno, f"<UNRESOLVED:{ast.dump(v)[:80]}>", "unresolved"))
    return results


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

    # Pass 1: _install_launchd callsites inside deploy.py.
    for lineno, label, kind in _collect_install_launchd_labels():
        if kind == "unresolved":
            not_covered.append(
                f"  deploy.py:{lineno}  →  {label}  "
                f"(meta-test walker needs to learn this AST shape)"
            )
            continue
        if "{bot_id}" in label:
            candidates = [label.format(bot_id=b) for b in candidate_bots]
        else:
            candidates = [label]
        if not any(c in expected for c in candidates):
            not_covered.append(
                f"  deploy.py:{lineno}  →  {label}  "
                f"(none of {candidates!r} in expected_plist_labels)"
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
