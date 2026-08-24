"""pod_baseline seed: majority computation, the undeclared rule, ties, unreadable.

The undeclared rule is Q7(b) (decided 2026-08-22): seeding refuses to elect a
no-intent sentinel, and "safest observed" survives only as the tiebreak among
surfaces that carry real values.
"""
from pod_baseline.census import BotReading, SurfaceReading, classify_readings
from pod_baseline.schema import (
    POD_BASELINE_SCHEMA_VERSION,
    STATE_CONFORM,
    STATE_LOOSENED,
    STATE_TIGHTENED,
    STATE_UNDECLARED,
    SURFACES,
)
from pod_baseline.seed import (
    TIEBREAK_LEXICOGRAPHIC,
    TIEBREAK_NONE,
    TIEBREAK_SAFEST,
    seed_from_majority,
)

_GENERATED_AT = "2026-08-15T00:00:00+00:00"


def _nonzero(counts):
    """counts() pre-fills every state with 0; keep the assertions readable."""
    return {state: n for state, n in counts.items() if n}


def _reading(bot_id, exec_policy="full", tool_profile="coding", browser="on",
             context_profile="balanced", model_policy="pod-defaults"):
    return BotReading(bot_id=bot_id, surfaces={
        "exec_policy": SurfaceReading(exec_policy),
        "tool_profile": SurfaceReading(tool_profile),
        "browser": SurfaceReading(browser),
        "context_profile": SurfaceReading(context_profile),
        "model_policy": SurfaceReading(model_policy),
    })


def test_seed_picks_modal_values():
    readings = [
        _reading("bot-a"),
        _reading("bot-b", browser="off"),
        _reading("bot-c"),
        _reading("security-bot", exec_policy="deny"),
    ]
    baseline, choices = seed_from_majority(readings, generated_at=_GENERATED_AT)
    assert baseline.schema_version == POD_BASELINE_SCHEMA_VERSION
    assert baseline.updated_at == _GENERATED_AT
    assert baseline.exceptions == []  # seeding never declares exceptions
    assert baseline.surfaces == {
        "exec_policy": "full",
        "tool_profile": "coding",
        "browser": "on",
        "context_profile": "balanced",
        "model_policy": "pod-defaults",
    }
    assert baseline.undeclared == []
    assert baseline.validate() == []
    by_surface = {c.surface: c for c in choices}
    assert set(by_surface) == set(SURFACES)
    assert by_surface["exec_policy"].counts == {"full": 3, "deny": 1}
    assert by_surface["exec_policy"].tie is False
    assert by_surface["exec_policy"].unknown == 0
    assert by_surface["exec_policy"].tie_broken_by == TIEBREAK_NONE
    assert not any(c.undeclared for c in choices)


def test_seed_tie_breaks_to_the_safest_observed_value():
    # Q7(b): "safest observed" survives as the TIEBREAK among real values.
    # off is tighter than on, so a 1–1 split seeds off — and would have
    # seeded off under the old lexicographic rule too, so exec_policy below
    # is the case that actually distinguishes them.
    readings = [_reading("bot-a", browser="on"), _reading("bot-b", browser="off")]
    baseline, choices = seed_from_majority(readings, generated_at=_GENERATED_AT)
    choice = next(c for c in choices if c.surface == "browser")
    assert choice.tie is True
    assert choice.tie_broken_by == TIEBREAK_SAFEST
    assert baseline.surfaces["browser"] == "off"

    # allowlist is tighter than full but LEXICOGRAPHICALLY smaller too;
    # deny-vs-allowlist is the discriminating pair — "allowlist" sorts
    # first, "deny" is safer.
    readings = [_reading("bot-a", exec_policy="allowlist"),
                _reading("bot-b", exec_policy="deny")]
    baseline, choices = seed_from_majority(readings, generated_at=_GENERATED_AT)
    choice = next(c for c in choices if c.surface == "exec_policy")
    assert choice.tie is True and choice.tie_broken_by == TIEBREAK_SAFEST
    assert baseline.surfaces["exec_policy"] == "deny"  # NOT the lexicographic pick


def test_seed_tie_with_no_ordering_falls_back_to_lexicographic_and_says_so():
    # coding and messaging are mutually incomparable upstream (see
    # pod_baseline.ordering), so the ordering cannot break this tie. The
    # fallback is arbitrary and must be labelled as such — never dressed up
    # as a safety choice.
    readings = [_reading("bot-a", tool_profile="messaging"),
                _reading("bot-b", tool_profile="coding")]
    baseline, choices = seed_from_majority(readings, generated_at=_GENERATED_AT)
    choice = next(c for c in choices if c.surface == "tool_profile")
    assert choice.tie is True
    assert choice.tie_broken_by == TIEBREAK_LEXICOGRAPHIC
    assert baseline.surfaces["tool_profile"] == "coding"


# ── Q7(b): the undeclared rule ───────────────────────────────────────────────

def test_seed_refuses_to_elect_a_sentinel_modal_value():
    # 3 of 4 bots have no tools.profile at all. "unset" is not a policy
    # position — electing it would report the fleet's silence back as pod
    # intent, which is exactly what Q7(b) exists to stop.
    readings = [
        _reading("bot-a", tool_profile="unset"),
        _reading("bot-b", tool_profile="unset"),
        _reading("bot-c", tool_profile="unset"),
        _reading("bot-d", tool_profile="coding"),
    ]
    baseline, choices = seed_from_majority(readings, generated_at=_GENERATED_AT)
    choice = next(c for c in choices if c.surface == "tool_profile")
    assert choice.undeclared is True
    assert choice.chosen == ""
    assert choice.blocking_sentinels == ("unset",)
    assert choice.counts == {"unset": 3, "coding": 1}  # the distribution survives
    assert "tool_profile" not in baseline.surfaces
    assert baseline.undeclared == ["tool_profile"]
    assert baseline.validate() == []


def test_seed_refuses_custom_as_well_as_unset():
    # There is no "safest" custom: it means "matches no named profile" /
    # "not on pod defaults", i.e. the absence of a declaration.
    readings = [_reading("bot-a", context_profile="custom", model_policy="custom"),
                _reading("bot-b", context_profile="custom", model_policy="custom")]
    baseline, choices = seed_from_majority(readings, generated_at=_GENERATED_AT)
    for surface in ("context_profile", "model_policy"):
        choice = next(c for c in choices if c.surface == surface)
        assert choice.undeclared and choice.blocking_sentinels == ("custom",)
    assert set(baseline.undeclared) == {"context_profile", "model_policy"}


def test_a_tied_sentinel_blocks_election_too():
    # If as many bots declared nothing as declared the leading value, the
    # leading value is not the pod's position either.
    readings = [_reading("bot-a", browser="unset"), _reading("bot-b", browser="on")]
    baseline, choices = seed_from_majority(readings, generated_at=_GENERATED_AT)
    choice = next(c for c in choices if c.surface == "browser")
    assert choice.tie is True and choice.undeclared is True
    assert choice.blocking_sentinels == ("unset",)
    assert "browser" not in baseline.surfaces


def test_a_minority_sentinel_does_not_block_election():
    # The rule keys on the MODAL reading, not on any occurrence: a couple of
    # unset stragglers must not veto a surface the pod clearly declared.
    readings = [_reading(f"bot-{i}", browser="on") for i in range(3)]
    readings.append(_reading("bot-x", browser="unset"))
    baseline, choices = seed_from_majority(readings, generated_at=_GENERATED_AT)
    choice = next(c for c in choices if c.surface == "browser")
    assert choice.undeclared is False and choice.blocking_sentinels == ()
    assert baseline.surfaces["browser"] == "on"


def test_custom_allow_is_not_a_sentinel():
    # An exclusive tools.allow list is somebody's deliberate list — merely
    # one that sits on no safety ladder. It is electable.
    readings = [_reading(f"bot-{i}", tool_profile="custom-allow") for i in range(2)]
    baseline, choices = seed_from_majority(readings, generated_at=_GENERATED_AT)
    choice = next(c for c in choices if c.surface == "tool_profile")
    assert choice.undeclared is False
    assert baseline.surfaces["tool_profile"] == "custom-allow"


def test_seed_excludes_unreadable_and_reports_them():
    unreadable = BotReading(bot_id="bot-c", surfaces={
        s: SurfaceReading(None, "sudo_rc=1") for s in SURFACES
    })
    readings = [_reading("bot-a", exec_policy="deny"), unreadable]
    baseline, choices = seed_from_majority(readings, generated_at=_GENERATED_AT)
    choice = next(c for c in choices if c.surface == "exec_policy")
    assert choice.unknown == 1
    assert choice.counts == {"deny": 1}
    assert baseline.surfaces["exec_policy"] == "deny"


def test_seed_with_no_readable_values_declares_nothing():
    unreadable = BotReading(bot_id="bot-a", surfaces={
        s: SurfaceReading(None, "timeout") for s in SURFACES
    })
    baseline, choices = seed_from_majority([unreadable], generated_at=_GENERATED_AT)
    assert baseline.surfaces == {}
    assert list(baseline.undeclared) == list(SURFACES)
    assert baseline.validate() == []
    assert all(c.undeclared and c.counts == {} and c.unknown == 1 for c in choices)
    # No vote was held, so no sentinel blocked one.
    assert all(c.blocking_sentinels == () for c in choices)


def test_seed_empty_pod_declares_nothing():
    baseline, choices = seed_from_majority([], generated_at=_GENERATED_AT)
    assert baseline.surfaces == {}
    assert list(baseline.undeclared) == list(SURFACES)
    assert baseline.validate() == []


# ── The spec's worked arithmetic, end to end ─────────────────────────────────

def _live_mini_readings():
    """The mini's live per-bot readings, 2026-08-22 (spec Q7(d) table).

    9 bots: exec_policy 8 × full + 1 × allowlist (the security-role bot);
    tool_profile 7 × unset + 2 × coding (the two bots onboarded through
    `openclaw onboard`); browser 9 × unset; context_profile 9 × custom;
    model_policy 9 × custom.
    """
    return [
        _reading(
            f"bot-{i}",
            exec_policy="allowlist" if i == 0 else "full",
            tool_profile="coding" if i < 2 else "unset",
            browser="unset", context_profile="custom", model_policy="custom",
        )
        for i in range(9)
    ]


def test_mini_reproduces_the_specs_predicted_census():
    # Spec Q7(b): the mini's 45 rows go from 42 conform / 3 drift to
    # 8 conform / 1 tightened / 36 undeclared. Different numbers here mean
    # the rule was implemented differently from the one that was decided.
    readings = _live_mini_readings()
    baseline, _ = seed_from_majority(readings, generated_at=_GENERATED_AT)
    assert baseline.surfaces == {"exec_policy": "full"}
    assert set(baseline.undeclared) == set(SURFACES) - {"exec_policy"}
    report = classify_readings(baseline, readings)
    assert len(report.rows) == 45
    assert _nonzero(report.counts()) == {
        STATE_CONFORM: 8, STATE_TIGHTENED: 1, STATE_UNDECLARED: 36,
    }
    # The one non-conforming row is the hardened bot, and it reads as an
    # improvement — not a fault, and not something needing an exception.
    tightened = [r for r in report.rows if r.state == STATE_TIGHTENED]
    assert [(r.bot_id, r.surface, r.observed) for r in tightened] == [
        ("bot-0", "exec_policy", "allowlist")
    ]
    assert report.undeclared_distribution()["tool_profile"] == {"unset": 7, "coding": 2}


def test_two_bot_pod_reproduces_the_specs_predicted_census():
    # Spec Q7(b): the VPS's 10 rows go from 10 conform / 0 drift to
    # 2 conform / 8 undeclared.
    readings = [
        _reading(f"bot-{i}", exec_policy="full", tool_profile="unset",
                 browser="unset", context_profile="custom", model_policy="custom")
        for i in range(2)
    ]
    baseline, _ = seed_from_majority(readings, generated_at=_GENERATED_AT)
    report = classify_readings(baseline, readings)
    assert len(report.rows) == 10
    assert _nonzero(report.counts()) == {STATE_CONFORM: 2, STATE_UNDECLARED: 8}
    # counts() pre-fills every state, so a zero is a positive statement
    # ("no loosened rows") rather than an absent key.
    assert report.counts()[STATE_LOOSENED] == 0
