"""tests/test_home_chat.py — Home conversational LLM layer.

Covers the home_chat module's three layers in isolation:
  * cost cap tracking + atomic file writes
  * pod-state digest rendering
  * call_llm() with a stub transport (no network)

Plus the /api/home/chat endpoint via the Flask test client.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ADMIN_PKG = Path(__file__).parent.parent
if str(_ADMIN_PKG) not in sys.path:
    sys.path.insert(0, str(_ADMIN_PKG))

import pytest  # noqa: E402

from evolve_admin.web import home_chat as _hc  # noqa: E402

# Provider-qualified test model — home_chat models are resolved via
# infra_llm and always carry the provider prefix (#3466).
_TEST_MODEL = "anthropic/claude-haiku-4-5"


# ─────────────────────────────────────────────────────────────────────────────
# Cost cap tracking
# ─────────────────────────────────────────────────────────────────────────────


def test_read_usage_fresh_when_missing(tmp_path):
    u = _hc.read_usage(tmp_path, now=datetime(2026, 5, 18, tzinfo=timezone.utc))
    assert u == {"date": "2026-05-18", "calls": 0, "cost_usd": 0.0}


def test_read_usage_returns_today_when_present(tmp_path):
    (tmp_path / "home-chat-usage.json").write_text(json.dumps({
        "date": "2026-05-18", "calls": 5, "cost_usd": 0.0123,
    }))
    u = _hc.read_usage(tmp_path, now=datetime(2026, 5, 18, tzinfo=timezone.utc))
    assert u == {"date": "2026-05-18", "calls": 5, "cost_usd": 0.0123}


def test_read_usage_resets_when_stale(tmp_path):
    """Yesterday's file → today reads as zero."""
    (tmp_path / "home-chat-usage.json").write_text(json.dumps({
        "date": "2026-05-17", "calls": 99, "cost_usd": 5.0,
    }))
    u = _hc.read_usage(tmp_path, now=datetime(2026, 5, 18, tzinfo=timezone.utc))
    assert u["calls"] == 0
    assert u["cost_usd"] == 0.0


def test_bump_usage_atomic_write(tmp_path):
    now = datetime(2026, 5, 18, tzinfo=timezone.utc)
    u1 = _hc.bump_usage(tmp_path, cost_usd=0.001, now=now)
    u2 = _hc.bump_usage(tmp_path, cost_usd=0.002, now=now)
    assert u1["calls"] == 1
    assert u2["calls"] == 2
    assert u2["cost_usd"] == 0.003


def test_cap_status_reflects_used(tmp_path):
    now = datetime(2026, 5, 18, tzinfo=timezone.utc)
    _hc.bump_usage(tmp_path, cost_usd=0.001, now=now)
    _hc.bump_usage(tmp_path, cost_usd=0.001, now=now)
    cap = _hc.cap_status(tmp_path, daily_cap=10, now=now)
    assert cap == {
        "used": 2,
        "remaining": 8,
        "daily_cap": 10,
        "cost_today_usd": 0.002,
    }


def test_cap_status_remaining_clamped_to_zero(tmp_path):
    now = datetime(2026, 5, 18, tzinfo=timezone.utc)
    for _ in range(15):
        _hc.bump_usage(tmp_path, cost_usd=0.001, now=now)
    cap = _hc.cap_status(tmp_path, daily_cap=10, now=now)
    assert cap["used"] == 15
    assert cap["remaining"] == 0    # never negative


# ─────────────────────────────────────────────────────────────────────────────
# Pod-state digest
# ─────────────────────────────────────────────────────────────────────────────


def test_digest_empty_inputs_returns_placeholder():
    out = _hc.build_pod_state_digest()
    assert "no pod state available" in out


def test_digest_summarizes_bots():
    bots = {
        "team_bot_a": {"live": True},
        "team_bot_b": {"live": False, "last_metric_date": "2026-05-18T08:00:00Z"},
        "admin_bot": {},
    }
    out = _hc.build_pod_state_digest(bots=bots)
    assert "1/3 online" in out
    assert "team_bot_a: online" in out
    assert "team_bot_b: active" in out
    assert "admin_bot: offline" in out


def test_digest_includes_version_drift_flag():
    bots = {"team_bot_a": {"live": True, "evolve_synced": False}}
    out = _hc.build_pod_state_digest(bots=bots)
    assert "version drift" in out


def test_digest_lists_firing_signals_with_vector_tag():
    firing = [
        {
            "title": "Team_bot_a Slack gateway unreachable",
            "producer": "sysadmin_watchdog",
            "bot_id": "team_bot_a",
            "severity_framework": {"vector": "operations", "magnitude": 3},
        },
    ]
    out = _hc.build_pod_state_digest(firing_signals=firing)
    assert "Firing alerts (1 total)" in out
    assert "Team_bot_a Slack gateway unreachable" in out
    assert "operations:3" in out
    assert "(team_bot_a)" in out


def test_digest_truncates_long_signal_list():
    firing = [
        {"title": f"sig {i}", "producer": "x",
         "severity_framework": {"vector": "operations", "magnitude": 1}}
        for i in range(15)
    ]
    out = _hc.build_pod_state_digest(firing_signals=firing, max_signals=5)
    assert "and 10 more" in out


def test_digest_lists_pending_proposals():
    proposals = [
        {"status": "pending", "admin_surface_summary": "Raise team_bot_a cache TTL",
         "bot_id": "team_bot_a", "urgency": "improvement"},
        {"status": "applied"},   # filtered
    ]
    out = _hc.build_pod_state_digest(pending_proposals=proposals)
    assert "Pending proposals (1)" in out
    assert "Raise team_bot_a cache TTL" in out
    assert "team_bot_a" in out
    assert "improvement" in out


def test_digest_urgent_block_lists_down_bots_with_metric_history():
    """Bots that previously reported metrics and are now offline are an
    active outage; the digest leads with an URGENT block listing them by
    name so the narrative prompt headlines the right thing. Regression
    for the 2026-06-03 OC-upgrade outage where the briefing buried
    "gateway down" under "noise across plugins, permissions, and cost
    tracking"."""
    bots = {
        "team_bot_a": {
            "live": False,
            "last_metric_date": "2026-06-02T08:00:00Z",
        },
        "team_bot_b": {
            "live": False,
            "last_metric_date": "2026-06-02T08:00:00Z",
        },
        "admin_bot": {"live": True},
    }
    out = _hc.build_pod_state_digest(bots=bots)
    assert "⚠ URGENT" in out
    assert "2 bot gateways DOWN" in out
    assert "team_bot_a" in out
    assert "team_bot_b" in out


def test_digest_urgent_block_skips_never_deployed_bots():
    """A bot that has never reported a metric is likely not-yet-deployed
    rather than down — including it in the URGENT block would noise up
    the headline on fresh pods. last_metric_date is the cheap proxy that
    matches what the Overview tile already shows."""
    bots = {
        "fresh_bot": {"live": False, "last_metric_date": None},
        "real_outage_bot": {
            "live": False,
            "last_metric_date": "2026-06-02T08:00:00Z",
        },
    }
    out = _hc.build_pod_state_digest(bots=bots)
    assert "URGENT" in out
    assert "1 bot gateway DOWN" in out
    assert "real_outage_bot" in out
    assert "fresh_bot" not in out.split("URGENT")[1].split("\n")[0]


def test_digest_urgent_block_precedes_pod_health_and_signals():
    """URGENT outage > pod-health WARN/FAIL > firing signals — order in
    the digest mirrors the priority guidance in the narrative prompt."""
    bots = {
        "team_bot_a": {"live": False, "last_metric_date": "2026-06-02"},
    }
    pod_health = {
        "summary": {"fail": 1, "warn": 0},
        "checks": [{
            "status": "FAIL", "category": "perms", "name": "x", "detail": "y",
        }],
    }
    firing = [
        {"title": "Some other signal", "producer": "p",
         "severity_framework": {"vector": "v", "magnitude": 1}},
    ]
    out = _hc.build_pod_state_digest(
        bots=bots, pod_health=pod_health, firing_signals=firing,
    )
    urgent_idx = out.index("URGENT")
    pod_health_idx = out.index("Pod health")
    firing_idx = out.index("Firing alerts")
    assert urgent_idx < pod_health_idx < firing_idx


def test_diagnostic_reply_uses_diagnostic_prompt_and_returns_text():
    """generate_diagnostic_reply uses a distinct system prompt (NOT-evo,
    no tools) and returns the standard {text, tokens, cost, model} shape."""
    captured: dict = {}

    def _fake_transport(system_prompt, messages, api_key, model, max_tokens):
        captured["system_prompt"] = system_prompt
        captured["messages"] = messages
        return {"text": "Try restarting the gateway.", "input_tokens": 100, "output_tokens": 20}

    out = _hc.generate_diagnostic_reply(
        user_message="evo is down, what do I do?",
        history=[],
        pod_state_digest="⚠ URGENT — 1 bot gateway DOWN: team_bot_a\n",
        api_key="sk-test",
        model=_TEST_MODEL,
        transport=_fake_transport,
    )
    assert out["text"] == "Try restarting the gateway."
    # Prompt makes the not-evo framing explicit so the LLM doesn't roleplay
    # as the down bot.
    assert "NOT evo" in captured["system_prompt"]
    assert "diagnostic" in captured["system_prompt"].lower()
    # Digest is interpolated so the model sees the outage state.
    assert "URGENT" in captured["system_prompt"]
    # User message rides on top of any history, last in the list.
    assert captured["messages"][-1]["content"] == "evo is down, what do I do?"


def test_diagnostic_reply_threads_conversation_history():
    """Outage triage is a back-and-forth — history must reach the LLM so
    follow-up questions aren't context-free."""
    seen: dict = {}

    def _fake_transport(system_prompt, messages, api_key, model, max_tokens):
        seen["messages"] = messages
        return {"text": "ok", "input_tokens": 1, "output_tokens": 1}

    # The UI emits role="evo" for assistant turns (matches the home_chat
    # session log); _sanitize_history maps that to API "assistant".
    history = [
        {"role": "user", "text": "evo is unresponsive"},
        {"role": "evo", "text": "check the LaunchDaemon status"},
    ]
    _hc.generate_diagnostic_reply(
        user_message="LaunchDaemon shows running but gateway is dead",
        history=history,
        pod_state_digest="",
        api_key="sk-test",
        model=_TEST_MODEL,
        transport=_fake_transport,
    )
    roles = [m["role"] for m in seen["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert seen["messages"][-1]["content"].startswith("LaunchDaemon")


def test_digest_includes_host_when_available():
    host = {
        "cpu_percent": 12, "memory": {"percent": 60},
        "disk": {"percent": 80}, "available": True,
    }
    out = _hc.build_pod_state_digest(host_health=host)
    assert "Host:" in out
    assert "CPU 12%" in out
    assert "Disk 80%" in out


def test_digest_omits_host_when_unavailable():
    out = _hc.build_pod_state_digest(host_health={"available": False})
    assert "Host:" not in out


def test_digest_headlines_tripped_breaker():
    """A tripped breaker means a bot is suspended. The digest must
    surface it as its own section (not buried in 'Firing alerts') so
    the LLM can lead with it, AND it must include the server-computed
    trip-age phrase so the LLM doesn't hallucinate relative time
    (issue #2165: 22h-old trip narrated as 'about an hour ago')."""
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    breakers = [
        {
            "bot_id": "security_bot",
            "type": "cost",
            "state": "tripped",
            "tripped_at": "2026-05-27T14:00:00+00:00",  # 22h ago
            "expires_at": "2026-05-29T06:35:00+00:00",  # in 18h 35m
            "initiated_by": "auto",
            "reason": "daily cap hit",
        },
    ]
    out = _hc.build_pod_state_digest(active_breakers=breakers, now=now)
    assert "Tripped breakers (1)" in out
    assert "security_bot" in out
    assert "cost cap tripped" in out
    assert "auto" in out
    assert "tripped 22h 0m ago" in out
    assert "reactivates in 18h 35m" in out
    assert "daily cap hit" in out


def test_format_breaker_trip_age_buckets():
    """Resolution buckets parallel _format_breaker_eta — sub-minute,
    sub-hour, sub-day, multi-day. Pre-computing these phrases is the
    fix for issue #2165 (LLM miscalculating relative time)."""
    now = datetime(2026, 6, 4, 8, 0, tzinfo=timezone.utc)
    fmt = _hc._format_breaker_trip_age
    assert fmt("2026-06-04T08:00:00+00:00", now=now) == "just now"
    assert fmt("2026-06-04T07:25:00+00:00", now=now) == "35m ago"
    assert fmt("2026-06-04T05:30:00+00:00", now=now) == "2h 30m ago"
    # The bug from the issue: trip at 09:03 prev day, narrated at 07:49
    # next day. Should be ~22h, not "an hour".
    assert fmt("2026-06-03T09:03:00+00:00", now=datetime(
        2026, 6, 4, 7, 49, tzinfo=timezone.utc,
    )) == "22h 46m ago"
    assert fmt("2026-06-01T04:00:00+00:00", now=now) == "3d 4h ago"


def test_format_breaker_trip_age_handles_missing_and_garbage():
    now = datetime(2026, 6, 4, 8, 0, tzinfo=timezone.utc)
    fmt = _hc._format_breaker_trip_age
    assert fmt(None, now=now) == "(trip time unknown)"
    assert fmt("", now=now) == "(trip time unknown)"
    assert fmt("not-an-iso-date", now=now) == "(trip time unknown)"


def test_digest_breaker_pod_scope_renders_pod_wide():
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    breakers = [{
        "bot_id": "pod",
        "type": "full",
        "state": "tripped",
        "expires_at": None,
        "initiated_by": "admin:pod_admin",
        "reason": "manual pause",
    }]
    out = _hc.build_pod_state_digest(active_breakers=breakers, now=now)
    assert "pod-wide: full halt tripped" in out
    assert "admin:pod_admin" in out
    assert "indefinite — manual reset required" in out


def test_digest_breaker_section_precedes_signals():
    """Ordering matters: breakers (bot is paused) outrank firing
    alerts (something to look at). The LLM prompt tells the model to
    headline breakers; the digest reinforces that by listing them
    first."""
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    breakers = [{
        "bot_id": "security_bot", "type": "cost", "state": "tripped",
        "expires_at": "2026-05-28T13:00:00+00:00",
        "initiated_by": "auto", "reason": "cap hit",
    }]
    firing = [{
        "title": "team_bot_a auth drift", "producer": "auth_probe",
        "bot_id": "team_bot_a",
        "severity_framework": {"vector": "config", "magnitude": 2},
    }]
    out = _hc.build_pod_state_digest(
        active_breakers=breakers, firing_signals=firing, now=now,
    )
    assert out.index("Tripped breakers") < out.index("Firing alerts")


def test_digest_headlines_pod_wide_pause():
    """A pod-wide pause means every bot is suspended. The digest must
    surface it above breakers and signals so the narrative leads with
    it. Regression: pause-state lives in {shared_dir}/recovery/, not
    in the Signal store, so the chat narrative was blind to it."""
    pause = {
        "paused": True,
        "paused_by": "admin:pod_admin",
        "paused_at": "2026-05-28T11:30:00+00:00",
        "reason": "investigating cost spike",
    }
    firing = [{
        "title": "team_bot_a auth drift", "producer": "auth_probe",
        "bot_id": "team_bot_a",
        "severity_framework": {"vector": "config", "magnitude": 2},
    }]
    out = _hc.build_pod_state_digest(pause_state=pause, firing_signals=firing)
    assert "Pod-wide pause active" in out
    assert "admin:pod_admin" in out
    assert "investigating cost spike" in out
    assert out.index("Pod-wide pause") < out.index("Firing alerts")


def test_digest_omits_pause_when_not_paused():
    out = _hc.build_pod_state_digest(pause_state=None)
    assert "Pod-wide pause" not in out
    out = _hc.build_pod_state_digest(pause_state={"paused": False})
    assert "Pod-wide pause" not in out


def test_digest_lists_pod_health_failures():
    """Pod-health FAIL/WARN checks come from independent infra probes
    (gateway liveness, launchd, file perms) — they don't auto-fire
    Signals, so the narrative needs them piped in explicitly."""
    health = {
        "summary": {"pass": 38, "warn": 1, "fail": 2, "ok": False},
        "checks": [
            {"status": "PASS", "category": "repo", "name": "ownership", "detail": "ok"},
            {"status": "WARN", "category": "launchd", "name": "repo-puller",
             "detail": "last run 3h ago"},
            {"status": "FAIL", "category": "gateway", "name": "team_bot_a-slack",
             "detail": "unreachable on port 18789"},
            {"status": "FAIL", "category": "perms", "name": "openclaw.json",
             "detail": "world-readable: mode 666"},
        ],
    }
    out = _hc.build_pod_state_digest(pod_health=health)
    assert "Pod health: 3 issues (2 fail, 1 warn)" in out
    assert "[FAIL] gateway/team_bot_a-slack" in out
    assert "[FAIL] perms/openclaw.json" in out
    assert "[WARN] launchd/repo-puller" in out
    fail_pos = out.index("[FAIL] gateway/team_bot_a-slack")
    warn_pos = out.index("[WARN] launchd/repo-puller")
    assert fail_pos < warn_pos


def test_digest_omits_pod_health_when_all_pass():
    health = {"summary": {"pass": 38, "warn": 0, "fail": 0, "ok": True}, "checks": []}
    out = _hc.build_pod_state_digest(pod_health=health)
    assert "Pod health:" not in out


def test_digest_pod_health_caps_check_list():
    health = {
        "summary": {"pass": 0, "warn": 0, "fail": 12, "ok": False},
        "checks": [
            {"status": "FAIL", "category": "x", "name": f"chk-{i}", "detail": "boom"}
            for i in range(12)
        ],
    }
    out = _hc.build_pod_state_digest(pod_health=health, max_health_checks=5)
    assert "and 7 more" in out


def test_digest_safe_wrapper_includes_tripped_breaker(tmp_path):
    """End-to-end: _build_pod_state_digest_safe reads breakers from
    the real breakers store under shared_dir."""
    sys.path.insert(0, str(_ADMIN_PKG.parent / "analyzer"))
    from datetime import timedelta as _td
    from breakers import store as _bstore

    _bstore.trip(
        shared_dir=tmp_path,
        scope="security_bot",
        breaker_type="cost",
        duration=_td(hours=18, minutes=35),
        initiated_by="auto",
        reason="daily cap hit",
    )

    from evolve_admin.web.home_chat_routes import _build_pod_state_digest_safe
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"sharedDir": str(tmp_path), "members": ["security_bot"]}))

    digest = _build_pod_state_digest_safe(net, tmp_path)

    assert "Tripped breakers" in digest
    assert "security_bot" in digest
    assert "cost cap tripped" in digest


def test_digest_safe_wrapper_includes_pause_and_pod_health(tmp_path):
    """End-to-end: _build_pod_state_digest_safe reads pause-state from
    {shared_dir}/recovery/ and pod-health from the Better Engine
    cache."""
    # Seed pause state
    pause_dir = tmp_path / "recovery"
    pause_dir.mkdir(parents=True)
    (pause_dir / "pause-state.json").write_text(json.dumps({
        "paused": True,
        "paused_by": "admin:pod_admin",
        "paused_at": "2026-05-28T11:30:00+00:00",
        "reason": "investigating cost spike",
    }))

    # Seed pod-health cache
    cache_dir = tmp_path / "better-engine" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "health-latest.json").write_text(json.dumps({
        "summary": {"pass": 30, "warn": 0, "fail": 1, "ok": False},
        "checks": [
            {"status": "FAIL", "category": "gateway", "name": "team_bot_a-slack",
             "detail": "unreachable"},
        ],
    }))

    from evolve_admin.web.home_chat_routes import _build_pod_state_digest_safe
    net = tmp_path / "network.json"
    net.write_text(json.dumps({"sharedDir": str(tmp_path), "members": []}))

    digest = _build_pod_state_digest_safe(net, tmp_path)

    assert "Pod-wide pause active" in digest
    assert "investigating cost spike" in digest
    assert "Pod health: 1 issues" in digest
    assert "[FAIL] gateway/team_bot_a-slack" in digest


# ─────────────────────────────────────────────────────────────────────────────
# Digest builder filters stale proposals via still_motivated
# ─────────────────────────────────────────────────────────────────────────────


def _seed_pending_for_digest_test(tmp_path: Path, *, bot_id: str = "team_bot_a"):
    """Write a pending proposal into ``tmp_path``/proposals/pending/ that the
    digest builder can pick up. Returns the live Proposal."""
    sys.path.insert(0, str(_ADMIN_PKG.parent / "analyzer"))
    from arbiter.state_machine import transition
    from arbiter.store import write_proposal
    from testing.harness import make_config_patch_proposal

    p = make_config_patch_proposal(
        target_path=f"/tmp/{bot_id}/x.json::flag",
        bot_id=bot_id,
        generator_id="test_sensor",
        claim_metric=f"test.digest.{bot_id}.metric",
    )
    transition(p, "pending", actor="arbiter")
    write_proposal(p, tmp_path)
    return p


def test_digest_filters_stale_proposal_and_archives_in_place(tmp_path):
    """A pending proposal whose claim is already met must NOT appear in
    the digest, and must be archived as ``resolved_externally`` on the
    way out so the next read agrees."""
    sys.path.insert(0, str(_ADMIN_PKG.parent / "analyzer"))
    from arbiter.store import find_proposal
    from metrics.registry import (
        MetricSpec,
        MetricValue,
        register,
        unregister,
    )

    p = _seed_pending_for_digest_test(tmp_path)

    # Register the claim metric to report a value past the up-target so
    # is_still_motivated layer 2 fires False.
    metric_name = p.claim.metric
    unregister(metric_name)
    register(
        MetricSpec(name=metric_name, description="t", unit="", source="test"),
        lambda _bot, _now: MetricValue(value=1.0, confidence=1.0),
    )
    try:
        from evolve_admin.web.home_chat_routes import _build_pod_state_digest_safe

        net = tmp_path / "network.json"
        net.write_text(json.dumps({"sharedDir": str(tmp_path), "members": ["team_bot_a"]}))
        digest = _build_pod_state_digest_safe(net, tmp_path)
    finally:
        unregister(metric_name)

    assert p.admin_surface_summary not in digest

    located = find_proposal(tmp_path, p.id)
    assert located is not None
    found, _path, subdir = located
    assert subdir == "archived"
    assert found.status == "resolved_externally"
    assert found.history[-1].actor == "home_briefing"


def test_digest_keeps_proposal_when_condition_still_active(tmp_path):
    """A pending proposal with no opinion from still_motivated stays
    surfaced. (Investigation proposals have no claim and no motivating
    signals; the layer returns None → surface.)"""
    sys.path.insert(0, str(_ADMIN_PKG.parent / "analyzer"))
    from arbiter.state_machine import transition
    from arbiter.store import find_proposal, write_proposal
    from testing.harness import make_investigation_proposal

    p = make_investigation_proposal(
        bot_id="team_bot_a",
        generator_id="evo_lookup",
        problem="Inspect team_bot_a session 51bac282",
    )
    transition(p, "pending", actor="arbiter")
    write_proposal(p, tmp_path)

    from evolve_admin.web.home_chat_routes import _build_pod_state_digest_safe

    net = tmp_path / "network.json"
    net.write_text(json.dumps({"sharedDir": str(tmp_path), "members": ["team_bot_a"]}))
    digest = _build_pod_state_digest_safe(net, tmp_path)

    assert "Inspect team_bot_a session 51bac282" in digest
    located = find_proposal(tmp_path, p.id)
    assert located is not None
    assert located[2] == "pending"


# ─────────────────────────────────────────────────────────────────────────────
# call_llm with a stub transport
# ─────────────────────────────────────────────────────────────────────────────


def test_call_llm_passes_system_prompt_and_messages():
    captured = {}

    def transport(system_prompt, messages, api_key, model, max_tokens):
        captured.update(
            system_prompt=system_prompt,
            messages=messages,
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
        )
        return {"text": "ok", "input_tokens": 100, "output_tokens": 50}

    out = _hc.call_llm(
        user_message="hello",
        history=[
            {"role": "user", "text": "hi"},
            {"role": "evo", "text": "hello back"},
        ],
        pod_state_digest="(bots: all green)",
        api_key="sk-test",
        model=_TEST_MODEL,
        transport=transport,
    )
    assert out["text"] == "ok"
    assert out["model"] == _TEST_MODEL
    assert captured["api_key"] == "sk-test"
    # System prompt embeds the digest
    assert "(bots: all green)" in captured["system_prompt"]
    # Messages: history (user/assistant) + new user turn
    assert captured["messages"][0] == {"role": "user", "content": "hi"}
    assert captured["messages"][1] == {"role": "assistant", "content": "hello back"}
    assert captured["messages"][-1] == {"role": "user", "content": "hello"}


def test_call_llm_caps_history():
    def transport(system_prompt, messages, api_key, model, max_tokens):
        return {"text": "", "input_tokens": 0, "output_tokens": 0}

    # 30 turns of history; cap=4 → only last 4 ride along + the new user message
    history = []
    for i in range(30):
        history.append({"role": "user" if i % 2 == 0 else "evo", "text": f"t{i}"})

    captured = {}
    def transport_capture(system_prompt, messages, api_key, model, max_tokens):
        captured["messages"] = messages
        return {"text": "", "input_tokens": 0, "output_tokens": 0}

    _hc.call_llm(
        user_message="new",
        history=history,
        pod_state_digest="",
        api_key="x",
        model=_TEST_MODEL,
        max_history_turns=4,
        transport=transport_capture,
    )
    # 4 history turns + 1 new user turn
    assert len(captured["messages"]) == 5
    assert captured["messages"][-1]["content"] == "new"


def test_call_llm_drops_malformed_history_entries():
    captured = {}
    def transport(system_prompt, messages, api_key, model, max_tokens):
        captured["messages"] = messages
        return {"text": "", "input_tokens": 0, "output_tokens": 0}

    _hc.call_llm(
        user_message="go",
        history=[
            {"role": "user", "text": "valid"},
            "not a dict",                   # dropped
            {"role": "weird", "text": "x"}, # dropped (unknown role)
            {"role": "user", "text": ""},   # dropped (empty)
            {"role": "evo", "text": "reply"},
        ],
        pod_state_digest="",
        api_key="x",
        model=_TEST_MODEL,
        transport=transport,
    )
    # Only valid alternating pairs survive + the new user turn
    assert captured["messages"] == [
        {"role": "user", "content": "valid"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "go"},
    ]


def test_call_llm_estimates_cost_with_haiku_rates():
    def transport(system_prompt, messages, api_key, model, max_tokens):
        return {"text": "", "input_tokens": 1_000_000, "output_tokens": 0}

    out = _hc.call_llm(
        user_message="x",
        history=[],
        pod_state_digest="",
        api_key="k",
        model=_TEST_MODEL,
        transport=transport,
    )
    # 1M input tokens × $0.25/M = $0.25
    assert out["cost_usd"] == pytest.approx(0.25, abs=1e-6)


def test_call_llm_estimates_combined_input_output_cost():
    def transport(system_prompt, messages, api_key, model, max_tokens):
        return {"text": "", "input_tokens": 1_000_000, "output_tokens": 1_000_000}

    out = _hc.call_llm(
        user_message="x",
        history=[],
        pod_state_digest="",
        api_key="k",
        model=_TEST_MODEL,
        transport=transport,
    )
    # $0.25 + $1.25
    assert out["cost_usd"] == pytest.approx(1.50, abs=1e-6)


def test_default_transport_routes_via_infra_llm_openai(monkeypatch):
    """The default transport reconstructs an infra_llm target from the
    provider-qualified model — an openai model flows through to the
    provider-agnostic client (#3466), with token counts estimated."""
    import infra_llm

    seen = {}

    def fake_complete(target, *, messages=None, system="", max_tokens=0, timeout=0):
        seen["provider"] = target.provider
        seen["model"] = target.model
        seen["api_key"] = target.api_key
        seen["system"] = system
        return "openai says hi"

    monkeypatch.setattr(infra_llm, "complete", fake_complete)
    out = _hc._default_transport(
        "system prompt", [{"role": "user", "content": "hello"}],
        "sk-openai-fake", "openai/gpt-4o-mini", 256,
    )
    assert seen["provider"] == "openai"
    assert seen["model"] == "openai/gpt-4o-mini"
    assert seen["api_key"] == "sk-openai-fake"
    assert out["text"] == "openai says hi"
    # Estimated: (len(system)+len(messages content)) // 4 and len(text) // 4
    assert out["input_tokens"] == (len("system prompt") + len("hello")) // 4
    assert out["output_tokens"] == len("openai says hi") // 4


def test_default_transport_rejects_bare_model():
    """A bare (unqualified) model id can't name a provider — the
    transport refuses rather than presuming one."""
    with pytest.raises(ValueError, match="not provider-qualified"):
        _hc._default_transport("s", [{"role": "user", "content": "x"}],
                               "sk-fake", "claude-haiku-4-5-20251001", 10)


# ─────────────────────────────────────────────────────────────────────────────
# /api/home/chat endpoint
# ─────────────────────────────────────────────────────────────────────────────


def _make_app(tmp_path: Path):
    net = tmp_path / "network.json"
    net.write_text(json.dumps({
        "sharedDir": str(tmp_path),
        "bots": {"evolve": {"role": "primary"}},
        "members": ["evolve"],
    }))
    sys.path.insert(0, str(_ADMIN_PKG.parent / "analyzer"))
    from evolve_admin.web.server import create_app
    return create_app(net)


def test_endpoint_rejects_empty_message(tmp_path):
    app = _make_app(tmp_path)
    client = app.test_client()
    r = client.post("/api/home/chat", json={"message": "   "})
    assert r.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# /api/home/chat — Phase 4.1 proxy-routed endpoint
# ─────────────────────────────────────────────────────────────────────────────
#
# Phase 4.1 (docs/spec-evo-oc-native-2026-05-19.md §3) replaced the
# dispatcher-first + Haiku-fallback path with a thin proxy that routes
# every message through evo's actual OC agent. The legacy
# ``test_endpoint_known_subcommand_returns_dispatch_source`` /
# ``test_endpoint_unrecognized_no_key_returns_dispatch_no_llm`` /
# ``test_endpoint_evo_prefix_idempotent`` /
# ``test_endpoint_natural_language_skips_dispatcher`` /
# ``test_endpoint_accepts_page_context_no_crash`` tests were removed
# because they asserted on ``source: "dispatch"`` / ``source:
# "dispatch_no_llm"`` — sources that the new proxy never emits.
# Replaced with the tests below.


def _patch_proxy(monkeypatch, *, text="ok", error=None, model="claude-sonnet-4-6"):
    """Helper — replace send_to_evo with a fake that captures kwargs.

    Returns a ``calls`` list each entry of which is the kwargs dict
    the route handler passed. Lets tests assert on session_id /
    page_context / message threading without spawning openclaw.
    """
    calls: list[dict] = []
    from evolve_admin.web import home_chat_routes as _routes  # noqa: F401
    from evolve_admin.evo import proxy as _proxy

    def fake_send(message, *, session_id, network_path, page_context=None, **kw):
        calls.append({
            "message": message,
            "session_id": session_id,
            "network_path": network_path,
            "page_context": page_context,
        })
        return _proxy.ProxyResult(
            text=text, session_id=session_id,
            model=model, error=error,
        )

    monkeypatch.setattr(_proxy, "send_to_evo", fake_send)
    return calls


def test_endpoint_routes_through_proxy(tmp_path, monkeypatch):
    """Every non-empty message hits send_to_evo. No dispatcher, no
    Haiku fallback — Phase 4.1 collapses both legacy paths into the
    single proxy call."""
    calls = _patch_proxy(monkeypatch, text="hi from evo")
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={"message": "hello"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["source"] == "evo"
    assert body["reply"] == "hi from evo"
    assert body["model"] == "claude-sonnet-4-6"
    assert len(calls) == 1
    assert calls[0]["message"] == "hello"


def test_endpoint_threads_page_context_to_proxy(tmp_path, monkeypatch):
    """page_context flows through to the proxy verbatim so the
    summary protocol can render the <page-context> block. Regression
    case: the three failure modes the operator surfaced 2026-05-19
    were all caused by page_context not reaching the model — the
    proxy MUST get it."""
    calls = _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    pc = {
        "page_id": "alerts",
        "view": "firing",
        "summary": {
            "headline": "43 firing alerts",
            "counts": {"total": 43, "critical": 2},
        },
    }
    r = app.test_client().post("/api/home/chat", json={
        "message": "what's wrong?",
        "page_context": pc,
    })
    assert r.status_code == 200
    assert len(calls) == 1
    assert calls[0]["page_context"] == pc


def test_endpoint_derives_session_id_from_page_id(tmp_path, monkeypatch):
    """Each admin UI page gets its own conversation thread. With no
    explicit session_id, the proxy derives one from page_id —
    deterministic so a reload resumes the same thread."""
    calls = _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    app.test_client().post("/api/home/chat", json={
        "message": "x",
        "page_context": {"page_id": "alerts"},
    })
    assert calls[0]["session_id"] == "admin-ui-alerts"


def test_endpoint_honors_explicit_session_id(tmp_path, monkeypatch):
    """Explicit session_id in the body takes precedence over the
    page-derived id. Lets the frontend pin a thread across page
    navigations (Phase 4.3 hook); needed by the Chat page's multi-
    session UI added in #1339 so each browser session gets its own
    OC memory instead of all of them sharing 'admin-ui-home'."""
    calls = _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    app.test_client().post("/api/home/chat", json={
        "message": "x",
        "session_id": "operator-pinned-1",
        "page_context": {"page_id": "alerts"},
    })
    assert calls[0]["session_id"] == "operator-pinned-1"


def test_endpoint_accepts_chat_page_multi_session_id_format(tmp_path, monkeypatch):
    """The Chat page sends OC session ids of the form
    ``admin-ui-home-<browser-sid>`` (e.g. ``admin-ui-home-s1abc234``).
    Format must be accepted verbatim — the multi-session UI's whole
    point is that each browser session gets its own OC memory."""
    calls = _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    app.test_client().post("/api/home/chat", json={
        "message": "x",
        "session_id": "admin-ui-home-s1abc234",
        "page_context": {"page_id": "home"},
    })
    assert calls[0]["session_id"] == "admin-ui-home-s1abc234"


@pytest.mark.parametrize("bad_sid", [
    "../../etc/passwd",            # path traversal
    "x/y",                         # slash → directory escape
    "admin\\ui",                   # backslash on weird filesystems
    "session id",                  # space → filesystem confusion
    "a" * 200,                     # length > 128
    "weird‮char",             # unicode RTL override
    "$(whoami)",                   # shell metachar
    "foo;bar",                     # shell separator
])
def test_endpoint_rejects_unsafe_session_id(tmp_path, monkeypatch, bad_sid):
    """OC uses session_id as the JSONL filename under shared_dir, so
    any value that confuses path resolution must be rejected. Frontend
    bug surfaces visibly (400) rather than silently falling back to a
    derived id and writing to the wrong session."""
    _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "x",
        "session_id": bad_sid,
        "page_context": {"page_id": "home"},
    })
    assert r.status_code == 400, (
        f"unsafe session_id {bad_sid!r} should have been rejected"
    )
    body = r.get_json()
    assert "invalid session_id" in body.get("error", "").lower()


def test_endpoint_empty_session_id_falls_back_to_derive(tmp_path, monkeypatch):
    """An empty / whitespace-only session_id is treated the same as
    missing — falls through to derive_session_id(page_id). This
    preserves back-compat for any caller that always passes the field
    but sometimes leaves it blank."""
    calls = _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    app.test_client().post("/api/home/chat", json={
        "message": "x",
        "session_id": "  ",
        "page_context": {"page_id": "home"},
    })
    assert calls[0]["session_id"] == "admin-ui-home"  # derived fallback


def test_endpoint_consecutive_anon_requests_share_session_id(tmp_path, monkeypatch):
    """#1367 follow-up — defense-in-depth. When the client omits
    ``session_id`` AND ``page_context.page_id`` (so the route can't
    derive from page_id either), two consecutive turns from the
    SAME browser must land on the SAME OC session.

    Previously the route fell back to ``derive_session_id(None)``
    which minted a fresh ``admin-ui-anon-<uuid4>`` per request,
    fragmenting one operator-perceived thread across many OC
    sessions and causing evo to "forget" prior turns. The fix sets
    an ``admin_ui_session`` cookie on first hit and keys the anon
    fallback off that value — same cookie, same session_id."""
    calls = _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    client = app.test_client()
    # First request — no cookie yet. Server mints one + sets it.
    r1 = client.post("/api/home/chat", json={"message": "first"})
    assert r1.status_code == 200
    # The test client persists Set-Cookie across requests on the
    # same client instance, so the second POST carries the cookie
    # the server just set.
    r2 = client.post("/api/home/chat", json={"message": "second"})
    assert r2.status_code == 200
    assert len(calls) == 2, (
        f"expected 2 proxy calls, got {len(calls)}"
    )
    sid1 = calls[0]["session_id"]
    sid2 = calls[1]["session_id"]
    assert sid1 == sid2, (
        f"#1367 bug regression: consecutive anon requests got "
        f"different session_ids ({sid1!r} vs {sid2!r}). The cookie-"
        f"based stable fallback isn't working — evo will see each "
        f"turn as a cold start."
    )
    # And the id reflects the stable-anon shape, not a per-request UUID.
    assert sid1.startswith("admin-ui-anon-")


def test_endpoint_sets_admin_ui_session_cookie_on_first_anon_request(tmp_path, monkeypatch):
    """The cookie that backs the stable anon fallback must actually
    be set on the response so subsequent turns carry it back.
    HttpOnly + SameSite=Lax are required for same-origin admin UI
    use without exposing the value to opportunistic JS."""
    _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    client = app.test_client()
    r = client.post("/api/home/chat", json={"message": "first"})
    assert r.status_code == 200
    set_cookies = [
        h for k, h in r.headers.items() if k.lower() == "set-cookie"
    ]
    assert any("admin_ui_session=" in c for c in set_cookies), (
        f"admin_ui_session cookie not set on response. Without it, "
        f"the next anon turn can't share a session id with this one. "
        f"Set-Cookie headers seen: {set_cookies!r}"
    )
    cookie_header = next(c for c in set_cookies if "admin_ui_session=" in c)
    assert "HttpOnly" in cookie_header, (
        "admin_ui_session must be HttpOnly so opportunistic JS can't "
        "read/clobber it."
    )
    assert "SameSite=Lax" in cookie_header, (
        "admin_ui_session must be SameSite=Lax for same-origin admin "
        "UI use."
    )


def test_endpoint_client_session_id_skips_cookie_fallback(tmp_path, monkeypatch):
    """When the client DOES send a valid session_id, the cookie-
    fallback path is irrelevant — the explicit id wins. Tests that
    Part 1 of the fix (client always sends session_id) renders Part 2
    moot in the common case, while keeping Part 2 as a safety net."""
    calls = _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    client = app.test_client()
    client.post("/api/home/chat", json={
        "message": "x",
        "session_id": "admin-ui-home-sxyz",
    })
    assert calls[0]["session_id"] == "admin-ui-home-sxyz"


def test_endpoint_surfaces_proxy_error_with_text(tmp_path, monkeypatch):
    """Subprocess failures bubble up as ``source: proxy_error`` with
    text the frontend can render in a red bubble. Mirrors the old
    ``llm_error`` UX so existing JS keeps working."""
    _patch_proxy(monkeypatch, text="timeout reached", error="timeout")
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={"message": "x"})
    body = r.get_json()
    assert r.status_code == 200
    assert body["source"] == "proxy_error"
    assert body["error"] == "timeout"
    assert body["reply"] == "timeout reached"


def test_endpoint_echoes_authority_in_response(tmp_path, monkeypatch):
    """Frontend sends authority tier; proxy returns it back. Phase 4.1
    doesn't gate tool calls on authority yet (4.2's job) — but the
    frontend's existing display still wants the value echoed."""
    _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "x", "authority": "auto-small",
    })
    body = r.get_json()
    assert body["authority"] == "auto-small"


def test_endpoint_authority_falls_back_to_ask_for_invalid(tmp_path, monkeypatch):
    """Defensive: a garbage authority value clamps to 'ask' (most
    conservative). Mirrors the legacy validation behavior."""
    _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "x", "authority": "yolo",
    })
    body = r.get_json()
    assert body["authority"] == "ask"


# ─────────────────────────────────────────────────────────────────────────────
# Operator tier preference (Auto / Fast / Standard / Power)
# docs/spec-user-tier-control-2026-05-26.md
# ─────────────────────────────────────────────────────────────────────────────


def _patch_proxy_capture_tier(monkeypatch, *, text="ok"):
    """Variant of _patch_proxy that captures the tier_preference kwarg
    so tests can assert on what we actually forwarded to send_to_evo
    (versus what the operator typed)."""
    calls: list[dict] = []
    from evolve_admin.evo import proxy as _proxy

    def fake_send(
        message, *, session_id, network_path,
        page_context=None, session_context=None,
        tier_preference=None, **kw,
    ):
        calls.append({
            "message": message,
            "session_id": session_id,
            "tier_preference": tier_preference,
            "session_context": session_context,
        })
        return _proxy.ProxyResult(
            text=text, session_id=session_id,
            model="claude-sonnet-4-6", error=None,
        )

    monkeypatch.setattr(_proxy, "send_to_evo", fake_send)
    return calls


def test_endpoint_default_tier_is_auto(tmp_path, monkeypatch):
    """No tier field on the body → "auto". Confirms the documented
    default and that proxy doesn't receive a spurious tier preference
    (so the plugin's classifier-driven routing stays intact)."""
    calls = _patch_proxy_capture_tier(monkeypatch)
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={"message": "x"})
    body = r.get_json()
    assert body["tier"] == "auto"
    assert body["effective_tier"] == "auto"
    assert body["tier_capped"] is False
    assert calls[0]["tier_preference"] == "auto"


def test_endpoint_echoes_tier_preference(tmp_path, monkeypatch):
    """Each valid tier value round-trips through the endpoint and
    reaches the proxy as-is."""
    calls = _patch_proxy_capture_tier(monkeypatch)
    app = _make_app(tmp_path)
    for choice in ("fast", "standard", "power", "auto"):
        r = app.test_client().post("/api/home/chat", json={
            "message": "x", "tier": choice,
        })
        body = r.get_json()
        assert body["tier"] == choice, choice
        # When tier1 cap isn't tripped, effective_tier == tier
        assert body["effective_tier"] == choice, choice
        assert body["tier_capped"] is False, choice
    # Last call's tier_preference matches the last choice ("auto")
    assert calls[-1]["tier_preference"] == "auto"


def test_endpoint_tier_falls_back_to_auto_for_invalid(tmp_path, monkeypatch):
    """Garbage tier value → "auto" (mirrors authority's defensive
    posture). The operator's intent on a malformed value is unknown;
    let the classifier decide rather than picking a tier."""
    calls = _patch_proxy_capture_tier(monkeypatch)
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "x", "tier": "ultra-mega-power",
    })
    body = r.get_json()
    assert body["tier"] == "auto"
    assert body["effective_tier"] == "auto"
    assert calls[0]["tier_preference"] == "auto"


def test_endpoint_tier_is_case_insensitive(tmp_path, monkeypatch):
    """POWER, Power, power all normalize to "power" so the frontend
    doesn't have to care about case."""
    calls = _patch_proxy_capture_tier(monkeypatch)
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "x", "tier": "POWER",
    })
    body = r.get_json()
    assert body["tier"] == "power"
    assert calls[0]["tier_preference"] == "power"


def _import_analyzer_modules():
    """Ensure the analyzer dir is on sys.path and the modules under
    test are loaded, so monkeypatch can target their attributes
    directly. The handler does the same sys.path insertion lazily on
    each request — this just front-loads it so the test patches
    apply to the module the handler will import."""
    analyzer_dir = _ADMIN_PKG.parent / "analyzer"
    if str(analyzer_dir) not in sys.path:
        sys.path.insert(0, str(analyzer_dir))
    import models as _models  # noqa: F401
    import primary_bot as _primary_bot  # noqa: F401
    return _models, _primary_bot


def test_endpoint_power_downgrades_when_tier1_cap_exhausted(tmp_path, monkeypatch):
    """tier1 daily cap is enforced server-side. When the cap is hit,
    Power is silently rewritten to Standard before forwarding to the
    plugin, AND the response carries `tier_capped: true` so the
    frontend can render the "Power capped today — used Standard" note.
    Spec: docs/spec-user-tier-control-2026-05-26.md "tier1 daily-cap
    handling". The downgrade is non-destructive: the operator's
    original `tier` value is still echoed back so the composer UI
    stays in sync with what they picked."""
    calls = _patch_proxy_capture_tier(monkeypatch)
    _models, _primary_bot = _import_analyzer_modules()

    # Stub today's tier1 usage at the cap (default 10) → trips gate.
    # Per-bot dailyCap defaults to 10 from _read_user_tier_override.
    def fake_usage(bot_id, shared_dir, tier=None):
        return {"tier1": 10}
    monkeypatch.setattr(_models, "get_tier_usage_today", fake_usage)

    def fake_primary(network):
        return "evolve"
    monkeypatch.setattr(_primary_bot, "primary_bot_id", fake_primary)

    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "deep analysis please", "tier": "power",
    })
    body = r.get_json()
    # User's original choice echoes back unchanged (composer UI)
    assert body["tier"] == "power"
    # But the downgrade actually fired and reached the plugin
    assert body["effective_tier"] == "standard"
    assert body["tier_capped"] is True
    assert calls[0]["tier_preference"] == "standard"


def test_endpoint_power_passes_through_when_cap_not_exhausted(tmp_path, monkeypatch):
    """The cap downgrade only fires when the limit is actually
    exhausted. Within-limit Power requests pass through unchanged."""
    calls = _patch_proxy_capture_tier(monkeypatch)
    _models, _primary_bot = _import_analyzer_modules()

    def fake_usage(bot_id, shared_dir, tier=None):
        return {"tier1": 3}  # well within default cap of 10
    monkeypatch.setattr(_models, "get_tier_usage_today", fake_usage)

    def fake_primary(network):
        return "evolve"
    monkeypatch.setattr(_primary_bot, "primary_bot_id", fake_primary)

    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "x", "tier": "power",
    })
    body = r.get_json()
    assert body["tier"] == "power"
    assert body["effective_tier"] == "power"
    assert body["tier_capped"] is False
    assert calls[0]["tier_preference"] == "power"


def test_endpoint_cap_check_failure_passes_power_through(tmp_path, monkeypatch):
    """Cap check failure must NEVER block the chat. If the cap-check
    helpers raise (e.g. analyzer import broke after a refactor), we
    fail open: forward Power, log the error, set tier_capped=False.
    Operator visibility comes from noticing they're still on Power
    when they expected the cap to fire — better than a 500."""
    calls = _patch_proxy_capture_tier(monkeypatch)
    _models, _primary_bot = _import_analyzer_modules()

    def fake_usage(*a, **kw):
        raise RuntimeError("simulated analyzer failure")
    monkeypatch.setattr(_models, "get_tier_usage_today", fake_usage)

    def fake_primary(network):
        return "evolve"
    monkeypatch.setattr(_primary_bot, "primary_bot_id", fake_primary)

    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "x", "tier": "power",
    })
    assert r.status_code == 200
    body = r.get_json()
    # Failed open — Power went through to the plugin
    assert body["effective_tier"] == "power"
    assert body["tier_capped"] is False
    assert calls[0]["tier_preference"] == "power"


def test_endpoint_non_power_tier_skips_cap_check(tmp_path, monkeypatch):
    """The cap check is ONLY for Power. Fast / Standard / Auto must
    not trigger it (no analyzer imports, no shared_dir reads). This
    keeps the hot path cheap for the common case."""
    calls = _patch_proxy_capture_tier(monkeypatch)
    _models, _primary_bot = _import_analyzer_modules()
    cap_called = {"count": 0}

    def fake_usage(*a, **kw):
        cap_called["count"] += 1
        return {"tier1": 0}
    monkeypatch.setattr(_models, "get_tier_usage_today", fake_usage)

    app = _make_app(tmp_path)
    for choice in ("auto", "fast", "standard"):
        app.test_client().post("/api/home/chat", json={
            "message": "x", "tier": choice,
        })
    assert cap_called["count"] == 0, (
        f"cap check fired for non-Power tiers ({cap_called['count']} times)"
    )
    assert [c["tier_preference"] for c in calls] == ["auto", "fast", "standard"]


def test_endpoint_respects_per_bot_dailyCap_override(tmp_path, monkeypatch):
    """Per-bot tiers.json::userTierOverride.dailyCap overrides the
    default cap of 10. Setting it to 3 means 3 tier1 calls today
    already trips the gate. Spec:
    docs/spec-user-tier-control-2026-05-26.md §"Per-bot opt-out +
    adjustable daily cap"."""
    _patch_proxy_capture_tier(monkeypatch)
    _models, _primary_bot = _import_analyzer_modules()

    # Write a per-bot tiers.json with cap=3
    (tmp_path / "evolve").mkdir()
    (tmp_path / "evolve" / "tiers.json").write_text(json.dumps({
        "userTierOverride": {"enabled": True, "dailyCap": 3},
    }))

    def fake_usage(bot_id, shared_dir, tier=None):
        return {"tier1": 3}  # right at the per-bot cap
    monkeypatch.setattr(_models, "get_tier_usage_today", fake_usage)

    def fake_primary(network):
        return "evolve"
    monkeypatch.setattr(_primary_bot, "primary_bot_id", fake_primary)

    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "x", "tier": "power",
    })
    body = r.get_json()
    # 3 used / 3 cap → tripped
    assert body["effective_tier"] == "standard"
    assert body["tier_capped"] is True


def test_endpoint_dailyCap_zero_disables_power(tmp_path, monkeypatch):
    """Cap=0 means "Power disabled on this bot" — every Power request
    downgrades. Operators set this on bots where they don't want the
    expensive tier ever (forge / security / etc.). The chip can still
    render visually if userTierOverride.enabled is True, but actual
    Power requests no-op to Standard with tier_capped=True."""
    _patch_proxy_capture_tier(monkeypatch)
    _models, _primary_bot = _import_analyzer_modules()

    (tmp_path / "evolve").mkdir()
    (tmp_path / "evolve" / "tiers.json").write_text(json.dumps({
        "userTierOverride": {"enabled": True, "dailyCap": 0},
    }))

    def fake_usage(bot_id, shared_dir, tier=None):
        return {"tier1": 0}  # nothing used today, but cap=0
    monkeypatch.setattr(_models, "get_tier_usage_today", fake_usage)

    def fake_primary(network):
        return "evolve"
    monkeypatch.setattr(_primary_bot, "primary_bot_id", fake_primary)

    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "x", "tier": "power",
    })
    body = r.get_json()
    assert body["effective_tier"] == "standard"
    assert body["tier_capped"] is True


def test_read_user_tier_override_defaults_for_missing_file(tmp_path):
    """A bot with no tiers.json yet → defaults (enabled=True, cap=10).
    The feature must work on pods that haven't written the file."""
    from evolve_admin.web.home_chat_routes import _read_user_tier_override
    out = _read_user_tier_override(tmp_path, "neverexisted")
    assert out == {"enabled": True, "dailyCap": 10}


def test_read_user_tier_override_clamps_invalid_dailyCap(tmp_path):
    """Out-of-range values (-1, 101, "ten") fall back to default 10.
    The chip UI can range-bound to 0-100; this is defense-in-depth
    against a hand-edited tiers.json."""
    from evolve_admin.web.home_chat_routes import _read_user_tier_override
    (tmp_path / "evolve").mkdir()
    for bad in (-1, 101, "ten", None):
        (tmp_path / "evolve" / "tiers.json").write_text(json.dumps({
            "userTierOverride": {"enabled": True, "dailyCap": bad},
        }))
        out = _read_user_tier_override(tmp_path, "evolve")
        assert out["dailyCap"] == 10, bad


def test_read_user_tier_override_honors_enabled_false(tmp_path):
    """`enabled: false` is the per-bot chip opt-out. The handler keeps
    accepting + processing tier-preference values from the body
    regardless (defense in depth — a cached frontend may still send
    them); the chip-rendering decision is the frontend's, driven by
    this flag via the tier-config endpoint."""
    from evolve_admin.web.home_chat_routes import _read_user_tier_override
    (tmp_path / "evolve").mkdir()
    (tmp_path / "evolve" / "tiers.json").write_text(json.dumps({
        "userTierOverride": {"enabled": False, "dailyCap": 10},
    }))
    out = _read_user_tier_override(tmp_path, "evolve")
    assert out == {"enabled": False, "dailyCap": 10}


def test_endpoint_leading_slash_short_circuit_still_echoes_tier(tmp_path, monkeypatch):
    """The /api/home/chat handler rejects leading-slash messages with
    an explanatory reply (defense-in-depth against the OC /approve
    exfil channel). That early-return path must still echo `tier` /
    `effective_tier` / `tier_capped` so the frontend's composer
    stays in sync — otherwise a rejected message would silently
    reset the chip to its default."""
    _patch_proxy_capture_tier(monkeypatch)
    app = _make_app(tmp_path)
    r = app.test_client().post("/api/home/chat", json={
        "message": "/approve abc", "tier": "power",
    })
    body = r.get_json()
    assert body["source"] == "evo"
    assert body["tier"] == "power"
    assert body["effective_tier"] == "power"
    assert body["tier_capped"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Natural-language messages must not be dispatched as commands
# ─────────────────────────────────────────────────────────────────────────────


def test_looks_like_command_true_for_bare_subcommands():
    """Short messages whose first token is a known subcommand qualify
    as commands. Handles `help`, `evo help`, and a few argument forms."""
    from evolve_admin.web.home_chat_routes import _looks_like_command
    assert _looks_like_command("help") is True
    assert _looks_like_command("evo help") is True
    assert _looks_like_command("cost 7d") is True
    assert _looks_like_command("alerts firing") is True
    assert _looks_like_command("Evo Help") is True  # case-insensitive


def test_looks_like_command_false_for_natural_language():
    """Multi-word natural-language questions DON'T qualify. This is the
    fix for the dispatcher misrouting 'help me fix the google workspace
    oath issue on admin_bot' as `evo help me fix…`."""
    from evolve_admin.web.home_chat_routes import _looks_like_command
    assert _looks_like_command("help me fix the google workspace oath issue on admin_bot") is False
    assert _looks_like_command("what is going on") is False
    assert _looks_like_command("show me the alerts") is False
    assert _looks_like_command("can you help fix the personal_bot issues") is False


def test_looks_like_command_false_for_unknown_first_word():
    """First word isn't in the subcommand registry → not a command."""
    from evolve_admin.web.home_chat_routes import _looks_like_command
    assert _looks_like_command("foo") is False
    assert _looks_like_command("evo foo") is False
    assert _looks_like_command("status") is False  # not registered


def test_looks_like_command_false_for_empty():
    """Empty/whitespace input is not a command."""
    from evolve_admin.web.home_chat_routes import _looks_like_command
    assert _looks_like_command("") is False
    assert _looks_like_command("   ") is False


# test_endpoint_natural_language_skips_dispatcher (removed) — the proxy
# no longer runs the dispatcher; the 'help me fix X' regression is now
# handled by routing every message through evo's agent, which is
# robust to natural-language phrasing by construction.


def test_endpoint_response_strips_evo_delimiters(tmp_path):
    """The dispatcher wraps replies in ═══ evo ═══ ... ═══ end evo ═══
    for Telegram. In the API surface we strip them so the JS doesn't
    need to know about the wire format."""
    app = _make_app(tmp_path)
    client = app.test_client()
    r = client.post("/api/home/chat", json={"message": "help"})
    body = r.get_json()
    assert "═══ evo ═══" not in body["reply"]
    assert "═══ end evo ═══" not in body["reply"]


# ─────────────────────────────────────────────────────────────────────────────
# page_context — evo drawer's per-page context-pack
# ─────────────────────────────────────────────────────────────────────────────


def test_prepend_page_context_no_context_returns_message_unchanged():
    """Empty / missing page_context → identity. The Chat-page path uses
    this; no page framing should be added when the operator is on the
    pod-wide Chat surface."""
    from evolve_admin.web.home_chat_routes import _prepend_page_context
    assert _prepend_page_context("hello", {}) == "hello"
    assert _prepend_page_context("hello", {"page_label": "X"}) == "hello"
    assert _prepend_page_context("hello", None) == "hello"  # type: ignore[arg-type]


def test_prepend_page_context_includes_page_label():
    """With just a page label (no state), the message is wrapped with a
    single framing line so the LLM knows what surface is in focus."""
    from evolve_admin.web.home_chat_routes import _prepend_page_context
    out = _prepend_page_context(
        "what's worst here?",
        {"page_id": "maintenance", "page_label": "Alerts"},
    )
    assert "[Operator is on the 'Alerts' page.]" in out
    assert out.endswith("what's worst here?")


def test_prepend_page_context_includes_state_snapshot():
    """When state is present, it's serialized inline so the LLM can
    answer about page contents without the operator pasting them."""
    from evolve_admin.web.home_chat_routes import _prepend_page_context
    pc = {
        "page_id": "maintenance",
        "page_label": "Alerts",
        "state": {"firing_count": 3, "firing_top": [{"title": "disk full"}]},
    }
    out = _prepend_page_context("what should I fix first?", pc)
    assert "'Alerts' page" in out
    assert "firing_count" in out
    assert "disk full" in out


def test_prepend_page_context_caps_state_size():
    """Page state is truncated so a chatty pack can't blow the prompt.
    Cap is 1500 chars on the JSON blob."""
    from evolve_admin.web.home_chat_routes import _prepend_page_context
    big = {"items": ["x" * 100 for _ in range(50)]}
    out = _prepend_page_context(
        "summarize",
        {"page_id": "x", "page_label": "X", "state": big},
    )
    # Header (~50) + cap (~1500) + message (~10) ≈ < 1700
    assert len(out) < 1700


def test_endpoint_accepts_page_context_no_crash(tmp_path, monkeypatch):
    """Posting page_context to /api/home/chat doesn't break the proxy
    path. Smoke test — page_context is optional from the JS side, so
    the endpoint must accept and-or-pass-through whatever shape it
    gets."""
    _patch_proxy(monkeypatch)
    app = _make_app(tmp_path)
    client = app.test_client()
    r = client.post("/api/home/chat", json={
        "message": "help",
        "page_context": {
            "page_id": "maintenance",
            "page_label": "Alerts",
            "state": {"firing_count": 5},
        },
    })
    assert r.status_code == 200
    body = r.get_json()
    assert body["source"] == "evo"


# ─────────────────────────────────────────────────────────────────────────────
# authority — chat prompt shapes its offers to match the operator's tier
# ─────────────────────────────────────────────────────────────────────────────


def test_build_system_prompt_defaults_to_ask():
    """No authority arg → conservative "ask" tier injected into the
    prompt. The LLM should never assume blanket permission to act."""
    prompt = _hc.build_system_prompt("(digest)")
    assert 'tier is "ask"' in prompt


def test_build_system_prompt_includes_authority_when_passed():
    """Each valid tier shows up verbatim in the prompt so the LLM can
    branch its behavior."""
    for tier in ("ask", "auto-small", "auto"):
        prompt = _hc.build_system_prompt("(digest)", authority=tier)
        assert f'tier is "{tier}"' in prompt, tier


def test_build_system_prompt_falls_back_for_invalid_authority():
    """Anything outside the three valid tiers → "ask". Defensive: the
    JS could in theory send a stale value if storage migrations lag."""
    prompt = _hc.build_system_prompt("(digest)", authority="god-mode")
    assert 'tier is "ask"' in prompt


def test_call_llm_forwards_authority_to_prompt():
    """End-to-end: call_llm(authority="auto") → the prompt the transport
    sees mentions "auto", not "ask"."""
    captured = {}

    def transport(system_prompt, messages, api_key, model, max_tokens):
        captured["system_prompt"] = system_prompt
        return {"text": "ok", "input_tokens": 1, "output_tokens": 1}

    _hc.call_llm(
        user_message="x",
        history=[],
        pod_state_digest="(digest)",
        api_key="sk",
        model=_TEST_MODEL,
        transport=transport,
        authority="auto",
    )
    assert 'tier is "auto"' in captured["system_prompt"]


def test_prompt_no_longer_encourages_deflection():
    """Regression: the old prompt explicitly told evo to "suggest the
    specific subcommand" when asked to fix something. That trained
    evo to deflect ("run `evo alerts`") instead of using the digest
    data it already had. The new prompt forbids that."""
    prompt = _hc.build_system_prompt("(digest)")
    # No more "suggest a subcommand" pattern; the new prompt explicitly
    # warns against "run `evo alerts` to see what's firing".
    assert "suggest the specific subcommand" not in prompt
    assert "USE THE DATA YOU ALREADY HAVE" in prompt


def test_prompt_declares_capability_boundary():
    """Regression for the offered-but-not-deliverable bug: evo used to
    offer "want me to start with a redeploy?" then walk it back next
    turn because redeploy isn't a subcommand it can run. The new prompt
    spells out the boundary in the "AVAILABLE COMMANDS" section and the
    "ABSOLUTE RULES" forbid hallucinating commands."""
    prompt = _hc.build_system_prompt("(digest)")
    assert "AVAILABLE COMMANDS" in prompt
    assert "ABSOLUTE RULES" in prompt
    # Redeploy specifically called out as something evo cannot do
    assert "Redeploy from Dashboard" in prompt
    assert "editing files" in prompt
    # The worked example no longer offers "want me to start with the redeploy"
    assert "Want me to start with the redeploy" not in prompt
    # The ABSOLUTE RULES explicitly forbid the made-up subcommands
    # evo had been emitting
    assert "`evo redeploy`" in prompt


def test_prompt_explains_button_affordance():
    """The operator was confused about HOW to act on suggestions like
    "Run `evo wizard`". The prompt now explains the one-click button
    affordance so evo can phrase offers as "click the button below"
    instead of bare terminal-style commands."""
    prompt = _hc.build_system_prompt("(digest)")
    assert "one-click button" in prompt
    assert "click the button" in prompt
    # The "PHRASING" section teaches the right verbal style
    assert "PHRASING" in prompt


# ─────────────────────────────────────────────────────────────────────────────
# Action catalog — the runtime registry filter
# ─────────────────────────────────────────────────────────────────────────────


def test_build_action_catalog_excludes_stub_handlers():
    """Stub subcommands (handler points at evolve_admin.evo.handlers.stub:*)
    must NOT show up — their handlers return 'coming soon' messages,
    so suggesting them as buttons is a broken promise (literally the
    bug the user hit when clicking 'Run evo wizard'."""
    catalog = _hc.build_action_catalog()
    names = {entry["name"] for entry in catalog}
    # Wizard, guide, default, profile all have stub handlers — excluded
    assert "wizard" not in names
    assert "guide" not in names
    # 'better' is excluded as chat-incompatible (needs Telegram context)
    assert "better" not in names
    # 'profile' / 'claim' need channel + sender_external_id we can't provide
    assert "profile" not in names
    assert "claim" not in names


def test_build_action_catalog_includes_real_handlers():
    """Subcommands with real handlers should be in the catalog with
    their actual short_help text (not invented descriptions)."""
    catalog = _hc.build_action_catalog()
    names = {entry["name"] for entry in catalog}
    # These all have real handlers in evo/handlers/*
    for sub in ("help", "alerts", "cost", "usage", "health",
                "integrations", "apps", "app-audit", "fail",
                "gallery", "fun"):
        assert sub in names, f"{sub} should be in catalog"
    # Short_help comes from the registry verbatim
    fail_entry = next(e for e in catalog if e["name"] == "fail")
    assert "report" in fail_entry["short_help"].lower()


def test_working_subcommand_names_matches_catalog():
    """The set used by extract_suggested_actions filter must equal the
    catalog names — same source of truth so prompt and filter agree."""
    names = _hc.working_subcommand_names()
    catalog_names = {entry["name"] for entry in _hc.build_action_catalog()}
    assert names == catalog_names


def test_prompt_includes_real_short_help_not_invented():
    """Regression for the hallucinated-descriptions bug: evo used to
    claim `evo wizard` 'walks you through a full personal_bot reset'. The
    registry short_help says 'run (or re-run) the Evolve setup wizard
    for this bot' — completely different. The prompt now injects the
    REGISTRY short_help verbatim so the LLM can't drift."""
    prompt = _hc.build_system_prompt("(digest)")
    # Pick a few subcommands and assert their short_help text is in
    # the rendered prompt (not the LLM's invented version).
    from evolve_admin.evo import subcommands as _sc
    for cmd in _sc._REGISTRY:
        if ".handlers.stub:" in cmd.handler:
            continue
        if cmd.name in _hc._CHAT_INCOMPATIBLE:
            continue
        # The first ~30 chars of short_help should appear in the prompt
        snippet = cmd.short_help[:30]
        assert snippet in prompt, f"{cmd.name} short_help missing from prompt"


def test_extract_suggestions_rejects_stub_commands():
    """Even if the LLM hallucinates and writes `evo wizard`, the action-
    button extractor must reject it because wizard is a stub. Two-layer
    defense — prompt tells evo not to recommend it, filter catches it
    if the LLM ignores the prompt."""
    text = "Try `evo wizard` to reset personal_bot."
    out = _hc.extract_suggested_actions(
        text, known_subcommands=_hc.working_subcommand_names(),
    )
    assert out == []  # wizard is a stub → filtered out


def test_extract_suggestions_accepts_real_commands():
    """Sanity: the filter doesn't block legitimate suggestions."""
    text = "I can run `evo fail` to log this."
    out = _hc.extract_suggested_actions(
        text, known_subcommands=_hc.working_subcommand_names(),
    )
    assert len(out) == 1
    assert out[0]["subcommand"] == "fail"


def test_endpoint_accepts_authority(tmp_path):
    """Posting authority through the route works — and an invalid one
    is rejected silently (falls back to "ask"). Smoke test only; we
    can't easily test the LLM path here without a stub key + transport
    in the route, but we can confirm the route accepts the field."""
    app = _make_app(tmp_path)
    client = app.test_client()
    for tier in ("ask", "auto-small", "auto", "garbage"):
        r = client.post("/api/home/chat", json={
            "message": "help", "authority": tier,
        })
        assert r.status_code == 200, tier


# ─────────────────────────────────────────────────────────────────────────────
# _sanitize_history collapses consecutive same-role turns
# ─────────────────────────────────────────────────────────────────────────────


def test_sanitize_history_collapses_consecutive_same_role():
    """Two consecutive 'evo' messages (rare — happens with retry) collapse
    so the API doesn't reject the messages array."""
    out = _hc._sanitize_history(
        [
            {"role": "user", "text": "q1"},
            {"role": "evo", "text": "a1"},
            {"role": "evo", "text": "a1-retry"},  # consecutive — collapses
            {"role": "user", "text": "q2"},
        ],
        max_turns=10,
    )
    assert out == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1-retry"},  # latest kept
        {"role": "user", "content": "q2"},
    ]


# ─────────────────────────────────────────────────────────────────────────────
# extract_suggested_actions
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_suggestions_pulls_backticked_evo_commands():
    text = (
        "Looks like team_bot_a's gateway is down. Try `evo alerts` to see the "
        "firing signals, then `evo health` for the substrate snapshot."
    )
    out = _hc.extract_suggested_actions(text, known_subcommands={"alerts", "health"})
    assert out == [
        {"kind": "dispatch", "subcommand": "alerts", "label": "Run evo alerts"},
        {"kind": "dispatch", "subcommand": "health", "label": "Run evo health"},
    ]


def test_extract_suggestions_dedupes_repeats():
    text = "Run `evo cost` to see daily spend, or `evo cost` for the trend."
    out = _hc.extract_suggested_actions(text, known_subcommands={"cost"})
    assert len(out) == 1
    assert out[0]["subcommand"] == "cost"


def test_extract_suggestions_filters_unknown_subcommands():
    """Hallucinated commands (e.g. `evo foo` when foo isn't in the registry)
    must NOT surface as clickable buttons — the click would dead-end on
    the dispatcher's 'unrecognized' reply."""
    text = "Try `evo alerts` or perhaps `evo foo` to see what's wrong."
    out = _hc.extract_suggested_actions(text, known_subcommands={"alerts"})
    assert [a["subcommand"] for a in out] == ["alerts"]


def test_extract_suggestions_no_registry_means_no_filter():
    """When known_subcommands is None the caller is opting out of
    validation — every backticked suggestion lands. Useful in tests."""
    text = "Run `evo anything` or `evo else`."
    out = _hc.extract_suggested_actions(text, known_subcommands=None)
    assert [a["subcommand"] for a in out] == ["anything", "else"]


def test_extract_suggestions_strips_arguments():
    """A backticked `evo fail <description>` reference becomes a one-click
    `evo fail` button for v1; the operator can still type the args in
    the prompt if needed."""
    text = "If something else breaks, use `evo fail <one-line description>`."
    out = _hc.extract_suggested_actions(text, known_subcommands={"fail"})
    assert out == [
        {"kind": "dispatch", "subcommand": "fail", "label": "Run evo fail"},
    ]


def test_extract_suggestions_case_normalizes_to_lower():
    text = "Try `evo Alerts`."
    out = _hc.extract_suggested_actions(text, known_subcommands={"alerts"})
    assert out[0]["subcommand"] == "alerts"


def test_extract_suggestions_handles_empty_or_no_matches():
    assert _hc.extract_suggested_actions("", known_subcommands={"alerts"}) == []
    assert _hc.extract_suggested_actions(
        "No commands here, just narrative.", known_subcommands={"alerts"},
    ) == []


def test_extract_suggestions_ignores_non_backticked_mentions():
    """The LLM is instructed to backtick commands; an inline `evo alerts`
    without backticks (just bold/plain text) is NOT extracted. This is
    intentional — the backtick signal is what the prompt established."""
    text = "Maybe run evo alerts to see what's firing."
    out = _hc.extract_suggested_actions(text, known_subcommands={"alerts"})
    assert out == []


# ─────────────────────────────────────────────────────────────────────────────
# Narrative — cache helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_digest_hash_is_sha1_of_input():
    """Two identical digests must produce identical hashes; different
    digests must differ. SHA1 is stable across runs so the cache key
    survives admin restarts."""
    h1 = _hc.digest_hash("Bots: 7/7 online\nFiring: 0")
    h2 = _hc.digest_hash("Bots: 7/7 online\nFiring: 0")
    h3 = _hc.digest_hash("Bots: 6/7 online\nFiring: 1")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 40   # SHA1 hex


def test_narrative_cache_round_trip(tmp_path):
    """Write then read returns the same payload."""
    now = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
    _hc.write_narrative_cache(
        tmp_path,
        digest_hash_str="abc123",
        text="All quiet on the pod.",
        cost_usd=0.0012,
        model="claude-haiku-4-5-20251001",
        input_tokens=800,
        output_tokens=120,
        now=now,
    )
    cache = _hc.read_narrative_cache(tmp_path)
    assert cache is not None
    assert cache["text"] == "All quiet on the pod."
    assert cache["digest_hash"] == "abc123"
    assert cache["cost_usd"] == 0.0012
    assert cache["input_tokens"] == 800
    assert cache["output_tokens"] == 120
    assert cache["generated_at"].startswith("2026-05-18T10:00:00")


def test_narrative_cache_missing_returns_none(tmp_path):
    assert _hc.read_narrative_cache(tmp_path) is None


def test_narrative_cache_corrupt_returns_none(tmp_path):
    (tmp_path / "home-narrative-cache.json").write_text("not json{{{")
    assert _hc.read_narrative_cache(tmp_path) is None


def test_cache_is_fresh_when_hash_matches_and_within_ttl():
    now = datetime(2026, 5, 18, 10, 5, tzinfo=timezone.utc)
    cache = {
        "digest_hash": "abc123",
        "generated_at": "2026-05-18T10:03:00Z",   # 2 min ago
        "text": "...",
    }
    assert _hc.cache_is_fresh(
        cache, digest_hash_str="abc123", ttl_seconds=300, now=now,
    )


def test_cache_is_stale_when_past_ttl():
    now = datetime(2026, 5, 18, 10, 10, tzinfo=timezone.utc)
    cache = {
        "digest_hash": "abc123",
        "generated_at": "2026-05-18T10:00:00Z",   # 10 min ago
        "text": "...",
    }
    assert not _hc.cache_is_fresh(
        cache, digest_hash_str="abc123", ttl_seconds=300, now=now,
    )


def test_cache_invalidates_when_digest_hash_changes():
    """Even within the TTL window, a different digest means stale.
    This is the key invariant — pod state changing always invalidates."""
    now = datetime(2026, 5, 18, 10, 5, tzinfo=timezone.utc)
    cache = {
        "digest_hash": "abc123",
        "generated_at": "2026-05-18T10:03:00Z",
        "text": "...",
    }
    assert not _hc.cache_is_fresh(
        cache, digest_hash_str="xyz789", ttl_seconds=300, now=now,
    )


def test_cache_handles_missing_or_malformed():
    """Defensive: missing fields, wrong types, None all return stale."""
    now = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
    assert not _hc.cache_is_fresh(None, digest_hash_str="x", now=now)
    assert not _hc.cache_is_fresh({}, digest_hash_str="x", now=now)
    assert not _hc.cache_is_fresh(
        {"digest_hash": "x"}, digest_hash_str="x", now=now,
    )
    assert not _hc.cache_is_fresh(
        {"digest_hash": "x", "generated_at": "not-iso"},
        digest_hash_str="x", now=now,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Narrative — generate_narrative()
# ─────────────────────────────────────────────────────────────────────────────


def test_generate_narrative_returns_text_and_cost():
    def transport(system_prompt, messages, api_key, model, max_tokens):
        # Caller passes the narrative system prompt + a single bare
        # user message asking for the summary. No history.
        assert "friendly daily-briefing voice" in system_prompt.lower() \
            or "evo, the friendly" in system_prompt.lower()
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        return {
            "text": "All quiet. Nothing firing.",
            "input_tokens": 600,
            "output_tokens": 40,
        }

    out = _hc.generate_narrative(
        pod_state_digest="(bots: 7/7 online)",
        api_key="sk-test",
        model=_TEST_MODEL,
        transport=transport,
    )
    assert out["text"] == "All quiet. Nothing firing."
    assert out["model"] == _TEST_MODEL
    # 600 input × $0.25/M + 40 output × $1.25/M
    assert out["cost_usd"] == pytest.approx(
        (600 * 0.25 + 40 * 1.25) / 1_000_000, abs=1e-6,
    )


def test_generate_narrative_embeds_digest_in_system_prompt():
    captured = {}

    def transport(system_prompt, messages, api_key, model, max_tokens):
        captured["system_prompt"] = system_prompt
        return {"text": "ok", "input_tokens": 0, "output_tokens": 0}

    _hc.generate_narrative(
        pod_state_digest="Bots: 3/7 online\nFiring: 2",
        api_key="x",
        model=_TEST_MODEL,
        transport=transport,
    )
    assert "Bots: 3/7 online" in captured["system_prompt"]
    assert "Firing: 2" in captured["system_prompt"]


def test_generate_narrative_strips_trailing_whitespace():
    def transport(system_prompt, messages, api_key, model, max_tokens):
        return {"text": "\n\n  All quiet.  \n", "input_tokens": 0, "output_tokens": 0}

    out = _hc.generate_narrative(
        pod_state_digest="",
        api_key="k",
        model=_TEST_MODEL,
        transport=transport,
    )
    assert out["text"] == "All quiet."


# ─────────────────────────────────────────────────────────────────────────────
# /api/home/narrative endpoint
# ─────────────────────────────────────────────────────────────────────────────


def test_narrative_endpoint_no_key_returns_no_llm(tmp_path):
    """Without an API key the endpoint returns source=no_llm and empty
    text; the frontend keeps the rule-based fallback in place."""
    app = _make_app(tmp_path)
    client = app.test_client()
    r = client.get("/api/home/narrative")
    assert r.status_code == 200
    body = r.get_json()
    assert body["source"] == "no_llm"
    assert body["error"] == "no_llm_credential"
    assert body["text"] == ""
    # cap_status piggybacks even on the no-LLM path so the UI sees
    # the daily counter consistently across both routes.
    assert "cap_status" in body


def test_narrative_endpoint_returns_cached_when_fresh(tmp_path, monkeypatch):
    """Pre-seed the cache with a fresh entry; endpoint should return
    source=cache without making an LLM call."""
    app = _make_app(tmp_path)
    client = app.test_client()

    # Compute the digest hash the route will look up. The route's
    # _build_pod_state_digest_safe runs against an empty shared_dir
    # which produces the "(no pod state available)" digest.
    expected_digest = _hc.build_pod_state_digest()
    digest_h = _hc.digest_hash(expected_digest)
    now = datetime.now(timezone.utc)
    _hc.write_narrative_cache(
        tmp_path,
        digest_hash_str=digest_h,
        text="All quiet — nothing to do.",
        cost_usd=0.0009,
        model="claude-haiku-4-5-20251001",
        input_tokens=400,
        output_tokens=50,
        now=now,
    )

    r = client.get("/api/home/narrative")
    body = r.get_json()
    assert body["source"] == "cache"
    assert body["text"] == "All quiet — nothing to do."
    # cap_status reflects no new call
    assert body["cap_status"]["used"] == 0


def test_narrative_endpoint_refresh_bypasses_cache(tmp_path, monkeypatch):
    """?refresh=1 ignores a fresh cache. With no API key the endpoint
    still falls through to source=no_llm rather than returning the
    cache — operator's explicit refresh request is honored."""
    app = _make_app(tmp_path)
    client = app.test_client()
    expected_digest = _hc.build_pod_state_digest()
    digest_h = _hc.digest_hash(expected_digest)
    _hc.write_narrative_cache(
        tmp_path,
        digest_hash_str=digest_h,
        text="cached.",
        cost_usd=0.0,
        model="x",
        input_tokens=0,
        output_tokens=0,
    )
    r = client.get("/api/home/narrative?refresh=1")
    body = r.get_json()
    # No API key in the test env → no_llm. The point is we did NOT
    # return source=cache despite the seeded fresh entry.
    assert body["source"] == "no_llm"


def test_narrative_endpoint_cap_status_reflects_chat_usage(tmp_path):
    """Narrative shares the same per-pod daily cap as the chat path —
    a chat burn shows up in the narrative response's cap_status."""
    app = _make_app(tmp_path)
    client = app.test_client()
    _hc.bump_usage(tmp_path, cost_usd=0.001)
    _hc.bump_usage(tmp_path, cost_usd=0.001)
    r = client.get("/api/home/narrative")
    body = r.get_json()
    assert body["cap_status"]["used"] == 2



# ── #3566 audit A-1: tiers.json lives in a bot-writable directory ────────────
#
# PR #3565 granted each bot a macOS ACE ``list,search,add_file,delete_child``
# on ``{sharedDir}/{botId}/`` so the OC plugin could temp+rename
# ``user-tier-prefs.json``. An ACL names a DIRECTORY, not a file, so the same
# grant replaces every entry at that level — including ``tiers.json``, whose
# ``userTierOverride.dailyCap`` gates Power spend. Reproduced against the
# literal ``tier_prefs_acl.BOT_TIER_PREFS_ACL_PERMS`` string on a 0500 dir.
#
# These tests drive the REAL filesystem path (no mocked stat): the vectors the
# ACE actually permits are a symlink and a FIFO, both of which a test can
# create as itself. The uid-mismatch branch is the only piece that needs a
# seam, and it is pinned through the real reader, not through a stub.


def _tiers_path(tmp_path, bot="botx"):
    (tmp_path / bot).mkdir(exist_ok=True)
    return tmp_path / bot / "tiers.json"


def _write_tiers(tmp_path, override, bot="botx"):
    p = _tiers_path(tmp_path, bot)
    p.write_text(json.dumps({"userTierOverride": override}))
    return p


def test_read_user_tier_override_trusts_operator_owned_file(tmp_path):
    """No-regression pin, REAL stat path. A tiers.json owned by this process
    (the admin server runs as ``evolve``; ``sudo evolve-admin`` writes as
    root) is authoritative, including a cap ABOVE the product default."""
    from evolve_admin.web.home_chat_routes import _read_user_tier_override
    _write_tiers(tmp_path, {"enabled": True, "dailyCap": 42})
    assert _read_user_tier_override(tmp_path, "botx")["dailyCap"] == 42


def test_trusted_uids_covers_root_and_this_process():
    """Both legitimate writers of tiers.json are trusted: ``evolve-admin``
    run as the admin-server user, and ``sudo evolve-admin`` run as root.
    ``cli.py`` is the only writer in the repo and it writes in-process."""
    import os as _os
    from evolve_admin.web.home_chat_routes import _trusted_uids
    uids = _trusted_uids()
    assert uids is not None
    assert 0 in uids and _os.getuid() in uids


def test_symlinked_tiers_json_is_refused_outright(tmp_path):
    """THE REVIEW CATCH. ``add_file`` lets a bot plant a SYMLINK named
    tiers.json. ``Path.stat()`` follows it and reports the TARGET's uid —
    aiming it at any root-owned file yields uid 0, the most-trusted uid
    there is, so a stat-based trust gate hands the attacker a free pass
    (verified: stat().st_uid == 0 through a symlink to /etc/hosts).

    The reader opens with O_NOFOLLOW, so a symlink is not read at all and
    the caller gets defaults — never the target's contents, never the
    target's ownership.
    """
    from evolve_admin.web.home_chat_routes import (
        _read_tiers_file, _read_user_tier_override,
    )
    evil = tmp_path / "evil.json"
    evil.write_text(json.dumps({"userTierOverride": {"dailyCap": 100}}))
    link = _tiers_path(tmp_path)
    link.symlink_to(evil)
    # Sanity: the naive call really would have followed and read it.
    assert json.loads(link.read_text())["userTierOverride"]["dailyCap"] == 100
    assert _read_tiers_file(link) == (None, None)
    assert _read_user_tier_override(tmp_path, "botx") == {
        "enabled": True, "dailyCap": 10,
    }


def test_fifo_tiers_json_does_not_hang_the_reader(tmp_path):
    """``add_file`` also permits ``mkfifo tiers.json`` (verified plantable
    under the ACE). ``Path.read_text()`` on a FIFO with no writer blocks
    FOREVER — and this reader sits in the /api/home/chat hot path, so one
    mkfifo would wedge a Flask worker per request. O_NONBLOCK + an
    S_ISREG assertion reject it instead.

    The test itself would hang on a regression, which is the point; it is
    the only honest way to pin "does not block".
    """
    import os as _os
    from evolve_admin.web.home_chat_routes import (
        _read_tiers_file, _read_user_tier_override,
    )
    fifo = _tiers_path(tmp_path)
    _os.mkfifo(fifo)
    assert _read_tiers_file(fifo) == (None, None)
    assert _read_user_tier_override(tmp_path, "botx")["dailyCap"] == 10


def test_directory_named_tiers_json_is_refused(tmp_path):
    """S_ISREG also covers the trivial ``mkdir tiers.json`` denial vector."""
    from evolve_admin.web.home_chat_routes import _read_tiers_file
    d = _tiers_path(tmp_path)
    d.mkdir()
    assert _read_tiers_file(d) == (None, None)


def test_read_tiers_file_returns_content_and_its_own_uid(tmp_path):
    """The owner uid and the bytes come from ONE O_NOFOLLOW fd, so the uid
    provably describes the content returned (no stat-then-open window)."""
    import os as _os
    from evolve_admin.web.home_chat_routes import _read_tiers_file
    p = _write_tiers(tmp_path, {"dailyCap": 7})
    text, uid = _read_tiers_file(p)
    assert uid == _os.getuid()
    assert json.loads(text)["userTierOverride"]["dailyCap"] == 7


def test_read_user_tier_override_clamps_cap_when_file_not_operator_owned(
    tmp_path, monkeypatch,
):
    """THE FINDING. A bot uses the #3565 dir ACE to replace tiers.json with
    ``{enabled: true, dailyCap: 100}``, clobbering the operator's
    ``{enabled: false, dailyCap: 0}``. The rename leaves the file owned by
    the BOT, so the cap is clamped to the product default rather than the
    bot's chosen 100.

    A test cannot create a file owned by another unix account, so the
    TRUSTED SET is the seam (not the stat call — the real stat path is
    pinned by the symlink/FIFO/uid tests above).
    """
    import evolve_admin.web.home_chat_routes as hcr
    _write_tiers(tmp_path, {"enabled": True, "dailyCap": 100})
    monkeypatch.setattr(hcr, "_trusted_uids", lambda: {0, 90210})
    out = hcr._read_user_tier_override(tmp_path, "botx")
    assert out["dailyCap"] == hcr._UNTRUSTED_DAILY_CAP_CEILING == 10


def test_untrusted_tiers_file_may_still_narrow_the_cap(tmp_path, monkeypatch):
    """The clamp is a ceiling, not a reset. A below-default cap in an
    untrusted file is still honoured — a bot electing to spend LESS is not
    a hazard, and discarding the file outright would let a bot cancel its
    own opt-out simply by corrupting it."""
    import evolve_admin.web.home_chat_routes as hcr
    monkeypatch.setattr(hcr, "_trusted_uids", lambda: {0, 90210})
    for cap in (0, 3):
        _write_tiers(tmp_path, {"enabled": True, "dailyCap": cap})
        assert hcr._read_user_tier_override(tmp_path, "botx")["dailyCap"] == cap


def test_untrusted_tiers_file_still_honors_enabled_false(tmp_path, monkeypatch):
    """``enabled: false`` is the restrictive direction, so it survives.

    NOT claimed: that the gate can RESTORE an ``enabled: false`` a bot
    clobbered. It cannot — that value exists nowhere else. See the
    limitation paragraph in the reader's docstring."""
    import evolve_admin.web.home_chat_routes as hcr
    monkeypatch.setattr(hcr, "_trusted_uids", lambda: {0, 90210})
    _write_tiers(tmp_path, {"enabled": False, "dailyCap": 5})
    assert hcr._read_user_tier_override(tmp_path, "botx") == {
        "enabled": False, "dailyCap": 5,
    }


def test_clamp_is_skipped_when_platform_has_no_getuid(tmp_path, monkeypatch):
    """``_trusted_uids() is None`` means "unanswerable", not "untrusted".
    Failing closed there would clamp every pod's cap forever on a platform
    the admin server does not even run on."""
    import evolve_admin.web.home_chat_routes as hcr
    _write_tiers(tmp_path, {"enabled": True, "dailyCap": 100})
    monkeypatch.setattr(hcr, "_trusted_uids", lambda: None)
    assert hcr._read_user_tier_override(tmp_path, "botx")["dailyCap"] == 100


def test_untrusted_clamp_logs_only_when_it_bites(tmp_path, monkeypatch, caplog):
    """This reader runs on every Power turn and every tier-config poll, so
    the warning must fire only when a value actually changed — otherwise a
    single mis-owned file grows admin-ui.err.log without bound."""
    import logging as _logging
    import evolve_admin.web.home_chat_routes as hcr
    monkeypatch.setattr(hcr, "_trusted_uids", lambda: {0, 90210})
    with caplog.at_level(_logging.WARNING, logger=hcr.log.name):
        _write_tiers(tmp_path, {"enabled": True, "dailyCap": 4})
        hcr._read_user_tier_override(tmp_path, "botx")
        assert caplog.records == []
        _write_tiers(tmp_path, {"enabled": True, "dailyCap": 100})
        hcr._read_user_tier_override(tmp_path, "botx")
        assert len(caplog.records) == 1
        assert "clamping dailyCap 100" in caplog.records[0].getMessage()


def test_dailyCap_true_is_not_read_as_one(tmp_path):
    """bool subclasses int. ``dailyCap: true`` must fall back to the
    default, not silently become a cap of 1 (which would look like a
    working near-total Power lockout). routes_admin_config validates the
    same field the same way."""
    from evolve_admin.web.home_chat_routes import _read_user_tier_override
    _write_tiers(tmp_path, {"enabled": True, "dailyCap": True})
    assert _read_user_tier_override(tmp_path, "botx")["dailyCap"] == 10


def test_power_gate_downgrades_using_the_clamped_cap(tmp_path, monkeypatch):
    """END TO END through the real Flask route, mirroring
    test_endpoint_respects_per_bot_dailyCap_override. With 12 Power turns
    used today and a forged ``dailyCap: 100``, an unclamped reader lets the
    turn run as Power; the clamp (10) must downgrade it to standard and set
    ``tier_capped``. This is what proves the clamp binds ENFORCEMENT rather
    than only the rendered number."""
    import evolve_admin.web.home_chat_routes as hcr
    _patch_proxy_capture_tier(monkeypatch)
    _models, _primary_bot = _import_analyzer_modules()

    _write_tiers(tmp_path, {"enabled": True, "dailyCap": 100}, bot="evolve")
    monkeypatch.setattr(hcr, "_trusted_uids", lambda: {0, 90210})
    monkeypatch.setattr(
        _models, "get_tier_usage_today",
        lambda bot_id, shared_dir, tier=None: {"tier1": 12},
    )
    monkeypatch.setattr(_primary_bot, "primary_bot_id", lambda network: "evolve")

    r = _make_app(tmp_path).test_client().post("/api/home/chat", json={
        "message": "x", "tier": "power",
    })
    body = r.get_json()
    assert body["effective_tier"] == "standard"
    assert body["tier_capped"] is True


def test_acl_perms_are_directory_scoped_not_file_scoped():
    """The grant string carries no filename — the reach is every entry in
    {sharedDir}/{botId}/, which is why the reader must not trust the file.
    Pins the facts the corrected tier_prefs_acl docstring asserts."""
    from evolve_admin import tier_prefs_acl
    perms = tier_prefs_acl.BOT_TIER_PREFS_ACL_PERMS
    assert "add_file" in perms and "delete_child" in perms
    assert tier_prefs_acl.TIER_PREFS_FILE_NAME not in perms
    doc = tier_prefs_acl.__doc__ or ""
    # The old, false precision claim must not come back.
    assert "and nothing more" not in doc
    assert "tiers.json" in doc and "app-integrity-coverage.json" in doc
