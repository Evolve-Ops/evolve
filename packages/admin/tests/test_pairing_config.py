"""Unit tests for evolve_admin.pairing.config.

Validators, deeplink builder, and the per-channel table. No Flask, no
filesystem. Anything here changing breaks every downstream consumer
(install wizard, pairing modal, tile chip) so the bar for these is
strict.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
if str(_ADMIN) not in sys.path:
    sys.path.insert(0, str(_ADMIN))

from evolve_admin.pairing.config import (  # noqa: E402
    ChannelConfig,
    all_ui_dicts,
    get_channel_config,
    known_channels,
)


def test_known_channels_includes_all_four():
    assert set(known_channels()) == {"telegram", "slack", "discord", "whatsapp"}


def test_get_channel_config_case_insensitive():
    assert get_channel_config("telegram") is not None
    assert get_channel_config("Telegram") is not None
    assert get_channel_config("TELEGRAM") is not None


def test_get_channel_config_unknown_returns_none():
    assert get_channel_config("signal") is None
    assert get_channel_config("") is None
    assert get_channel_config(None) is None  # type: ignore[arg-type]


# ── Telegram validator ──────────────────────────────────────────────────

def test_telegram_validator_accepts_numeric_user_ids():
    cfg = get_channel_config("telegram")
    assert cfg.validate_id("1260193629") is True
    assert cfg.validate_id("123456") is True   # 6-digit floor
    assert cfg.validate_id("123456789012") is True  # 12-digit ceiling


def test_telegram_validator_rejects_obvious_garbage():
    cfg = get_channel_config("telegram")
    assert cfg.validate_id("12345") is False  # too short
    assert cfg.validate_id("1234567890123") is False  # too long
    assert cfg.validate_id("+1260193629") is False  # has + (mistakenly E.164)
    assert cfg.validate_id("12-34-56") is False
    assert cfg.validate_id("") is False
    assert cfg.validate_id("abcdefghij") is False


def test_telegram_validator_trims_whitespace():
    cfg = get_channel_config("telegram")
    assert cfg.validate_id("  1260193629  ") is True


# ── Slack validator ─────────────────────────────────────────────────────

def test_slack_validator_accepts_member_ids():
    cfg = get_channel_config("slack")
    assert cfg.validate_id("U01ABCDE2FG") is True
    assert cfg.validate_id("U01ABCDE2") is True
    assert cfg.validate_id("W01ABCDE2FG") is True  # workspace bot prefix


def test_slack_validator_rejects_lowercase_and_email():
    cfg = get_channel_config("slack")
    assert cfg.validate_id("u01abcde2fg") is False
    assert cfg.validate_id("user@example.com") is False
    assert cfg.validate_id("U01") is False  # too short
    assert cfg.validate_id("") is False


# ── Discord validator ───────────────────────────────────────────────────

def test_discord_validator_accepts_snowflakes():
    cfg = get_channel_config("discord")
    # 17, 18, 19 digit boundaries (Discord's epoch advances over time)
    assert cfg.validate_id("12345678901234567") is True
    assert cfg.validate_id("123456789012345678") is True
    assert cfg.validate_id("1234567890123456789") is True


def test_discord_validator_rejects_short_and_alpha():
    cfg = get_channel_config("discord")
    assert cfg.validate_id("12345") is False
    assert cfg.validate_id("12345678901234567890") is False  # 20 digits
    assert cfg.validate_id("U01ABCDE2FG") is False  # Slack-shape


# ── WhatsApp validator ──────────────────────────────────────────────────

def test_whatsapp_validator_accepts_e164():
    cfg = get_channel_config("whatsapp")
    assert cfg.validate_id("+14155551234") is True
    assert cfg.validate_id("+447911123456") is True
    assert cfg.validate_id("+8613812345678") is True


def test_whatsapp_validator_rejects_missing_plus_or_short():
    cfg = get_channel_config("whatsapp")
    assert cfg.validate_id("14155551234") is False  # no +
    assert cfg.validate_id("+1415555") is False     # too short
    assert cfg.validate_id("+") is False
    assert cfg.validate_id("") is False


# ── Telegram deeplink ───────────────────────────────────────────────────

def test_telegram_deeplink_with_token():
    cfg = get_channel_config("telegram")
    url = cfg.deeplink_for("atlas_bot", token="pair-7K3M")
    assert url == "https://t.me/atlas_bot?start=pair-7K3M"


def test_telegram_deeplink_strips_leading_at_sign():
    cfg = get_channel_config("telegram")
    url = cfg.deeplink_for("@atlas_bot", token="x")
    assert url == "https://t.me/atlas_bot?start=x"


def test_telegram_deeplink_without_token_drops_query_param():
    cfg = get_channel_config("telegram")
    url = cfg.deeplink_for("atlas_bot")
    # No ?start= dangling — that would look broken.
    assert url == "https://t.me/atlas_bot"


def test_telegram_deeplink_returns_none_without_username():
    cfg = get_channel_config("telegram")
    assert cfg.deeplink_for(None) is None
    assert cfg.deeplink_for("") is None


def test_non_telegram_channels_have_no_deeplink():
    for ch in ("slack", "discord", "whatsapp"):
        cfg = get_channel_config(ch)
        assert cfg.deeplink_for("anything", token="x") is None


# ── UI dict shape ───────────────────────────────────────────────────────

def test_to_ui_dict_includes_required_keys():
    cfg = get_channel_config("telegram")
    d = cfg.to_ui_dict()
    for k in ("channel", "label", "id_label", "id_format_hint",
              "id_validator_pattern", "discovery_method",
              "has_deeplink", "open_button_label"):
        assert k in d, f"missing key {k}"


def test_all_ui_dicts_preserves_order():
    dicts = all_ui_dicts()
    assert [d["channel"] for d in dicts] == known_channels()


def test_to_ui_dict_deeplink_flag_matches_template():
    assert get_channel_config("telegram").to_ui_dict()["has_deeplink"] is True
    for ch in ("slack", "discord", "whatsapp"):
        assert get_channel_config(ch).to_ui_dict()["has_deeplink"] is False


# ── ChannelConfig constructor ───────────────────────────────────────────

def test_channel_config_rejects_unknown_discovery():
    with pytest.raises(ValueError):
        ChannelConfig(
            channel="x", label="X", id_label="x",
            id_format_hint="", id_validator_pattern=r"^\d+$",
            discovery_method="bogus", deeplink_template=None,
            open_button_label="x",
        )


def test_channel_config_deeplink_method_requires_template():
    with pytest.raises(ValueError):
        ChannelConfig(
            channel="x", label="X", id_label="x",
            id_format_hint="", id_validator_pattern=r"^\d+$",
            discovery_method="deeplink", deeplink_template=None,
            open_button_label="x",
        )
