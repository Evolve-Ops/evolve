"""Tests for evolve_admin.lifecycle.inventory.compile_bot_inventory.

Pure read-only discovery — these tests synthesize a fake bot install
on tmp_path and verify the inventory finds the expected artifacts with
the correct ``removed_by`` classifications.

Pinned behavior:
  - Per-bot launchd plists discovered and classified:
      * ai.openclaw.<bot>-gateway → ARCHIVE+DELETE (not DETACH — gateway stays on detach)
      * ai.evolve.<bot>.* → DETACH+ARCHIVE+DELETE (Evolve-installed)
      * ai.openclaw.evolve.*.<bot> → DETACH+ARCHIVE+DELETE
  - openclaw.json fragments: file is ARCHIVE+; evolve plugin block is DETACH+
  - Telegram channel emits manual_action for @BotFather cleanup
  - Backup repo URL emits manual_action and is REMOVED_BY nothing (operator's call)
  - SSH deploy key under /Users/evolve/.ssh/evolve-backup-<bot> emits manual_action
  - openclaw crons are surfaced per-job
  - Signal counts tagged with bot_id are surfaced; sweep on detach/archive
  - macOS user is only removed by DELETE
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN = Path(__file__).resolve().parent.parent
if str(_ADMIN) not in sys.path:
    sys.path.insert(0, str(_ADMIN))

from evolve_admin.lifecycle import (
    BotInventory, InventoryItem, ItemCategory, LifecycleAction,
    compile_bot_inventory,
)


# ── helpers ──────────────────────────────────────────────────────────


def _make_bot(tmp_path: Path, bot_id: str = "admin_bot") -> tuple[Path, Path]:
    """Construct (home, workspace) under tmp_path for a fake bot."""
    home = tmp_path / "Users" / bot_id
    workspace = home / ".openclaw" / "workspace"
    workspace.mkdir(parents=True)
    return home, workspace


def _write_openclaw_json(home: Path, data: dict) -> None:
    p = home / ".openclaw" / "openclaw.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))


def _resolvers(tmp_path: Path, bot_id: str = "admin_bot"):
    """Build resolver fns that route the bot's home to tmp_path."""
    def _home(bid, network):
        return tmp_path / "Users" / bid
    def _user(bid, network):
        return bid
    return _home, _user


def _basic_network(bot_id: str = "admin_bot", **bot_overrides) -> dict:
    bot_cfg = {"role": "member", "port": 18800}
    bot_cfg.update(bot_overrides)
    return {
        "members": [bot_id, "evolve"],
        "primary": "evolve",
        "bots": {bot_id: bot_cfg},
        "sharedDir": "/Users/Shared/evolve",
    }


def _by_category(inv: BotInventory, cat: ItemCategory) -> list[InventoryItem]:
    return [it for it in inv.items if it.category == cat]


def _by_name(inv: BotInventory, name_substr: str) -> list[InventoryItem]:
    return [it for it in inv.items if name_substr in it.name]


# ── basic shape ──────────────────────────────────────────────────────


def test_returns_botinventory_with_bot_id(tmp_path: Path):
    _make_bot(tmp_path)
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",  # nonexistent — OK
        home_resolver=home_r, user_resolver=user_r,
    )
    assert isinstance(inv, BotInventory)
    assert inv.bot_id == "admin_bot"
    assert inv.macos_user == "admin_bot"
    assert inv.is_primary is False


def test_primary_flag_set_correctly(tmp_path: Path):
    _make_bot(tmp_path, "evolve")
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "evolve",
        network={"members": ["evolve"], "primary": "evolve", "bots": {"evolve": {}}},
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    assert inv.is_primary is True


def test_summary_includes_per_action_counts(tmp_path: Path):
    _make_bot(tmp_path)
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    s = inv.summary
    assert "total_items" in s
    assert "removed_by_detach" in s
    assert "removed_by_archive" in s
    assert "removed_by_delete" in s
    assert "manual_cleanup_items" in s


# ── network + macOS user ─────────────────────────────────────────────


def test_network_membership_item_present(tmp_path: Path):
    _make_bot(tmp_path)
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    network_items = _by_category(inv, ItemCategory.NETWORK)
    assert len(network_items) == 1
    item = network_items[0]
    # network.json membership is removed by Archive+; Detach uses the
    # evolve_disabled flag instead.
    assert LifecycleAction.DETACH not in item.removed_by
    assert LifecycleAction.ARCHIVE in item.removed_by
    assert LifecycleAction.DELETE in item.removed_by


def test_macos_user_only_removed_by_delete(tmp_path: Path):
    _make_bot(tmp_path)
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    macos = _by_category(inv, ItemCategory.MACOS_USER)
    assert len(macos) == 1
    assert macos[0].removed_by == frozenset({LifecycleAction.DELETE})


# ── launchd plist discovery ───────────────────────────────────────────


def test_gateway_plist_archive_not_detach(tmp_path: Path):
    """ai.openclaw.<bot>-gateway is Archive+, not Detach — detach leaves
    the gateway running, so removing its plist would defeat the purpose."""
    _make_bot(tmp_path)
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    (launchd / "ai.openclaw.admin_bot-gateway.plist").write_text("")
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=launchd,
        home_resolver=home_r, user_resolver=user_r,
    )
    gateway = [it for it in _by_category(inv, ItemCategory.LAUNCHD)
               if "gateway" in it.name]
    assert len(gateway) == 1
    assert LifecycleAction.DETACH not in gateway[0].removed_by
    assert LifecycleAction.ARCHIVE in gateway[0].removed_by


def test_evolve_per_bot_plists_detach_and_up(tmp_path: Path):
    """ai.evolve.<bot>.backup and ai.openclaw.evolve.apply.<bot> are
    Evolve infra — detach removes them along with archive/delete."""
    _make_bot(tmp_path)
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    (launchd / "ai.evolve.admin_bot.backup.plist").write_text("")
    (launchd / "ai.openclaw.evolve.apply.admin_bot.plist").write_text("")
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=launchd,
        home_resolver=home_r, user_resolver=user_r,
    )
    plists = _by_category(inv, ItemCategory.LAUNCHD)
    assert len(plists) == 2
    for it in plists:
        assert LifecycleAction.DETACH in it.removed_by
        assert LifecycleAction.ARCHIVE in it.removed_by


def test_non_bot_plists_ignored(tmp_path: Path):
    """ai.openclaw.team_bot_a-gateway shouldn't show up for bot=admin_bot."""
    _make_bot(tmp_path)
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    (launchd / "ai.openclaw.team_bot_a-gateway.plist").write_text("")
    (launchd / "com.apple.something.plist").write_text("")
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=launchd,
        home_resolver=home_r, user_resolver=user_r,
    )
    assert _by_category(inv, ItemCategory.LAUNCHD) == []


# ── openclaw.json fragments ──────────────────────────────────────────


def test_openclaw_json_file_present(tmp_path: Path):
    home, _ = _make_bot(tmp_path)
    _write_openclaw_json(home, {"plugins": {"entries": {}}})
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    oc_items = _by_name(inv, "openclaw.json")
    # The file itself is one item; the plugin block may add more.
    assert any(it.name == "openclaw.json" for it in oc_items)


def test_evolve_plugin_entry_classified_as_detach(tmp_path: Path):
    home, _ = _make_bot(tmp_path)
    _write_openclaw_json(home, {
        "plugins": {"entries": {"evolve": {"some": "config"}}},
    })
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    evolve_plugin = [it for it in inv.items if "plugins.entries.evolve" in it.name]
    assert len(evolve_plugin) == 1
    assert LifecycleAction.DETACH in evolve_plugin[0].removed_by


def test_legacy_list_plugin_evolve_classified_as_detach(tmp_path: Path):
    home, _ = _make_bot(tmp_path)
    _write_openclaw_json(home, {"plugins": ["evolve", "team_bot_a-stuff"]})
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    evolve_plugin = [it for it in inv.items if "evolve entry" in it.name]
    assert len(evolve_plugin) == 1
    assert LifecycleAction.DETACH in evolve_plugin[0].removed_by


def test_no_evolve_plugin_no_detach_item(tmp_path: Path):
    home, _ = _make_bot(tmp_path)
    _write_openclaw_json(home, {"plugins": {"entries": {"other": {}}}})
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    assert not any("evolve" in it.name.lower() for it in inv.items
                   if it.category == ItemCategory.OPENCLAW_CONFIG)


# ── channels / integrations ──────────────────────────────────────────


def test_telegram_channel_emits_botfather_callout(tmp_path: Path):
    home, _ = _make_bot(tmp_path)
    _write_openclaw_json(home, {
        "channels": {
            "telegram": {"enabled": True, "botToken": "1234:abcd"},
        },
    })
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    tg = _by_name(inv, "channel:telegram")
    assert len(tg) == 1
    assert "BotFather" in tg[0].manual_action


def test_slack_channel_emits_revoke_callout(tmp_path: Path):
    home, _ = _make_bot(tmp_path)
    _write_openclaw_json(home, {
        "channels": {"slack": {"enabled": True, "token": "xoxb-..."}},
    })
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    slack = _by_name(inv, "channel:slack")
    assert len(slack) == 1
    assert "api.slack.com" in slack[0].manual_action


def test_disabled_channel_skipped(tmp_path: Path):
    home, _ = _make_bot(tmp_path)
    _write_openclaw_json(home, {
        "channels": {
            "telegram": {"enabled": False, "botToken": "1234:abcd"},
            "discord": {"enabled": True},
        },
    })
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    channels = _by_category(inv, ItemCategory.CHANNEL)
    names = {it.name for it in channels}
    assert "channel:discord" in names
    assert "channel:telegram" not in names


# ── crons ────────────────────────────────────────────────────────────


def test_openclaw_crons_surfaced_per_job(tmp_path: Path):
    home, _ = _make_bot(tmp_path)
    cron_dir = home / ".openclaw" / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_text(json.dumps({
        "jobs": [
            {"name": "morning-briefing", "schedule": "0 7 * * *", "enabled": True},
            {"name": "old-job", "schedule": "*/15 * * * *", "enabled": False},
        ],
    }))
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    crons = _by_category(inv, ItemCategory.OPENCLAW_CRON)
    assert len(crons) == 2
    # Crons stay on detach (they're the bot's own state).
    for it in crons:
        assert LifecycleAction.DETACH not in it.removed_by
        assert LifecycleAction.ARCHIVE in it.removed_by


def test_no_cron_file_no_cron_items(tmp_path: Path):
    _make_bot(tmp_path)
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    assert _by_category(inv, ItemCategory.OPENCLAW_CRON) == []


# ── credentials ──────────────────────────────────────────────────────


def test_workspace_dotenv_with_content_surfaced(tmp_path: Path):
    home, workspace = _make_bot(tmp_path)
    (workspace / ".env").write_text("SLACK_TOKEN=xoxb-...\n")
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    creds = _by_category(inv, ItemCategory.CREDENTIAL)
    env_items = [it for it in creds if ".env" in it.name]
    assert len(env_items) == 1
    assert env_items[0].manual_action  # external-cred warning present


def test_empty_credentials_dir_skipped(tmp_path: Path):
    home, workspace = _make_bot(tmp_path)
    (workspace / "credentials").mkdir()
    # empty — no content
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    assert not any("credentials" in it.name for it in
                   _by_category(inv, ItemCategory.CREDENTIAL))


def test_manifests_dir_no_external_callout(tmp_path: Path):
    """workspace/manifests/ is internal — declarative integration descriptions,
    not credentials. Surface it but no manual_action."""
    home, workspace = _make_bot(tmp_path)
    manifests = workspace / "manifests"
    manifests.mkdir()
    (manifests / "google.json").write_text("{}")
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    manifest_items = [it for it in _by_category(inv, ItemCategory.CREDENTIAL)
                      if "manifests" in it.name]
    assert len(manifest_items) == 1
    assert manifest_items[0].manual_action == ""


# ── backup ───────────────────────────────────────────────────────────


def test_backup_repo_url_surfaces_manual_action(tmp_path: Path):
    _make_bot(tmp_path)
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot",
        network=_basic_network(backupRepoUrl="git@github.com:foo/bar.git"),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    backup = _by_category(inv, ItemCategory.BACKUP)
    repo_items = [it for it in backup if it.name == "backup repo"]
    assert len(repo_items) == 1
    # Never auto-removed by any action — always operator's call.
    assert repo_items[0].removed_by == frozenset()
    assert "github" in repo_items[0].manual_action.lower()


def test_no_backup_url_no_backup_repo_item(tmp_path: Path):
    _make_bot(tmp_path)
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    backup = _by_category(inv, ItemCategory.BACKUP)
    assert not any(it.name == "backup repo" for it in backup)


# ── signals + proposals ──────────────────────────────────────────────


def _write_firing_signal(firing: Path, sig_id: str, bot_id: str) -> None:
    """Write a store-valid firing Signal so it loads through signals.store
    (Phase B reads go through iter_signals, which skips malformed records)."""
    firing.mkdir(parents=True, exist_ok=True)
    (firing / f"{sig_id}.json").write_text(json.dumps({
        "id": sig_id, "state": "firing",
        "signature": f"test_producer:test_type:{bot_id}:{sig_id}",
        "producer": "test_producer", "type": "test_type",
        "flavor": "maintenance", "severity": "warn",
        "scope": "bot", "bot_id": bot_id,
    }))


def test_signals_tagged_with_bot_id_counted(tmp_path: Path):
    _make_bot(tmp_path)
    firing = tmp_path / "shared" / "signals" / "firing"
    _write_firing_signal(firing, "sig-a", "admin_bot")
    _write_firing_signal(firing, "sig-b", "admin_bot")
    _write_firing_signal(firing, "sig-c", "team_bot_a")  # different bot
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    sigs = _by_category(inv, ItemCategory.SIGNAL)
    firing_items = [it for it in sigs if "firing" in it.name]
    assert len(firing_items) == 1
    assert "2 signal" in firing_items[0].detail


def test_proposals_targeting_bot_counted(tmp_path: Path):
    _make_bot(tmp_path)
    # Write store-valid proposals so they load through arbiter.store
    # (Phase B reads go through iter_proposals, which skips malformed
    # records). Real proposals carry bot_id; the inventory reader matches
    # on target_bot OR bot_id.
    from arbiter.state_machine import transition
    from arbiter.store import write_proposal
    from testing.harness import make_config_patch_proposal

    shared = tmp_path / "shared"
    for i, bot in enumerate(("admin_bot", "admin_bot", "team_bot_a")):
        p = make_config_patch_proposal(
            target_path=str(tmp_path / f"cfg-{i}.json"), bot_id=bot,
        )
        transition(p, "pending", actor="test", reason="seed")
        write_proposal(p, shared)
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    props = _by_category(inv, ItemCategory.PROPOSAL)
    pending_items = [it for it in props if "pending" in it.name]
    assert len(pending_items) == 1
    assert "2 proposal" in pending_items[0].detail


# ── config_intents ───────────────────────────────────────────────────


def test_config_intents_surfaced(tmp_path: Path):
    _make_bot(tmp_path)
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot",
        network=_basic_network(config_intents=[
            {"field": "tools.exec.security", "value": "full", "reason_id": "i-abc"},
        ]),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    intents = _by_category(inv, ItemCategory.INTENT)
    assert len(intents) == 1
    assert "tools.exec.security" in intents[0].name


# ── full integration ──────────────────────────────────────────────────


def test_to_dict_is_json_serializable(tmp_path: Path):
    """The CLI prints this as JSON; verify the dataclass output round-trips."""
    home, _ = _make_bot(tmp_path)
    _write_openclaw_json(home, {
        "plugins": {"entries": {"evolve": {}}},
        "channels": {"telegram": {"enabled": True, "botToken": "x"}},
    })
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=tmp_path / "LaunchDaemons",
        home_resolver=home_r, user_resolver=user_r,
    )
    # Must not raise.
    s = json.dumps(inv.to_dict())
    rt = json.loads(s)
    assert rt["bot_id"] == "admin_bot"
    assert len(rt["items"]) > 0
    # removed_by serializes as a sorted list of strings.
    for item in rt["items"]:
        assert isinstance(item["removed_by"], list)


def test_items_for_action_filters_correctly(tmp_path: Path):
    home, _ = _make_bot(tmp_path)
    _write_openclaw_json(home, {"plugins": {"entries": {"evolve": {}}}})
    launchd = tmp_path / "LaunchDaemons"
    launchd.mkdir()
    (launchd / "ai.openclaw.admin_bot-gateway.plist").write_text("")
    (launchd / "ai.evolve.admin_bot.backup.plist").write_text("")
    home_r, user_r = _resolvers(tmp_path)
    inv = compile_bot_inventory(
        "admin_bot", network=_basic_network(),
        shared_dir=tmp_path / "shared",
        launchd_dir=launchd,
        home_resolver=home_r, user_resolver=user_r,
    )

    detach_items = inv.items_for(LifecycleAction.DETACH)
    archive_items = inv.items_for(LifecycleAction.ARCHIVE)
    delete_items = inv.items_for(LifecycleAction.DELETE)

    # Each is a strict-or-equal subset of the next.
    assert len(detach_items) <= len(archive_items)
    assert len(archive_items) <= len(delete_items)

    # Detach should NOT include the gateway plist or the macOS user
    detach_names = {it.name for it in detach_items}
    assert "ai.openclaw.admin_bot-gateway" not in detach_names
    assert not any("macOS user" in n for n in detach_names)

    # But it SHOULD include the evolve infra plist and the plugin entry
    assert "ai.evolve.admin_bot.backup" in detach_names
    assert "plugins.entries.evolve" in detach_names

    # Delete includes everything detach+archive include, PLUS macOS user
    delete_names = {it.name for it in delete_items}
    assert any("macOS user" in n for n in delete_names)
