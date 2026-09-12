"""M1-B1 safety proof — every converted consumer's derived set equals the
literal set it carried before the channel registry existed.

Each test below pins the PRE-REFACTOR literal (copied verbatim from the
module it replaced, with the source line noted) and asserts the registry
projection reproduces it. Deliberate widenings are called out explicitly and
assert the *superset* relation plus a value-level no-change proof, so a
reviewer can see exactly what moved and why.

This file is the reason the refactor is reviewable: if a future registry edit
would silently change a consumer's set, it fails here first.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN = Path(__file__).parent.parent
if str(_ADMIN) not in sys.path:
    sys.path.insert(0, str(_ADMIN))

from evolve_admin import channel_registry as cr  # noqa: E402


# ── notify priority (2 consumers, one column) ────────────────────────────

# breakers_enforce.py:68 (pre-refactor) and alerts/dispatcher.py:384
# (pre-refactor) — byte-identical tuples maintained by hand in two files.
_LEGACY_NOTIFY_PRIORITY = ("telegram", "signal", "whatsapp", "slack", "discord")


def test_breakers_user_channel_priority_projection_is_unchanged():
    from evolve_admin import breakers_enforce

    assert breakers_enforce._USER_CHANNEL_PRIORITY == _LEGACY_NOTIFY_PRIORITY


def test_dispatcher_default_channel_priority_projection_is_unchanged():
    from evolve_admin.alerts import dispatcher

    assert dispatcher._DEFAULT_CHANNEL_PRIORITY == _LEGACY_NOTIFY_PRIORITY


def test_the_two_priority_tables_are_now_identical_by_construction():
    """They were identical by coincidence (two hand-maintained tuples with a
    "keep in sync" comment). Post-B1 they are the same derivation."""
    from evolve_admin import breakers_enforce
    from evolve_admin.alerts import dispatcher

    assert (
        breakers_enforce._USER_CHANNEL_PRIORITY
        == dispatcher._DEFAULT_CHANNEL_PRIORITY
        == cr.by_notify_priority()
    )


# ── pairing config (pairing/config.py:163 `_CHANNELS`) ───────────────────

# The four channels pairing/config._CHANNELS carried, in its declared order.
_LEGACY_PAIRING_CHANNELS = ["telegram", "slack", "discord", "whatsapp"]


def test_pairing_known_channels_projection_is_unchanged():
    from evolve_admin.pairing import config as pairing_config

    assert pairing_config.known_channels() == _LEGACY_PAIRING_CHANNELS


def test_pairing_ui_dicts_are_byte_identical_to_the_pre_refactor_rows():
    """The admin-UI bundle serializes these verbatim — any drift in a hint
    string or validator pattern is a visible UI change."""
    from evolve_admin.pairing import config as pairing_config

    rows = {d["channel"]: d for d in pairing_config.all_ui_dicts()}
    assert list(rows) == _LEGACY_PAIRING_CHANNELS
    assert rows["telegram"] == {
        "channel": "telegram",
        "label": "Telegram",
        "id_label": "Telegram user ID",
        "id_format_hint": (
            "6-12 digit number. If you don't know yours, DM the bot "
            "and paste the code it replies with."
        ),
        "id_validator_pattern": r"^\d{6,12}$",
        "discovery_method": "deeplink",
        "has_deeplink": True,
        "open_button_label": "Open in Telegram",
    }
    assert rows["slack"] == {
        "channel": "slack",
        "label": "Slack",
        "id_label": "Slack member ID",
        "id_format_hint": (
            "Starts with U (e.g. U01ABCDE2FG). Find it in your "
            "Slack profile → ⋮ menu → Copy member ID."
        ),
        "id_validator_pattern": r"^[UW][A-Z0-9]{8,10}$",
        "discovery_method": "dm_bot",
        "has_deeplink": False,
        "open_button_label": "Copy bot handle",
    }
    assert rows["discord"] == {
        "channel": "discord",
        "label": "Discord",
        "id_label": "Discord user ID",
        "id_format_hint": (
            "17-19 digit snowflake. Enable Developer Mode in "
            "Settings → Advanced, then right-click yourself → Copy User ID."
        ),
        "id_validator_pattern": r"^\d{17,19}$",
        "discovery_method": "dm_bot",
        "has_deeplink": False,
        "open_button_label": "Copy bot handle",
    }
    assert rows["whatsapp"] == {
        "channel": "whatsapp",
        "label": "WhatsApp",
        "id_label": "WhatsApp phone number",
        "id_format_hint": (
            "E.164 format with country code, e.g. +14155551234. "
            "It's your own phone number — no need to look anything up."
        ),
        "id_validator_pattern": r"^\+\d{10,15}$",
        "discovery_method": "dm_bot",
        "has_deeplink": False,
        "open_button_label": "Copy bot handle",
    }


def test_pairing_lookup_returns_none_for_registry_channels_without_pairing():
    """Signal has a registry row but no pairing flow — the pairing view must
    keep saying "no" (routes_bot_users hides it from the Users UI)."""
    from evolve_admin.pairing import config as pairing_config

    assert cr.get("signal") is not None
    assert pairing_config.get_channel_config("signal") is None
    assert pairing_config.get_channel_config("imessage") is None


# ── the four identical pairing tuples ────────────────────────────────────

# Four modules each carried this same tuple with a "mirrors X — keep in sync"
# comment: setup_checklist.py:132 `_PAIRING_CHANNELS`,
# web/routes_bot_users.py:87 `KNOWN_PROVIDERS`,
# roster_resolver.py:59 `ROSTER_CHANNELS`,
# evo/tools/action_roster.py:55 `KNOWN_CHANNELS`.
_LEGACY_PAIRING_TUPLE = ("telegram", "slack", "discord", "whatsapp")


def test_setup_checklist_pairing_channels_projection_is_unchanged():
    from evolve_admin import setup_checklist

    assert setup_checklist._PAIRING_CHANNELS == _LEGACY_PAIRING_TUPLE


def test_routes_bot_users_known_providers_projection_is_unchanged():
    from evolve_admin.web import routes_bot_users

    assert routes_bot_users.KNOWN_PROVIDERS == _LEGACY_PAIRING_TUPLE


def test_roster_resolver_channels_projection_is_unchanged():
    from evolve_admin import roster_resolver

    assert roster_resolver.ROSTER_CHANNELS == _LEGACY_PAIRING_TUPLE


def test_action_roster_known_channels_projection_is_unchanged():
    from evolve_admin.evo.tools import action_roster

    assert action_roster.KNOWN_CHANNELS == _LEGACY_PAIRING_TUPLE


def test_signal_stays_out_of_the_pairing_projection():
    """routes_bot_users has deliberately hidden Signal from the Users UI
    since 2026-05-29. The registry must not quietly re-admit it."""
    assert "signal" not in cr.pairing_channel_ids()


# ── name resolution (evo/name_resolver.py:57 `SUPPORTED_CHANNELS`) ───────

_LEGACY_NAME_RESOLUTION = frozenset({"telegram", "slack", "discord"})


def test_name_resolver_supported_channels_projection_is_unchanged():
    from evolve_admin.evo import name_resolver

    assert name_resolver.SUPPORTED_CHANNELS == _LEGACY_NAME_RESOLUTION


def test_user_resolver_inherits_the_same_narrow_set():
    from evolve_admin.evo import user_resolver

    assert user_resolver.SUPPORTED_USER_PLATFORMS == _LEGACY_NAME_RESOLUTION


def test_whatsapp_and_signal_stay_out_of_name_resolution():
    """The canonical narrowness case from the M1 design record: both are
    fully supported channels with no name-resolution adapter. If the registry
    ever widened this set, name resolution would fail silently on them."""
    assert "whatsapp" not in cr.name_resolution_channels()
    assert "signal" not in cr.name_resolution_channels()
    assert cr.get("whatsapp") is not None and cr.get("signal") is not None


# ── verify handlers (wizard_verify.py:968 `_CHANNEL_HANDLERS`) ───────────

_LEGACY_VERIFY_HANDLERS = {
    "telegram": "_check_telegram_token",
    "slack":    "_check_slack_token",
    "discord":  "_check_discord_token",
    "imessage": "_check_imessage_local",
    "whatsapp": "_check_whatsapp_local",
    "signal":   "_check_signal_local",
}


def test_wizard_verify_channel_handlers_projection_is_unchanged():
    from evolve_admin import wizard_verify

    assert wizard_verify._CHANNEL_HANDLERS == _LEGACY_VERIFY_HANDLERS


def test_every_declared_verify_handler_actually_exists():
    """The registry's ``verify_handler`` column names functions in
    wizard_verify. Nothing dispatched through this map before B1 (the
    executor uses a literal if/elif chain), so a typo would have been
    invisible — this makes the column load-bearing."""
    from evolve_admin import wizard_verify

    for channel, fn_name in cr.verify_handlers().items():
        assert callable(getattr(wizard_verify, fn_name, None)), (
            f"{channel}: registry names {fn_name!r} but wizard_verify has no "
            "such callable"
        )


# ── labels — three DELIBERATE WIDENINGS, each proved harmless ────────────

# safety_summary.py:48 `_CHANNEL_LABEL` (prose form).
_LEGACY_SAFETY_LABELS = {
    "telegram":  "Telegram",
    "slack":     "Slack",
    "discord":   "Discord",
    "email":     "email",
    "sms":       "SMS",
    "whatsapp":  "WhatsApp",
    "signal":    "Signal",
    "webhook":   "a configured webhook",
}


def test_safety_summary_labels_are_unchanged_for_every_legacy_key():
    from evolve_admin import safety_summary

    got = safety_summary._CHANNEL_LABEL
    for cid, label in _LEGACY_SAFETY_LABELS.items():
        assert got[cid] == label, f"{cid}: prose label drifted"


def test_safety_summary_labels_widen_by_exactly_imessage():
    """WIDENING: the legacy table omitted iMessage, so the summary rendered
    the ``.title()`` fallback "Imessage". Label-only; no membership logic
    reads this dict (safety_summary.py:200 is a ``.get`` with a fallback)."""
    from evolve_admin import safety_summary

    added = set(safety_summary._CHANNEL_LABEL) - set(_LEGACY_SAFETY_LABELS)
    assert added == {"imessage"}
    assert safety_summary._CHANNEL_LABEL["imessage"] == "iMessage"


# evo/wizard/prompts.py:2577 `_CHANNEL_DISPLAY_NAMES` (product-name form).
_LEGACY_WIZARD_DISPLAY_NAMES = {
    "telegram": "Telegram",
    "slack": "Slack",
    "discord": "Discord",
    "imessage": "iMessage",
    "whatsapp": "WhatsApp",
    "sms": "SMS",
}


def test_wizard_display_names_are_unchanged_for_every_legacy_key():
    from evolve_admin.evo.wizard import prompts

    for cid, label in _LEGACY_WIZARD_DISPLAY_NAMES.items():
        assert prompts._channel_display(cid) == label


def test_wizard_display_name_widening_is_a_provable_no_op():
    """WIDENING: 6 keys → 9. Each added key renders exactly what the
    pre-existing ``.capitalize()`` fallback already produced, so no rendered
    string changes anywhere."""
    from evolve_admin.evo.wizard import prompts

    added = set(prompts._CHANNEL_DISPLAY_NAMES) - set(_LEGACY_WIZARD_DISPLAY_NAMES)
    assert added == {"signal", "email", "webhook"}
    for cid in added:
        assert prompts._CHANNEL_DISPLAY_NAMES[cid] == cid.capitalize()


def test_wizard_display_fallback_for_unknown_ids_still_works():
    from evolve_admin.evo.wizard import prompts

    assert prompts._channel_display("mattermost") == "Mattermost"
    assert prompts._channel_display("") == "the channel"


# bot_templates/validator.py:50 `_KNOWN_CHANNEL_PATTERNS` (advisory, warn-only).
_LEGACY_CHANNEL_PATTERNS = frozenset({
    "any-messaging", "slack", "telegram", "discord", "signal", "email", "none",
})


def test_template_channel_patterns_are_a_superset_of_the_legacy_set():
    """WIDENING: warn-only advisory list. It covered 5 of 9 channels, so a
    template targeting WhatsApp/iMessage/SMS/webhook drew a spurious
    "unknown channel" warning. Widening removes false positives and cannot
    block anything (validator.py:227 appends a warning, never an error)."""
    from evolve_admin.bot_templates import validator

    assert _LEGACY_CHANNEL_PATTERNS <= validator._KNOWN_CHANNEL_PATTERNS
    added = validator._KNOWN_CHANNEL_PATTERNS - _LEGACY_CHANNEL_PATTERNS
    assert added == {"whatsapp", "imessage", "sms", "webhook"}


def test_template_channel_pattern_sentinels_are_not_registry_channels():
    from evolve_admin.bot_templates import validator

    assert not (validator._CHANNEL_PATTERN_SENTINELS & cr.known_ids())


# applications/coherence_pass_a.py:172 `_MESSAGING_INTEGRATION_IDS`.
_LEGACY_MESSAGING_INTEGRATION_IDS = frozenset({
    "slack", "telegram", "discord", "imessage", "whatsapp",
    "signal", "email", "gmail", "twilio", "sms",
})


def test_messaging_integration_ids_projection_is_unchanged():
    """NOT widened — the C-A4 coherence gate blocks app approvals on this
    set, so it stays byte-identical. ``webhook`` is excluded by the registry
    column (a delivery sink, not a channel); the two vendor aliases
    (gmail/twilio) have no registry row and stay local."""
    from evolve_admin.applications import coherence_pass_a

    assert coherence_pass_a._MESSAGING_INTEGRATION_IDS == _LEGACY_MESSAGING_INTEGRATION_IDS
    assert coherence_pass_a.MESSAGING_INTEGRATION_IDS == _LEGACY_MESSAGING_INTEGRATION_IDS
    assert "webhook" not in coherence_pass_a._MESSAGING_INTEGRATION_IDS
