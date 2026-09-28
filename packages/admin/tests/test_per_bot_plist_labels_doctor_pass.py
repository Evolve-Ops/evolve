"""Regression test for the doctor-pass orphan-sweeper loop.

PR #1748 added ``_install_launchd_doctor_pass`` to ``deploy_bot``, which
creates ``ai.openclaw.evolve.doctor-pass.<bot_id>.plist`` for every bot.
But the PR didn't update ``per_bot_evolve_plist_labels`` — the source of
truth that ``expected_plist_labels`` feeds to ``find_orphaned_plists``.

Effect: every Versions-page upgrade banner showed "N orphaned launchd
jobs detected from a previous Evolve version" listing all the doctor-pass
plists. The orphan-sweeper would unload + ``rm`` them; the next deploy
would re-create them; the next upgrade would flag them again. Infinite
loop visible to operators on every upgrade.

This test pins the doctor-pass label into ``per_bot_evolve_plist_labels``
so the next time someone adds a per-bot daemon, the absence here trips
a test rather than a confused operator.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from evolve_admin.deploy import per_bot_evolve_plist_labels, expected_plist_labels


def test_doctor_pass_in_per_bot_labels():
    """doctor-pass per-bot plist must be in the canonical per-bot label set."""
    labels = per_bot_evolve_plist_labels("team_bot_a")
    assert "ai.openclaw.evolve.doctor-pass.team_bot_a" in labels, (
        "ai.openclaw.evolve.doctor-pass.<bot_id> is installed by "
        "_install_launchd_doctor_pass during deploy_bot (added in PR #1748). "
        "Missing it here means find_orphaned_plists flags it as orphan on "
        "every upgrade — same wedge that hit 8/8 bots on 2026-05-30."
    )


def test_doctor_pass_in_expected_plist_labels_for_each_member():
    """expected_plist_labels must include doctor-pass for every network member,
    because that's what the orphan-sweeper compares the on-disk plists
    against. If a member is missing from this set, the sweeper deletes
    that bot's doctor-pass plist on the next upgrade.
    """
    network = {
        "members": ["team_bot_a", "team_bot_b", "personal_bot"],
        "sharedDir": "/tmp/shared-test-doctor-pass",
        "bots": {},
    }
    labels = expected_plist_labels(network)
    for bot_id in network["members"]:
        assert f"ai.openclaw.evolve.doctor-pass.{bot_id}" in labels, (
            f"expected_plist_labels missing ai.openclaw.evolve.doctor-pass.{bot_id}"
        )
