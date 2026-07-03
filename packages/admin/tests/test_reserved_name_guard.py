"""EVO-SEP-S5: reserved service/assistant account guard.

A bot literally named ``evolve`` aliases the pod-infrastructure daemon namespace
(``ai.evolve.evolve.*``) and the Unix service user the admin server itself runs
as. Two confirmed footguns on a live pod:

  * ``delete-bot evolve`` → ``delete_user("evolve", remove_home=True)`` deletes
    the admin server's own account + home and bricks the pod.
  * ``retire-bot evolve`` / ``remove-evolve evolve`` resolve per-bot daemon
    labels that include ``ai.evolve.evolve.backup`` (the real pod-infra backup
    daemon) and boot it out.

These tests pin the two-half fix:

  A) ``retire_bot`` / ``delete_bot`` / ``remove_evolve_plugin`` hard-refuse the
     reserved ids ``{evolve, evo}`` BEFORE any archive, service teardown,
     account deletion, or network mutation — verified by tripwiring every
     destructive sink so the test errors if one is reached.
  B) the per-bot *teardown* label resolvers refuse to emit a pod-infra
     (``ai.evolve.evolve.*``) label — structurally, on both macOS and Linux
     profiles — while a normal bot id passes cleanly.

Spec: docs/spec-evo-account-separation-2026-05-25.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from click.testing import CliRunner  # noqa: E402

from evolve_admin import retire  # noqa: E402
from evolve_admin.cli import main  # noqa: E402
from evolve_admin.config import (  # noqa: E402
    RESERVED_BOT_IDS,
    is_pod_infra_label,
    is_reserved_account,
)
from evolve_admin.deploy import per_bot_evolve_plist_labels  # noqa: E402

RESERVED = sorted(RESERVED_BOT_IDS)  # ["evo", "evolve"]


# ── helpers ──────────────────────────────────────────────────────────────────


def _write_net(tmp_path: Path, *, members, bots=None) -> Path:
    """Minimal network.json. Reserved-id tests place the reserved id in
    ``members`` to mirror the live pre-separation pod (where ``evolve`` is
    simultaneously primary, member, and service user)."""
    p = tmp_path / "network.json"
    p.write_text(json.dumps({
        "networkId": "test-pod",
        "primary": members[0],
        "members": list(members),
        "sharedDir": str(tmp_path / "shared"),
        "bots": bots if bots is not None else {m: {"role": "member"} for m in members},
    }, indent=2))
    return p


def _tripwire(label: str):
    """Return a callable that fails the test if it is ever invoked — used to
    prove a destructive sink is never reached past the guard."""
    def _boom(*a, **k):
        raise AssertionError(f"reserved-name guard bypassed: {label} was invoked")
    return _boom


def _arm_retire_tripwires(monkeypatch):
    """Tripwire every destructive sink retire_bot would reach."""
    monkeypatch.setattr(retire, "generate_closure_summary", _tripwire("generate_closure_summary"))
    monkeypatch.setattr(retire, "archive_bot_data", _tripwire("archive_bot_data"))
    monkeypatch.setattr(retire, "stop_bot_services", _tripwire("stop_bot_services"))
    monkeypatch.setattr(retire, "remove_from_network", _tripwire("remove_from_network"))


# ── is_reserved_account ──────────────────────────────────────────────────────


@pytest.mark.parametrize("bot_id", ["evolve", "evo"])
def test_is_reserved_account_true_for_reserved(bot_id):
    assert is_reserved_account(bot_id) is True
    assert is_reserved_account(bot_id, {"bots": {}}) is True


@pytest.mark.parametrize("bot_id", ["ghost", "darwin", "atlas", ""])
def test_is_reserved_account_false_for_normal(bot_id):
    assert is_reserved_account(bot_id) is False


def test_is_reserved_account_resolves_alias_user(tmp_path):
    """A bot whose *id* is innocuous but whose resolved macOS user is a
    reserved account must still be refused — otherwise an alias slips past a
    name-only check and the delete still nukes the service user's home."""
    network = {
        "members": ["helper"],
        "bots": {"helper": {"role": "member", "user": "evolve"}},
    }
    assert is_reserved_account("helper", network) is True
    # And a non-aliased innocuous bot is fine.
    network2 = {"members": ["helper"], "bots": {"helper": {"role": "member", "user": "helper"}}}
    assert is_reserved_account("helper", network2) is False


# ── Half A: retire_bot / delete_bot / remove_evolve_plugin refuse ────────────


@pytest.mark.parametrize("bot_id", RESERVED)
def test_retire_bot_refuses_reserved_without_touching_sinks(tmp_path, monkeypatch, bot_id):
    net = _write_net(tmp_path, members=[bot_id, "ghost"])
    _arm_retire_tripwires(monkeypatch)

    result = retire.retire_bot(bot_id, network_path=net, shared_dir=tmp_path / "shared")

    assert result.success is False
    assert any("EVO-SEP-S5" in e or "protected" in e for e in result.errors), result.errors
    # Network.json untouched — the reserved id is still a member.
    assert bot_id in json.loads(net.read_text())["members"]
    # No archive directory was created.
    assert not (tmp_path / "shared" / "archived-bots").exists()


@pytest.mark.parametrize("bot_id", RESERVED)
def test_delete_bot_refuses_reserved_without_deleting_user(tmp_path, monkeypatch, bot_id):
    net = _write_net(tmp_path, members=[bot_id, "ghost"])
    # delete_bot calls the module-global retire_bot then the isolation seam;
    # both must be unreachable past the guard.
    monkeypatch.setattr(retire, "retire_bot", _tripwire("retire_bot"))
    from evolve_admin.runtime import isolation as _iso
    monkeypatch.setattr(_iso, "get_isolation", _tripwire("get_isolation"))

    result = retire.delete_bot(bot_id, network_path=net, shared_dir=tmp_path / "shared")

    assert result.success is False
    assert any("EVO-SEP-S5" in e or "protected" in e for e in result.errors), result.errors
    assert bot_id in json.loads(net.read_text())["members"]


@pytest.mark.parametrize("bot_id", RESERVED)
def test_delete_bot_refuses_reserved_even_when_not_in_network(tmp_path, monkeypatch, bot_id):
    """The dangerous branch: when the reserved id is NOT a network member,
    delete_bot's 'already retired' path skips retire_bot and goes straight to
    delete_user(). The guard must fire here too — before any home-existence
    check or account deletion."""
    net = _write_net(tmp_path, members=["ghost"], bots={"ghost": {"role": "member"}})
    # If the guard failed, these would be reached and the test would error.
    monkeypatch.setattr(retire, "_bot_home_exists_on_disk", _tripwire("_bot_home_exists_on_disk"))
    from evolve_admin.runtime import isolation as _iso
    monkeypatch.setattr(_iso, "get_isolation", _tripwire("get_isolation"))

    result = retire.delete_bot(bot_id, network_path=net, shared_dir=tmp_path / "shared")

    assert result.success is False
    assert any("EVO-SEP-S5" in e or "protected" in e for e in result.errors), result.errors


@pytest.mark.parametrize("bot_id", RESERVED)
def test_remove_evolve_plugin_refuses_reserved(tmp_path, monkeypatch, bot_id):
    net = _write_net(tmp_path, members=[bot_id, "ghost"])
    monkeypatch.setattr(retire, "stop_bot_services", _tripwire("stop_bot_services"))
    monkeypatch.setattr(
        retire, "_strip_evolve_plugin_from_openclaw_json",
        _tripwire("_strip_evolve_plugin_from_openclaw_json"),
    )

    result = retire.remove_evolve_plugin(bot_id, network_path=net, shared_dir=tmp_path / "shared")

    assert result.success is False
    assert any("EVO-SEP-S5" in e or "protected" in e for e in result.errors), result.errors
    # Did not mark evolve_disabled in network.json.
    bots = json.loads(net.read_text()).get("bots", {})
    assert not bots.get(bot_id, {}).get("evolve_disabled"), bots


def test_normal_bot_passes_guard(tmp_path):
    """No false positives — a normal bot id is not reserved and its per-bot
    labels carry no pod-infra collision."""
    assert is_reserved_account("darwin") is False
    labels = per_bot_evolve_plist_labels("darwin")
    assert labels and not any(is_pod_infra_label(lbl) for lbl in labels)


# ── Half B: structural label-collision guard ─────────────────────────────────


def test_is_pod_infra_label_launchd_and_systemd_forms():
    from evolve_admin.config import POD_INFRA_LABEL_PREFIX
    assert POD_INFRA_LABEL_PREFIX == "ai.evolve.evolve."
    # launchd label form
    assert is_pod_infra_label("ai.evolve.evolve.backup") is True
    assert is_pod_infra_label("ai.evolve.evolve.heal") is True
    # systemd unit-name form (Linux render = label + suffix)
    assert is_pod_infra_label("ai.evolve.evolve.backup.service") is True
    assert is_pod_infra_label("ai.evolve.evolve.heal.timer") is True
    # per-bot labels for normal/evolve-bot daemons are NOT pod-infra
    assert is_pod_infra_label("ai.evolve.ghost.backup") is False
    assert is_pod_infra_label("ai.openclaw.evolve.apply.evolve") is False
    assert is_pod_infra_label("ai.openclaw.ghost-gateway") is False


@pytest.mark.parametrize("profile_name", ["macos", "linux"])
def test_per_bot_teardown_labels_refuse_pod_infra_collision(monkeypatch, tmp_path, profile_name):
    """``_per_bot_plist_labels`` / ``_evolve_only_plist_labels`` must raise for a
    bot literally named ``evolve`` on BOTH profiles — the label set would
    include the pod-infra ``ai.evolve.evolve.backup`` daemon."""
    from platform_profile import LINUX, MACOS, set_profile
    set_profile(LINUX if profile_name == "linux" else MACOS)
    try:
        # Point the on-disk glob at an empty dir so only the deploy label set
        # contributes (the collision must come from the static label, not disk).
        empty = tmp_path / "LaunchDaemons"
        empty.mkdir()
        monkeypatch.setattr(retire, "LAUNCHD_DIR", empty)

        with pytest.raises(ValueError, match=r"ai\.evolve\.evolve\.\*"):
            retire._per_bot_plist_labels("evolve")
        with pytest.raises(ValueError, match=r"ai\.evolve\.evolve\.\*"):
            retire._evolve_only_plist_labels("evolve")
    finally:
        set_profile(MACOS)


@pytest.mark.parametrize("profile_name", ["macos", "linux"])
def test_per_bot_teardown_labels_ok_for_normal_bot(monkeypatch, tmp_path, profile_name):
    from platform_profile import LINUX, MACOS, set_profile
    set_profile(LINUX if profile_name == "linux" else MACOS)
    try:
        empty = tmp_path / "LaunchDaemons"
        empty.mkdir()
        monkeypatch.setattr(retire, "LAUNCHD_DIR", empty)

        labels = retire._per_bot_plist_labels("darwin")
        assert labels and not any(is_pod_infra_label(lbl) for lbl in labels)
        only = retire._evolve_only_plist_labels("darwin")
        assert not any(is_pod_infra_label(lbl) for lbl in only)
    finally:
        set_profile(MACOS)


def test_assert_no_pod_infra_collision_message_lists_offender():
    with pytest.raises(ValueError) as ei:
        retire._assert_no_pod_infra_collision("evolve", {"ai.evolve.evolve.backup", "ai.openclaw.x"})
    msg = str(ei.value)
    assert "ai.evolve.evolve.backup" in msg
    assert "EVO-SEP-S5" in msg


# ── CLI layer refusal ────────────────────────────────────────────────────────


@pytest.mark.parametrize("cmd", ["retire-bot", "delete-bot", "remove-evolve", "detach-bot"])
@pytest.mark.parametrize("bot_id", RESERVED)
def test_cli_lifecycle_refuses_reserved(tmp_path, cmd, bot_id):
    net = _write_net(tmp_path, members=[bot_id, "ghost"])
    runner = CliRunner()
    # --dry-run so the guard (which runs before the root check) is the only
    # thing that can stop the command; a passing guard would otherwise print
    # planning output.
    r = runner.invoke(main, ["--network", str(net), cmd, bot_id, "--dry-run"])
    assert r.exit_code == 1, r.output
    assert "protected" in r.output.lower()
    assert "EVO-SEP-S5" in r.output
    # Short-circuited before any planning / preview output.
    assert "Pre-flight inventory" not in r.output
    assert "dry-run complete" not in r.output


@pytest.mark.parametrize("bot_id", RESERVED)
def test_lifecycle_cli_refusal_message(tmp_path, bot_id):
    net = _write_net(tmp_path, members=[bot_id, "ghost"])
    msg = retire.lifecycle_cli_refusal(bot_id, net)
    assert msg and "protected" in msg and "EVO-SEP-S5" in msg


def test_lifecycle_cli_refusal_none_for_normal_bot(tmp_path):
    net = _write_net(tmp_path, members=["ghost"], bots={"ghost": {"role": "member"}})
    assert retire.lifecycle_cli_refusal("ghost", net) is None
