"""Unit tests for pairing_chip.compute_pairing_chips.

Chip lights when a bot has a configured messaging channel but no
operator is paired into it, OR when the bot's recorded primary user
hasn't been approved into allowFrom yet.

The fixture redirects bot home dirs to tmp_path so the function's
on-disk file reads exercise the real code path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER = Path(__file__).parent.parent
if str(_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_ANALYZER))

import pairing_chip  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


def _network(tmp_path: Path, primary_external_ids: dict | None = None) -> dict:
    """Minimal network dict the chip helper consumes."""
    bot_block: dict = {"role": "member"}
    if primary_external_ids is not None:
        bot_block["primary_user"] = {
            "name": "Sam Sample",
            "external_ids": primary_external_ids,
        }
    return {
        "sharedDir": str(tmp_path / "shared"),
        "bots": {"atlas": bot_block},
    }


def _creds_dir(tmp_path: Path, bot: str) -> Path:
    d = tmp_path / "Users" / bot / ".openclaw" / "credentials"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(autouse=True)
def _redirect_bot_home(tmp_path, monkeypatch):
    """Force ``_resolve_bot_home`` to point inside tmp_path."""
    monkeypatch.setattr(
        pairing_chip, "_resolve_bot_home",
        lambda bot, network: tmp_path / "Users" / bot,
    )


# ── No configured channels ─────────────────────────────────────────────────


def test_no_pairing_files_returns_empty(tmp_path):
    network = _network(tmp_path)
    # No credentials/ dir at all → no chip; the bot doesn't have any
    # channels configured yet.
    chips = pairing_chip.compute_pairing_chips(network, "atlas")
    assert chips == []


# ── Configured but unpaired ────────────────────────────────────────────────


def test_empty_allowfrom_triggers_chip(tmp_path):
    network = _network(tmp_path)
    creds = _creds_dir(tmp_path, "atlas")
    # OC created allowFrom.json (someone tried to pair) but no one is
    # approved yet → urgent chip.
    (creds / "telegram-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": []}))
    chips = pairing_chip.compute_pairing_chips(network, "atlas")
    assert len(chips) == 1
    chip = chips[0]
    assert chip["id"] == "pairing_needed_telegram"
    assert chip["severity"] == "warn"
    assert "Telegram" in chip["label"]
    # nav uses the "pair:<bot>:<ch>" convention so chipNav routes to
    # the pairing modal.
    assert chip["nav"] == "pair:atlas:telegram"


def test_pending_only_still_triggers_chip(tmp_path):
    """Even if pairing.json exists with pending requests but allowFrom
    is missing entirely, the chip should fire (channel is configured,
    nobody approved)."""
    network = _network(tmp_path)
    creds = _creds_dir(tmp_path, "atlas")
    (creds / "telegram-pairing.json").write_text(
        json.dumps({"version": 1, "requests": [{
            "id": "1260193629", "code": "ABC",
            "meta": {"firstName": "Sam"},
        }]}))
    # No allowFrom.json at all.
    chips = pairing_chip.compute_pairing_chips(network, "atlas")
    assert any(c["id"] == "pairing_needed_telegram" for c in chips)


def test_one_approved_clears_chip(tmp_path):
    network = _network(tmp_path)
    creds = _creds_dir(tmp_path, "atlas")
    (creds / "telegram-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": ["1260193629"]}))
    chips = pairing_chip.compute_pairing_chips(network, "atlas")
    # No pairing_needed chip — admin is paired.
    assert not any(c["id"].startswith("pairing_needed_") for c in chips)


# ── Primary-user-not-paired ────────────────────────────────────────────────


def test_admin_paired_but_primary_missing_lights_primary_chip(tmp_path):
    """Admin paired during install wizard, primary user (a different
    person) hasn't paired yet — the chip should surface the gap."""
    network = _network(tmp_path, primary_external_ids={"telegram": "8888888888"})
    creds = _creds_dir(tmp_path, "atlas")
    # Admin (1260193629) is in allowFrom but primary user's ID is not.
    (creds / "telegram-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": ["1260193629"]}))
    chips = pairing_chip.compute_pairing_chips(network, "atlas")
    assert any(c["id"] == "pairing_primary_missing_telegram" for c in chips)
    primary_chip = next(
        c for c in chips if c["id"] == "pairing_primary_missing_telegram")
    # Uses the primary user's first name in the chip label
    assert "Sam" in primary_chip["label"]
    assert primary_chip["nav"] == "pair:atlas:telegram"


def test_primary_paired_quiets_primary_chip(tmp_path):
    network = _network(tmp_path, primary_external_ids={"telegram": "8888888888"})
    creds = _creds_dir(tmp_path, "atlas")
    (creds / "telegram-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": ["1260193629", "8888888888"]}))
    chips = pairing_chip.compute_pairing_chips(network, "atlas")
    assert chips == []


# ── Multiple channels ──────────────────────────────────────────────────────


def test_multi_channel_chips_independent(tmp_path):
    """Telegram and Slack chips should fire independently — pairing
    Slack doesn't clear the Telegram chip and vice versa."""
    network = _network(tmp_path)
    creds = _creds_dir(tmp_path, "atlas")
    # Telegram: configured + paired
    (creds / "telegram-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": ["1260193629"]}))
    # Slack: configured + nobody paired → chip
    (creds / "slack-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": []}))
    chips = pairing_chip.compute_pairing_chips(network, "atlas")
    assert any(c["id"] == "pairing_needed_slack" for c in chips)
    assert not any(c["id"] == "pairing_needed_telegram" for c in chips)


# ── Defensive cases ────────────────────────────────────────────────────────


def test_invalid_inputs_return_empty():
    assert pairing_chip.compute_pairing_chips(None, "atlas") == []
    assert pairing_chip.compute_pairing_chips({}, "") == []
    assert pairing_chip.compute_pairing_chips("not a dict", "atlas") == []  # type: ignore[arg-type]


def test_malformed_allowfrom_treated_as_empty(tmp_path):
    network = _network(tmp_path)
    creds = _creds_dir(tmp_path, "atlas")
    (creds / "telegram-default-allowFrom.json").write_text("not valid json {{{")
    chips = pairing_chip.compute_pairing_chips(network, "atlas")
    # File present + unparseable → treated as empty allowFrom → chip
    assert any(c["id"] == "pairing_needed_telegram" for c in chips)


# ── openclaw.json channels gate ────────────────────────────────────────────


def _write_oc_config(tmp_path: Path, bot: str, channels: dict) -> None:
    oc_dir = tmp_path / "Users" / bot / ".openclaw"
    oc_dir.mkdir(parents=True, exist_ok=True)
    (oc_dir / "openclaw.json").write_text(json.dumps({"channels": channels}))


def test_leftover_credential_files_no_chip_when_channel_unconfigured(tmp_path):
    """Repro of the phantom-chip bug: a skill uninstall left
    whatsapp-pairing.json on disk, openclaw.json no longer lists the
    channel — chip should stay silent."""
    network = _network(tmp_path)
    creds = _creds_dir(tmp_path, "atlas")
    (creds / "whatsapp-pairing.json").write_text(
        json.dumps({"version": 1, "requests": []}))
    _write_oc_config(tmp_path, "atlas", {"slack": {"enabled": True}})
    chips = pairing_chip.compute_pairing_chips(network, "atlas")
    assert chips == []


def test_disabled_channel_in_openclaw_json_no_chip(tmp_path):
    """Channel present in openclaw.json but enabled=false → no chip even
    if credential files exist."""
    network = _network(tmp_path)
    creds = _creds_dir(tmp_path, "atlas")
    (creds / "telegram-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": []}))
    _write_oc_config(tmp_path, "atlas", {"telegram": {"enabled": False}})
    assert pairing_chip.compute_pairing_chips(network, "atlas") == []


def test_configured_channel_still_fires_when_unpaired(tmp_path):
    """openclaw.json lists the channel as enabled and allowFrom is empty
    → chip fires (the normal case the gate must not break)."""
    network = _network(tmp_path)
    creds = _creds_dir(tmp_path, "atlas")
    (creds / "telegram-default-allowFrom.json").write_text(
        json.dumps({"version": 1, "allowFrom": []}))
    _write_oc_config(tmp_path, "atlas", {"telegram": {"enabled": True}})
    chips = pairing_chip.compute_pairing_chips(network, "atlas")
    assert any(c["id"] == "pairing_needed_telegram" for c in chips)
