"""tests/test_forge_invocation_mode_default.py — forge defaults at-risk
script-backed apps off the leaky agent_invokes mode.

Spec: internal/spec-agent-freelance-bypass-phase2-2026-06-06.md.

`forge_engine._normalize_freelance_invocation` runs in Phase 5c.0 (just before
the bot_guidance freelance-bypass gate). When a forged app's bot_guidance
routes a chat trigger through a bot-local script, it upgrades the manifest from
``invocation_mode='agent_invokes'`` to ``'plugin_intercept'`` and synthesizes a
complete ``invocation`` contract on every ``event_triggers[]`` entry so Layer C
intercepts the trigger structurally instead of trusting the LLM (which can
freelance a confabulated success when the script fails).

These tests pin:
  * at-risk-shaped spec → plugin_intercept + full invocation contract that
    PASSES ``bot_guidance_freelance_validator`` (the Phase 5c gate),
  * non-script (non-at-risk) app → untouched,
  * the no-op guard rails (no triggers / already opted-in / un-convertible
    triggers / authored-field preservation / invokes-derived script path).

The marker-detection itself is owned by
``test_bot_guidance_freelance_validator.py``; this file pins the forge wiring.
"""

from __future__ import annotations

from evolve_admin.applications.forge_engine import (
    _DEFAULT_FREELANCE_FALLBACK,
    _normalize_freelance_invocation,
)
from evolve_admin.applications.bot_guidance_freelance_validator import (
    validate_bot_guidance,
)
from evolve_admin.applications.manifest import ApplicationManifest


# ── Fixtures ────────────────────────────────────────────────────────────────


def _at_risk_guidance() -> list:
    """bot_guidance prose that routes a chat trigger through a bot-local
    script — trips the validator's at-risk-shaped heuristic AND carries a
    mineable ``python3 scripts/<name>.py`` path."""
    return [
        {
            "section": "Daily Tasks",
            "content": (
                "When a member messages you, run exactly: python3 "
                "scripts/tasks.py /tmp/tasks-<msg.id>.json. The script's reply "
                "is the response. Do NOT freelance with your general tools if "
                "the script invocation fails."
            ),
        }
    ]


def _bare_trigger(
    *, pattern: str = r"^\s*/tasks\b", invokes: str = "tasks",
) -> dict:
    """A v7 event_trigger with no invocation block — the leaky default an LLM
    build typically emits."""
    return {
        "id": "tasks_cmd",
        "source": "telegram",
        "audience": "members",
        "invokes": invokes,
        "match": {
            "channel": "telegram_dm",
            "pattern": pattern,
            "exclude_pattern": None,
        },
    }


def _manifest(**kw) -> ApplicationManifest:
    base = dict(id="tasks", name="Daily Tasks", bot_id="test-bot")
    base.update(kw)
    return ApplicationManifest(**base)


# ── At-risk script app → plugin_intercept + full contract ───────────────────


def test_at_risk_script_app_upgrades_to_plugin_intercept():
    m = _manifest(
        bot_guidance=_at_risk_guidance(),
        event_triggers=[_bare_trigger()],
    )
    assert m.invocation_mode == "agent_invokes"  # leaky default in

    changed = _normalize_freelance_invocation(m)

    assert changed is True
    assert m.invocation_mode == "plugin_intercept"

    inv = m.event_triggers[0]["invocation"]
    # Script mined from prose (consistent with invokes="tasks").
    assert inv["script"] == "scripts/tasks.py"
    # Generic protocol the plugin can actually parse.
    assert inv["stdout_protocol"] == "raw_text"
    assert inv["on_failure"] == "post_fallback"
    assert inv["fallback_text"] == _DEFAULT_FREELANCE_FALLBACK
    # Request plumbing the plugin always passes as argv[1].
    assert "{message_id}" in inv["request_file_template"]
    assert inv["request_payload"]["message_text"] == "{message_text}"
    assert inv["request_payload"]["from_id"] == "{from_id}"


def test_upgraded_manifest_passes_the_freelance_bypass_gate():
    """The whole point: the synthesized contract must clear the Phase 5c
    validator (build_blocker) it runs through immediately after."""
    m = _manifest(
        bot_guidance=_at_risk_guidance(),
        event_triggers=[
            _bare_trigger(),
            _bare_trigger(pattern=r"(?<![A-Za-z0-9_])@[A-Za-z]\w{2,}\b"),
        ],
    )
    assert _normalize_freelance_invocation(m) is True

    res = validate_bot_guidance(m)
    assert res["ok"] is True, res
    assert res["invocation_mode"] == "plugin_intercept"
    assert res["severity"] == "info"
    assert res["trigger_count"] == 2


def test_at_risk_app_without_prose_script_derives_from_invokes():
    """At-risk by hard-stop language but no mineable python3 path → the script
    is derived from the trigger's invokes logical_name (validator-consistent)."""
    m = _manifest(
        bot_guidance=[
            {
                "section": "Rules",
                "content": (
                    "Do NOT freelance with your general tools if the script "
                    "invocation fails — post the failure message instead."
                ),
            }
        ],
        event_triggers=[_bare_trigger(invokes="runner")],
    )
    assert _normalize_freelance_invocation(m) is True
    assert m.event_triggers[0]["invocation"]["script"] == "scripts/runner.py"
    assert validate_bot_guidance(m)["ok"] is True


def test_authored_invocation_fields_are_preserved():
    trig = _bare_trigger(invokes="custom")
    trig["invocation"] = {
        "script": "scripts/custom.py",
        "stdout_protocol": "atlas_research",   # known author choice — keep
        "on_failure": "silent",                # keep (silent needs no fallback)
        "request_file_template": "/tmp/custom-{message_id}.json",
        "request_payload": {"q": "{message_text}"},
    }
    m = _manifest(bot_guidance=_at_risk_guidance(), event_triggers=[trig])

    assert _normalize_freelance_invocation(m) is True

    inv = m.event_triggers[0]["invocation"]
    assert inv["script"] == "scripts/custom.py"
    assert inv["stdout_protocol"] == "atlas_research"
    assert inv["on_failure"] == "silent"
    assert inv["request_payload"] == {"q": "{message_text}"}
    # silent path doesn't require fallback_text, so an empty one is fine.
    assert validate_bot_guidance(m)["ok"] is True


def test_unknown_authored_protocol_is_replaced_with_raw_text():
    """A bogus author-set stdout_protocol (the plugin would drop it) gets
    rewritten to the generic raw_text so the contract is runtime-viable and
    passes the validator."""
    trig = _bare_trigger()
    trig["invocation"] = {"stdout_protocol": "made_up_protocol"}
    m = _manifest(bot_guidance=_at_risk_guidance(), event_triggers=[trig])

    assert _normalize_freelance_invocation(m) is True
    assert m.event_triggers[0]["invocation"]["stdout_protocol"] == "raw_text"
    assert validate_bot_guidance(m)["ok"] is True


# ── No-op cases ─────────────────────────────────────────────────────────────


def test_non_script_app_is_unchanged():
    m = _manifest(
        bot_guidance=[
            {"section": "Setup", "content": "Run the daily cron at 9am; call journal-add."}
        ],
        event_triggers=[_bare_trigger()],
    )
    changed = _normalize_freelance_invocation(m)

    assert changed is False
    assert m.invocation_mode == "agent_invokes"
    assert "invocation" not in m.event_triggers[0]


def test_at_risk_with_no_triggers_stays_agent_invokes():
    """plugin_intercept requires a non-empty trigger list; the validator
    treats at-risk + agent_invokes + no-triggers as info-level, so we leave
    it alone rather than flipping into a build_blocker."""
    m = _manifest(bot_guidance=_at_risk_guidance(), event_triggers=[])
    assert _normalize_freelance_invocation(m) is False
    assert m.invocation_mode == "agent_invokes"


def test_already_plugin_intercept_is_not_second_guessed():
    trig = _bare_trigger()
    trig["invocation"] = {
        "script": "scripts/tasks.py",
        "stdout_protocol": "raw_text",
        "on_failure": "post_fallback",
        "fallback_text": "nope",
    }
    m = _manifest(
        bot_guidance=_at_risk_guidance(),
        event_triggers=[trig],
        invocation_mode="plugin_intercept",
    )
    before = dict(m.event_triggers[0]["invocation"])
    assert _normalize_freelance_invocation(m) is False
    assert m.event_triggers[0]["invocation"] == before


def test_subagent_mode_is_not_touched():
    m = _manifest(
        bot_guidance=_at_risk_guidance(),
        event_triggers=[_bare_trigger()],
        invocation_mode="subagent",
    )
    assert _normalize_freelance_invocation(m) is False
    assert m.invocation_mode == "subagent"


def test_trigger_without_pattern_leaves_manifest_at_agent_invokes():
    """plugin_intercept needs every trigger contract-clean; a trigger with no
    match.pattern can't be converted, so the whole manifest stays put."""
    bad = {
        "id": "no_pattern",
        "source": "telegram",
        "audience": "members",
        "invokes": "tasks",
        "match": {"channel": "telegram_dm", "exclude_pattern": None},
    }
    m = _manifest(
        bot_guidance=_at_risk_guidance(),
        event_triggers=[_bare_trigger(), bad],
    )
    assert _normalize_freelance_invocation(m) is False
    assert m.invocation_mode == "agent_invokes"
    # First trigger must NOT have been mutated (all-or-nothing commit).
    assert "invocation" not in m.event_triggers[0]


def test_trigger_with_uncompilable_pattern_is_a_noop():
    m = _manifest(
        bot_guidance=_at_risk_guidance(),
        event_triggers=[_bare_trigger(pattern=r"([unclosed")],
    )
    assert _normalize_freelance_invocation(m) is False
    assert m.invocation_mode == "agent_invokes"
