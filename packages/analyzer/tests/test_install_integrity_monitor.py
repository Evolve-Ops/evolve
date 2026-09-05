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
# internal/spec-drift-alert-taxonomy-2026-06-26.md §L3.


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


# ── Pod-scope plugin install-tree integrity (spec §6.3) ─────────────────────
#
# The deploy-time gate (deploy.install_oc_plugin) only sees drift when someone
# deploys; the drift accrues between deploys. These cover the monitor that
# closes that observation gap.


class _Probe:
    """Scripted _probe_plugin_signature replacement."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, shared_dir):
        self.calls += 1
        # Last result repeats, so a two-probe confirm needs only one entry
        # when both probes should agree.
        idx = min(self.calls - 1, len(self.results) - 1)
        return self.results[idx]


def _no_sleep(_seconds):
    return None


def test_plugin_ok_emits_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(iim, "_probe_plugin_signature", _Probe(("ok", "")))
    assert iim.detect_plugin_integrity(tmp_path, sleep_fn=_no_sleep)[0] == []


def test_plugin_absent_install_dir_emits_nothing(monkeypatch, tmp_path):
    """A fresh pod with nothing deployed is not drift. Firing here would make
    every pre-first-deploy pod page the operator."""
    monkeypatch.setattr(
        iim, "_probe_plugin_signature", _Probe(("absent", "no install dir")),
    )
    assert iim.detect_plugin_integrity(tmp_path, sleep_fn=_no_sleep)[0] == []


def test_plugin_deploying_emits_nothing(monkeypatch, tmp_path):
    """A deploy holding the pod lock is rewriting the tree by design."""
    monkeypatch.setattr(
        iim, "_probe_plugin_signature", _Probe(("deploying", "lock held")),
    )
    assert iim.detect_plugin_integrity(tmp_path, sleep_fn=_no_sleep)[0] == []


def test_plugin_mismatch_confirmed_fires_alert(monkeypatch, tmp_path):
    probe = _Probe(("mismatch", "digest mismatch: stamped=sha256:a computed=sha256:b"))
    monkeypatch.setattr(iim, "_probe_plugin_signature", probe)
    out, _keep = iim.detect_plugin_integrity(tmp_path, sleep_fn=_no_sleep)
    assert len(out) == 1
    sig = out[0]
    assert sig["type"] == "plugin_digest_mismatch"
    assert sig["severity"] == "alert"
    assert sig["scope"] == "pod"
    assert sig["bot_id"] is None
    assert sig["producer"] == iim.PRODUCER
    # It must be confirmed, not fired on a single probe.
    assert probe.calls == 2
    # The body has to tell the operator the deploy consequence, not just that
    # a hash differs.
    assert "refuse" in sig["body"]
    assert "evolve-admin upgrade" in sig["body"]


def test_plugin_mismatch_not_confirmed_is_suppressed(monkeypatch, tmp_path):
    """A single probe landing inside build_plugin's rm -rf + cp -R window must
    not page anyone. Only the web Upgrade path holds the deploy lock while it
    syncs, so the re-probe is what covers the CLI deploy and the puller."""
    probe = _Probe(("mismatch", "transient"), ("ok", ""))
    monkeypatch.setattr(iim, "_probe_plugin_signature", probe)
    assert iim.detect_plugin_integrity(tmp_path, sleep_fn=_no_sleep)[0] == []
    assert probe.calls == 2


def test_plugin_mismatch_recheck_uses_the_second_detail(monkeypatch, tmp_path):
    """The confirmed detail is what the operator reads — it must come from the
    confirming probe, not the stale first one."""
    probe = _Probe(("mismatch", "first read"), ("mismatch", "second read"))
    monkeypatch.setattr(iim, "_probe_plugin_signature", probe)
    out, _keep = iim.detect_plugin_integrity(tmp_path, sleep_fn=_no_sleep)
    assert "second read" in out[0]["details"]["verification_detail"]


def test_plugin_legacy_stamp_fires_info_without_recheck(monkeypatch, tmp_path):
    """The rollout tracker. Info-tier, and no confirm probe — a legacy stamp is
    a stable fact, not a race."""
    probe = _Probe(("legacy", "carries no x-evolve-trust.treeDigest"))
    monkeypatch.setattr(iim, "_probe_plugin_signature", probe)
    out, _keep = iim.detect_plugin_integrity(tmp_path, sleep_fn=_no_sleep)
    assert len(out) == 1
    sig = out[0]
    assert sig["type"] == "plugin_stamp_legacy"
    assert sig["severity"] == "info"
    assert sig["scope"] == "pod"
    assert probe.calls == 1
    # It must name the constant it gates, or it is not a rollout tracker.
    assert sig["details"]["blocks_flip_of"] == "plugin_signature.REQUIRE_TREE_DIGEST"
    assert "REQUIRE_TREE_DIGEST" in sig["body"]
    assert "Do not flip" in sig["body"]


def test_plugin_signatures_are_digest_independent():
    """Signature must NOT embed the observed digest. If it did, every fresh
    tamper would mint a new Signal and sweep-resolve the previous one, so an
    escalating problem would render as a series of self-healing alerts."""
    a = iim._signal_for_plugin_digest_mismatch("stamped=sha256:aa computed=sha256:bb")
    b = iim._signal_for_plugin_digest_mismatch("stamped=sha256:cc computed=sha256:dd")
    assert a["signature"] == b["signature"]
    # And the two types must not collide with each other.
    legacy = iim._signal_for_plugin_stamp_legacy("x")
    assert legacy["signature"] != a["signature"]


def test_run_includes_plugin_finding_in_kept_signatures(monkeypatch, tmp_path):
    """The finding has to ride the same sweep_resolve path as everything else,
    or it will never auto-resolve when the operator fixes it."""
    observed = []
    monkeypatch.setattr(
        iim.signals_store, "observe",
        lambda sd, **kw: observed.append(kw),
    )
    swept = {}

    def _sweep(sd, *, producer, kept_signatures, reason):
        swept["kept"] = set(kept_signatures)
        return []

    monkeypatch.setattr(iim.signals_store, "sweep_resolve", _sweep)
    spec = iim._signal_for_plugin_digest_mismatch("boom")
    kept, n_fired, _ = iim.run(
        tmp_path, {"members": []},
        plugin_detector=lambda _sd, **kw: ([spec], set()),
    )
    assert spec["signature"] in kept
    assert spec["signature"] in swept["kept"]
    assert n_fired == 1
    assert observed and observed[0]["type"] == "plugin_digest_mismatch"


def test_run_survives_a_throwing_plugin_detector(monkeypatch, tmp_path, capsys):
    """A broken plugin probe must not take the per-bot gauntlet findings with
    it — the monitor's other job still has to get done."""
    monkeypatch.setattr(iim.signals_store, "observe", lambda sd, **kw: None)
    monkeypatch.setattr(
        iim.signals_store, "sweep_resolve",
        lambda sd, **kw: [],
    )

    def _boom(_sd, **_kw):
        raise RuntimeError("probe exploded")

    kept, n_fired, _ = iim.run(
        tmp_path, {"members": []}, plugin_detector=_boom,
    )
    assert n_fired == 0
    assert "plugin integrity probe failed" in capsys.readouterr().out


def test_plugin_types_have_severity_defaults():
    """Guard: a type without a _SIGNAL_DEFAULTS entry KeyErrors at emit time,
    which is the worst place to find out."""
    for t in (
        iim._SIGNAL_TYPE_PLUGIN_DIGEST_MISMATCH,
        iim._SIGNAL_TYPE_PLUGIN_STAMP_LEGACY,
    ):
        assert t in iim._SIGNAL_DEFAULTS
        assert iim._SIGNAL_DEFAULTS[t]["severity"] in ("info", "warn", "alert")


# ── The sweep must not auto-resolve on an inconclusive pass ─────────────────
#
# run() feeds `kept` to sweep_resolve, which archives every Signal of this
# producer whose signature is absent. So "I could not tell" and "it cleared"
# must not both render as an empty spec list — otherwise a daily run that
# overlaps a deploy hands the operator a false all-clear on a live alert.


def _sweep_capture(monkeypatch):
    seen = {}
    monkeypatch.setattr(iim.signals_store, "observe", lambda sd, **kw: None)

    def _sweep(sd, *, producer, kept_signatures, reason):
        seen["kept"] = set(kept_signatures)
        return []

    monkeypatch.setattr(iim.signals_store, "sweep_resolve", _sweep)
    return seen


def test_deploying_pass_preserves_signatures(monkeypatch, tmp_path):
    monkeypatch.setattr(
        iim, "_probe_plugin_signature", _Probe(("deploying", "lock held")),
    )
    specs, keep = iim.detect_plugin_integrity(tmp_path, sleep_fn=_no_sleep)
    assert specs == []
    assert keep == iim._plugin_signature_keys(), (
        "a deploy in flight must not let the sweep resolve a firing Signal"
    )


def test_unverifiable_pass_preserves_signatures(monkeypatch, tmp_path):
    monkeypatch.setattr(
        iim, "_probe_plugin_signature", _Probe(("unverifiable", "EACCES")),
    )
    specs, keep = iim.detect_plugin_integrity(tmp_path, sleep_fn=_no_sleep)
    assert specs == []
    assert keep == iim._plugin_signature_keys()


def test_clean_pass_allows_resolve(monkeypatch, tmp_path):
    """The other direction: a genuinely clean verification MUST let an old
    Signal resolve, or the alert would never clear."""
    monkeypatch.setattr(iim, "_probe_plugin_signature", _Probe(("ok", "")))
    specs, keep = iim.detect_plugin_integrity(tmp_path, sleep_fn=_no_sleep)
    assert specs == []
    assert keep == set()


def test_run_keeps_signatures_when_detector_raises(monkeypatch, tmp_path, capsys):
    seen = _sweep_capture(monkeypatch)

    def _boom(_sd, **_kw):
        raise RuntimeError("probe exploded")

    iim.run(tmp_path, {"members": []}, plugin_detector=_boom)
    assert seen["kept"] >= iim._plugin_signature_keys(), (
        "a crashed probe must not read as 'install integrity restored'"
    )
    assert "plugin integrity probe failed" in capsys.readouterr().out


def test_run_keeps_inconclusive_signatures_out_of_the_sweep(monkeypatch, tmp_path):
    seen = _sweep_capture(monkeypatch)
    keep = iim._plugin_signature_keys()
    iim.run(
        tmp_path, {"members": []},
        plugin_detector=lambda _sd, **_kw: ([], keep),
    )
    assert seen["kept"] >= keep


def test_mismatch_keeps_legacy_signature_alive(monkeypatch, tmp_path):
    """A failed verification says nothing about whether the stamp carries a
    treeDigest, so the rollout tracker must not be resolved by it."""
    monkeypatch.setattr(
        iim, "_probe_plugin_signature", _Probe(("mismatch", "bad")),
    )
    specs, keep = iim.detect_plugin_integrity(tmp_path, sleep_fn=_no_sleep)
    assert specs[0]["type"] == "plugin_digest_mismatch"
    assert keep == {
        iim.make_signature(iim.PRODUCER, "plugin_stamp_legacy", "plugin"),
    }


def test_deleted_install_tree_on_a_deployed_pod_fires(monkeypatch, tmp_path):
    """An absent install dir means opposite things on a fresh pod and on one
    that has deployed. Silence on the latter is a fail-open: every gateway has
    lost the plugin and every future deploy will refuse."""
    import json as _json

    (tmp_path / "install.json").write_text(
        _json.dumps({"bot_versions": {"admin_bot": {"version": "1"}}}),
    )
    assert iim._pod_has_ever_deployed(tmp_path) is True
    assert iim._pod_has_ever_deployed(tmp_path / "nope") is False


def test_pod_has_ever_deployed_tolerates_junk(tmp_path):
    (tmp_path / "install.json").write_text("not json {{{")
    assert iim._pod_has_ever_deployed(tmp_path) is False
    (tmp_path / "install.json").write_text('{"bot_versions": {}}')
    assert iim._pod_has_ever_deployed(tmp_path) is False


# ── _probe_plugin_signature against a real tree ────────────────────────────
#
# Every test above stubs this function, so its ok/legacy/mismatch mapping was
# unpinned — inverting `if not ok` kept the suite green. plugin_signature
# works on any directory and stamp_manifest_in_place falls back to a direct
# write when the target is writable, so a real tree is cheap here.

import pytest  # noqa: E402


def _real_install_tree(root):
    import json as _json

    (root / "dist").mkdir(parents=True)
    (root / "dist" / "index.js").write_bytes(b"main")
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / "node_modules" / "dep" / "i.js").write_bytes(b"dep")
    (root / "package.json").write_bytes(b"{}")
    (root / "package-lock.json").write_bytes(b"{}")
    (root / "openclaw.plugin.json").write_text(_json.dumps({"id": "evolve"}))
    return root


@pytest.fixture
def _probe_env(monkeypatch, tmp_path):
    """Point the probe at a temp tree with no deploy lock contention."""
    import evolve_admin.deploy as _deploy
    import evolve_admin.deploy_resilience as _dres

    tree = _real_install_tree(tmp_path / "plugin")
    monkeypatch.setattr(_deploy, "PLUGIN_INSTALL_DIR", tree, raising=True)
    monkeypatch.setattr(_dres, "try_acquire_deploy_lock", lambda sd: object())
    monkeypatch.setattr(_dres, "release_deploy_lock", lambda h: None)
    return tree


def test_real_probe_clean_tree_is_ok(_probe_env, tmp_path):
    from evolve_admin.plugin_signature import stamp_install_tree

    stamp_install_tree(_probe_env)
    assert iim._probe_plugin_signature(tmp_path)[0] == "ok"


def test_real_probe_detects_tamper(_probe_env, tmp_path):
    from evolve_admin.plugin_signature import stamp_install_tree

    stamp_install_tree(_probe_env)
    (_probe_env / "node_modules" / "dep" / "i.js").write_bytes(b"evil")
    state, detail = iim._probe_plugin_signature(tmp_path)
    assert state == "mismatch"
    assert "mismatch" in detail


def test_real_probe_detects_legacy_stamp(_probe_env, tmp_path):
    """distDigest only — the rollout-tracker state, and the one the mini is
    actually in today."""
    import json as _json

    from evolve_admin.plugin_signature import (
        canonical_dist_digest,
        stamp_manifest_in_place,
    )

    stamp_manifest_in_place(
        _probe_env / "openclaw.plugin.json",
        digest=canonical_dist_digest(_probe_env / "dist"),
    )
    trust = _json.loads(
        (_probe_env / "openclaw.plugin.json").read_text())["x-evolve-trust"]
    assert "treeDigest" not in trust  # precondition
    assert iim._probe_plugin_signature(tmp_path)[0] == "legacy"


def test_real_probe_unstamped_is_mismatch_not_legacy(_probe_env, tmp_path):
    """No trust block at all is a verification FAILURE, not the rollout
    state — they must not collapse together."""
    assert iim._probe_plugin_signature(tmp_path)[0] == "mismatch"


def test_real_probe_deploy_lock_held_short_circuits(
    _probe_env, tmp_path, monkeypatch,
):
    import evolve_admin.deploy_resilience as _dres

    monkeypatch.setattr(_dres, "try_acquire_deploy_lock", lambda sd: None)
    assert iim._probe_plugin_signature(tmp_path)[0] == "deploying"


def test_real_probe_absent_tree_never_deployed(monkeypatch, tmp_path):
    import evolve_admin.deploy as _deploy
    import evolve_admin.deploy_resilience as _dres

    monkeypatch.setattr(
        _deploy, "PLUGIN_INSTALL_DIR", tmp_path / "gone", raising=True)
    monkeypatch.setattr(_dres, "try_acquire_deploy_lock", lambda sd: object())
    monkeypatch.setattr(_dres, "release_deploy_lock", lambda h: None)
    assert iim._probe_plugin_signature(tmp_path)[0] == "absent"


def test_real_probe_absent_tree_after_a_deploy_is_a_mismatch(
    monkeypatch, tmp_path,
):
    import json as _json

    import evolve_admin.deploy as _deploy
    import evolve_admin.deploy_resilience as _dres

    monkeypatch.setattr(
        _deploy, "PLUGIN_INSTALL_DIR", tmp_path / "gone", raising=True)
    monkeypatch.setattr(_dres, "try_acquire_deploy_lock", lambda sd: object())
    monkeypatch.setattr(_dres, "release_deploy_lock", lambda h: None)
    (tmp_path / "install.json").write_text(
        _json.dumps({"bot_versions": {"admin_bot": {"v": "1"}}}))
    state, detail = iim._probe_plugin_signature(tmp_path)
    assert state == "mismatch"
    assert "removed" in detail
