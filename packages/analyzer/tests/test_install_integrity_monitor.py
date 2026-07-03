"""tests/test_install_integrity_monitor.py — install_integrity_monitor tests.

The monitor wraps the wizard verification gauntlet
(packages/admin/evolve_admin/wizard_verify.py) and translates non-OK
CheckResults into Signal-spec dicts. Tests inject a fake gauntlet
runner that returns canned CheckResult sequences so we can exercise
each translation path without needing a live bot.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import install_integrity_monitor as iim  # noqa: E402


# ── Fakes (mirror wizard_verify.CheckResult / GauntletResult shape) ─────────


@dataclass
class FakeCheck:
    name: str
    status: str = "ok"
    summary: str = ""
    detail: str = ""
    issues: list[dict] = field(default_factory=list)


@dataclass
class FakeGauntlet:
    bot_id: str
    checks: list[FakeCheck]
    overall: str = "ok"


def _network(bot_ids=("team_bot_a", "atlas")) -> dict:
    return {"bots": {b: {"role": "member"} for b in bot_ids}}


def _ok_gauntlet(bot_id):
    # Three checks as of 2026-06-01 — the old "e2e_echo" row was
    # retired with the pairing wizard rework.
    return FakeGauntlet(bot_id=bot_id, checks=[
        FakeCheck("ownership", "ok"),
        FakeCheck("agent_dry_run", "ok"),
        FakeCheck("channels", "ok"),
    ])


def _fake_runner(gauntlet_by_bot):
    def runner(bot_id, *, network, **kwargs):
        return gauntlet_by_bot[bot_id]
    return runner


# ── detect_install_integrity_issues — per-shape translation ─────────────────


def test_clean_gauntlet_emits_no_signals():
    g = _ok_gauntlet("team_bot_a")
    assert iim.detect_install_integrity_issues("team_bot_a", g) == []


def test_ownership_fail_emits_one_signal():
    g = FakeGauntlet(bot_id="team_bot_a", checks=[
        FakeCheck("ownership", "fail",
                  summary="2 path(s) under .openclaw/ not owned by team_bot_a",
                  issues=[
                      {"path": "/Users/team_bot_a/.openclaw/auth-profiles.json",
                       "actual_owner": "root", "expected_owner": "team_bot_a"},
                      {"path": "/Users/team_bot_a/.openclaw/pairing-allowFrom",
                       "actual_owner": "root", "expected_owner": "team_bot_a"},
                  ]),
        FakeCheck("agent_dry_run", "ok"),
        FakeCheck("channels", "ok"),
        FakeCheck("e2e_echo", "pending"),
    ])
    specs = iim.detect_install_integrity_issues("team_bot_a", g)
    assert len(specs) == 1
    s = specs[0]
    assert s["type"] == "ownership_drift"
    assert s["scope"] == "bot"
    assert s["bot_id"] == "team_bot_a"
    assert s["severity"] == "warn"
    assert "team_bot_a" in s["title"]
    assert s["details"]["issue_count"] == 2
    assert "/Users/team_bot_a/.openclaw/auth-profiles.json" in s["body"]


# ── Cry-wolf guard: never fire ownership_drift with 0 issues (Chip L3a) ──────
# Regression: install_integrity_monitor:ownership_drift:darwin fired since
# 2026-06-23 with issue_count: 0 / issues: [] because the `if not issues`
# guard only ran inside the exempt_ocjson_checks branch. A non-exempt bot
# whose ownership check "fails" but carries no actionable dict issues built a
# meaningless 0-path Signal that never sweep-resolved. See
# docs/spec-drift-alert-taxonomy-2026-06-26.md §L3.


def test_ownership_fail_with_empty_issues_emits_nothing():
    """A non-exempt bot whose ownership check is 'fail' but has an empty
    issues list must NOT produce a 0-path ownership_drift Signal."""
    g = FakeGauntlet(bot_id="darwin", checks=[
        FakeCheck("ownership", "fail",
                  summary="0 path(s) under .openclaw/ wrong-owned",
                  issues=[]),
        FakeCheck("agent_dry_run", "ok"),
        FakeCheck("channels", "ok"),
    ])
    assert iim.detect_install_integrity_issues("darwin", g) == []


def test_signal_for_ownership_returns_none_on_empty_issues_non_exempt():
    """Direct: _signal_for_ownership returns None when there are no
    dict-shaped issues (non-exempt bot) — never an issue_count:0 spec."""
    check = FakeCheck("ownership", "fail",
                      summary="0 path(s) wrong-owned", issues=[])
    assert iim._signal_for_ownership("darwin", check) is None


def test_signal_for_ownership_returns_none_on_non_dict_only_issues():
    """A 'fail' carrying only non-dict junk (no actionable paths) is suppressed."""
    check = FakeCheck("ownership", "fail", summary="weird",
                      issues=["not-a-dict", 42])  # type: ignore[list-item]
    assert iim._signal_for_ownership("darwin", check) is None


def test_signal_for_ownership_returns_none_when_empty_after_exempt_narrowing():
    """On the exempt evo primary, if every issue was the suppressed
    openclaw.json path the narrowed list is empty and the unconditional
    guard returns None."""
    check = FakeCheck("ownership", "fail",
                      summary="1 path(s) wrong-owned",
                      issues=[
                          {"path": "/home/evo/.openclaw/openclaw.json",
                           "actual_owner": "evo", "expected_owner": "evo"},
                      ])
    assert iim._signal_for_ownership(
        "evo", check, exempt_ocjson_checks=True) is None


def test_signal_for_ownership_still_fires_on_genuine_wrong_owned_path():
    """A genuine wrong-owned path STILL produces a Signal with the right
    issue_count — the fix suppresses only the empty case."""
    check = FakeCheck("ownership", "fail",
                      summary="1 path(s) wrong-owned",
                      issues=[
                          {"path": "/Users/darwin/.openclaw/auth-profiles.json",
                           "actual_owner": "root", "expected_owner": "darwin"},
                      ])
    sig = iim._signal_for_ownership("darwin", check)
    assert sig is not None
    assert sig["type"] == "ownership_drift"
    assert sig["details"]["issue_count"] == 1
    assert "/Users/darwin/.openclaw/auth-profiles.json" in sig["body"]


def test_signal_for_ownership_exempt_still_fires_on_non_ocjson_path():
    """On the exempt evo primary, a wrong-owned auth-profiles.json (not the
    suppressed openclaw.json) STILL fires — exemption narrows, never silences."""
    check = FakeCheck("ownership", "fail",
                      summary="2 path(s) wrong-owned",
                      issues=[
                          {"path": "/home/evo/.openclaw/openclaw.json",
                           "actual_owner": "evo", "expected_owner": "evo"},
                          {"path": "/home/evo/.openclaw/auth-profiles.json",
                           "actual_owner": "root", "expected_owner": "evo"},
                      ])
    sig = iim._signal_for_ownership("evo", check, exempt_ocjson_checks=True)
    assert sig is not None
    assert sig["details"]["issue_count"] == 1  # openclaw.json dropped, the other kept
    assert "auth-profiles.json" in sig["body"]


def test_missing_primary_model_emits_specific_type():
    """The [#1701]/[#1703]/[#1707] atlas regression should map to its own Signal type."""
    g = FakeGauntlet(bot_id="atlas", checks=[
        FakeCheck("ownership", "ok"),
        FakeCheck("agent_dry_run", "fail",
                  summary=("agents.defaults.model.primary not set — OC will "
                           "fall back to a default model"),
                  issues=[{"field": "agents.defaults.model.primary", "value": None}]),
        FakeCheck("channels", "ok"),
        FakeCheck("e2e_echo", "pending"),
    ])
    specs = iim.detect_install_integrity_issues("atlas", g)
    assert len(specs) == 1
    assert specs[0]["type"] == "missing_primary_model"
    assert "atlas" in specs[0]["title"]
    assert "OpenAI" in specs[0]["body"]  # mentions the fallback class


def test_legacy_credential_shape_emits_specific_type():
    """The [#1752] anthropic_api_key shape bug should map to its own Signal type."""
    g = FakeGauntlet(bot_id="atlas", checks=[
        FakeCheck("ownership", "ok"),
        FakeCheck("agent_dry_run", "fail",
                  summary="Legacy key shape 'anthropic_api_key' — OC expects 'anthropic:api_key'",
                  issues=[{
                      "path": "auth-profiles.json::profiles.anthropic_api_key",
                      "key": "anthropic_api_key",
                      "canonical": "anthropic:api_key",
                      "summary": "Legacy key shape 'anthropic_api_key' — OC expects 'anthropic:api_key'",
                  }]),
        FakeCheck("channels", "ok"),
        FakeCheck("e2e_echo", "pending"),
    ])
    specs = iim.detect_install_integrity_issues("atlas", g)
    assert len(specs) == 1
    assert specs[0]["type"] == "legacy_credential_shape"
    assert "atlas" in specs[0]["title"]
    assert "underscore" in specs[0]["body"].lower() or "colon" in specs[0]["body"].lower()


def test_missing_credential_emits_specific_type():
    """primary=claude-* but no anthropic:* credential → its own Signal type."""
    g = FakeGauntlet(bot_id="team_bot_a", checks=[
        FakeCheck("ownership", "ok"),
        FakeCheck("agent_dry_run", "fail",
                  summary=("No anthropic credential found in auth-profiles.json — "
                           "expected one of ('anthropic:api_key', 'anthropic:auth_token') "
                           "for primary model 'claude-sonnet-4-6'"),
                  issues=[{
                      "path": "auth-profiles.json::profiles",
                      "key": None,
                      "summary": ("No anthropic credential found in auth-profiles.json — "
                                  "expected one of ('anthropic:api_key', 'anthropic:auth_token') "
                                  "for primary model 'claude-sonnet-4-6'"),
                  }]),
        FakeCheck("channels", "ok"),
        FakeCheck("e2e_echo", "pending"),
    ])
    specs = iim.detect_install_integrity_issues("team_bot_a", g)
    assert len(specs) == 1
    assert specs[0]["type"] == "missing_credential"
    assert "team_bot_a" in specs[0]["title"]


def test_gateway_down_emits_specific_type():
    g = FakeGauntlet(bot_id="atlas", checks=[
        FakeCheck("ownership", "ok"),
        FakeCheck("agent_dry_run", "fail",
                  summary="Gateway on port 19040 not responding to /evolve/status",
                  detail="Probe result: probe failed: Connection refused"),
        FakeCheck("channels", "ok"),
        FakeCheck("e2e_echo", "pending"),
    ])
    specs = iim.detect_install_integrity_issues("atlas", g)
    assert len(specs) == 1
    assert specs[0]["type"] == "gateway_down"


def test_config_invalid_emits_specific_type():
    g = FakeGauntlet(bot_id="atlas", checks=[
        FakeCheck("ownership", "ok"),
        FakeCheck("agent_dry_run", "fail",
                  summary="openclaw.json rejected by the config validator",
                  detail="openclaw config validate reported:\n  1. plugins.entries.evolve: unknown property",
                  issues=[{"path": "plugins.entries.evolve", "message": "unknown property"}]),
        FakeCheck("channels", "ok"),
        FakeCheck("e2e_echo", "pending"),
    ])
    specs = iim.detect_install_integrity_issues("atlas", g)
    assert len(specs) == 1
    assert specs[0]["type"] == "config_invalid"


def test_channels_fail_emits_signal_with_failed_channels_in_title():
    g = FakeGauntlet(bot_id="atlas", checks=[
        FakeCheck("ownership", "ok"),
        FakeCheck("agent_dry_run", "ok"),
        FakeCheck("channels", "fail",
                  summary="1 of 1 channel(s) failed handshake",
                  issues=[{"channel": "telegram",
                           "summary": "Telegram: Unauthorized",
                           "info": {"status": 401}}]),
        FakeCheck("e2e_echo", "pending"),
    ])
    specs = iim.detect_install_integrity_issues("atlas", g)
    assert len(specs) == 1
    assert specs[0]["type"] == "channel_handshake_failed"
    assert "telegram" in specs[0]["title"]
    assert "Unauthorized" in specs[0]["body"]


def test_multi_failure_emits_multiple_signals():
    """A bot failing both ownership + channels gets one signal per check (different types)."""
    g = FakeGauntlet(bot_id="team_bot_a", checks=[
        FakeCheck("ownership", "fail",
                  summary="1 path(s) wrong-owned",
                  issues=[{"path": "/Users/team_bot_a/.openclaw/auth-profiles.json",
                           "actual_owner": "root", "expected_owner": "team_bot_a"}]),
        FakeCheck("agent_dry_run", "ok"),
        FakeCheck("channels", "fail",
                  summary="1 of 2 channel(s) failed handshake",
                  issues=[{"channel": "slack", "summary": "Slack: invalid_auth"}]),
        FakeCheck("e2e_echo", "pending"),
    ])
    specs = iim.detect_install_integrity_issues("team_bot_a", g)
    types = sorted(s["type"] for s in specs)
    assert types == ["channel_handshake_failed", "ownership_drift"]


def test_unmapped_agent_dry_run_shape_emits_nothing(capsys):
    """An agent_dry_run fail with a summary that doesn't match any known shape
    must not produce a malformed Signal — it should log + emit nothing."""
    g = FakeGauntlet(bot_id="team_bot_a", checks=[
        FakeCheck("agent_dry_run", "fail",
                  summary="some new failure mode we haven't taught the monitor about"),
    ])
    specs = iim.detect_install_integrity_issues("team_bot_a", g)
    assert specs == []
    captured = capsys.readouterr()
    assert "unmapped shape" in captured.out


def test_e2e_pending_is_not_a_signal():
    """Check 4 is always pending in unattended runs — never a Signal."""
    g = _ok_gauntlet("team_bot_a")
    # e2e_echo defaults to "pending" in _ok_gauntlet; should not surface
    assert iim.detect_install_integrity_issues("team_bot_a", g) == []


def test_skipped_check_emits_no_signal():
    """STATUS_SKIP (e.g. channels skip when no channel configured) is ok, not a finding."""
    g = FakeGauntlet(bot_id="team_bot_a", checks=[
        FakeCheck("ownership", "ok"),
        FakeCheck("agent_dry_run", "ok"),
        FakeCheck("channels", "skip", summary="No messaging channels configured"),
        FakeCheck("e2e_echo", "pending"),
    ])
    assert iim.detect_install_integrity_issues("team_bot_a", g) == []


# ── Signature stability ─────────────────────────────────────────────────────


def test_signature_is_stable_across_runs():
    """Same root-cause finding on consecutive runs must produce the same
    signature so the Signal store dedupes / bumps observation_count instead
    of creating a new firing Signal."""
    g1 = FakeGauntlet(bot_id="team_bot_a", checks=[
        FakeCheck("ownership", "fail",
                  summary="1 path", issues=[{"path": "x", "actual_owner": "root", "expected_owner": "team_bot_a"}]),
    ])
    g2 = FakeGauntlet(bot_id="team_bot_a", checks=[
        FakeCheck("ownership", "fail",
                  summary="1 path (different summary text)",
                  issues=[{"path": "y", "actual_owner": "root", "expected_owner": "team_bot_a"}]),
    ])
    sig1 = iim.detect_install_integrity_issues("team_bot_a", g1)[0]["signature"]
    sig2 = iim.detect_install_integrity_issues("team_bot_a", g2)[0]["signature"]
    assert sig1 == sig2  # signature is (producer, type, bot_id), not path-list-content


def test_signature_differs_across_signal_types():
    """Two findings on the same bot in different categories must produce
    distinct signatures so they don't collide in the Signal store."""
    g = FakeGauntlet(bot_id="team_bot_a", checks=[
        FakeCheck("ownership", "fail", summary="x",
                  issues=[{"path": "x", "actual_owner": "root", "expected_owner": "team_bot_a"}]),
        FakeCheck("channels", "fail", summary="y",
                  issues=[{"channel": "telegram", "summary": "y"}]),
    ])
    sigs = {s["signature"] for s in iim.detect_install_integrity_issues("team_bot_a", g)}
    assert len(sigs) == 2


def test_signature_differs_across_bots():
    """Same finding on two different bots → two distinct signatures."""
    g_kit = FakeGauntlet(bot_id="team_bot_a", checks=[
        FakeCheck("ownership", "fail", summary="x",
                  issues=[{"path": "x", "actual_owner": "root", "expected_owner": "team_bot_a"}]),
    ])
    g_atlas = FakeGauntlet(bot_id="atlas", checks=[
        FakeCheck("ownership", "fail", summary="x",
                  issues=[{"path": "x", "actual_owner": "root", "expected_owner": "atlas"}]),
    ])
    sig_kit = iim.detect_install_integrity_issues("team_bot_a", g_kit)[0]["signature"]
    sig_atlas = iim.detect_install_integrity_issues("atlas", g_atlas)[0]["signature"]
    assert sig_kit != sig_atlas


# ── collect — per-bot iteration ─────────────────────────────────────────────


def test_collect_iterates_all_bots():
    runner = _fake_runner({
        "team_bot_a":   FakeGauntlet("team_bot_a", [
            FakeCheck("ownership", "fail",
                      summary="x", issues=[{"path": "x", "actual_owner": "root", "expected_owner": "team_bot_a"}]),
        ]),
        "atlas": FakeGauntlet("atlas", [
            FakeCheck("channels", "fail",
                      summary="y", issues=[{"channel": "telegram", "summary": "y"}]),
        ]),
    })
    specs = iim.collect(_network(("team_bot_a", "atlas")), gauntlet_runner=runner)
    by_bot = sorted(s["bot_id"] for s in specs)
    assert by_bot == ["atlas", "team_bot_a"]


def test_collect_skips_bot_when_runner_throws(capsys):
    def runner(bot_id, **kwargs):
        if bot_id == "team_bot_a":
            raise RuntimeError("simulated gauntlet crash")
        return _ok_gauntlet(bot_id)
    specs = iim.collect(_network(("team_bot_a", "atlas")), gauntlet_runner=runner)
    # Crash → team_bot_a produces no specs, atlas (clean) also no specs → empty
    assert specs == []
    captured = capsys.readouterr()
    assert "team_bot_a" in captured.out and "crashed" in captured.out


def test_collect_empty_network_returns_empty():
    assert iim.collect({}, gauntlet_runner=lambda **kw: None) == []
    assert iim.collect({"bots": {}}, gauntlet_runner=lambda **kw: None) == []


# ── run — observe + sweep_resolve plumbing ──────────────────────────────────


def test_run_dry_run_writes_nothing_and_returns_kept(tmp_path, capsys):
    """--dry-run mode prints would-observe payloads and DOES NOT touch the store."""
    runner = _fake_runner({
        "team_bot_a": FakeGauntlet("team_bot_a", [
            FakeCheck("ownership", "fail", summary="x",
                      issues=[{"path": "x", "actual_owner": "root", "expected_owner": "team_bot_a"}]),
        ]),
    })
    kept, n_fired, n_resolved = iim.run(
        tmp_path, _network(("team_bot_a",)), dry_run=True, gauntlet_runner=runner,
    )
    assert n_fired == 1
    assert n_resolved == 0  # dry-run skips sweep_resolve too
    assert len(kept) == 1
    captured = capsys.readouterr()
    assert "would_observe" in captured.out


def test_run_invokes_observe_and_sweep(monkeypatch, tmp_path):
    """Real (not dry-run) path calls store.observe per detection + sweep_resolve at end."""
    observed: list[dict] = []
    swept: list[dict] = []

    def fake_observe(shared_dir, **kw):
        observed.append(kw)
        return None

    def fake_sweep(shared_dir, **kw):
        swept.append(kw)
        return []

    monkeypatch.setattr(iim.signals_store, "observe", fake_observe)
    monkeypatch.setattr(iim.signals_store, "sweep_resolve", fake_sweep)

    runner = _fake_runner({
        "team_bot_a": FakeGauntlet("team_bot_a", [
            FakeCheck("ownership", "fail", summary="x",
                      issues=[{"path": "x", "actual_owner": "root", "expected_owner": "team_bot_a"}]),
        ]),
        "atlas": _ok_gauntlet("atlas"),
    })
    kept, n_fired, n_resolved = iim.run(
        tmp_path, _network(("team_bot_a", "atlas")), gauntlet_runner=runner,
    )
    assert n_fired == 1
    # observe called exactly once (team_bot_a's ownership)
    assert len(observed) == 1
    assert observed[0]["type"] == "ownership_drift"
    assert observed[0]["bot_id"] == "team_bot_a"
    # sweep_resolve called exactly once with PRODUCER + the surviving signature set
    assert len(swept) == 1
    assert swept[0]["producer"] == iim.PRODUCER
    assert observed[0]["signature"] in swept[0]["kept_signatures"]


def test_run_continues_when_one_observe_throws(monkeypatch, tmp_path, capsys):
    """A failed observe call must not abort the whole pass."""
    def fake_observe(shared_dir, **kw):
        raise RuntimeError("simulated store outage")

    def fake_sweep(shared_dir, **kw):
        return []

    monkeypatch.setattr(iim.signals_store, "observe", fake_observe)
    monkeypatch.setattr(iim.signals_store, "sweep_resolve", fake_sweep)

    runner = _fake_runner({
        "team_bot_a": FakeGauntlet("team_bot_a", [
            FakeCheck("ownership", "fail", summary="x",
                      issues=[{"path": "x", "actual_owner": "root", "expected_owner": "team_bot_a"}]),
        ]),
        "atlas": FakeGauntlet("atlas", [
            FakeCheck("channels", "fail", summary="y",
                      issues=[{"channel": "telegram", "summary": "y"}]),
        ]),
    })
    kept, n_fired, n_resolved = iim.run(
        tmp_path, _network(("team_bot_a", "atlas")), gauntlet_runner=runner,
    )
    # Both detections counted, both observe calls attempted (and failed).
    assert n_fired == 2
    captured = capsys.readouterr()
    assert "observe failed" in captured.out


def test_run_handles_sweep_failure(monkeypatch, tmp_path, capsys):
    """sweep_resolve failure is logged but doesn't bubble."""
    monkeypatch.setattr(iim.signals_store, "observe", lambda shared_dir, **kw: None)
    def boom(shared_dir, **kw):
        raise RuntimeError("simulated sweep outage")
    monkeypatch.setattr(iim.signals_store, "sweep_resolve", boom)

    runner = _fake_runner({"team_bot_a": _ok_gauntlet("team_bot_a")})
    kept, n_fired, n_resolved = iim.run(
        tmp_path, _network(("team_bot_a",)), gauntlet_runner=runner,
    )
    assert n_resolved == 0
    captured = capsys.readouterr()
    assert "sweep_resolve failed" in captured.out


# ── CLI / main ──────────────────────────────────────────────────────────────


def test_main_dry_run_smoke(monkeypatch, tmp_path, capsys):
    """End-to-end argparse path with --dry-run, mocked I/O."""
    monkeypatch.setattr(iim, "load_config", lambda p: _network(("team_bot_a",)))
    monkeypatch.setattr(iim, "get_shared_dir", lambda c: tmp_path)
    monkeypatch.setattr(
        iim, "collect",
        lambda network, gauntlet_runner=None: [{
            "signature": "install_integrity_monitor:ownership_drift:team_bot_a",
            "producer": iim.PRODUCER,
            "type": "ownership_drift",
            "flavor": "maintenance",
            "severity": "warn",
            "scope": "bot",
            "bot_id": "team_bot_a",
            "title": "team_bot_a: 1 path wrong-owned",
            "body": "...",
            "details": {"bot_id": "team_bot_a"},
        }],
    )
    monkeypatch.setattr(sys, "argv", ["install_integrity_monitor", "--dry-run"])
    iim.main()
    captured = capsys.readouterr()
    assert "dry-run" in captured.out
    assert "1 would-fire" in captured.out


# ── Pod-wide coalesce for legacy credential shape (2026-06-03 review) ───────


def _legacy_cred_gauntlet(bot_id):
    return FakeGauntlet(bot_id=bot_id, checks=[
        FakeCheck("ownership", "ok"),
        FakeCheck("agent_dry_run", "fail",
                  summary=(
                      f"Legacy key shape 'anthropic_api_key' on {bot_id} — "
                      "OC expects 'anthropic:api_key'"
                  ),
                  issues=[{
                      "path": "auth-profiles.json::profiles.anthropic_api_key",
                      "key": "anthropic_api_key",
                      "canonical": "anthropic:api_key",
                      "summary": (
                          "Legacy key shape 'anthropic_api_key' — OC expects "
                          "'anthropic:api_key'"
                      ),
                  }]),
        FakeCheck("channels", "ok"),
    ])


def test_run_coalesces_legacy_credential_shape_into_one_pod_signal(
    monkeypatch, tmp_path,
):
    """9 bots with legacy keys would otherwise produce 9 per-bot
    ``legacy_credential_shape`` Signals. The fix is a uniform migration,
    so we coalesce to one pod-scope ``legacy_credential_shape_pod``
    Signal listing every affected bot."""
    observed: list[dict] = []
    swept: list[dict] = []

    monkeypatch.setattr(iim.signals_store, "observe",
                        lambda shared_dir, **kw: observed.append(kw))
    monkeypatch.setattr(iim.signals_store, "sweep_resolve",
                        lambda shared_dir, **kw: (swept.append(kw) or []))

    bot_ids = ("team_bot_a", "atlas", "team_bot_c")
    runner = _fake_runner({b: _legacy_cred_gauntlet(b) for b in bot_ids})

    kept, n_fired, _ = iim.run(
        tmp_path, _network(bot_ids), gauntlet_runner=runner,
    )

    # Exactly ONE observe call — the pod-scope rollup. No per-bot
    # legacy_credential_shape calls.
    assert len(observed) == 1
    pod = observed[0]
    assert pod["type"] == "legacy_credential_shape_pod"
    assert pod["scope"] == "pod"
    assert pod["bot_id"] is None
    assert sorted(pod["details"]["bot_ids"]) == ["atlas", "team_bot_a", "team_bot_c"]
    assert pod["details"]["bot_count"] == 3
    # The per-bot legacy signatures were intercepted; they don't show up
    # in kept_signatures.
    assert pod["signature"] in kept
    per_bot_keys = [k for k in kept if "legacy_credential_shape:" in k]
    assert per_bot_keys == []
    assert n_fired == 1


def test_run_legacy_cred_coalesce_only_emits_when_at_least_one_bot_fails(
    monkeypatch, tmp_path,
):
    """No bots have the legacy issue → no pod-scope signal."""
    observed: list[dict] = []
    monkeypatch.setattr(iim.signals_store, "observe",
                        lambda shared_dir, **kw: observed.append(kw))
    monkeypatch.setattr(iim.signals_store, "sweep_resolve",
                        lambda shared_dir, **kw: [])

    runner = _fake_runner({"team_bot_a": _ok_gauntlet("team_bot_a")})
    kept, n_fired, _ = iim.run(
        tmp_path, _network(("team_bot_a",)), gauntlet_runner=runner,
    )
    assert observed == []
    assert n_fired == 0
    assert kept == set()


def test_run_legacy_cred_coalesce_preserves_other_per_bot_signals(
    monkeypatch, tmp_path,
):
    """A bot with a legacy key AND an ownership failure: the legacy
    issue rolls into the pod-scope signal, but the ownership issue
    stays as its own per-bot Signal."""
    observed: list[dict] = []
    monkeypatch.setattr(iim.signals_store, "observe",
                        lambda shared_dir, **kw: observed.append(kw))
    monkeypatch.setattr(iim.signals_store, "sweep_resolve",
                        lambda shared_dir, **kw: [])

    # team_bot_a — ownership AND legacy creds.
    mixed = FakeGauntlet(bot_id="team_bot_a", checks=[
        FakeCheck("ownership", "fail", summary="x",
                  issues=[{"path": "x", "actual_owner": "root",
                           "expected_owner": "team_bot_a"}]),
        FakeCheck("agent_dry_run", "fail",
                  summary="Legacy key shape",
                  issues=[{
                      "path": "auth-profiles.json::profiles.anthropic_api_key",
                      "key": "anthropic_api_key",
                      "canonical": "anthropic:api_key",
                      "summary": "Legacy key shape",
                  }]),
        FakeCheck("channels", "ok"),
    ])
    # atlas — only legacy creds.
    runner = _fake_runner({
        "team_bot_a": mixed,
        "atlas": _legacy_cred_gauntlet("atlas"),
    })

    iim.run(tmp_path, _network(("team_bot_a", "atlas")), gauntlet_runner=runner)

    types = [o["type"] for o in observed]
    # ownership_drift survives per-bot; legacy is coalesced.
    assert "ownership_drift" in types
    assert "legacy_credential_shape_pod" in types
    assert "legacy_credential_shape" not in types
    pod = next(o for o in observed if o["type"] == "legacy_credential_shape_pod")
    assert sorted(pod["details"]["bot_ids"]) == ["atlas", "team_bot_a"]


def test_build_signal_for_pod_legacy_cred_shape_helper_shape():
    """The helper's output shape is identical to what the per-bot specs
    use, so signals_store.observe can splat it directly."""
    spec = iim.build_signal_for_pod_legacy_cred_shape(["team_bot_c", "atlas", "team_bot_b"])
    assert spec["producer"] == iim.PRODUCER
    assert spec["type"] == "legacy_credential_shape_pod"
    assert spec["scope"] == "pod"
    assert spec["bot_id"] is None
    assert spec["flavor"] == "maintenance"
    assert spec["severity"] == "warn"
    assert "3 bots have legacy" in spec["title"]
    # bot_ids stored sorted for stable display
    assert spec["details"]["bot_ids"] == ["atlas", "team_bot_b", "team_bot_c"]
    # Operator triage rendered inline so the alert tile carries its own
    # explanation.
    assert "what_it_means" in spec["details"]
    assert "fix_steps" in spec["details"]


def test_build_signal_for_pod_legacy_cred_shape_handles_singular():
    spec = iim.build_signal_for_pod_legacy_cred_shape(["atlas"])
    assert "1 bot have" in spec["title"] or "1 bot has" in spec["title"] or "1 bot " in spec["title"]
    assert spec["details"]["bot_count"] == 1
