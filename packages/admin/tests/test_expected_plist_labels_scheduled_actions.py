"""Regression tests for the scheduled_actions branch of
``deploy.expected_plist_labels``.

Motivation
----------

PR #2164's ``install_launchd_command_action`` and PR #2168's
``apply-actions`` install per-bot app LaunchDaemons at
``ai.evolve.<bot>.<app>`` labels under ``/Library/LaunchDaemons/``.
Pre-2026-06-05 those labels were NOT in ``expected_plist_labels``,
so ``find_orphaned_plists`` flagged them as orphans, and
``remove_orphaned_plists`` deleted them on every operator deploy.

That's how atlas-daily-digest's cron silently vanished after the
first ``evolve-admin deploy --all`` post-install — the operator's
log line was::

    16:18:02 Removed orphan: ai.evolve.atlas.atlas-daily-digest
    16:18:02 Removed orphan: ai.evolve.bot-a.ea-evening
    16:18:02 Removed orphan: ai.evolve.bot-a.ea-morning
    16:18:02 Removed orphan: ai.evolve.bot-a.ea-premeet

— four real cron daemons deleted in one second, no warning.

This file pins:

1. ``expected_plist_labels`` adds labels collected from each bot's
   installed manifests' ``scheduled_actions[].installed_artifact``.
2. Falls back to ``install.plist_label`` (with ``${bot_id}``
   substitution) when ``installed_artifact`` isn't yet stamped.
3. Per-bot read failures don't blank the whole expected set.
4. ``find_orphaned_plists`` logs a WARN when a plist matching the
   per-bot app pattern (``ai.evolve.<bot>.<rest>``) ends up as an
   orphan — the belt-and-suspenders that surfaces the next
   regression in log lines instead of in silent cron disappearance.
5. End-to-end: a stamped Atlas-style plist survives a sweep.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy as _deploy  # noqa: E402
from evolve_admin.applications.manifest import ApplicationManifest  # noqa: E402
from evolve_admin.deploy import (  # noqa: E402
    expected_plist_labels,
    find_orphaned_plists,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


def _manifest(bot_id: str, app_id: str, scheduled_actions: list) -> ApplicationManifest:
    """Build a minimal ApplicationManifest with the given scheduled_actions."""
    m = ApplicationManifest(id=app_id, name=app_id, bot_id=bot_id)
    m.scheduled_actions = scheduled_actions
    return m


def _patch_list_manifests(monkeypatch: pytest.MonkeyPatch, per_bot: dict) -> None:
    """Replace ``applications.manifest.list_manifests`` with a stub returning
    the supplied per-bot manifests. ``per_bot`` is keyed by bot_id; values are
    lists of ApplicationManifest objects.
    """
    from evolve_admin.applications import manifest as manifest_mod

    def fake(shared_dir, bot_id):
        return per_bot.get(bot_id, [])

    monkeypatch.setattr(manifest_mod, "list_manifests", fake)


# ── Core behavior — labels propagate from installed_artifact ────────────────


def test_label_added_from_installed_artifact_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical happy path: Phase 4.5 stamped
    ``installed_artifact`` with the full plist path. Extract the
    label and add it to the expected set."""
    _patch_list_manifests(monkeypatch, {
        "atlas": [_manifest("atlas", "atlas-daily-digest", [
            {
                "id": "atlas-daily-digest",
                "mechanism": "launchd",
                "installed_artifact":
                    "/Library/LaunchDaemons/ai.evolve.atlas.atlas-daily-digest.plist",
            },
        ])],
    })
    labels = expected_plist_labels(
        {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
    )
    assert "ai.evolve.atlas.atlas-daily-digest" in labels


def test_label_added_from_plist_label_fallback_with_bot_id_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When installed_artifact isn't populated, fall back to
    install.plist_label with ${bot_id} substitution. This is the
    case during the gap between manifest seeding and Phase 4.5
    stamping — without it the sweeper would briefly consider the
    label orphan."""
    _patch_list_manifests(monkeypatch, {
        "atlas": [_manifest("atlas", "atlas-daily-digest", [
            {
                "id": "atlas-daily-digest",
                "mechanism": "launchd",
                "install": {"plist_label": "ai.evolve.${bot_id}.atlas-daily-digest"},
                # No installed_artifact yet.
            },
        ])],
    })
    labels = expected_plist_labels(
        {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
    )
    assert "ai.evolve.atlas.atlas-daily-digest" in labels


def test_installed_artifact_wins_over_plist_label_when_both_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If both fields are present and the operator manually edited
    plist_label after install (mid-migration), ``installed_artifact``
    is the source of truth — it's what landed on disk."""
    _patch_list_manifests(monkeypatch, {
        "atlas": [_manifest("atlas", "atlas-daily-digest", [
            {
                "id": "x",
                "mechanism": "launchd",
                "install": {"plist_label": "ai.evolve.${bot_id}.NEW-LABEL"},
                "installed_artifact":
                    "/Library/LaunchDaemons/ai.evolve.atlas.OLD-LABEL.plist",
            },
        ])],
    })
    labels = expected_plist_labels(
        {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
    )
    assert "ai.evolve.atlas.OLD-LABEL" in labels
    assert "ai.evolve.atlas.NEW-LABEL" not in labels


def test_no_scheduled_actions_adds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bot with no manifests, or whose manifests have empty
    scheduled_actions, contributes no extra labels."""
    _patch_list_manifests(monkeypatch, {
        "atlas": [_manifest("atlas", "no-cron-app", [])],
    })
    labels_with = expected_plist_labels(
        {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
    )
    _patch_list_manifests(monkeypatch, {})
    labels_without = expected_plist_labels(
        {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
    )
    # The only labels that should differ between these two are
    # any per-bot infra labels picked up from list_manifests; for
    # a bot with no scheduled_actions, they should be identical.
    assert labels_with == labels_without


def test_per_bot_read_failure_doesnt_blank_expected_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bot's manifest read raising must not corrupt the labels
    collected from other bots. Otherwise a single corrupt manifests
    dir would turn every per-bot app plist on the pod into an
    orphan candidate."""
    from evolve_admin.applications import manifest as manifest_mod

    def fake(shared_dir, bot_id):
        if bot_id == "broken":
            raise OSError("disk gone")
        return [_manifest(bot_id, "x", [{
            "id": "x", "mechanism": "launchd",
            "installed_artifact":
                f"/Library/LaunchDaemons/ai.evolve.{bot_id}.x.plist",
        }])]

    monkeypatch.setattr(manifest_mod, "list_manifests", fake)

    labels = expected_plist_labels(
        {"members": ["atlas", "broken", "bot-a"], "bots": {},
         "sharedDir": "/Users/Shared/evolve"},
    )
    # atlas and bot-a contributed; broken silently dropped.
    assert "ai.evolve.atlas.x" in labels
    assert "ai.evolve.bot-a.x" in labels


def test_non_dict_scheduled_action_is_silently_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive against on-disk manifest corruption — a non-dict
    entry shouldn't crash the labels walk."""
    _patch_list_manifests(monkeypatch, {
        "atlas": [_manifest("atlas", "a", [
            None,
            "garbage",
            {
                "id": "real", "mechanism": "launchd",
                "installed_artifact":
                    "/Library/LaunchDaemons/ai.evolve.atlas.real.plist",
            },
        ])],
    })
    labels = expected_plist_labels(
        {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
    )
    assert "ai.evolve.atlas.real" in labels


# ── Regression: bot-home LaunchAgents must NOT enter the daemon set ─────────
#
# 2026-06-11 incident. Two installed apps each had a scheduled action whose
# ``installed_artifact`` was stamped with a per-user LaunchAgent path in the
# bot's home — NOT /Library/LaunchDaemons/:
#
#   atlas app-ops-automation  -> /Users/atlas/Library/LaunchAgents/ai.openclaw.atlas-selfheal.plist
#   bot-a app-cost-monitoring -> /Users/bot-a/Library/LaunchAgents/ai.evolve.bot-a.spend-alert.plist
#
# ``expected_plist_labels`` took the basename regardless of the directory, so
# both labels entered the daemon set. ``pod_health._check_launchd`` then looked
# for them under /Library/LaunchDaemons/, didn't find them, and fired a
# perpetual false ``pod_health_launchd`` "Plist not found" Signal for each —
# even though both plists were correctly installed as LaunchAgents (and the
# underlying work is also covered pod-wide by ai.evolve.evolve.heal /
# ai.evolve.evolve.spend-alert). Pin that a LaunchAgent artifact contributes
# no label, while a real LaunchDaemon artifact on the same bot still does.


def test_launchagent_artifact_excluded_selfheal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_list_manifests(monkeypatch, {
        "atlas": [_manifest("atlas", "app-ops-automation", [{
            "id": "operations-automation-atlas-selfheal",
            "mechanism": "launchd",
            "install": {
                "command": "/bin/bash /Users/atlas/.openclaw/workspace/ops/scripts/gateway-selfheal.sh",
                "plist_label": "ai.openclaw.atlas-selfheal",
            },
            "installed_artifact":
                "/Users/atlas/Library/LaunchAgents/ai.openclaw.atlas-selfheal.plist",
        }])],
    })
    labels = expected_plist_labels(
        {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
    )
    assert "ai.openclaw.atlas-selfheal" not in labels


def test_launchagent_artifact_excluded_spend_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_list_manifests(monkeypatch, {
        "bot-a": [_manifest("bot-a", "app-cost-monitoring", [{
            "id": "cost-monitoring-alerts-spend-alert",
            "mechanism": "launchd",
            "install": {
                "command": "/usr/bin/python3 /Users/bot-a/.openclaw/workspace/evolve/spend_alert.py --network /Users/Shared/evolve/network.json",
                "plist_label": "ai.evolve.bot-a.spend-alert",
            },
            "installed_artifact":
                "/Users/bot-a/Library/LaunchAgents/ai.evolve.bot-a.spend-alert.plist",
        }])],
    })
    labels = expected_plist_labels(
        {"members": ["bot-a"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
    )
    assert "ai.evolve.bot-a.spend-alert" not in labels


def test_tilde_launchagent_artifact_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """forge_engine stamps some agents as ``~/Library/LaunchAgents/...`` — the
    parent dir name is still ``LaunchAgents``, so the exclusion holds for the
    tilde form too."""
    _patch_list_manifests(monkeypatch, {
        "atlas": [_manifest("atlas", "task-check", [{
            "id": "task-check",
            "mechanism": "launchd",
            "installed_artifact": "~/Library/LaunchAgents/com.atlas.task-check.plist",
        }])],
    })
    labels = expected_plist_labels(
        {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
    )
    assert "com.atlas.task-check" not in labels


def test_launchagent_destination_fallback_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-stamp window: no installed_artifact yet, but install.destination
    explicitly says launchagent — must not enter the daemon set."""
    _patch_list_manifests(monkeypatch, {
        "atlas": [_manifest("atlas", "ops", [{
            "id": "atlas-selfheal",
            "mechanism": "launchd",
            "install": {
                "plist_label": "ai.openclaw.${bot_id}-selfheal",
                "destination": "launchagent",
            },
            # No installed_artifact yet.
        }])],
    })
    labels = expected_plist_labels(
        {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
    )
    assert "ai.openclaw.atlas-selfheal" not in labels


def test_launchdaemon_artifact_still_added_alongside_launchagent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix is targeted, not a blanket drop: a real LaunchDaemon artifact is
    still collected even when a sibling action on the same bot installs a
    LaunchAgent. Otherwise the fix would re-open the 2026-06-05 orphan-sweep
    regression for legitimate per-bot app daemons."""
    _patch_list_manifests(monkeypatch, {
        "bot-a": [
            _manifest("bot-a", "app-cost-monitoring", [{
                "id": "spend-alert",
                "mechanism": "launchd",
                "installed_artifact":
                    "/Users/bot-a/Library/LaunchAgents/ai.evolve.bot-a.spend-alert.plist",
            }]),
            _manifest("bot-a", "app-daily-digest", [{
                "id": "daily-digest",
                "mechanism": "launchd",
                "installed_artifact":
                    "/Library/LaunchDaemons/ai.evolve.bot-a.daily-digest.plist",
            }]),
        ],
    })
    labels = expected_plist_labels(
        {"members": ["bot-a"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
    )
    assert "ai.evolve.bot-a.spend-alert" not in labels
    assert "ai.evolve.bot-a.daily-digest" in labels


# ── realized_only — pod_health vs orphan-sweeper diverge on unstamped ───────
#
# 2026-06-17 incident. atlas's task-manager.json is a thin v7-arc instance
# whose gallery Spec (p-9bfa1c84) declares two ``launchd`` scheduled actions
# (task-manager-cache-urgent, task-manager-daily-prune). hydrate_v7_arc_instance
# overlays scheduled_actions from the Spec, but the app was adopted by the
# SCANNER (installed_by: scanner_promote), which does NOT run forge's
# _materialize_scheduled_actions — so neither action has an installed_artifact
# stamp and neither plist is on /Library/LaunchDaemons/. The bare plist_label
# fallback still manufactured ai.evolve.atlas.task-manager-{cache-urgent,
# daily-prune}, which pod_health._check_launchd then reported as two perpetual
# false "Plist not found" Signals.
#
# The fix gives pod_health a realized-only view (realized_only=True) that drops
# the unstamped fallback, while the orphan-sweeper keeps the broad default set
# so it never deletes a just-installed cron during the pre-stamp window.


def test_realized_only_drops_unstamped_plist_label_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pod_health view: an action with a plist_label but NO installed_artifact
    (never materialized) must NOT enter the realized set — otherwise
    _check_launchd fires a false "Plist not found"."""
    _patch_list_manifests(monkeypatch, {
        "atlas": [_manifest("atlas", "task-manager", [
            {
                "id": "task-manager-cache-urgent",
                "mechanism": "launchd",
                "install": {"plist_label": "ai.evolve.${bot_id}.task-manager-cache-urgent"},
                # No installed_artifact — scanner-adopted, never materialized.
            },
            {
                "id": "task-manager-daily-prune",
                "mechanism": "launchd",
                "install": {"plist_label": "ai.evolve.${bot_id}.task-manager-daily-prune"},
            },
        ])],
    })
    network = {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"}
    realized = expected_plist_labels(network, realized_only=True)
    assert "ai.evolve.atlas.task-manager-cache-urgent" not in realized
    assert "ai.evolve.atlas.task-manager-daily-prune" not in realized

    # The orphan-sweeper's broad default view STILL includes them so it never
    # deletes a just-installed cron during the pre-stamp window.
    broad = expected_plist_labels(network)
    assert "ai.evolve.atlas.task-manager-cache-urgent" in broad
    assert "ai.evolve.atlas.task-manager-daily-prune" in broad


def test_realized_only_keeps_stamped_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """True-positive preserved: a stamped LaunchDaemon artifact stays in the
    realized view, so pod_health still alerts if its plist later vanishes."""
    _patch_list_manifests(monkeypatch, {
        "atlas": [_manifest("atlas", "atlas-daily-digest", [{
            "id": "atlas-daily-digest",
            "mechanism": "launchd",
            "installed_artifact":
                "/Library/LaunchDaemons/ai.evolve.atlas.atlas-daily-digest.plist",
        }])],
    })
    realized = expected_plist_labels(
        {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
        realized_only=True,
    )
    assert "ai.evolve.atlas.atlas-daily-digest" in realized


def test_realized_only_keeps_infra_and_per_bot_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The narrowing is surgical: realized_only only drops the unstamped
    scheduled-actions fallback. Pod-wide infra + per-bot evolve daemons stay in
    the set, because their absence IS a real failure pod_health should catch."""
    _patch_list_manifests(monkeypatch, {"atlas": []})
    network = {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"}
    realized = expected_plist_labels(network, realized_only=True)
    broad = expected_plist_labels(network)
    # A pod-wide infra daemon and a per-bot evolve daemon are present in BOTH
    # views (no scheduled_actions in play here, so the sets are identical).
    assert "ai.evolve.evolve.pod-health" in realized
    assert "ai.openclaw.evolve.apply.atlas" in realized
    assert realized == broad


def test_realized_only_drops_unstamped_but_keeps_stamped_same_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed app: one action stamped (materialized), one not. Realized view
    keeps the stamped one and drops the unstamped one — independently."""
    _patch_list_manifests(monkeypatch, {
        "atlas": [_manifest("atlas", "task-manager", [
            {
                "id": "task-manager-cache-urgent",
                "mechanism": "launchd",
                "install": {"plist_label": "ai.evolve.${bot_id}.task-manager-cache-urgent"},
            },
            {
                "id": "task-manager-daily-prune",
                "mechanism": "launchd",
                "installed_artifact":
                    "/Library/LaunchDaemons/ai.evolve.atlas.task-manager-daily-prune.plist",
            },
        ])],
    })
    realized = expected_plist_labels(
        {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
        realized_only=True,
    )
    assert "ai.evolve.atlas.task-manager-cache-urgent" not in realized
    assert "ai.evolve.atlas.task-manager-daily-prune" in realized


def test_sweeper_protects_unstamped_just_installed_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orphan-sweeper (privileged, destructive) MUST NOT delete a
    just-installed plist that's on disk but whose action hasn't been stamped
    yet. find_orphaned_plists uses the broad default set, so the unstamped
    plist_label fallback keeps it out of the orphan list. Pinning that the fix
    did not narrow the sweeper's view."""
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    label = "ai.evolve.atlas.task-manager-cache-urgent"
    (launchd / f"{label}.plist").write_text("<plist/>")

    monkeypatch.setattr(_deploy, "LAUNCHD_DIR", launchd)
    _patch_list_manifests(monkeypatch, {
        "atlas": [_manifest("atlas", "task-manager", [{
            "id": "task-manager-cache-urgent",
            "mechanism": "launchd",
            "install": {"plist_label": "ai.evolve.${bot_id}.task-manager-cache-urgent"},
            # No installed_artifact yet — the pre-stamp window.
        }])],
    })

    orphan_stems = {p.stem for p in find_orphaned_plists(
        {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
    )}
    assert label not in orphan_stems, (
        "unstamped-but-just-installed plist flagged as orphan — the sweeper "
        "would delete a legitimate cron during the pre-stamp window"
    )


# ── End-to-end regression — sweep does NOT delete stamped plist ─────────────


def test_atlas_daily_digest_survives_find_orphaned_plists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact 2026-06-05 regression: atlas-daily-digest's plist
    landed at /Library/LaunchDaemons/ai.evolve.atlas.atlas-daily-digest.plist,
    Phase 4.5 stamped the action with installed_artifact, then the
    operator ran ``evolve-admin deploy --all`` and the sweeper
    deleted it.

    With this PR the manifest walk populates the expected set, so
    find_orphaned_plists doesn't flag it. Pinning the contract here."""
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    label = "ai.evolve.atlas.atlas-daily-digest"
    (launchd / f"{label}.plist").write_text("<plist/>")

    monkeypatch.setattr(_deploy, "LAUNCHD_DIR", launchd)
    _patch_list_manifests(monkeypatch, {
        "atlas": [_manifest("atlas", "atlas-daily-digest", [{
            "id": "atlas-daily-digest",
            "mechanism": "launchd",
            "installed_artifact": f"/Library/LaunchDaemons/{label}.plist",
        }])],
    })

    orphans = find_orphaned_plists(
        {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
    )
    orphan_stems = {p.stem for p in orphans}
    assert label not in orphan_stems, (
        "atlas-daily-digest plist flagged as orphan — sweep would "
        "delete it on next deploy (the exact 2026-06-05 regression)"
    )


def test_ea_pack_three_bot_a_plists_all_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bot-a-side companion to the atlas regression: ea-pack
    installs three plists on bot-a. All three got swept on the same
    deploy run. Pin all three."""
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    labels = (
        "ai.evolve.bot-a.ea-morning",
        "ai.evolve.bot-a.ea-evening",
        "ai.evolve.bot-a.ea-premeet",
    )
    for label in labels:
        (launchd / f"{label}.plist").write_text("<plist/>")

    monkeypatch.setattr(_deploy, "LAUNCHD_DIR", launchd)
    _patch_list_manifests(monkeypatch, {
        "bot-a": [_manifest("bot-a", "ea-pack", [
            {
                "id": "ea-morning-brief", "mechanism": "launchd",
                "installed_artifact":
                    "/Library/LaunchDaemons/ai.evolve.bot-a.ea-morning.plist",
            },
            {
                "id": "ea-evening-sweep", "mechanism": "launchd",
                "installed_artifact":
                    "/Library/LaunchDaemons/ai.evolve.bot-a.ea-evening.plist",
            },
            {
                "id": "ea-premeet-brief", "mechanism": "launchd",
                "installed_artifact":
                    "/Library/LaunchDaemons/ai.evolve.bot-a.ea-premeet.plist",
            },
        ])],
    })

    orphan_stems = {p.stem for p in find_orphaned_plists(
        {"members": ["bot-a"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
    )}
    for label in labels:
        assert label not in orphan_stems


# ── Belt-and-suspenders — warn on suspicious orphan patterns ────────────────


def test_per_bot_app_orphan_logs_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A plist whose label matches ``ai.evolve.<bot>.<rest>`` (with
    bot being a network member) but isn't in the expected set should
    log WARN before being treated as orphan. This is the visibility
    surface that catches the next regression class — when the
    manifest is missing/corrupted but the plist is on disk."""
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    label = "ai.evolve.atlas.orphaned-app"
    (launchd / f"{label}.plist").write_text("<plist/>")

    monkeypatch.setattr(_deploy, "LAUNCHD_DIR", launchd)
    # No manifest for the orphaned app → not in expected.
    _patch_list_manifests(monkeypatch, {"atlas": []})

    with caplog.at_level(logging.WARNING, logger="evolve_admin.deploy"):
        orphans = find_orphaned_plists(
            {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
        )

    # The orphan is still flagged (we don't auto-shield it — the
    # warning is the surface, not a refusal-to-sweep).
    assert any(p.stem == label for p in orphans)
    # WARN line mentions both the label and the bot — operator can
    # find the right manifests dir without grep.
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any(label in m and "atlas" in m for m in warnings), (
        f"expected per-bot-app warning mentioning {label} and atlas; "
        f"got: {warnings}"
    )


def test_orphan_not_matching_per_bot_pattern_does_not_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A genuinely retired pod-wide infra plist (label doesn't match
    the per-bot app pattern) gets swept silently without warning —
    the warning is targeted, not noisy."""
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    (launchd / "ai.evolve.evolve.retired-fake-daemon.plist").write_text("<plist/>")

    monkeypatch.setattr(_deploy, "LAUNCHD_DIR", launchd)
    _patch_list_manifests(monkeypatch, {})

    with caplog.at_level(logging.WARNING, logger="evolve_admin.deploy"):
        orphans = find_orphaned_plists(
            {"members": ["atlas"], "bots": {}, "sharedDir": "/Users/Shared/evolve"},
        )

    assert any("retired-fake-daemon" in p.stem for p in orphans)
    # No per-bot-app warning since 'evolve' isn't a bot member here.
    per_bot_warnings = [
        r.message for r in caplog.records
        if r.levelname == "WARNING" and "per-bot app pattern" in r.message
    ]
    assert per_bot_warnings == []
