"""tests/test_user_tier_override_cli.py — where ``models cap`` / ``models
user-tier-control`` actually land.

The bug these cover: both commands used to ``write_text`` straight into
``{sharedDir}/{bot}/tiers.json``, which the gateway plugin reads only as a
back-compat FALLBACK. ``loadTiersFile`` (packages/plugin/src/observer/
ModelRouter.ts) reads ``~/.openclaw/evolve-tiers.json`` first, and every bot
on the reference pod has that file — so the CLI wrote a file routing never
consulted and printed a green check.

So the load-bearing test here is not "the seam was called": it drives the real
click command through the real ``oc_model`` writer against a fake bot home and
asserts the bytes appear at the path the PLUGIN reads — with that path derived
from ModelRouter.ts itself, so the test fails if either side moves.

WHERE it lands is this file. WHAT ROUTING THEN RESOLVES — the other half of the
same bug, and the claim the PR body made — is
``test_models_cap_reaches_routing``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ._tier_pod import (  # noqa: E402 — must follow the sys.path bootstrap
    BOT,
    make_pod,
    mirror as _mirror,
    plugin_primary_tiers_path_parts,
    run as _run,
    tiers_file as _tiers_file,
)


@pytest.fixture
def pod(tmp_path, monkeypatch):
    return make_pod(tmp_path, monkeypatch)


# ── the mismatch itself ──────────────────────────────────────────────────────

def test_cap_lands_at_the_path_the_plugin_reads(pod):
    """``models cap`` must write the file ``loadTiersFile`` reads FIRST."""
    result = _run(pod, ["models", "cap", BOT, "20"])
    assert result.exit_code == 0, result.output

    plugin_path = Path(pod["home"]).joinpath(*plugin_primary_tiers_path_parts())
    assert plugin_path.exists(), (
        f"CLI wrote nothing at {plugin_path} — the path ModelRouter.loadTiersFile "
        "reads before it ever consults the shared-dir fallback"
    )
    assert json.loads(plugin_path.read_text())["userTierOverride"]["dailyCap"] == 20


def test_user_tier_control_lands_at_the_path_the_plugin_reads(pod):
    result = _run(pod, ["models", "user-tier-control", BOT, "off"])
    assert result.exit_code == 0, result.output

    plugin_path = Path(pod["home"]).joinpath(*plugin_primary_tiers_path_parts())
    assert json.loads(plugin_path.read_text())["userTierOverride"]["enabled"] is False


def test_the_plugins_primary_path_is_still_the_one_we_target():
    """Guard the derivation above against a silent plugin-side rename."""
    assert plugin_primary_tiers_path_parts() == [".openclaw", "evolve-tiers.json"]


# ── partial-merge semantics ──────────────────────────────────────────────────

def test_cap_sends_only_dailyCap(pod):
    """The pre-migration code filled in ``enabled: True`` when the SHARED-DIR
    file lacked it — a default read off the wrong file, so a ``models cap``
    run could resurrect the chip on a bot whose canonical config had
    ``enabled: false``. Send only the field the command owns."""
    _run(pod, ["models", "cap", BOT, "20"])
    assert list(pod["seen"]["updates"]["userTierOverride"]) == ["dailyCap"]


def test_cap_does_not_reenable_a_disabled_chip(pod):
    """The above, asserted on the resulting file rather than on the call."""
    _run(pod, ["models", "user-tier-control", BOT, "off"])
    _run(pod, ["models", "cap", BOT, "20"])
    override = _tiers_file(pod)["userTierOverride"]
    assert override == {"enabled": False, "dailyCap": 20}


def test_user_tier_control_preserves_an_existing_cap(pod):
    _run(pod, ["models", "cap", BOT, "0"])
    _run(pod, ["models", "user-tier-control", BOT, "on"])
    assert _tiers_file(pod)["userTierOverride"] == {"dailyCap": 0, "enabled": True}


def test_the_write_is_scoped_to_the_named_bot(pod):
    _run(pod, ["models", "cap", BOT, "20"])
    assert pod["seen"]["bot"] == BOT
    assert pod["seen"]["network_path"] == str(pod["network_path"])


# ── the legacy mirror ────────────────────────────────────────────────────────

def test_the_shared_dir_mirror_is_still_written(pod):
    """``home_chat_routes._read_user_tier_override`` reads ONLY the shared-dir
    file, and the Power gate in /api/home/chat spends ``dailyCap`` from it.
    Dropping the mirror would move the bug rather than fix it."""
    _run(pod, ["models", "cap", BOT, "7"])
    assert _mirror(pod)["userTierOverride"]["dailyCap"] == 7


def test_the_mirror_carries_what_actually_landed(pod):
    _run(pod, ["models", "user-tier-control", BOT, "off"])
    _run(pod, ["models", "cap", BOT, "7"])
    assert _mirror(pod)["userTierOverride"] == _tiers_file(pod)["userTierOverride"]


def test_the_mirror_preserves_unrelated_keys(pod):
    mirror = pod["shared"] / BOT / "tiers.json"
    mirror.parent.mkdir(parents=True)
    mirror.write_text(json.dumps({"cascade": {"enabled": True}}))
    _run(pod, ["models", "cap", BOT, "7"])
    assert _mirror(pod)["cascade"] == {"enabled": True}


def test_the_home_chat_reader_sees_the_cap(pod):
    """End-to-end on the mirror: the CLI's value reaches the actual reader."""
    from evolve_admin.web.home_chat_routes import _read_user_tier_override

    _run(pod, ["models", "cap", BOT, "3"])
    assert _read_user_tier_override(pod["shared"], BOT)["dailyCap"] == 3


def test_a_failed_mirror_does_not_fail_a_landed_write(pod, monkeypatch):
    """The authoritative write already landed; aborting would misreport it."""
    from evolve_admin import user_tier_override

    monkeypatch.setattr(
        user_tier_override, "mirror_path",
        lambda network, bot: Path("/nonexistent-root-for-tests") / bot / "tiers.json",
    )
    result = _run(pod, ["models", "cap", BOT, "7"])
    assert result.exit_code == 0, result.output
    assert _tiers_file(pod)["userTierOverride"]["dailyCap"] == 7


# ── failure reporting ────────────────────────────────────────────────────────

def test_a_refused_write_exits_nonzero(pod, monkeypatch):
    """The old code could not fail: ``write_text`` to an evolve-owned dir
    always worked, so "success" said nothing about routing."""
    import oc_cli

    monkeypatch.setattr(
        oc_cli, "oc_full_config_set_with_error",
        lambda bot_id, updates, network_path=None: (None, "unknown bot_id"),
    )
    result = _run(pod, ["models", "cap", BOT, "20"])
    assert result.exit_code == 1
    assert "unknown bot_id" in result.output


def test_a_write_that_does_not_persist_exits_nonzero(pod, monkeypatch):
    """A truthy setter result is not proof of persistence (model_tier_apply)."""
    import oc_cli

    monkeypatch.setattr(
        oc_cli, "oc_full_config_set_with_error",
        lambda bot_id, updates, network_path=None: (
            {"bot": bot_id, "userTierOverride": {"dailyCap": 10}}, None
        ),
    )
    result = _run(pod, ["models", "cap", BOT, "20"])
    assert result.exit_code == 1
    assert "did not persist" in result.output


def test_an_unknown_field_is_refused_before_the_write(pod):
    from evolve_admin.user_tier_override import (
        UserTierOverrideWriteError,
        apply_user_tier_override,
    )

    with pytest.raises(UserTierOverrideWriteError, match="unknown"):
        apply_user_tier_override(
            {"sharedDir": str(pod["shared"])}, pod["network_path"], BOT,
            {"dailyCapp": 20},
        )
    assert pod["seen"] == {}


# ── heal drift accounting ────────────────────────────────────────────────────

def test_the_write_is_declared_so_heal_does_not_report_drift(pod):
    """evolve-tiers.json drift is namespaced ``tiers:<key>``; every writer
    self-declares (spec-delta-digest-audit-noise-2026-08-25 D3), or its own
    write reads as an unexplained hand edit forever."""
    _run(pod, ["models", "cap", BOT, "20"])
    assert len(pod["audits"]) == 1
    action, bot_id, details, oc_keys = pod["audits"][0]
    assert action == "models.user_tier_override.set"
    assert bot_id == BOT
    assert details == {"dailyCap": 20}
    assert "tiers:userTierOverride" in (oc_keys or set())
