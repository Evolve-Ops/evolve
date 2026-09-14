"""pod_baseline census: pure extractors + fixture-driven pod sweep.

The fixture census runs under BOTH platform profiles (macOS + Linux pins via
platform_profile.set_profile) — home_overrides make the reads themselves
platform-neutral, and the test asserts the classification is identical.
"""
import copy
import json

import pytest

import platform_profile
from cost_profiles import BUILTIN_PROFILES, _deep_merge_cost
from pod_baseline.census import (
    BotReading,
    SurfaceReading,
    browser_from_config,
    classify_readings,
    context_profile_from_config,
    exec_policy_from_config,
    model_policy_from_tiers_doc,
    pod_roster,
    read_bot_surfaces,
    read_pod_surfaces,
    tool_profile_from_config,
)
from pod_baseline.schema import (
    STATE_CONFORM,
    STATE_DIVERGENT,
    STATE_EXCEPTION,
    STATE_LOOSENED,
    STATE_TIGHTENED,
    STATE_UNDECLARED,
    STATE_DISPLAY_ORDER,
    STATE_UNREADABLE,
    SURFACES,
    BaselineException,
    PodBaseline,
)


# ── Extractors ────────────────────────────────────────────────────────────────

def test_exec_policy_extractor():
    assert exec_policy_from_config({}) == "unset"
    assert exec_policy_from_config({"tools": {"exec": {"security": "deny"}}}) == "deny"
    assert exec_policy_from_config({"tools": {"exec": {"security": ""}}}) == "unset"
    assert exec_policy_from_config({"tools": {"exec": {"security": 3}}}) == "unset"


def test_tool_profile_extractor():
    assert tool_profile_from_config({}) == "unset"
    assert tool_profile_from_config({"tools": {"profile": "coding"}}) == "coding"
    # An exclusive allow list REPLACES the profile upstream — report that.
    assert tool_profile_from_config(
        {"tools": {"profile": "coding", "allow": ["exec"]}}
    ) == "custom-allow"
    # Empty allow list is inert; alsoAllow is additive and does not mask.
    assert tool_profile_from_config({"tools": {"profile": "full", "allow": []}}) == "full"
    assert tool_profile_from_config(
        {"tools": {"profile": "minimal", "alsoAllow": ["browser"]}}
    ) == "minimal"


def test_browser_extractor():
    assert browser_from_config({}) == "unset"
    assert browser_from_config({"browser": {"enabled": True}}) == "on"
    assert browser_from_config({"browser": {"enabled": False}}) == "off"
    assert browser_from_config({"browser": {"cdpUrl": "ws://x"}}) == "unset"
    assert browser_from_config({"browser": {"enabled": "yes"}}) == "unset"


def _config_with_profile_applied(profile_name):
    """Build an openclaw.json dict as apply_profile_to_bot would leave it."""
    settings = {
        k: copy.deepcopy(v)
        for k, v in BUILTIN_PROFILES[profile_name]["settings"].items()
        if k != "cache_retention"  # BE-config-owned, never lands in openclaw.json
    }
    return _deep_merge_cost({}, settings)


@pytest.mark.parametrize("profile_name", sorted(BUILTIN_PROFILES))
def test_context_profile_recognizes_each_builtin(profile_name):
    cfg = _config_with_profile_applied(profile_name)
    assert context_profile_from_config(cfg) == profile_name


def test_context_profile_survives_additive_operator_keys():
    # apply_profile_to_bot deep-merges; an operator-set heartbeat.every must
    # not break recognition of the applied profile.
    cfg = _config_with_profile_applied("balanced")
    cfg["agents"]["defaults"]["heartbeat"]["every"] = "10m"
    assert context_profile_from_config(cfg) == "balanced"


def test_context_profile_unset_and_custom():
    assert context_profile_from_config({}) == "unset"
    assert context_profile_from_config(
        {"agents": {"defaults": {"bootstrapTotalMaxChars": 12345}}}
    ) == "custom"
    # A mutated builtin no longer matches it.
    cfg = _config_with_profile_applied("balanced")
    cfg["agents"]["defaults"]["contextPruning"]["keepLastAssistants"] = 99
    assert context_profile_from_config(cfg) == "custom"


def test_context_profile_matches_custom_named_profiles():
    customs = ({"name": "lab", "settings": {"bootstrapTotalMaxChars": 12345}},)
    cfg = {"agents": {"defaults": {"bootstrapTotalMaxChars": 12345}}}
    assert context_profile_from_config(cfg, customs) == "lab"


def test_model_policy_extractor():
    # Keep in sync with primary_bot.bot_has_custom_tiers: non-empty rungs
    # array IS the Custom flip; anything else is pod-defaults.
    assert model_policy_from_tiers_doc({}) == "pod-defaults"
    assert model_policy_from_tiers_doc({"rungs": []}) == "pod-defaults"
    assert model_policy_from_tiers_doc({"rungs": "oops"}) == "pod-defaults"
    assert model_policy_from_tiers_doc({"rungs": [{"slug": "sonnet-class"}]}) == "custom"


# ── Fixture-driven census, both platform profiles ─────────────────────────────

def _write_bot_home(tmp_path, bot_id, openclaw=None, tiers=None, corrupt=False):
    home = tmp_path / "homes" / bot_id
    oc_dir = home / ".openclaw"
    oc_dir.mkdir(parents=True, exist_ok=True)
    if corrupt:
        (oc_dir / "openclaw.json").write_text("{definitely not json")
    elif openclaw is not None:
        (oc_dir / "openclaw.json").write_text(json.dumps(openclaw))
    if tiers is not None:
        (oc_dir / "evolve-tiers.json").write_text(json.dumps(tiers))
    return home


def _fixture_pod(tmp_path):
    network = {"bots": {"bot-a": {}, "bot-b": {}, "bot-c": {}, "security-bot": {}}}
    homes = {
        # Fully conforming bot.
        "bot-a": _write_bot_home(tmp_path, "bot-a", openclaw={
            "tools": {"exec": {"security": "full"}, "profile": "coding"},
            "browser": {"enabled": True},
        }),
        # Divergent on four surfaces (exec, tool profile, browser, model).
        "bot-b": _write_bot_home(
            tmp_path, "bot-b",
            openclaw={
                "tools": {"exec": {"security": "allowlist"}, "allow": ["sessions"]},
                "browser": {"enabled": False},
            },
            tiers={"rungs": [{"slug": "sonnet-class"}]},
        ),
        # Corrupt openclaw.json — the four oc surfaces must be UNREADABLE.
        "bot-c": _write_bot_home(tmp_path, "bot-c", corrupt=True),
        # Matches its declared exec exception; conforms elsewhere.
        "security-bot": _write_bot_home(tmp_path, "security-bot", openclaw={
            "tools": {"exec": {"security": "deny"}, "profile": "coding"},
            "browser": {"enabled": True},
        }),
    }
    baseline = PodBaseline(
        updated_at="2026-08-15T00:00:00+00:00",
        surfaces={
            "exec_policy": "full",
            "tool_profile": "coding",
            "browser": "on",
            "context_profile": "unset",
            "model_policy": "pod-defaults",
        },
        exceptions=[BaselineException(
            bot_id="security-bot",
            surface="exec_policy",
            value="deny",
            reason="locked down by design",
            declared_at="2026-08-15T00:00:00+00:00",
        )],
    )
    return network, homes, baseline


def _run_fixture_census(tmp_path):
    network, homes, baseline = _fixture_pod(tmp_path)
    readings = read_pod_surfaces(network, home_overrides=homes)
    return classify_readings(baseline, readings)


@pytest.mark.parametrize("profile", [platform_profile.MACOS, platform_profile.LINUX],
                         ids=["macos", "linux"])
def test_census_classifies_fixture_pod(tmp_path, profile):
    platform_profile.set_profile(profile)
    try:
        report = _run_fixture_census(tmp_path)
    finally:
        platform_profile.set_profile(None)

    states = {(r.bot_id, r.surface): r.state for r in report.rows}
    # Every bot × surface is classified — nothing silently skipped.
    assert len(report.rows) == 4 * len(SURFACES)

    assert all(states[("bot-a", s)] == "conform" for s in SURFACES)

    # Q7(a): bot-b's four deviations are three different facts, not one.
    # exec allowlist-under-full and browser off-under-on are HARDENINGS;
    # custom-allow and model_policy sit on no safety ladder at all.
    assert states[("bot-b", "exec_policy")] == STATE_TIGHTENED
    assert states[("bot-b", "tool_profile")] == STATE_DIVERGENT  # custom-allow vs coding
    assert states[("bot-b", "browser")] == STATE_TIGHTENED
    assert states[("bot-b", "context_profile")] == STATE_CONFORM
    assert states[("bot-b", "model_policy")] == STATE_DIVERGENT  # custom vs pod-defaults
    # Not one of them is the fault state.
    assert not any(st == STATE_LOOSENED for st in states.values())

    for surface in ("exec_policy", "tool_profile", "browser", "context_profile"):
        assert states[("bot-c", surface)] == "unreadable"
    assert states[("bot-c", "model_policy")] == "conform"  # no tiers file = defaults

    assert states[("security-bot", "exec_policy")] == "exception"
    assert states[("security-bot", "browser")] == "conform"

    counts = report.counts()
    # 4 bots × 5 surfaces = 20 rows: bot-a 5 conform, bot-b 1 conform + 2
    # tightened + 2 divergent, bot-c 4 unreadable + 1 conform, security-bot
    # 1 exception + 4 conform. Every surface is declared here, so no row is
    # undeclared — this fixture is the "nothing else moved" check on the
    # four pre-Q7 states.
    assert {k: v for k, v in counts.items() if v} == {
        "conform": 11, "tightened": 2, "divergent": 2,
        "unreadable": 4, "exception": 1,
    }
    assert sum(counts.values()) == len(report.rows)
    # Pre-filled: every state is named even at zero, so B2's JSON consumer
    # cannot confuse "none of these" with "this producer does not report it".
    assert set(counts) == set(STATE_DISPLAY_ORDER)
    assert counts["undeclared"] == 0 and counts["loosened"] == 0
    assert report.undeclared_surfaces == []
    # A drift row names what the bot should have been at.
    drift_row = next(r for r in report.rows
                     if (r.bot_id, r.surface) == ("bot-b", "exec_policy"))
    assert drift_row.observed == "allowlist" and drift_row.expected == "full"
    exc_row = next(r for r in report.rows
                   if (r.bot_id, r.surface) == ("security-bot", "exec_policy"))
    assert exc_row.exception_reason == "locked down by design"


def test_census_report_identical_across_platform_profiles(tmp_path):
    platform_profile.set_profile(platform_profile.MACOS)
    try:
        macos_report = _run_fixture_census(tmp_path)
    finally:
        platform_profile.set_profile(None)
    platform_profile.set_profile(platform_profile.LINUX)
    try:
        linux_report = _run_fixture_census(tmp_path)
    finally:
        platform_profile.set_profile(None)
    # Only the census's own row `error` strings may embed absolute fixture
    # paths (they don't — but keep the comparison on the full dicts so any
    # future platform leak fails loudly).
    assert macos_report.to_dict() == linux_report.to_dict()


def test_missing_openclaw_json_reads_as_all_unset(tmp_path):
    network = {"bots": {"bot-a": {}}}
    home = tmp_path / "homes" / "bot-a"
    (home / ".openclaw").mkdir(parents=True)
    readings = read_pod_surfaces(network, home_overrides={"bot-a": home})
    values = {s: sr.value for s, sr in readings[0].surfaces.items()}
    assert values == {
        "exec_policy": "unset",
        "tool_profile": "unset",
        "browser": "unset",
        "context_profile": "unset",
        "model_policy": "pod-defaults",
    }


def test_roster_prefers_members_and_excludes_planned_bots():
    # Mirrors deploy._is_provisionable_bot: members ∪ primary is the
    # authority; a planned bot (bots{} block with only "purpose") never
    # votes as a phantom all-unset bot.
    network = {
        "primary": "bot-a",
        "members": ["team-bot-a", "team-bot-b"],
        "bots": {
            "bot-a": {"user": "bot-a"},
            "team-bot-a": {"user": "team-bot-a"},
            "team-bot-b": {"user": "team-bot-b"},
            "planned-bot": {"purpose": "future idea, no account yet"},
        },
    }
    assert pod_roster(network) == ["bot-a", "team-bot-a", "team-bot-b"]


def test_roster_legacy_fallback_to_bots_keys():
    network = {"bots": {"bot-b": {}, "bot-a": {}}}
    assert pod_roster(network) == ["bot-a", "bot-b"]


def test_non_dict_openclaw_json_is_unreadable_not_a_crash(tmp_path):
    network = {"bots": {"bot-a": {}}}
    home = tmp_path / "homes" / "bot-a"
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "openclaw.json").write_text('["not", "an", "object"]')
    (home / ".openclaw" / "evolve-tiers.json").write_text("null")
    reading = read_pod_surfaces(network, home_overrides={"bot-a": home})[0]
    for surface in ("exec_policy", "tool_profile", "browser", "context_profile",
                    "model_policy"):
        assert reading.surfaces[surface].value is None
        assert reading.surfaces[surface].error == "not_an_object"


def test_extractor_crash_degrades_to_unreadable_surface(tmp_path):
    # {"agents": null} crashes _extract_cost_settings; the census must
    # degrade that ONE surface to unreadable, not take down the sweep.
    network = {"bots": {"bot-a": {}}}
    home = tmp_path / "homes" / "bot-a"
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "openclaw.json").write_text(
        json.dumps({"agents": None, "tools": {"exec": {"security": "full"}}})
    )
    reading = read_bot_surfaces(network, "bot-a", home_override=home)
    assert reading.surfaces["exec_policy"].value == "full"
    assert reading.surfaces["context_profile"].value is None
    assert reading.surfaces["context_profile"].error.startswith("extract_error")


def test_context_profile_skips_corrupt_custom_profile_entries():
    customs = ("not-a-dict", {"name": "lab", "settings": {"bootstrapTotalMaxChars": 1}})
    cfg = {"agents": {"defaults": {"bootstrapTotalMaxChars": 1}}}
    assert context_profile_from_config(cfg, customs) == "lab"


def test_stale_exception_is_recoverable_from_the_report():
    # A bot matching the POD baseline under a live exception is drift, and
    # the row must say so distinctly — B2's reconcile must not "fix" the
    # bot toward the stale exception.
    baseline = PodBaseline(
        surfaces={s: "unset" for s in SURFACES} | {"exec_policy": "full"},
        exceptions=[BaselineException(
            bot_id="security-bot", surface="exec_policy", value="deny",
            reason="locked down", declared_at="2026-08-15T00:00:00Z",
        )],
    )
    reading = BotReading(bot_id="security-bot", surfaces={
        s: SurfaceReading("unset") for s in SURFACES
    } | {"exec_policy": SurfaceReading("full")})
    report = classify_readings(baseline, [reading])
    row = next(r for r in report.rows if r.surface == "exec_policy")
    assert row.state == STATE_LOOSENED and row.expected == "deny"
    assert row.pod_baseline_value == "full"
    assert row.stale_exception is True
    assert row.to_dict()["stale_exception"] is True
    # Real drift under an exception is NOT stale.
    reading2 = BotReading(bot_id="security-bot", surfaces={
        s: SurfaceReading("unset") for s in SURFACES
    } | {"exec_policy": SurfaceReading("allowlist")})
    row2 = next(r for r in classify_readings(baseline, [reading2]).rows
                if r.surface == "exec_policy")
    assert row2.state == STATE_LOOSENED and row2.stale_exception is False


def test_classify_readings_tolerates_missing_surface_key():
    baseline = PodBaseline(surfaces={s: "unset" for s in SURFACES})
    reading = BotReading(bot_id="bot-a", surfaces={"exec_policy": SurfaceReading("unset")})
    report = classify_readings(baseline, [reading])
    states = {r.surface: r.state for r in report.rows}
    assert states["exec_policy"] == "conform"
    assert all(states[s] == "unreadable" for s in SURFACES if s != "exec_policy")


# ── Q7(b): undeclared surfaces in the census ─────────────────────────────────

def _declared_only(*surfaces, **values):
    """A baseline declaring only the named surfaces; the rest undeclared."""
    return PodBaseline(
        surfaces={s: values[s] for s in surfaces},
        undeclared=[s for s in SURFACES if s not in surfaces],
    )


def test_undeclared_surface_rows_are_never_conform_or_drift():
    baseline = _declared_only("exec_policy", exec_policy="full")
    assert baseline.validate() == []
    reading = BotReading(bot_id="bot-a", surfaces={
        "exec_policy": SurfaceReading("full"),
        "tool_profile": SurfaceReading("coding"),
        "browser": SurfaceReading("unset"),
        "context_profile": SurfaceReading("custom"),
        "model_policy": SurfaceReading("custom"),
    })
    report = classify_readings(baseline, [reading])
    states = {r.surface: r.state for r in report.rows}
    assert states["exec_policy"] == STATE_CONFORM
    assert all(states[s] == STATE_UNDECLARED
               for s in SURFACES if s != "exec_policy")
    # No fabricated pod value stands in for the missing declaration.
    for row in report.rows:
        if row.state == STATE_UNDECLARED:
            assert row.pod_baseline_value == "" and row.expected == ""
    assert report.undeclared_surfaces == [
        "tool_profile", "browser", "context_profile", "model_policy",
    ]


def test_undeclared_distribution_collapses_the_per_bot_rows():
    baseline = _declared_only("exec_policy", exec_policy="full")
    readings = [
        BotReading(bot_id=f"bot-{i}", surfaces={
            s: SurfaceReading("full" if s == "exec_policy"
                              else ("coding" if i < 2 else "unset"))
            for s in SURFACES
        })
        for i in range(5)
    ]
    dist = classify_readings(baseline, readings).undeclared_distribution()
    assert dist["tool_profile"] == {"coding": 2, "unset": 3}
    assert set(dist) == {"tool_profile", "browser", "context_profile", "model_policy"}
    assert "exec_policy" not in dist


def test_unreadable_beats_undeclared_and_stays_out_of_the_distribution():
    # "We could not look" must not launder into an observed value inside a
    # pod-level silence line.
    baseline = _declared_only("exec_policy", exec_policy="full")
    readings = [
        BotReading(bot_id="bot-a", surfaces={
            s: SurfaceReading("full" if s == "exec_policy" else "unset")
            for s in SURFACES
        }),
        BotReading(bot_id="bot-b", surfaces={
            s: (SurfaceReading("full") if s == "exec_policy"
                else SurfaceReading(None, "sudo_rc=1"))
            for s in SURFACES
        }),
    ]
    report = classify_readings(baseline, readings)
    counts = report.counts()
    assert counts[STATE_UNREADABLE] == 4
    assert counts[STATE_UNDECLARED] == 4
    assert report.undeclared_distribution()["browser"] == {"unset": 1}


def test_exception_survives_on_an_undeclared_surface():
    # An exception IS a declared intent for that bot; classifying it as
    # "nobody declared this" would discard a real declaration.
    baseline = _declared_only("exec_policy", exec_policy="full")
    baseline.exceptions = [BaselineException(
        bot_id="kiosk-bot", surface="browser", value="off",
        reason="no browser on the kiosk", declared_at="2026-08-22T00:00:00Z",
    )]
    assert baseline.validate() == []
    readings = [
        BotReading(bot_id="kiosk-bot", surfaces={
            s: SurfaceReading("full" if s == "exec_policy" else "off")
            for s in SURFACES
        }),
        BotReading(bot_id="other-bot", surfaces={
            s: SurfaceReading("full" if s == "exec_policy" else "off")
            for s in SURFACES
        }),
    ]
    report = classify_readings(baseline, readings)
    by_key = {(r.bot_id, r.surface): r for r in report.rows}
    assert by_key[("kiosk-bot", "browser")].state == STATE_EXCEPTION
    assert by_key[("other-bot", "browser")].state == STATE_UNDECLARED
    # The exception row is not an undeclared row, so it is not in the
    # collapsed distribution either.
    assert report.undeclared_distribution()["browser"] == {"off": 1}


def test_a_surface_in_neither_list_classifies_undeclared_not_conform():
    # validate() reports this as a problem, but classification must not
    # fall back to comparing against a fabricated default — reporting rows
    # conform against a value nobody wrote is the failure Q7 exists to end.
    baseline = PodBaseline(surfaces={"exec_policy": "full"}, undeclared=[])
    assert baseline.validate()  # loud, as it should be
    reading = BotReading(bot_id="bot-a", surfaces={
        s: SurfaceReading("unset") for s in SURFACES
    } | {"exec_policy": SurfaceReading("full")})
    states = {r.surface: r.state for r in classify_readings(baseline, [reading]).rows}
    assert states["exec_policy"] == STATE_CONFORM
    assert all(states[s] == STATE_UNDECLARED for s in SURFACES if s != "exec_policy")


def test_report_to_dict_carries_the_undeclared_facts_for_b2():
    baseline = _declared_only("exec_policy", exec_policy="full")
    reading = BotReading(bot_id="bot-a", surfaces={
        s: SurfaceReading("full" if s == "exec_policy" else "custom")
        for s in SURFACES
    })
    payload = classify_readings(baseline, [reading]).to_dict()
    assert payload["undeclared_surfaces"] == [
        "tool_profile", "browser", "context_profile", "model_policy",
    ]
    assert payload["undeclared_distribution"]["model_policy"] == {"custom": 1}
    assert payload["counts"][STATE_UNDECLARED] == 4


def test_direction_reaches_the_rows_through_classify_readings():
    # The surface name must actually thread through to the ordering; if it
    # did not, everything here would read DIVERGENT.
    baseline = PodBaseline(
        surfaces={"exec_policy": "allowlist", "tool_profile": "coding",
                  "browser": "on", "context_profile": "balanced",
                  "model_policy": "pod-defaults"},
    )
    reading = BotReading(bot_id="bot-a", surfaces={
        "exec_policy": SurfaceReading("deny"),        # tighter
        "tool_profile": SurfaceReading("full"),       # looser
        "browser": SurfaceReading("off"),             # tighter
        "context_profile": SurfaceReading("lean"),    # cost axis — divergent
        "model_policy": SurfaceReading("custom"),     # provenance — divergent
    })
    states = {r.surface: r.state for r in classify_readings(baseline, [reading]).rows}
    assert states == {
        "exec_policy": STATE_TIGHTENED,
        "tool_profile": STATE_LOOSENED,
        "browser": STATE_TIGHTENED,
        "context_profile": STATE_DIVERGENT,
        "model_policy": STATE_DIVERGENT,
    }


# ── Review fixes (2026-08-23 adversarial pass) ───────────────────────────────

def test_surface_undeclared_flag_is_independent_of_row_state():
    """B2's rule is per-SURFACE; the row state cannot express it.

    A bot with a declared exception on an undeclared surface classifies
    against that exception — including a drift DIRECTION. Keying B2's
    "no per-bot drift Signal for an undeclared surface" off the state would
    emit an alert on a surface nobody declared; keying it off this flag
    does not.
    """
    baseline = _declared_only("exec_policy", exec_policy="full")
    baseline.exceptions = [BaselineException(
        bot_id="lab-bot", surface="tool_profile", value="coding",
        reason="lab box", declared_at="2026-08-22T00:00:00Z",
    )]
    assert baseline.validate() == []
    readings = [
        BotReading(bot_id="lab-bot", surfaces={
            s: SurfaceReading("full") for s in SURFACES
        }),
        BotReading(bot_id="plain-bot", surfaces={
            s: SurfaceReading("full") for s in SURFACES
        }),
    ]
    report = classify_readings(baseline, readings)
    by_key = {(r.bot_id, r.surface): r for r in report.rows}

    exc_row = by_key[("lab-bot", "tool_profile")]
    # The state is a drift DIRECTION, not `undeclared`...
    assert exc_row.state == STATE_LOOSENED
    # ...but the surface is undeclared, and the row says so.
    assert exc_row.surface_undeclared is True
    assert exc_row.to_dict()["surface_undeclared"] is True

    plain_row = by_key[("plain-bot", "tool_profile")]
    assert plain_row.state == STATE_UNDECLARED and plain_row.surface_undeclared is True
    # A declared surface never carries the flag.
    assert by_key[("lab-bot", "exec_policy")].surface_undeclared is False

    # The exact predicate B2 must use, spelled out so it is testable: 4
    # undeclared surfaces × 2 bots. Keying off the state instead would miss
    # the exception row and let a fabricated `alert` through.
    b2_suppressed = {(r.bot_id, r.surface) for r in report.rows if r.surface_undeclared}
    assert len(b2_suppressed) == 8
    assert ("lab-bot", "tool_profile") in b2_suppressed
    by_state = {(r.bot_id, r.surface) for r in report.rows
                if r.state == STATE_UNDECLARED}
    assert ("lab-bot", "tool_profile") not in by_state


def test_declared_sentinel_surfaces_are_named():
    """The pre-Q7 baseline both live pods carry must not land inert.

    Seeding can no longer elect `custom`/`unset`, but an existing file
    still declares them — and those rows still classify conform. The
    census cannot rewrite the baseline, so it must name the condition.
    """
    baseline = PodBaseline(surfaces={
        "exec_policy": "full", "tool_profile": "unset", "browser": "unset",
        "context_profile": "custom", "model_policy": "custom",
    })
    assert baseline.validate() == []  # a valid file — that is the problem
    reading = BotReading(bot_id="bot-a", surfaces={
        "exec_policy": SurfaceReading("full"),
        "tool_profile": SurfaceReading("unset"),
        "browser": SurfaceReading("unset"),
        "context_profile": SurfaceReading("custom"),
        "model_policy": SurfaceReading("custom"),
    })
    report = classify_readings(baseline, [reading])
    assert report.declared_sentinel_surfaces == [
        "tool_profile", "browser", "context_profile", "model_policy",
    ]
    assert report.to_dict()["declared_sentinel_surfaces"] == \
        report.declared_sentinel_surfaces
    # Still conform (the file says so) — which is exactly why it is flagged.
    assert {k: v for k, v in report.counts().items() if v} == {STATE_CONFORM: 5}
    assert report.undeclared_surfaces == []


def test_a_post_q7_baseline_has_no_declared_sentinels():
    baseline = _declared_only("exec_policy", exec_policy="full")
    reading = BotReading(bot_id="bot-a", surfaces={
        s: SurfaceReading("full") for s in SURFACES
    })
    assert classify_readings(baseline, [reading]).declared_sentinel_surfaces == []


def test_undeclared_excluded_accounts_for_every_missing_row():
    baseline = _declared_only("exec_policy", exec_policy="full")
    baseline.exceptions = [BaselineException(
        bot_id="bot-x", surface="browser", value="off",
        reason="kiosk", declared_at="2026-08-22T00:00:00Z",
    )]
    readings = [
        BotReading(bot_id="bot-a", surfaces={
            s: SurfaceReading("full" if s == "exec_policy" else "unset")
            for s in SURFACES
        }),
        BotReading(bot_id="bot-x", surfaces={
            s: SurfaceReading("full" if s == "exec_policy" else "off")
            for s in SURFACES
        }),
        BotReading(bot_id="bot-z", surfaces={
            s: (SurfaceReading("full") if s == "exec_policy"
                else SurfaceReading(None, "sudo_rc=1"))
            for s in SURFACES
        }),
    ]
    report = classify_readings(baseline, readings)
    dist = report.undeclared_distribution()
    excluded = report.undeclared_excluded()
    # browser: bot-a undeclared, bot-x under an exception, bot-z unreadable.
    assert dist["browser"] == {"unset": 1}
    assert excluded["browser"] == {STATE_EXCEPTION: 1, STATE_UNREADABLE: 1}
    # Distribution + excluded together account for every bot on the surface.
    for surface in report.undeclared_surfaces:
        covered = sum(dist.get(surface, {}).values()) + \
            sum(excluded.get(surface, {}).values())
        assert covered == len(readings), surface
    # A declared surface never appears in the excluded map.
    assert "exec_policy" not in excluded
