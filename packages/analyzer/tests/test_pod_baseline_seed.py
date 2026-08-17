"""pod_baseline seed: majority computation, ties, unreadable exclusion."""
from pod_baseline.census import BotReading, SurfaceReading
from pod_baseline.schema import POD_BASELINE_SCHEMA_VERSION, SURFACES
from pod_baseline.seed import seed_from_majority

_GENERATED_AT = "2026-08-15T00:00:00+00:00"


def _reading(bot_id, exec_policy="full", tool_profile="coding", browser="on",
             context_profile="unset", model_policy="pod-defaults"):
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
        "context_profile": "unset",
        "model_policy": "pod-defaults",
    }
    assert baseline.validate() == []
    by_surface = {c.surface: c for c in choices}
    assert set(by_surface) == set(SURFACES)
    assert by_surface["exec_policy"].counts == {"full": 3, "deny": 1}
    assert by_surface["exec_policy"].tie is False
    assert by_surface["exec_policy"].unknown == 0


def test_seed_tie_breaks_lexicographically_and_flags_it():
    readings = [_reading("bot-a", browser="on"), _reading("bot-b", browser="off")]
    baseline, choices = seed_from_majority(readings, generated_at=_GENERATED_AT)
    choice = next(c for c in choices if c.surface == "browser")
    assert choice.tie is True
    assert baseline.surfaces["browser"] == "off"  # lexicographic tie-break


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


def test_seed_with_no_readable_values_seeds_unset():
    unreadable = BotReading(bot_id="bot-a", surfaces={
        s: SurfaceReading(None, "timeout") for s in SURFACES
    })
    baseline, choices = seed_from_majority([unreadable], generated_at=_GENERATED_AT)
    assert all(v == "unset" for v in baseline.surfaces.values())
    assert all(c.counts == {} and c.unknown == 1 for c in choices)


def test_seed_empty_pod_seeds_unset_everywhere():
    baseline, choices = seed_from_majority([], generated_at=_GENERATED_AT)
    assert set(baseline.surfaces) == set(SURFACES)
    assert all(v == "unset" for v in baseline.surfaces.values())
    assert baseline.validate() == []
