"""Unit tests for the per-bot Setup checklist data layer."""

from __future__ import annotations

import pytest

from bot_purpose import get_bot_purpose, has_purpose
from evolve_admin.config import RESERVED_BOT_PURPOSES, ensure_reserved_bot_purposes
from evolve_admin.setup_checklist import (
    CHECKLIST_ITEMS,
    ChecklistItem,
    STATE_DISMISSED,
    STATE_DONE,
    STATE_PENDING,
    _detect_purpose,
    counter,
    dismiss_item,
    evaluate,
    get_state,
    is_chip_visible,
    reset_chip,
    set_item_state,
    should_show_in_actions_menu,
    suppress_chip,
)


def _detect_true(bot_id, network, openclaw_cfg):
    return True


def _detect_false(bot_id, network, openclaw_cfg):
    return False


@pytest.fixture
def fake_registry() -> tuple[ChecklistItem, ...]:
    """2-item registry — exercises every branch without coupling tests to
    the real item list."""
    return (
        ChecklistItem("alpha", "Alpha", _detect_false, deep_link="/a"),
        ChecklistItem("beta", "Beta", _detect_false, deep_link="/b"),
    )


def _net() -> dict:
    return {"bots": {"team_bot_a": {}}}


def test_evaluate_seeds_pending_state_for_a_fresh_bot(fake_registry):
    network = _net()
    merged = evaluate(network, "team_bot_a", items=fake_registry)
    assert set(merged.keys()) == {"alpha", "beta"}
    assert all(entry["state"] == STATE_PENDING for entry in merged.values())
    state = get_state(network, "team_bot_a")
    assert state["tile_chip_suppressed_at"] is None
    assert set(state["items"].keys()) == {"alpha", "beta"}


def test_evaluate_returns_label_and_deep_link_for_rendering(fake_registry):
    network = _net()
    merged = evaluate(network, "team_bot_a", items=fake_registry)
    assert merged["alpha"]["label"] == "Alpha"
    assert merged["alpha"]["deep_link"] == "/a"
    assert merged["alpha"]["auto_detected"] is True


def test_detector_returning_true_marks_item_done():
    network = _net()
    registry = (
        ChecklistItem("alpha", "Alpha", _detect_true, deep_link="/a"),
        ChecklistItem("beta", "Beta", _detect_false, deep_link="/b"),
    )
    merged = evaluate(network, "team_bot_a", items=registry)
    assert merged["alpha"]["state"] == STATE_DONE
    assert merged["beta"]["state"] == STATE_PENDING


def test_dismiss_item_wins_over_detector(fake_registry):
    """An operator's 'no thanks' must persist even if the detector says done."""
    network = _net()
    dismiss_item(network, "team_bot_a", "pairing")  # uses real registry id
    # Re-evaluate with the production registry — dismissed should stick.
    merged = evaluate(network, "team_bot_a")
    assert merged["pairing"]["state"] == STATE_DISMISSED
    assert merged["pairing"]["auto_detected"] is False


def test_dismissed_item_counted_with_done(fake_registry):
    network = _net()
    dismiss_item(network, "team_bot_a", "pairing")
    registry = (
        ChecklistItem("pairing", "Pairing", _detect_false, deep_link="/p"),
        ChecklistItem("github", "Github", _detect_true, deep_link="/g"),
    )
    merged = evaluate(network, "team_bot_a", items=registry)
    done, total = counter(merged)
    assert (done, total) == (2, 2)


def test_chip_visible_when_any_item_pending_and_not_suppressed(fake_registry):
    network = _net()
    merged = evaluate(network, "team_bot_a", items=fake_registry)
    assert is_chip_visible(network, "team_bot_a", merged) is True


def test_chip_hidden_when_no_pending_items():
    network = _net()
    dismiss_item(network, "team_bot_a", "pairing")
    registry = (
        ChecklistItem("pairing", "Pairing", _detect_false, deep_link="/p"),
        ChecklistItem("github", "Github", _detect_true, deep_link="/g"),
    )
    merged = evaluate(network, "team_bot_a", items=registry)
    assert is_chip_visible(network, "team_bot_a", merged) is False


def test_suppress_then_reset_round_trip(fake_registry):
    network = _net()
    evaluate(network, "team_bot_a", items=fake_registry)
    assert is_chip_visible(network, "team_bot_a", evaluate(network, "team_bot_a", items=fake_registry)) is True

    suppress_chip(network, "team_bot_a")
    # Re-evaluating without a state change preserves last_changed_at, so
    # the suppression actually hides the chip.
    merged = evaluate(network, "team_bot_a", items=fake_registry)
    assert is_chip_visible(network, "team_bot_a", merged) is False

    reset_chip(network, "team_bot_a")
    merged = evaluate(network, "team_bot_a", items=fake_registry)
    assert is_chip_visible(network, "team_bot_a", merged) is True


def test_reset_keeps_per_item_dismissals():
    """Reset only clears tile_chip_suppressed_at — dismissed items stay
    dismissed. Operator toggles individual items via the Settings page."""
    network = _net()
    dismiss_item(network, "team_bot_a", "pairing")
    suppress_chip(network, "team_bot_a")
    reset_chip(network, "team_bot_a")
    state = get_state(network, "team_bot_a")
    assert state["tile_chip_suppressed_at"] is None
    assert state["items"]["pairing"]["state"] == STATE_DISMISSED


def test_new_item_in_registry_resurfaces_suppressed_chip(fake_registry):
    """If a new item is added to the registry after the operator suppressed
    the chip, the chip resurfaces — fresh consideration deserves a fresh prompt."""
    network = _net()
    evaluate(network, "team_bot_a", items=fake_registry)
    suppress_chip(network, "team_bot_a")
    # Confirm baseline: 2-item registry stays hidden.
    merged = evaluate(network, "team_bot_a", items=fake_registry)
    assert is_chip_visible(network, "team_bot_a", merged) is False

    expanded = fake_registry + (
        ChecklistItem("gamma", "Gamma", _detect_false, deep_link="/g"),
    )
    merged = evaluate(network, "team_bot_a", items=expanded)
    assert is_chip_visible(network, "team_bot_a", merged) is True


def test_evaluate_is_idempotent_when_nothing_changes(fake_registry):
    """Re-running evaluate() without a detector flip must NOT update
    last_changed_at — otherwise suppression timestamps would constantly be
    beaten by newly-bumped pending timestamps."""
    network = _net()
    first = evaluate(network, "team_bot_a", items=fake_registry)
    ts_before = {k: v["last_changed_at"] for k, v in first.items()}
    second = evaluate(network, "team_bot_a", items=fake_registry)
    ts_after = {k: v["last_changed_at"] for k, v in second.items()}
    assert ts_before == ts_after


def test_evaluate_bumps_timestamp_when_detector_flips():
    """A detector going pending → done must update last_changed_at, so the
    operator sees the row turn green and the chip resurfaces if needed."""
    network = _net()
    flipping = {"value": False}

    def _flip(bot_id, network, openclaw_cfg):
        return flipping["value"]

    registry = (ChecklistItem("flip", "Flip", _flip, deep_link="/f"),)
    # Add 'flip' to ITEMS_BY_ID for set_item_state (not strictly needed here;
    # evaluate() routes through stored items directly, not ITEMS_BY_ID).
    first = evaluate(network, "team_bot_a", items=registry)
    ts_before = first["flip"]["last_changed_at"]
    flipping["value"] = True
    second = evaluate(network, "team_bot_a", items=registry)
    assert second["flip"]["state"] == STATE_DONE
    assert second["flip"]["last_changed_at"] >= ts_before


def test_set_item_state_rejects_invalid_state():
    network = _net()
    with pytest.raises(ValueError):
        set_item_state(network, "team_bot_a", "pairing", "bogus")


def test_set_item_state_rejects_unknown_item():
    network = _net()
    with pytest.raises(KeyError):
        set_item_state(network, "team_bot_a", "not_a_real_item", STATE_DONE)


def test_real_registry_has_expected_items_and_no_duplicates():
    ids = [item.id for item in CHECKLIST_ITEMS]
    assert len(ids) == len(set(ids)), "duplicate ids in CHECKLIST_ITEMS"
    expected = {
        "purpose",
        "pairing",
        "github",
        "search",
        "ai_optim_tiers",
        "secondary_llm",
        "cost_profile",
        "gallery_app",
        "spend_cap",
    }
    assert expected.issubset(set(ids))
    # Purpose is foundational (the Fit Reviewer's anchor) → it leads the list.
    assert ids[0] == "purpose"


def test_actions_menu_hidden_when_chip_never_suppressed(fake_registry):
    """No suppression → chip itself is the entry point; Actions menu stays clean."""
    network = _net()
    merged = evaluate(network, "team_bot_a", items=fake_registry)
    assert should_show_in_actions_menu(network, "team_bot_a", merged) is False


def test_actions_menu_shown_when_suppressed_and_pending(fake_registry):
    """Operator dismissed the chip but still has work → surface in Actions menu
    as the escape hatch."""
    network = _net()
    evaluate(network, "team_bot_a", items=fake_registry)
    suppress_chip(network, "team_bot_a")
    merged = evaluate(network, "team_bot_a", items=fake_registry)
    # Chip itself is hidden …
    assert is_chip_visible(network, "team_bot_a", merged) is False
    # … but Actions menu picks up the slack.
    assert should_show_in_actions_menu(network, "team_bot_a", merged) is True


def test_actions_menu_hidden_when_suppressed_but_nothing_pending():
    """Suppressed + everything done/dismissed → nothing to nag about."""
    network = _net()
    dismiss_item(network, "team_bot_a", "pairing")
    registry = (
        ChecklistItem("pairing", "Pairing", _detect_false, deep_link="/p"),
        ChecklistItem("github", "Github", _detect_true, deep_link="/g"),
    )
    evaluate(network, "team_bot_a", items=registry)
    suppress_chip(network, "team_bot_a")
    merged = evaluate(network, "team_bot_a", items=registry)
    assert should_show_in_actions_menu(network, "team_bot_a", merged) is False


def test_get_state_creates_scaffold_on_first_access():
    network: dict = {}
    state = get_state(network, "team_bot_a")
    assert state == {"tile_chip_suppressed_at": None, "items": {}}
    # Confirm it was persisted into the network dict (not a fresh copy).
    assert network["bots"]["team_bot_a"]["setup_checklist"] is state


# ── Built-in (reserved) bot awareness ─────────────────────────────────────────

_MEMBER_ONLY_ITEMS = {"gallery_app", "github", "secondary_llm"}
_RESERVED_APPLICABLE_ITEMS = {
    "purpose", "pairing", "search", "ai_optim_tiers", "cost_profile", "spend_cap",
}
_ALL_ITEMS = _RESERVED_APPLICABLE_ITEMS | _MEMBER_ONLY_ITEMS


def test_reserved_bot_gets_only_applicable_items():
    """A built-in assistant (evo) yields exactly the 6 applicable items — the
    three member-only items are filtered out before any detector runs."""
    network = {"bots": {"evo": {}}}
    merged = evaluate(network, "evo")  # real CHECKLIST_ITEMS registry
    assert set(merged.keys()) == _RESERVED_APPLICABLE_ITEMS
    for absent in _MEMBER_ONLY_ITEMS:
        assert absent not in merged
    _, total = counter(merged)
    assert total == 6


def test_reserved_bot_na_items_never_seed_stored_state():
    """Filtered-out items must not be persisted into network.json for evo."""
    network = {"bots": {"evo": {}}}
    evaluate(network, "evo")
    stored = get_state(network, "evo")["items"]
    for absent in _MEMBER_ONLY_ITEMS:
        assert absent not in stored
    assert set(stored.keys()) == _RESERVED_APPLICABLE_ITEMS


def test_non_reserved_bot_still_gets_all_nine():
    """A member bot is unaffected — all 9 items apply, counter total is 9."""
    network = {"bots": {"team_bot_a": {}}}
    merged = evaluate(network, "team_bot_a")
    assert set(merged.keys()) == _ALL_ITEMS
    _, total = counter(merged)
    assert total == 9


def test_ensure_reserved_bot_purposes_seeds_when_absent():
    network = {"bots": {"evo": {}, "evolve": {}, "team_bot_a": {}}}
    changed = ensure_reserved_bot_purposes(network)
    assert changed is True
    for bid in ("evo", "evolve"):
        assert has_purpose(network, bid)
        p = get_bot_purpose(network, bid)
        assert p["archetype"] == "custom"
        assert p["mission"] == RESERVED_BOT_PURPOSES[bid]["mission"]
    # The member bot is never given a purpose by the reserved-bot seed.
    assert not has_purpose(network, "team_bot_a")


def test_ensure_reserved_bot_purposes_is_noop_on_rerun():
    network = {"bots": {"evo": {}}}
    assert ensure_reserved_bot_purposes(network) is True
    first = get_bot_purpose(network, "evo")
    # Second run must report no change and leave the purpose byte-identical.
    assert ensure_reserved_bot_purposes(network) is False
    assert get_bot_purpose(network, "evo") == first


def test_ensure_reserved_bot_purposes_does_not_overwrite_existing():
    """An operator-edited purpose on a reserved bot is preserved."""
    custom = {
        "archetype": "custom",
        "mission": "Operator-written mission.",
        "captured": "declared",
        "confidence": 1.0,
    }
    network = {"bots": {"evo": {"purpose": custom}}}
    assert ensure_reserved_bot_purposes(network) is False
    assert get_bot_purpose(network, "evo")["mission"] == "Operator-written mission."


def test_ensure_reserved_bot_purposes_only_touches_present_bots():
    """A reserved bot absent from the roster is never invented."""
    network = {"bots": {"team_bot_a": {}}}
    assert ensure_reserved_bot_purposes(network) is False
    assert "evo" not in network["bots"]
    assert "evolve" not in network["bots"]


def test_seeded_purpose_makes_checklist_purpose_row_done():
    """After seeding, the purpose detector reads True so the row is Done."""
    network = {"bots": {"evo": {}}}
    ensure_reserved_bot_purposes(network)
    assert _detect_purpose("evo", network, None) is True
    merged = evaluate(network, "evo")
    assert merged["purpose"]["state"] == STATE_DONE
