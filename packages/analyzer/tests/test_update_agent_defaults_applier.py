"""Tests for the UpdateAgentDefaults applier.

Phase 4b of the 2026-06 cost-cap normalization (spec:
docs/spec-cost-caps-2026-06-05.md) emptied the applier's dotpath
whitelist. The two knobs it previously handled —
``agents.defaults.models.*.params.cacheRetention`` (PR A, 2026-05-30)
and ``agents.defaults.sessionBudgetCapUsd`` (PR C) — now live in
better-engine-config and are written via ``/api/arbiter/bot-setup``,
not via the sandbox-override path this applier targeted.

These tests pin the dormant state: the action type still constructs +
serializes (so a future PR widening the whitelist doesn't have to
re-establish the registry plumbing), and every apply attempt rejects
because no dotpath is allowed.
"""
from __future__ import annotations

import pytest

# Importing triggers register_applier
from arbiter.appliers import agent_defaults as _agent_defaults_app  # noqa: F401
from arbiter.appliers.base import get_applier
from schema.proposal import (
    UpdateAgentDefaults,
    _UPDATE_AGENT_DEFAULTS_ALLOWED_DOTPATHS,
    _UPDATE_AGENT_DEFAULTS_VALUE_VALIDATORS,
    action_from_dict,
    action_to_dict,
)


def test_whitelist_is_empty_post_phase_4b():
    """Phase 4b emptied the dotpath whitelist; pinning here so a future
    widening PR has to explicitly update both the whitelist and these
    tests in tandem."""
    assert _UPDATE_AGENT_DEFAULTS_ALLOWED_DOTPATHS == frozenset(), (
        "UpdateAgentDefaults dotpath whitelist must be empty post-Phase-4b; "
        "widening it requires updating the validators table + reviewing the "
        "materializer slice + adding tests for each new dotpath."
    )
    assert _UPDATE_AGENT_DEFAULTS_VALUE_VALIDATORS == {}, (
        "Validators table must mirror the empty whitelist."
    )


def test_action_type_construct_and_serialize_round_trip():
    """The action type still constructs + round-trips through
    action_to_dict / action_from_dict — registry plumbing stays intact
    so a future widening PR can reuse it."""
    action = UpdateAgentDefaults(
        bot_id="team-bot-a",
        fields={"some.future.dotpath": "value"},
    )
    serialized = action_to_dict(action)
    assert serialized["kind"] == "UpdateAgentDefaults"
    assert serialized["bot_id"] == "team-bot-a"
    assert serialized["fields"] == {"some.future.dotpath": "value"}

    deserialized = action_from_dict(serialized)
    assert isinstance(deserialized, UpdateAgentDefaults)
    assert deserialized.bot_id == "team-bot-a"
    assert deserialized.fields == {"some.future.dotpath": "value"}


def test_applier_is_registered():
    """The applier remains registered so a future widening PR can hook
    new dotpaths without re-establishing the registry entry."""
    applier = get_applier("UpdateAgentDefaults")
    assert applier is not None


def test_apply_rejects_legacy_cache_retention_dotpath():
    """The pre-Phase-4b dotpath for cacheRetention is no longer in the
    whitelist; any apply attempt rejects."""
    applier = get_applier("UpdateAgentDefaults")
    action = UpdateAgentDefaults(
        bot_id="team-bot-a",
        fields={"agents.defaults.models.*.params.cacheRetention": "long"},
    )
    result = applier.apply(action, bot_id="team-bot-a")
    assert not result.ok
    # Error message should name the unmapped dotpath so the operator (or
    # the generator log) sees why the apply refused.
    err_text = str(result.details).lower()
    assert "cacheretention" in err_text or "schema" in err_text or "whitelist" in err_text


def test_apply_rejects_legacy_session_budget_dotpath():
    """The pre-Phase-4b dotpath for sessionBudgetCapUsd is no longer in
    the whitelist; any apply attempt rejects."""
    applier = get_applier("UpdateAgentDefaults")
    action = UpdateAgentDefaults(
        bot_id="team-bot-a",
        fields={"agents.defaults.sessionBudgetCapUsd": 2.50},
    )
    result = applier.apply(action, bot_id="team-bot-a")
    assert not result.ok


def test_apply_rejects_arbitrary_dotpath():
    """Any other dotpath is also rejected — whitelist is the gate."""
    applier = get_applier("UpdateAgentDefaults")
    action = UpdateAgentDefaults(
        bot_id="team-bot-a",
        fields={"agents.defaults.something.else": "anything"},
    )
    result = applier.apply(action, bot_id="team-bot-a")
    assert not result.ok
