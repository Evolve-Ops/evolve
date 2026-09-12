"""Tests for the EVOLVE-ACCT-OCJSON monitor exemption.

The separated evo primary (spec-evo-account-separation-2026-05-25) does not
host a stat-able bot-shaped ``openclaw.json`` the way an ordinary member bot
does — the privileged ``evolve`` service account carries none, and the ``evo``
account's config may live in the migrated OC agent SQLite store rather than at
the JSON path the monitors stat. Three monitors false-fired on this:

  - ``audit.audit_config``                       → "cannot read openclaw.json"
  - ``install_integrity_monitor`` Check 1        → ownership_drift
  - ``install_integrity_monitor`` Check 2 (cfg)  → config_invalid

The ownership exemption (Check 1) is NARROWED, not blanket: only the
un-stat-able ``openclaw.json`` row is filtered out of the ownership walk.
A wrong-owned ``auth-profiles.json`` / ``sessions.json`` / ``workspace/``
path under evo's ``.openclaw/`` is genuine cross-account drift on secret
files and STILL fires ownership_drift even for the separated evo primary.

These tests pin:
  1. separated-pod evo primary → no false fire from the three openclaw.json
     checks (ownership of openclaw.json, config-validate, audit read);
  2. a wrong-owned non-openclaw.json secret under the separated primary →
     ownership_drift STILL fires (the narrowing is not a blind spot);
  3. an ordinary bot with a real missing/invalid openclaw.json → STILL fires
     (the exemption is narrow — it must not suppress real findings);
  4. a pre-separation pod → unchanged (true no-op of the new predicate).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402
import install_integrity_monitor as iim  # noqa: E402
import primary_bot  # noqa: E402


# ── primary_bot.primary_is_separated_evo — the shared predicate ─────────────


def test_predicate_true_on_separated_pod():
    """Primary `evo` on its own dedicated `evo` account → separated."""
    net = {
        "primary": "evo",
        "bots": {"evo": {"role": "primary", "user": "evo"},
                 "atlas": {"role": "member", "user": "atlas"}},
    }
    assert primary_bot.primary_is_separated_evo(net) is True


def test_predicate_false_on_macos_precutover_pod():
    """Primary `evo` still running on the `evolve` service account (macOS,
    pre-E.2.b cutover) → NOT separated; its openclaw.json lives at
    /Users/evolve and SHOULD be audited."""
    net = {
        "primary": "evo",
        "bots": {"evo": {"role": "primary", "user": "evolve"}},
    }
    assert primary_bot.primary_is_separated_evo(net) is False


def test_predicate_false_on_legacy_evolve_primary_pod():
    """Legacy pod whose primary is a bot literally named `evolve` → not
    separated (no `evo` account); the predicate no-ops."""
    net = {"bots": {"evolve": {"role": "primary"}}}  # legacy fallback resolves
    assert primary_bot.primary_is_separated_evo(net) is False


def test_predicate_false_when_no_primary():
    assert primary_bot.primary_is_separated_evo({"bots": {}}) is False


# ── audit.audit_config — exemption + real-finding preservation ──────────────


def _separated_net() -> dict:
    return {
        "primary": "evo",
        "bots": {"evo": {"role": "primary", "user": "evo", "port": 19030},
                 "atlas": {"role": "member", "user": "atlas", "port": 19001}},
    }


def _precutover_net() -> dict:
    return {
        "primary": "evo",
        "bots": {"evo": {"role": "primary", "user": "evolve", "port": 19030}},
    }


def test_audit_config_skips_separated_evo_primary_without_subprocess():
    """The separated evo primary returns an OK 'skipped' finding and NEVER
    shells out to `sudo /bin/cat` — so the rc!=0 'cannot read' warn cannot
    fire."""
    net = _separated_net()
    with patch.object(audit.subprocess, "run") as mock_run:
        findings = audit.audit_config("evo", net, Path("/tmp/shared"))
    mock_run.assert_not_called()
    assert len(findings) == 1
    assert findings[0].level == "ok"
    assert findings[0].category == "config"
    assert "skipped" in findings[0].message
    # No spurious warn-level "cannot read openclaw.json".
    assert not any(f.level == "warn" for f in findings)


def test_audit_config_ordinary_bot_still_fires_on_missing_ocjson():
    """An ordinary member bot whose openclaw.json genuinely can't be read
    STILL fires the warn finding — the exemption must not suppress it."""
    net = _separated_net()

    @dataclass
    class _R:
        returncode: int = 1
        stdout: str = ""
        stderr: str = "No such file"

    with patch.object(audit.subprocess, "run", return_value=_R()) as mock_run:
        findings = audit.audit_config("atlas", net, Path("/tmp/shared"))
    mock_run.assert_called_once()
    assert any(
        f.level == "warn" and "cannot read openclaw.json" in f.message
        for f in findings
    ), findings


def test_audit_config_precutover_primary_still_audited():
    """A pre-cutover (macOS) primary `evo` running on `evolve` is NOT exempt —
    its openclaw.json is present at /Users/evolve and must be audited. A
    rc!=0 read here is a REAL finding, not a suppressed one."""
    net = _precutover_net()

    @dataclass
    class _R:
        returncode: int = 1
        stdout: str = ""
        stderr: str = "No such file"

    with patch.object(audit.subprocess, "run", return_value=_R()) as mock_run:
        findings = audit.audit_config("evo", net, Path("/tmp/shared"))
    mock_run.assert_called_once()
    assert any(
        f.level == "warn" and "cannot read openclaw.json" in f.message
        for f in findings
    ), findings


# ── install_integrity_monitor — Check 1 (ownership) + Check 2 (config) ──────


@dataclass
class _FakeCheck:
    name: str
    status: str = "ok"
    summary: str = ""
    detail: str = ""
    issues: list[dict] = field(default_factory=list)


@dataclass
class _FakeGauntlet:
    bot_id: str
    checks: list
    overall: str = "fail"


def _failing_ownership_and_config(bot_id: str) -> _FakeGauntlet:
    return _FakeGauntlet(bot_id=bot_id, checks=[
        _FakeCheck("ownership", "fail",
                   summary=f"3 path(s) under .openclaw/ not owned by {bot_id}",
                   issues=[{"path": f"/home/{bot_id}/.openclaw/openclaw.json",
                            "actual_owner": "root", "expected_owner": bot_id}]),
        _FakeCheck("agent_dry_run", "fail",
                   summary="openclaw.json rejected by the config validator",
                   issues=[{"field": "x"}]),
        _FakeCheck("channels", "ok"),
    ])


def test_iim_exempt_suppresses_ownership_and_config_for_separated_primary():
    g = _failing_ownership_and_config("evo")
    specs = iim.detect_install_integrity_issues(
        "evo", g, exempt_ocjson_checks=True,
    )
    types = {s["type"] for s in specs}
    assert "ownership_drift" not in types
    assert "config_invalid" not in types
    assert specs == []  # both findings were the two ocjson checks


def test_iim_exempt_keeps_gateway_down_finding():
    """The exemption is narrow: a gateway-down finding on the same bot STILL
    fires — only the openclaw.json ownership row + config-validate are
    suppressed."""
    g = _FakeGauntlet(bot_id="evo", checks=[
        _FakeCheck("ownership", "fail",
                   summary="1 path(s) under .openclaw/ not owned by evo",
                   issues=[{"path": "/home/evo/.openclaw/openclaw.json",
                            "expected_owner": "evo"}]),
        _FakeCheck("agent_dry_run", "fail",
                   summary="Gateway on port 19030 not responding to /evolve/status"),
        _FakeCheck("channels", "ok"),
    ])
    specs = iim.detect_install_integrity_issues(
        "evo", g, exempt_ocjson_checks=True,
    )
    types = {s["type"] for s in specs}
    assert "ownership_drift" not in types
    assert "gateway_down" in types


def test_iim_exempt_still_fires_ownership_on_nonocjson_secret_drift():
    """The narrowing must NOT blind us to genuine cross-account drift: a
    wrong-owned auth-profiles.json (or any non-openclaw.json path) under the
    separated evo primary's .openclaw/ STILL raises ownership_drift even when
    the exemption is active. Only the un-stat-able openclaw.json row is
    filtered; the rest is a real security signal."""
    g = _FakeGauntlet(bot_id="evo", checks=[
        _FakeCheck("ownership", "fail",
                   summary="2 path(s) under .openclaw/ not owned by evo",
                   issues=[
                       # The one the evolve account legitimately can't stat —
                       # this row is dropped.
                       {"path": "/home/evo/.openclaw/openclaw.json",
                        "actual_owner": "root", "expected_owner": "evo"},
                       # A genuinely wrong-owned secret — MUST still fire.
                       {"path": "/home/evo/.openclaw/auth-profiles.json",
                        "actual_owner": "root", "expected_owner": "evo"},
                   ]),
        _FakeCheck("channels", "ok"),
    ])
    specs = iim.detect_install_integrity_issues(
        "evo", g, exempt_ocjson_checks=True,
    )
    own = [s for s in specs if s["type"] == "ownership_drift"]
    assert len(own) == 1, specs
    # Only the non-openclaw.json path survives into the fired finding.
    fired_paths = [i["path"] for i in own[0]["details"]["issues"]]
    assert fired_paths == ["/home/evo/.openclaw/auth-profiles.json"]
    assert own[0]["details"]["issue_count"] == 1


def test_iim_exempt_suppresses_when_only_ocjson_ownership_drift():
    """When the ONLY wrong-owned path is the openclaw.json the evolve account
    can't stat, ownership_drift is fully suppressed (no fire)."""
    g = _FakeGauntlet(bot_id="evo", checks=[
        _FakeCheck("ownership", "fail",
                   summary="1 path(s) under .openclaw/ not owned by evo",
                   issues=[{"path": "/home/evo/.openclaw/openclaw.json",
                            "actual_owner": "root", "expected_owner": "evo"}]),
        _FakeCheck("channels", "ok"),
    ])
    specs = iim.detect_install_integrity_issues(
        "evo", g, exempt_ocjson_checks=True,
    )
    assert "ownership_drift" not in {s["type"] for s in specs}


def test_iim_ordinary_bot_still_fires_both():
    """An ordinary member bot (not exempt) still fires ownership_drift +
    config_invalid — the exemption must not leak to other bots."""
    g = _failing_ownership_and_config("atlas")
    specs = iim.detect_install_integrity_issues(
        "atlas", g, exempt_ocjson_checks=False,
    )
    types = {s["type"] for s in specs}
    assert "ownership_drift" in types
    assert "config_invalid" in types


def test_iim_collect_exempts_only_separated_primary():
    """End-to-end through collect(): on a separated pod, the evo primary's
    ownership+config findings are dropped while a member bot with the SAME
    failing gauntlet still fires both."""
    net = _separated_net()
    gauntlets = {
        "evo": _failing_ownership_and_config("evo"),
        "atlas": _failing_ownership_and_config("atlas"),
    }

    def runner(bot_id, *, network, **kwargs):
        return gauntlets[bot_id]

    specs = iim.collect(net, gauntlet_runner=runner)
    by_bot: dict[str, set[str]] = {}
    for s in specs:
        by_bot.setdefault(s.get("bot_id") or "?", set()).add(s["type"])
    assert by_bot.get("evo", set()) == set()  # both ocjson findings suppressed
    assert "ownership_drift" in by_bot.get("atlas", set())
    assert "config_invalid" in by_bot.get("atlas", set())


def test_iim_collect_noop_on_presep_pod():
    """On a pre-separation pod the evo primary is NOT exempt — collect() fires
    its ownership+config findings exactly as before this change."""
    net = _precutover_net()
    gauntlets = {"evo": _failing_ownership_and_config("evo")}

    def runner(bot_id, *, network, **kwargs):
        return gauntlets[bot_id]

    specs = iim.collect(net, gauntlet_runner=runner)
    types = {s["type"] for s in specs}
    assert "ownership_drift" in types
    assert "config_invalid" in types
