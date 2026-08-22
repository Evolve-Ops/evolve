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
from pod_baseline.schema import SURFACES, BaselineException, PodBaseline


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

    assert states[("bot-b", "exec_policy")] == "drift"
    assert states[("bot-b", "tool_profile")] == "drift"  # custom-allow vs coding
    assert states[("bot-b", "browser")] == "drift"
    assert states[("bot-b", "context_profile")] == "conform"
    assert states[("bot-b", "model_policy")] == "drift"  # custom vs pod-defaults

    for surface in ("exec_policy", "tool_profile", "browser", "context_profile"):
        assert states[("bot-c", surface)] == "unreadable"
    assert states[("bot-c", "model_policy")] == "conform"  # no tiers file = defaults

    assert states[("security-bot", "exec_policy")] == "exception"
    assert states[("security-bot", "browser")] == "conform"

    counts = report.counts()
    # 4 bots × 5 surfaces = 20 rows: bot-a 5 conform, bot-b 1 conform + 4
    # drift, bot-c 4 unreadable + 1 conform, security-bot 1 exception + 4 conform.
    assert counts == {"conform": 11, "drift": 4, "unreadable": 4, "exception": 1}
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
    # A bot matching the POD baseline under a live exception is DRIFT, and
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
    assert row.state == "drift" and row.expected == "deny"
    assert row.pod_baseline_value == "full"
    assert row.stale_exception is True
    assert row.to_dict()["stale_exception"] is True
    # Real drift under an exception is NOT stale.
    reading2 = BotReading(bot_id="security-bot", surfaces={
        s: SurfaceReading("unset") for s in SURFACES
    } | {"exec_policy": SurfaceReading("allowlist")})
    row2 = next(r for r in classify_readings(baseline, [reading2]).rows
                if r.surface == "exec_policy")
    assert row2.state == "drift" and row2.stale_exception is False


def test_classify_readings_tolerates_missing_surface_key():
    baseline = PodBaseline(surfaces={s: "unset" for s in SURFACES})
    reading = BotReading(bot_id="bot-a", surfaces={"exec_policy": SurfaceReading("unset")})
    report = classify_readings(baseline, [reading])
    states = {r.surface: r.state for r in report.rows}
    assert states["exec_policy"] == "conform"
    assert all(states[s] == "unreadable" for s in SURFACES if s != "exec_policy")
