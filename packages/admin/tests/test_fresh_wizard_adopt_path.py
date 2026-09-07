"""`setup --fresh` is the documented front door for operators with bots.

Since PR #3813, docs/help/install-macos.md and docs/help/install-linux-vsp.md
§"I already run OpenClaw bots" send every operator with existing OpenClaw
installs to ``sudo evolve-admin setup --fresh``. The non-fresh wizard's
service-layer gate (test_setup_wizard_service_layer_gate.py) now points there
too, which makes --fresh the ONLY adopt path — so its three adopt behaviours
are load-bearing for the public install and pinned here:

  1. ``wizard.find_oc_candidates`` discovers existing installs (Step 2,
     "Bot roster") — the wizard cannot offer to adopt what it cannot see.
  2. ``setup_wizard._create_bot_account`` returns early when the account
     exists — adopting must never try to re-create a live bot's account.
  3. ``setup_wizard._setup_oc_for_bot`` leaves an existing openclaw.json
     alone — an operator's models, keys, tools and channels are not rewritten.

Each test observes the real call (the isolation seam's recorded calls, the
bytes on disk), not a raising sentinel.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evolve_admin import setup_wizard, wizard
from runtime.isolation import FakeIsolation, get_isolation, set_isolation


@pytest.fixture
def fake_isolation():
    prev = get_isolation()
    iso = FakeIsolation()
    set_isolation(iso)
    try:
        yield iso
    finally:
        set_isolation(prev)


# ── 1. discovery still finds an existing install ─────────────────────────────

def test_find_oc_candidates_discovers_an_existing_install(monkeypatch, tmp_path):
    """A home with a real openclaw.json is offered for adoption, port and all."""
    homes = tmp_path / "homes"
    (homes / "team_bot" / ".openclaw").mkdir(parents=True)
    (homes / "team_bot" / ".openclaw" / "openclaw.json").write_text(
        json.dumps({"gateway": {"port": 19004}})
    )
    # A home with no OpenClaw install must not become a candidate.
    (homes / "someone_else").mkdir()

    class _Profile:
        user_home_root = str(homes)

    monkeypatch.setattr(wizard, "get_profile", lambda: _Profile())
    monkeypatch.setattr(wizard, "_gateway_plists_by_user", dict)

    found = wizard.find_oc_candidates()

    assert [c.user for c in found] == ["team_bot"]
    assert found[0].port == 19004
    assert found[0].is_pod_member is False  # not yet claimed


def test_find_oc_candidates_flags_installs_already_in_the_pod(monkeypatch, tmp_path):
    """Re-running against a pod marks claimed installs instead of re-offering."""
    homes = tmp_path / "homes"
    (homes / "team_bot" / ".openclaw").mkdir(parents=True)
    (homes / "team_bot" / ".openclaw" / "openclaw.json").write_text(
        json.dumps({"gateway": {"port": 19004}})
    )

    class _Profile:
        user_home_root = str(homes)

    monkeypatch.setattr(wizard, "get_profile", lambda: _Profile())
    monkeypatch.setattr(wizard, "_gateway_plists_by_user", dict)

    found = wizard.find_oc_candidates(
        network={"bots": {"team_bot": {"user": "team_bot", "port": 19004}}}
    )
    assert found[0].is_pod_member is True


# ── 2. account creation is a no-op on an existing account ────────────────────

def test_create_bot_account_adopts_an_existing_account(fake_isolation, capsys):
    """The seam records nothing — adoption must not touch a live bot's account."""
    fake_isolation.seed_user("team_bot", uid=505)

    assert setup_wizard._create_bot_account("team_bot") is True
    assert fake_isolation.calls == []  # no create_user, no delete_user


def test_create_bot_account_still_creates_a_genuinely_new_account(fake_isolation):
    """The early return is scoped to existing accounts, not a blanket skip."""
    setup_wizard._create_bot_account("brand_new_bot")
    assert [c for c in fake_isolation.calls if c[0] == "create_user"]


# ── 3. an existing openclaw.json is left byte-for-byte alone ─────────────────

def _stub_privileged_calls(monkeypatch, tmp_path: Path, recorded: list):
    """Let _setup_oc_for_bot run for real, minus the sudo and the ACLs."""
    monkeypatch.setattr(setup_wizard, "user_home", lambda _u: tmp_path)
    monkeypatch.setattr(setup_wizard, "set_evolve_read_acl", lambda _b: None)

    real_run = subprocess.run

    def fake_run(argv, *a, **kw):
        recorded.append(list(argv))
        if len(argv) >= 3 and argv[:3] == ["sudo", "/bin/mkdir", "-p"]:
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
            return real_run(["true"], capture_output=True, text=True)
        if len(argv) >= 2 and argv[1] == "/bin/cat":
            # Probe for an existing auth-profiles.json — report "absent".
            return real_run(["false"], capture_output=True, text=True)
        return real_run(["true"], capture_output=True, text=True)

    monkeypatch.setattr(setup_wizard.subprocess, "run", fake_run)


def test_setup_oc_for_bot_leaves_an_existing_config_alone(monkeypatch, tmp_path):
    """The operator's models, keys, tools and channels survive adoption."""
    oc_dir = tmp_path / ".openclaw"
    oc_dir.mkdir(parents=True)
    original = json.dumps(
        {
            "agents": {"defaults": {"model": "operators-own-model"}},
            "gateway": {"port": 19004},
            "channels": {"telegram": {"enabled": True}},
        },
        indent=2,
    )
    dst = oc_dir / "openclaw.json"
    dst.write_text(original)

    recorded: list = []
    _stub_privileged_calls(monkeypatch, tmp_path, recorded)

    assert setup_wizard._setup_oc_for_bot("team_bot", 19004) is True
    assert dst.read_text() == original
    # And nothing tried to copy a fresh template over it.
    assert not [c for c in recorded if c[:2] == ["sudo", "/bin/cp"] and c[-1] == str(dst)]


def test_setup_oc_for_bot_still_writes_a_config_when_there_is_none(
    monkeypatch, tmp_path,
):
    """The skip is scoped to an existing file — a new bot still gets a config."""
    recorded: list = []
    _stub_privileged_calls(monkeypatch, tmp_path, recorded)

    assert setup_wizard._setup_oc_for_bot("brand_new_bot", 19005) is True

    dst = str(tmp_path / ".openclaw" / "openclaw.json")
    assert [c for c in recorded if c[:2] == ["sudo", "/bin/cp"] and c[-1] == dst]
