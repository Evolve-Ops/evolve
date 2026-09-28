"""tests/test_bot_auth_signal.py — pod_health bot_auth provisioning Signal.

Pins the DELIVERABLE-2 observability layer that closes the #3136 silent-failure
class: a credential-less bot must surface as a ``pod_health_bot_auth`` Signal
(an alert), not stay mute until a user types into it.

  * ``health._check_bot_auth_provisioning`` maps the
    ``oc_auth_provision.audit_bot_auth`` verdict to a per-bot CheckResult
    (missing→FAIL, unknown→WARN-no-flap, ok→PASS).
  * ``health._emit_health_signals`` turns a bot_auth FAIL into a firing
    ``pod_health_bot_auth`` Signal scoped to the bot, and a later PASS
    sweep-resolves it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_ADMIN = Path(__file__).resolve().parents[1]
_ANALYZER = Path(__file__).resolve().parents[2] / "analyzer"
for _p in (_ADMIN, _ANALYZER):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evolve_admin import health  # noqa: E402
from signals import store as signals_store  # noqa: E402


# ── _check_bot_auth_provisioning: verdict → CheckResult status ───────────────


def _run_check(verdict, detail="detail"):
    report = health.HealthReport()
    with patch("evolve_admin.oc_auth_provision.audit_bot_auth",
               return_value=(verdict, detail)), \
         patch.object(health, "get_bot_user", lambda b, n: b), \
         patch.object(health, "_bot_home", lambda b, n=None: Path(f"/home/{b}")):
        health._check_bot_auth_provisioning(report, ["darwin"], {"members": ["darwin"]})
    return [c for c in report.checks if c.category == "bot_auth"]


def test_missing_verdict_is_fail_with_deploy_fix():
    checks = _run_check("missing", "default model shows Auth:no")
    assert len(checks) == 1
    c = checks[0]
    assert c.status == health.FAIL
    assert c.name == "darwin:auth"
    assert c.fix_args == {"action": "deploy_bot", "bot_id": "darwin"}


def test_unknown_verdict_is_warn_not_fail_no_flap():
    """A transient 'unknown' must be WARN, never FAIL — so a one-off CLI read
    error can't page the operator or flap a previously-clear bot to alert."""
    checks = _run_check("unknown")
    assert len(checks) == 1
    assert checks[0].status == health.WARN


def test_ok_verdict_is_pass():
    checks = _run_check("ok")
    assert len(checks) == 1
    assert checks[0].status == health.PASS


def test_per_bot_exception_is_skipped_not_fatal():
    """audit_bot_auth raising for one bot must not abort the whole scan."""
    report = health.HealthReport()
    with patch("evolve_admin.oc_auth_provision.audit_bot_auth",
               side_effect=RuntimeError("boom")), \
         patch.object(health, "get_bot_user", lambda b, n: b), \
         patch.object(health, "_bot_home", lambda b, n=None: Path(f"/home/{b}")):
        health._check_bot_auth_provisioning(report, ["darwin"], {})
    assert [c for c in report.checks if c.category == "bot_auth"] == []


# ── _emit_health_signals: FAIL → firing pod_health_bot_auth Signal ───────────


def _report_with(status, name="darwin:auth", detail="auth store unprovisioned"):
    report = health.HealthReport()
    report.checks.append(health.CheckResult(status, "bot_auth", name, detail))
    return report


def test_bot_auth_fail_emits_firing_bot_scoped_signal(tmp_path):
    report = _report_with(health.FAIL)
    health._emit_health_signals(report, tmp_path, members=["darwin"], primary_bot_id="evo")
    fired = list(signals_store.iter_active(
        tmp_path, producer="pod_health", state="firing"))
    bot_auth = [s for s in fired if s.type == "pod_health_bot_auth"]
    assert len(bot_auth) == 1
    sig = bot_auth[0]
    assert sig.scope == "bot"
    assert sig.bot_id == "darwin"
    assert sig.severity == "alert"


def test_bot_auth_pass_sweep_resolves_prior_firing(tmp_path):
    """A bot that recovers (PASS) sweep-resolves its prior firing signal — the
    full-path sweep (sweep_types=None) covers pod_health_bot_auth."""
    health._emit_health_signals(_report_with(health.FAIL), tmp_path,
                                members=["darwin"], primary_bot_id="evo")
    # now the bot is healthy — PASS check, nothing kept → sweep resolves it.
    health._emit_health_signals(_report_with(health.PASS), tmp_path,
                                members=["darwin"], primary_bot_id="evo")
    still_firing = [s for s in signals_store.iter_active(
        tmp_path, producer="pod_health", state="firing")
        if s.type == "pod_health_bot_auth"]
    assert still_firing == []


def test_bot_auth_unknown_warn_keeps_signal_firing_no_flap(tmp_path):
    """An 'unknown' tick emits a WARN on the SAME signature — the signal stays
    firing (severity refreshed) rather than resolving and re-opening (a flap)."""
    health._emit_health_signals(_report_with(health.FAIL), tmp_path,
                                members=["darwin"], primary_bot_id="evo")
    health._emit_health_signals(
        _report_with(health.WARN, detail="could not verify auth provisioning"),
        tmp_path, members=["darwin"], primary_bot_id="evo")
    firing = [s for s in signals_store.iter_active(
        tmp_path, producer="pod_health", state="firing")
        if s.type == "pod_health_bot_auth"]
    assert len(firing) == 1  # still firing, not resolved
