"""Regression: launchd pod_health Signals must carry the real owning bot.

2026-06-11. ``_emit_health_signals`` scoped every Signal by splitting
``check.name`` on the first ``:`` and treating the head as a bot id. The
launchd check names every row ``launchd:<plist_label>``, so the head is the
literal category word ``launchd`` — and every ``pod_health_launchd`` Signal
got ``scope="bot", bot_id="launchd"`` (a bot that doesn't exist) plus, for
app/per-bot daemons, the no-op ``install-infra-jobs`` fix hint. That made a
benign false-positive read like a missing infra daemon.

These tests pin:

1. ``_launchd_label_bot_id`` attributes per-bot gateway / Evolve-daemon /
   ``ai.evolve.<bot>.<app>`` labels to their bot, and leaves pod-wide infra
   (and unattributable labels) unscoped (None).
2. ``_plist_fix_cmd`` returns ``deploy --bot <bot>`` for per-bot daemons and
   ``install-infra-jobs`` only for genuine pod-wide infra.
3. End-to-end through ``_emit_health_signals``: a launchd FAIL emits a Signal
   whose ``bot_id`` is the real bot (or None for pod infra) — never the
   string ``"launchd"``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _path in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from evolve_admin.health import (  # noqa: E402
    FAIL,
    CheckResult,
    HealthReport,
    _emit_health_signals,
    _launchd_label_bot_id,
    _plist_fix_cmd,
)

_MEMBERS = ["atlas", "bot-a", "evolve"]


# ── 1. label → bot attribution ──────────────────────────────────────────────


@pytest.mark.parametrize("label,expected", [
    # Per-bot gateway.
    ("ai.openclaw.atlas-gateway", "atlas"),
    # Per-bot Evolve daemons (per_bot_evolve_plist_labels).
    ("ai.openclaw.evolve.cost-converter.atlas", "atlas"),
    ("ai.openclaw.evolve.doctor-pass.bot-a", "bot-a"),
    ("ai.evolve.atlas.backup", "atlas"),
    # Per-bot app daemon (PR #2164 ai.evolve.<bot>.<app>).
    ("ai.evolve.bot-a.daily-digest", "bot-a"),
    # Pod-wide infra → unscoped.
    ("ai.evolve.evolve.heal", None),
    ("ai.evolve.evolve.spend-alert", None),
    ("ai.openclaw.evolve.measure", None),
    ("ai.openclaw.evolve-gateway", "evolve"),  # primary; caller folds → pod
    # Unknown / unattributable → unscoped, never a bogus bot.
    ("ai.openclaw.some-unknown-daemon", None),
])
def test_launchd_label_bot_id(label, expected):
    assert _launchd_label_bot_id(label, _MEMBERS) == expected


def test_launchd_label_bot_id_empty_members_is_safe():
    # No members → can't attribute, but must not raise or invent a bot.
    assert _launchd_label_bot_id("ai.openclaw.atlas-gateway", []) is None


# ── 2. fix-command correctness ──────────────────────────────────────────────


def test_plist_fix_cmd_per_bot_gateway_uses_deploy_bot():
    assert _plist_fix_cmd("ai.openclaw.atlas-gateway", _MEMBERS) == \
        "sudo evolve-admin deploy --bot atlas"


def test_plist_fix_cmd_per_bot_daemon_uses_deploy_bot():
    assert _plist_fix_cmd("ai.openclaw.evolve.doctor-pass.bot-a", _MEMBERS) == \
        "sudo evolve-admin deploy --bot bot-a"


def test_plist_fix_cmd_pod_infra_uses_install_infra_jobs():
    assert _plist_fix_cmd("ai.evolve.evolve.heal", _MEMBERS) == \
        "sudo evolve-admin install-infra-jobs"


# ── 3. end-to-end Signal scoping ────────────────────────────────────────────


@pytest.fixture
def shared(tmp_path: Path) -> Path:
    d = tmp_path / "evolve"
    for sub in ("firing", "snoozed", "archived"):
        (d / "signals" / sub).mkdir(parents=True)
    return d


def _firing(shared: Path) -> dict[str, dict]:
    """Map check-name → emitted Signal dict, keyed by details.name."""
    out: dict[str, dict] = {}
    for p in (shared / "signals" / "firing").glob("*.json"):
        sig = json.loads(p.read_text())
        out[sig.get("details", {}).get("name", sig.get("signature"))] = sig
    return out


def test_launchd_signals_carry_real_bot_never_the_category_word(shared: Path):
    report = HealthReport()
    report.checks = [
        CheckResult(FAIL, "launchd", "launchd:ai.openclaw.atlas-gateway",
                    "Plist not found"),
        CheckResult(FAIL, "launchd", "launchd:ai.evolve.evolve.heal",
                    "Plist not found"),
        CheckResult(FAIL, "launchd", "launchd:ai.evolve.bot-a.daily-digest",
                    "Plist not found"),
    ]

    _emit_health_signals(
        report, shared, members=_MEMBERS, primary_bot_id="evolve",
    )

    sigs = _firing(shared)
    # The headline regression: no Signal is scoped to the phantom "launchd" bot.
    assert all(s["bot_id"] != "launchd" for s in sigs.values()), \
        {n: s["bot_id"] for n, s in sigs.items()}

    gw = sigs["launchd:ai.openclaw.atlas-gateway"]
    assert (gw["scope"], gw["bot_id"]) == ("bot", "atlas")

    heal = sigs["launchd:ai.evolve.evolve.heal"]
    assert (heal["scope"], heal["bot_id"]) == ("pod", None)

    app = sigs["launchd:ai.evolve.bot-a.daily-digest"]
    assert (app["scope"], app["bot_id"]) == ("bot", "bot-a")


def test_launchd_content_check_name_strips_trailing_suffix(shared: Path):
    """A ``launchd:<label>:content`` row still resolves the bot from <label>."""
    report = HealthReport()
    report.checks = [
        CheckResult(FAIL, "launchd",
                    "launchd:ai.openclaw.atlas-gateway:content",
                    "Plist content invalid"),
    ]
    _emit_health_signals(
        report, shared, members=_MEMBERS, primary_bot_id="evolve",
    )
    sig = _firing(shared)["launchd:ai.openclaw.atlas-gateway:content"]
    assert (sig["scope"], sig["bot_id"]) == ("bot", "atlas")
