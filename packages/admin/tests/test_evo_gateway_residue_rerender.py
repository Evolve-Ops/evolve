"""Regression tests for EVO-GATEWAY-RESIDUE-RERENDER.

A post-evo-account-separation pod has ``network.primary == "evo"``,
``members == ["darwin", "evo"]`` and NO bot named ``evolve`` (the privileged
``evolve`` service account runs only the admin daemon and carries no
bot-shaped ``openclaw.json``; spec-evo-account-separation-2026-05-25).

The bug: ``restart-gateways`` unconditionally appended the literal ``"evolve"``
to its roster (``all_bots.append("evolve")``), feeding a phantom ``"evolve"``
into ``restart_all_gateways`` → ``_restart_gateway_linux`` →
``install_bot_gateway_plist``. That rendered ``ai.openclaw.evolve-gateway`` on
the primary's port (:19030, bind collision → crash-loop status=78/CONFIG) and
seeded ``/home/evolve/.openclaw/openclaw.json`` — on EVERY run, winning the
race against the #3167 orphan-sweeper.

These tests pin the fail-closed roster contract:

1. ``_is_provisionable_bot`` admits members + the resolved primary, refuses a
   non-member phantom — and is byte-identical on a legacy pod with a real
   ``evolve`` bot (also falls back to ``bots.keys()`` when ``members`` is unset).
2. ``_restart_gateway_linux`` (the deploy-time restart funnel where the phantom
   entered) REAPS a stale non-member unit on disk instead of restarting it
   (sweeper-consistency, same run), restarts a member normally, and no-ops a
   non-member with no unit. The gate is here, NOT inside the
   ``install_bot_gateway_plist`` primitive (which the wizard/CLI call directly
   with an already-validated bot_id — see test_install_user_resolution).
3. ``_resolve_evolve_app_target`` deliberately KEEPS its degenerate-network
   ``or "evolve"`` fallback (a different surface — first-party app install onto
   the primary's .openclaw, not a gateway unit; the residue-rerender gate lives
   at the gateway-restart funnel, not here). A legacy ``evolve``-primary pod
   still resolves to ``evolve`` (macOS byte-identity, test #3052 contract).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from platform_profile import LINUX, set_profile  # noqa: E402

from evolve_admin import deploy  # noqa: E402
from runtime.scheduler import FakeScheduler, JobSpec  # noqa: E402


# evo-primary roster: members [darwin, evo], NO "evolve" bot.
_EVO_PRIMARY_NET = {
    "networkId": "test-pod",
    "primary": "evo",
    "members": ["darwin", "evo"],
    "bots": {
        "darwin": {"role": "member", "user": "darwin", "port": 19000},
        "evo": {"role": "primary", "user": "evo", "port": 19030},
    },
}

# Legacy evolve-primary roster: a real bot literally named "evolve".
_LEGACY_EVOLVE_NET = {
    "networkId": "legacy-pod",
    "primary": "evolve",
    "members": ["evolve"],
    "bots": {"evolve": {"role": "primary", "user": "evolve", "port": 19030}},
}


# ── 1. _is_provisionable_bot — the roster authority ────────────────────────────


def test_is_provisionable_admits_members_and_primary():
    assert deploy._is_provisionable_bot("darwin", _EVO_PRIMARY_NET) is True
    assert deploy._is_provisionable_bot("evo", _EVO_PRIMARY_NET) is True


def test_is_provisionable_refuses_phantom_evolve_on_evo_primary():
    # The whole bug in one assertion: "evolve" is NOT a member here.
    assert deploy._is_provisionable_bot("evolve", _EVO_PRIMARY_NET) is False


def test_is_provisionable_legacy_evolve_bot_still_admitted():
    # macOS byte-identity: a real evolve bot is a member → provisionable.
    assert deploy._is_provisionable_bot("evolve", _LEGACY_EVOLVE_NET) is True


def test_is_provisionable_falls_back_to_bots_keys_without_members():
    net = {"primary": "evolve", "bots": {"evolve": {"role": "primary"}}}
    assert deploy._is_provisionable_bot("evolve", net) is True
    assert deploy._is_provisionable_bot("ghost", net) is False


# ── 2. install_bot_gateway_plist refuses non-members ───────────────────────────


# NOTE: the membership gate lives at the deploy-time restart funnel
# (_restart_gateway_linux), NOT inside install_bot_gateway_plist —
# install_bot_gateway_plist is a tested primitive the wizard/CLI call directly
# with an already-validated bot_id (see test_install_user_resolution.py), so
# gating it there would break legitimate direct installs. The reap/skip tests
# below pin the gate at its correct home.


# ── 2. _restart_gateway_linux reaps stale non-member units ─────────────────────


@pytest.fixture
def linux_profile():
    set_profile(LINUX)
    try:
        yield
    finally:
        set_profile(None)


def _gateway_spec(bot_id: str, port: int) -> JobSpec:
    return JobSpec(
        label=f"ai.openclaw.{bot_id}-gateway",
        run_at_load=True,
        keep_alive=True,
        program_args=["/usr/bin/node", "/x/index.js", "gateway", "--port", str(port)],
    )


def test_restart_linux_reaps_stale_phantom_unit(linux_profile):
    """A stale ai.openclaw.evolve-gateway on disk on an evo-primary pod is
    REMOVED (not restarted) — the same deploy run reaps it, so it cannot
    re-arm the crash-loop on the primary's port."""
    sched = FakeScheduler()
    sched.seed_job(_gateway_spec("evolve", 19030))  # the phantom on disk
    label = "ai.openclaw.evolve-gateway"

    with patch.object(deploy, "load_network", return_value=_EVO_PRIMARY_NET), \
         patch.object(deploy, "get_scheduler", return_value=sched):
        deploy._restart_gateway_linux("evolve", "evolve")

    verbs = [c[0] for c in sched.calls]
    assert ("remove", label) in sched.calls
    assert "restart" not in verbs  # never restarted the phantom
    assert label not in sched.jobs  # reaped


def test_restart_linux_member_restarts_normally(linux_profile):
    """A real member with an installed unit is restarted (not reaped)."""
    sched = FakeScheduler()
    sched.seed_job(_gateway_spec("evo", 19030))

    with patch.object(deploy, "load_network", return_value=_EVO_PRIMARY_NET), \
         patch.object(deploy, "get_scheduler", return_value=sched):
        deploy._restart_gateway_linux("evo", "evo")

    assert ("restart", "ai.openclaw.evo-gateway") in sched.calls
    assert ("remove", "ai.openclaw.evo-gateway") not in sched.calls


def test_restart_linux_non_member_no_unit_is_noop(linux_profile):
    """A non-member with NO unit on disk: nothing installed, nothing removed."""
    sched = FakeScheduler()  # empty — no units

    with patch.object(deploy, "load_network", return_value=_EVO_PRIMARY_NET), \
         patch.object(deploy, "get_scheduler", return_value=sched):
        deploy._restart_gateway_linux("evolve", "evolve")

    assert sched.calls == []  # no install, no restart, no remove
    assert "ai.openclaw.evolve-gateway" not in sched.jobs


# ── 3. _resolve_evolve_app_target keeps its tested byte-identity fallback ──────
#
# The first-party APP-install target (the primary's .openclaw, a different
# surface from a gateway UNIT) deliberately KEEPS the degenerate-network
# ``or "evolve"`` fallback — see test_evolve_app_required_tools_target (#3052
# macOS byte-identity proof). The residue-rerender fix does NOT touch it; the
# gate is at the gateway-restart funnel above. This test pins that the legacy
# resolution is intact (a sibling regression to the gate, not a change of it).


def test_resolve_app_target_legacy_evolve_still_resolves():
    """A legacy evolve-primary pod resolves to 'evolve' (macOS byte-identity)."""
    bot_id, _account, _oc = deploy._resolve_evolve_app_target(_LEGACY_EVOLVE_NET)
    assert bot_id == "evolve"
