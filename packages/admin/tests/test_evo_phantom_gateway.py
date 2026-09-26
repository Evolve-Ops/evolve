"""Regression tests for EVO-LINUX-PHANTOM-GATEWAY.

A fresh evo-primary install used to provision TWO primary gateways: the
canonical per-bot ``ai.openclaw.evo-gateway`` AND a hardcoded legacy
``ai.openclaw.evolve-gateway`` (the wizard's ``_provision_evo_oc`` pinned
the label to the literal ``evolve-gateway``). Both bound the same gateway
port (:19030), so the loser crash-looped and starved the real evo gateway
— observed live on a fresh VPS (systemd restart counter reached 19, port
never bound).

The fix resolves the primary gateway label from the RESOLVED primary id —
``per_bot_gateway_plist_label(primary_bot_id(network) or "evolve")`` — at
every site that names the primary gateway (the wizard install, the expected-
plist set, the infra-job restart, the health softening):

  - evo-primary pod (``network.primary == "evo"``) → ``ai.openclaw.evo-gateway``.
  - legacy evolve-primary pod (``network.primary == "evolve"``) →
    ``ai.openclaw.evolve-gateway`` — byte-identical to the pre-account-
    separation literal, so existing macOS pods are unaffected.

These tests pin:

1. ``expected_plist_labels`` lists the RESOLVED primary gateway and NOT the
   phantom — so health/pod_health expect the gateway that actually exists.
2. ``_provision_evo_oc`` provisions exactly the resolved primary gateway
   label (``evo-gateway`` for an evo-primary install, ``evolve-gateway``
   for a legacy evolve-primary install) — never an extra 'evolve'-named one
   on evo-primary.
3. Gateway discovery resolves the primary by SHAPE (``*-gateway`` glob),
   requiring no literal ``evolve-gateway`` to exist.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolve_admin.deploy import expected_plist_labels  # noqa: E402


# ── 1. expected_plist_labels lists the resolved gateway, not the phantom ───────


def test_expected_plist_labels_evo_primary_no_phantom(tmp_path):
    """An evo-primary network expects ai.openclaw.evo-gateway and NOT the
    legacy ai.openclaw.evolve-gateway phantom."""
    net = {
        "networkId": "test-pod",
        "primary": "evo",
        "members": [],
        "bots": {"evo": {"role": "primary", "user": "evo", "port": 19030}},
        "sharedDir": str(tmp_path),
    }
    labels = expected_plist_labels(net)
    assert "ai.openclaw.evo-gateway" in labels
    assert "ai.openclaw.evolve-gateway" not in labels


def test_expected_plist_labels_legacy_evolve_primary(tmp_path):
    """A legacy evolve-primary network still expects ai.openclaw.evolve-gateway
    (no regression for existing macOS pods)."""
    net = {
        "networkId": "test-pod",
        "primary": "evolve",
        "members": [],
        "bots": {"evolve": {"role": "primary", "port": 19030}},
        "sharedDir": str(tmp_path),
    }
    labels = expected_plist_labels(net)
    assert "ai.openclaw.evolve-gateway" in labels
    assert "ai.openclaw.evo-gateway" not in labels


def test_expected_plist_labels_legacy_fallback_when_primary_unset(tmp_path):
    """A pre-`primary`-field pod with a bot literally named ``evolve`` resolves
    to evolve-gateway via primary_bot_id's legacy ``or "evolve"`` fallback."""
    net = {
        "networkId": "test-pod",
        "members": [],
        "bots": {"evolve": {"role": "primary"}},
        "sharedDir": str(tmp_path),
    }
    labels = expected_plist_labels(net)
    assert "ai.openclaw.evolve-gateway" in labels


# ── 2. _provision_evo_oc provisions exactly the resolved primary gateway ───────


@pytest.fixture
def macos_profile():
    """Pin the macOS profile so Step H takes the launchd branch deterministically
    on a Linux CI runner. Restores autodetection afterwards."""
    import platform_profile
    platform_profile.set_profile(platform_profile.MACOS)
    try:
        yield
    finally:
        platform_profile.set_profile(None)


def _provision_capturing_label(tmp_path: Path, bot_id: str, gateway_account: str):
    """Drive ``_provision_evo_oc`` with all host-touching calls mocked and
    return the launchd label it passed to ``_evolve_gateway_jobspec`` (arg 0).
    """
    from evolve_admin import setup_wizard

    home = tmp_path / "Users" / gateway_account
    captured: dict = {}

    def fake_run(argv, *a, **k):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def capture_jobspec(label, *a, **k):
        captured["label"] = label
        return MagicMock()

    sched = MagicMock()
    sched.status.return_value = {"managed": False}
    sched.raw.return_value = (0, "", "")
    sched.restart.return_value = (True, "")

    auth_profiles = {
        "version": 1,
        "profiles": {"anthropic": {"provider": "anthropic"}},
        "lastGood": {},
    }

    with patch("subprocess.run", side_effect=fake_run), \
         patch.object(setup_wizard, "user_home", lambda acct: home), \
         patch.object(setup_wizard, "_create_bot_account", return_value=True), \
         patch.object(setup_wizard, "_provision_evo_account", return_value=True), \
         patch.object(setup_wizard, "_log_admin_action"), \
         patch.object(setup_wizard, "_select_api_keys_for_evolve",
                      return_value=auth_profiles), \
         patch.object(setup_wizard, "_embedding_chain_for_credentials",
                      return_value=["openai"]), \
         patch.object(setup_wizard, "_evolve_openclaw_config",
                      return_value={"gateway": {"port": 19030,
                                                "auth": {"token": "tok"}}}), \
         patch.object(setup_wizard, "_evolve_gateway_jobspec",
                      side_effect=capture_jobspec), \
         patch.object(setup_wizard, "render_launchd_plist", return_value="plist"), \
         patch.object(setup_wizard, "get_launchd_scheduler", return_value=sched):
        ok = setup_wizard._provision_evo_oc(
            "testpod", tmp_path / "shared", "pod-admin", [], True,
            telegram_token="dummy", bot_id=bot_id, gateway_account=gateway_account,
        )
    return ok, captured.get("label")


def test_provision_evo_primary_installs_evo_gateway_not_phantom(tmp_path, macos_profile):
    """Fresh evo-primary install (bot_id='evo') provisions ai.openclaw.evo-gateway
    — NOT the legacy ai.openclaw.evolve-gateway phantom that crash-loops."""
    ok, label = _provision_capturing_label(tmp_path, bot_id="evo",
                                           gateway_account="evolve")
    assert ok is True
    assert label == "ai.openclaw.evo-gateway"
    assert label != "ai.openclaw.evolve-gateway"


def test_provision_legacy_evolve_primary_still_installs_evolve_gateway(tmp_path, macos_profile):
    """A legacy evolve-primary install (bot_id='evolve') still provisions
    ai.openclaw.evolve-gateway — byte-identical, no regression."""
    ok, label = _provision_capturing_label(tmp_path, bot_id="evolve",
                                           gateway_account="evolve")
    assert ok is True
    assert label == "ai.openclaw.evolve-gateway"


# ── 3. Gateway discovery resolves the primary by shape, not literal ───────────


def test_discovery_finds_primary_gateway_by_shape(tmp_path):
    """``_discover_bot_gateways`` finds the primary's gateway via the bot-id-
    agnostic ``*-gateway`` glob — an evo-primary pod needs no literal
    ``evolve-gateway`` plist for discovery to work."""
    from evolve_admin import repo_puller

    (tmp_path / "ai.openclaw.evo-gateway.plist").write_text("x")
    (tmp_path / "ai.openclaw.team-bot-a-gateway.plist").write_text("x")

    found = repo_puller._discover_bot_gateways(launchd_dir=tmp_path)
    assert "ai.openclaw.evo-gateway" in found
    assert "ai.openclaw.team-bot-a-gateway" in found
    # The primary gateway resolves back to the primary bot id by shape.
    assert repo_puller._short_bot_name("ai.openclaw.evo-gateway") == "evo"


# ── 4. Disk-discovered gateways are reconciled against the pod roster ──────────
#
# macOS sibling of EVO-GATEWAY-RESIDUE-RERENDER (PR #3279, which gated the Linux
# CLI/provisioning path). On macOS the residue path is the repo-puller: a stale
# ``ai.openclaw.<non-member>-gateway.plist`` left on disk (renamed/removed bot)
# is discovered by the bot-id-agnostic ``*-gateway`` glob and would be
# kickstarted every plugin rebuild — re-arming a dead gateway. The puller now
# reconciles the discovered labels against ``network["members"]`` ∪ the resolved
# primary and restarts only roster members. (Complementary to the #3167
# orphan-sweeper, which REMOVES such units; this declines to RESTART them in the
# window before a sweep.)


def test_reconcile_drops_nonmember_keeps_members_and_primary():
    """A stale non-member plist is reconciled OUT of the restart set while
    every roster member AND the resolved primary stay in — even when the
    primary isn't listed in the ``members`` array (it's unioned in)."""
    from evolve_admin import repo_puller

    # primary "evo" is NOT in members[] — must still be kept via the union.
    network = {
        "networkId": "test-pod",
        "primary": "evo",
        "members": ["darwin"],
        "bots": {
            "evo": {"role": "primary", "user": "evolve", "port": 19030},
            "darwin": {"port": 19000},
        },
        "sharedDir": "/tmp/x",
    }
    labels = [
        "ai.openclaw.darwin-gateway",       # member
        "ai.openclaw.evo-gateway",          # resolved primary (not in members[])
        "ai.openclaw.oldbot-gateway",       # stale residue — non-member
    ]
    kept, skipped = repo_puller._reconcile_gateways_to_roster(labels, network=network)

    assert "ai.openclaw.darwin-gateway" in kept
    assert "ai.openclaw.evo-gateway" in kept
    assert "ai.openclaw.oldbot-gateway" not in kept
    assert skipped == ["ai.openclaw.oldbot-gateway"]


def test_reconcile_legacy_pod_no_members_falls_back_to_bots_keys():
    """A legacy pod with no ``members`` array reconciles against ``bots.keys()``
    (∪ the legacy ``evolve`` primary fallback) — a real bot is kept, a phantom
    is dropped."""
    from evolve_admin import repo_puller

    network = {
        "networkId": "legacy-pod",
        "bots": {"evolve": {"role": "primary"}, "team-bot-a": {"port": 19001}},
    }
    labels = [
        "ai.openclaw.evolve-gateway",   # legacy primary
        "ai.openclaw.team-bot-a-gateway",      # member via bots.keys()
        "ai.openclaw.ghost-gateway",    # not a bot — residue
    ]
    kept, skipped = repo_puller._reconcile_gateways_to_roster(labels, network=network)

    assert "ai.openclaw.evolve-gateway" in kept
    assert "ai.openclaw.team-bot-a-gateway" in kept
    assert skipped == ["ai.openclaw.ghost-gateway"]


def test_reconcile_keeps_all_when_roster_unknown():
    """If the roster can't be determined (no readable network.json), the puller
    keeps every discovered label rather than filtering blind — over-restarting a
    real member is harmless, starving one of its restart is not."""
    from evolve_admin import deploy, repo_puller

    labels = ["ai.openclaw.darwin-gateway", "ai.openclaw.oldbot-gateway"]
    with patch.object(deploy, "load_network", side_effect=OSError("no network.json")):
        kept, skipped = repo_puller._reconcile_gateways_to_roster(labels, network=None)

    assert kept == labels
    assert skipped == []
