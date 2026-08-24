"""Unit tests for evolve_admin.channel_registry — the single channel table.

Two jobs:

1. **Registry integrity** — the selection API behaves, the rows are
   self-consistent, and the install metadata agrees with OpenClaw's own
   committed channel snapshot (docs/skills/oc-channel-coverage.json).
2. **Projection equality** — every consumer's derived set equals the literal
   set that consumer carried before M1-B1. Those live in
   ``test_channel_registry_projections.py`` (one test per converted consumer)
   so a reviewer can read the refactor's safety proof in one file.

No Flask, no filesystem writes, no subprocess.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN = Path(__file__).parent.parent
if str(_ADMIN) not in sys.path:
    sys.path.insert(0, str(_ADMIN))

from evolve_admin import channel_registry as cr  # noqa: E402

_REPO_ROOT = _ADMIN.parent.parent


# ── Shape / integrity ────────────────────────────────────────────────────


def test_ids_are_unique_lowercase_and_nonempty():
    ids = [c.id for c in cr.all_channels()]
    assert len(ids) == len(set(ids)), "duplicate channel id in registry"
    for cid in ids:
        assert cid and cid == cid.lower().strip()


def test_get_is_case_insensitive_and_none_for_unknown():
    assert cr.get("TELEGRAM") is cr.get("telegram")
    assert cr.get("  Slack ").id == "slack"
    assert cr.get("nope") is None
    assert cr.get(None) is None  # type: ignore[arg-type]


def test_every_row_has_a_display_label():
    for c in cr.all_channels():
        assert c.display_label.strip(), f"{c.id} has no display_label"
        assert c.prose_label.strip(), f"{c.id} has no prose_label"


def test_prose_label_defaults_to_display_label():
    assert cr.get("telegram").prose_label == "Telegram"
    # The two deliberate overrides.
    assert cr.get("email").prose_label == "email"
    assert cr.get("webhook").prose_label == "a configured webhook"


def test_pairing_row_channel_matches_row_id():
    for c in cr.all_channels():
        if c.pairing is not None:
            assert c.pairing.channel == c.id


def test_notify_priorities_are_unique_and_positive():
    prios = [
        c.notify_priority for c in cr.all_channels()
        if c.notify_priority is not None
    ]
    assert prios, "no channel carries a notify priority"
    assert len(prios) == len(set(prios)), "duplicate notify_priority"
    assert all(p >= 1 for p in prios)


def test_install_class_and_plugin_id_agree():
    for c in cr.all_channels():
        if c.install is None:
            assert c.oc_plugin_id is None, f"{c.id}: plugin id without install"
        else:
            assert c.oc_plugin_id, f"{c.id}: install class without plugin id"


def test_constructor_rejects_bad_install_class():
    with pytest.raises(ValueError):
        cr.ChannelSpec(id="x", display_label="X", install="wat")


def test_constructor_rejects_mismatched_pairing_channel():
    cfg = cr.ChannelConfig(
        channel="other", label="Other", id_label="id", id_format_hint="h",
        id_validator_pattern=r"^\d+$", discovery_method="dm_bot",
        deeplink_template=None, open_button_label="Copy",
    )
    with pytest.raises(ValueError):
        cr.ChannelSpec(id="x", display_label="X", pairing=cfg)


def test_constructor_rejects_plugin_id_without_install():
    with pytest.raises(ValueError):
        cr.ChannelSpec(id="x", display_label="X", oc_plugin_id="@openclaw/x")


# ── Selection API ────────────────────────────────────────────────────────


def test_where_preserves_display_order():
    subset = cr.ids_where(lambda c: c.supports_pairing)
    full = [c.id for c in cr.all_channels()]
    assert list(subset) == [i for i in full if i in set(subset)]


def test_by_notify_priority_is_sorted_by_the_priority_column():
    ordered = cr.by_notify_priority()
    prios = [cr.get(i).notify_priority for i in ordered]
    assert prios == sorted(prios)


def test_labels_where_prose_flag_switches_column():
    plain = cr.labels_where(lambda c: c.id == "email")
    prose = cr.labels_where(lambda c: c.id == "email", prose=True)
    assert plain == {"email": "Email"}
    assert prose == {"email": "email"}


def test_display_label_falls_back_to_title_case_for_unknown_ids():
    assert cr.display_label("mattermost") == "Mattermost"
    assert cr.display_label("telegram") == "Telegram"
    assert cr.display_label("email", prose=True) == "email"


def test_known_ids_matches_all_channels():
    assert cr.known_ids() == frozenset(c.id for c in cr.all_channels())


# ── Capability columns are narrow ON PURPOSE ─────────────────────────────


def test_name_resolution_is_a_strict_subset_of_all_channels():
    """The regression this registry exists to prevent: a consumer asking for
    "all channels" and silently claiming capability it does not have."""
    assert cr.name_resolution_channels() < cr.known_ids()


def test_channels_without_a_verify_handler_are_excluded_from_the_map():
    handlers = cr.verify_handlers()
    for c in cr.all_channels():
        assert (c.id in handlers) == (c.verify_handler is not None)


def test_webhook_is_not_a_messaging_integration():
    """A webhook is a delivery sink, not a channel a person is reachable on."""
    assert "webhook" in cr.known_ids()
    assert "webhook" not in cr.messaging_integration_ids()


# ── Agreement with OpenClaw's own channel snapshot ───────────────────────


def _oc_snapshot() -> dict:
    p = _REPO_ROOT / "docs" / "skills" / "oc-channel-coverage.json"
    return json.loads(p.read_text(encoding="utf-8"))


def test_install_metadata_matches_the_oc_channel_snapshot():
    """``install`` / ``oc_plugin_id`` are DERIVED facts about OpenClaw, not
    Evolve opinions — they must agree with tools/list_oc_channels.py's
    committed snapshot. ``bundled`` → core; ``npm``/``clawhub`` → plugin."""
    by_id = {c["id"]: c for c in _oc_snapshot()["channels"]}
    expected_class = {
        "bundled": cr.INSTALL_CORE,
        "npm": cr.INSTALL_OFFICIAL_PLUGIN,
        "clawhub": cr.INSTALL_OFFICIAL_PLUGIN,
    }
    for c in cr.all_channels():
        if c.install is None:
            # email / webhook are not OC channels at all.
            assert c.id not in by_id, f"{c.id} IS an OC channel — set install"
            continue
        assert c.id in by_id, f"{c.id} claims an OC install but OC ships no such channel"
        row = by_id[c.id]
        assert c.install == expected_class[row["default_install"]], (
            f"{c.id}: registry says {c.install}, OC snapshot says "
            f"{row['default_install']}"
        )
        assert c.oc_plugin_id == row["npm_name"], (
            f"{c.id}: plugin id drifted from the OC snapshot"
        )


def test_registry_labels_match_the_oc_snapshot_where_oc_has_one():
    by_id = {c["id"]: c for c in _oc_snapshot()["channels"]}
    for c in cr.all_channels():
        if c.id in by_id:
            assert c.display_label == by_id[c.id]["label"], (
                f"{c.id}: display_label drifted from OC's own label"
            )
