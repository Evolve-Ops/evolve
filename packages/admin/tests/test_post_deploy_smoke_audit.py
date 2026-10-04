"""tests/test_post_deploy_smoke_audit.py — post-deploy smoke audit.

A bug in ``audit_oc_security`` (evolve-ops/evolve#1088) emitted phantom CRITICAL
findings for every bot in the pod for an unknown window. Nothing in deploy
asserted "a freshly-deployed bot should produce no CRITICAL audit findings,"
so the noise accumulated unchecked.

These tests cover the smoke-audit step added to fix that:

  - ``run_smoke_audit`` resets the 23h time-gate, calls ``audit_oc_security``,
    splits findings by level, and converts any exception into a structured
    error rather than crashing the deploy
  - The CLI gate (``_exit_on_audit_criticals``) exits non-zero on criticals or
    audit-run errors, and is overridden by --allow-audit-criticals
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))


@dataclass
class _FakeFinding:
    """Stand-in for analyzer.audit.Finding — duck-typed on .level/.message/.detail."""
    level: str
    message: str
    detail: str = ""
    category: str = "config"
    bot_id: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# run_smoke_audit
# ─────────────────────────────────────────────────────────────────────────────


def test_run_smoke_audit_deletes_time_gate_file(tmp_path, monkeypatch):
    """The 23h time-gate file must be deleted before the audit runs, otherwise
    audit_oc_security returns [] when called within 23h of the last run."""
    from evolve_admin import deploy as _deploy

    ts = tmp_path / "security" / "last-oc-audit-admin_bot"
    ts.parent.mkdir(parents=True)
    ts.write_text("9999999999")

    called: dict = {}

    def fake_audit(bot_id, shared_dir):
        called["existed_at_call"] = ts.exists()
        return []

    fake_mod = mock.MagicMock()
    fake_mod.audit_oc_security = fake_audit
    monkeypatch.setitem(sys.modules, "audit", fake_mod)

    _deploy.run_smoke_audit("admin_bot", tmp_path)
    assert called["existed_at_call"] is False


def test_run_smoke_audit_splits_findings_by_level(tmp_path, monkeypatch):
    from evolve_admin import deploy as _deploy

    findings = [
        _FakeFinding(level="critical", message="🔴 loopback_no_auth"),
        _FakeFinding(level="critical", message="🔴 trusted_proxies_missing"),
        _FakeFinding(level="warn", message="permissions corrected"),
        _FakeFinding(level="ok", message="audit OK"),
        _FakeFinding(level="skipped", message="not configured"),
    ]
    fake_mod = mock.MagicMock()
    fake_mod.audit_oc_security = lambda *_a, **_kw: findings
    monkeypatch.setitem(sys.modules, "audit", fake_mod)

    r = _deploy.run_smoke_audit("admin_bot", tmp_path)
    assert r.error is None
    assert len(r.critical_findings) == 2
    assert len(r.warn_findings) == 1
    assert not r.is_clean  # criticals present


def test_run_smoke_audit_clean_run_is_is_clean(tmp_path, monkeypatch):
    from evolve_admin import deploy as _deploy

    fake_mod = mock.MagicMock()
    fake_mod.audit_oc_security = lambda *_a, **_kw: [
        _FakeFinding(level="ok", message="all good"),
    ]
    monkeypatch.setitem(sys.modules, "audit", fake_mod)

    r = _deploy.run_smoke_audit("admin_bot", tmp_path)
    assert r.is_clean
    assert r.error is None


def test_run_smoke_audit_swallows_subprocess_error(tmp_path, monkeypatch):
    """A bug in audit_oc_security must not crash the deploy CLI — surface as
    an audit-run error instead, since the deploy itself already happened."""
    from evolve_admin import deploy as _deploy

    def raise_subprocess_err(*_a, **_kw):
        raise subprocess.CalledProcessError(returncode=1, cmd=["openclaw", "security"])

    fake_mod = mock.MagicMock()
    fake_mod.audit_oc_security = raise_subprocess_err
    monkeypatch.setitem(sys.modules, "audit", fake_mod)

    r = _deploy.run_smoke_audit("admin_bot", tmp_path)
    assert r.critical_findings == []
    assert r.warn_findings == []
    assert r.error is not None
    assert "CalledProcessError" in r.error


def test_run_smoke_audit_swallows_generic_exception(tmp_path, monkeypatch):
    from evolve_admin import deploy as _deploy

    def boom(*_a, **_kw):
        raise RuntimeError("audit blew up")

    fake_mod = mock.MagicMock()
    fake_mod.audit_oc_security = boom
    monkeypatch.setitem(sys.modules, "audit", fake_mod)

    r = _deploy.run_smoke_audit("admin_bot", tmp_path)
    assert r.error is not None
    assert "RuntimeError" in r.error
    assert "audit blew up" in r.error


def test_run_smoke_audit_handles_missing_audit_module(tmp_path, monkeypatch):
    """If the analyzer audit module isn't importable, the deploy must still
    finish — but the operator must see the failure."""
    from evolve_admin import deploy as _deploy

    # Force ImportError by stubbing the module with an object that breaks
    # `from audit import audit_oc_security`.
    class _Broken:
        def __getattr__(self, name):
            raise ImportError(f"forced for test: {name}")
    monkeypatch.setitem(sys.modules, "audit", _Broken())

    r = _deploy.run_smoke_audit("admin_bot", tmp_path)
    assert r.error is not None
    assert "audit module not importable" in r.error or "ImportError" in r.error


# ─────────────────────────────────────────────────────────────────────────────
# CLI exit gate
# ─────────────────────────────────────────────────────────────────────────────


def test_exit_gate_clean_is_zero_exit(monkeypatch, capsys):
    from evolve_admin import cli as _cli
    from evolve_admin.deploy import SmokeAuditResult

    # No criticals, no errors: function should return without sys.exit.
    _cli._exit_on_audit_criticals([("admin_bot", SmokeAuditResult())], allow_criticals=False)


def test_exit_gate_criticals_exits_nonzero(monkeypatch, capsys):
    from evolve_admin import cli as _cli
    from evolve_admin.deploy import SmokeAuditResult

    result = SmokeAuditResult(
        critical_findings=[_FakeFinding(level="critical", message="🔴 boom")],
    )
    with pytest.raises(SystemExit) as exc_info:
        _cli._exit_on_audit_criticals([("admin_bot", result)], allow_criticals=False)
    assert exc_info.value.code == 2

    out = capsys.readouterr().out
    assert "smoke audit" in out.lower()
    assert "1 critical" in out


def test_exit_gate_audit_run_error_exits_nonzero(monkeypatch, capsys):
    """Audit failure is also news — exit non-zero so CI/operators don't miss it."""
    from evolve_admin import cli as _cli
    from evolve_admin.deploy import SmokeAuditResult

    result = SmokeAuditResult(error="audit blew up: RuntimeError")
    with pytest.raises(SystemExit) as exc_info:
        _cli._exit_on_audit_criticals([("admin_bot", result)], allow_criticals=False)
    assert exc_info.value.code == 2

    out = capsys.readouterr().out
    assert "errored" in out


def test_exit_gate_allow_criticals_does_not_exit(monkeypatch, capsys):
    """`--allow-audit-criticals` lets the operator ship anyway — but a warning
    still prints so it's not invisible in scrollback."""
    from evolve_admin import cli as _cli
    from evolve_admin.deploy import SmokeAuditResult

    result = SmokeAuditResult(
        critical_findings=[_FakeFinding(level="critical", message="🔴 boom")],
    )
    _cli._exit_on_audit_criticals([("admin_bot", result)], allow_criticals=True)

    out = capsys.readouterr().out
    assert "overridden" in out.lower() or "allow" in out.lower()


def test_exit_gate_summarizes_across_bots(monkeypatch, capsys):
    """`--all` deploys mean multiple bots may fire criticals — the gate must
    surface them all, not just the first."""
    from evolve_admin import cli as _cli
    from evolve_admin.deploy import SmokeAuditResult

    results = [
        ("admin_bot", SmokeAuditResult(
            critical_findings=[_FakeFinding(level="critical", message="🔴 a")],
        )),
        ("team_bot_a", SmokeAuditResult()),  # clean
        ("admin_bot", SmokeAuditResult(error="audit blew up")),
    ]
    with pytest.raises(SystemExit):
        _cli._exit_on_audit_criticals(results, allow_criticals=False)

    out = capsys.readouterr().out
    assert "admin_bot" in out
    assert "admin_bot" in out


# ─────────────────────────────────────────────────────────────────────────────
# Renderer surfaces criticals prominently (the operator can't miss them)
# ─────────────────────────────────────────────────────────────────────────────


def test_render_smoke_audit_prints_each_critical(monkeypatch, capsys):
    """Each critical finding's message must appear in the deploy console output
    so the operator sees what fired without re-running the audit by hand."""
    from evolve_admin import cli as _cli
    from evolve_admin.deploy import SmokeAuditResult

    result = SmokeAuditResult(
        critical_findings=[
            _FakeFinding(level="critical", message="🔴 loopback_no_auth"),
            _FakeFinding(level="critical", message="🔴 trusted_proxies_missing"),
        ],
        warn_findings=[_FakeFinding(level="warn", message="minor drift")],
    )
    monkeypatch.setattr(_cli, "run_smoke_audit", lambda *_a, **_kw: result)

    _cli._run_post_deploy_smoke_audit("admin_bot", Path("/tmp/fake-shared"))

    out = capsys.readouterr().out
    assert "loopback_no_auth" in out
    assert "trusted_proxies_missing" in out
    assert "CRITICAL" in out
    # Warn count + each message rendered inline. The old pointer at
    # `evolve-admin audit run --bot <bot>` was a lie — that command
    # doesn't exist — so we print the warn message right here instead.
    assert "1 warn" in out
    assert "minor drift" in out


def test_render_smoke_audit_clean_message_present(monkeypatch, capsys):
    from evolve_admin import cli as _cli
    from evolve_admin.deploy import SmokeAuditResult

    monkeypatch.setattr(_cli, "run_smoke_audit", lambda *_a, **_kw: SmokeAuditResult())
    _cli._run_post_deploy_smoke_audit("team_bot_a", Path("/tmp/fake-shared"))

    out = capsys.readouterr().out
    assert "clean" in out.lower()
    assert "0 critical" in out


def test_render_smoke_audit_caps_long_warn_detail(monkeypatch, capsys):
    """security.trust_model.multi_user_heuristic and similar audit findings
    carry 8+ lines of detail. Inline-rendering all of it floods the deploy
    log on `deploy --all`. Cap to _AUDIT_DETAIL_LINES with a tail hint."""
    from evolve_admin import cli as _cli
    from evolve_admin.deploy import SmokeAuditResult

    long_detail = "\n".join(f"line {i}" for i in range(1, 9))  # 8 lines
    result = SmokeAuditResult(
        warn_findings=[_FakeFinding(
            level="warn",
            message="team_bot_a (security.trust_model.multi_user_heuristic): multi-user setup",
            detail=long_detail,
        )],
    )
    monkeypatch.setattr(_cli, "run_smoke_audit", lambda *_a, **_kw: result)
    _cli._run_post_deploy_smoke_audit("team_bot_a", Path("/tmp/fake-shared"))

    out = capsys.readouterr().out
    assert "line 1" in out, "first cap-window line must print"
    assert f"line {_cli._AUDIT_DETAIL_LINES}" in out, "last cap-window line must print"
    assert "line 8" not in out, "lines past the cap must be elided"
    assert f"{8 - _cli._AUDIT_DETAIL_LINES} more line(s) elided" in out


def test_render_smoke_audit_short_detail_uncapped(monkeypatch, capsys):
    """Detail shorter than the cap should render in full with no elision hint."""
    from evolve_admin import cli as _cli
    from evolve_admin.deploy import SmokeAuditResult

    short_detail = "one line of detail"
    result = SmokeAuditResult(
        warn_findings=[_FakeFinding(level="warn", message="x", detail=short_detail)],
    )
    monkeypatch.setattr(_cli, "run_smoke_audit", lambda *_a, **_kw: result)
    _cli._run_post_deploy_smoke_audit("team_bot_a", Path("/tmp/fake-shared"))

    out = capsys.readouterr().out
    assert "one line of detail" in out
    assert "elided" not in out


def test_render_smoke_audit_surfaces_run_error(monkeypatch, capsys):
    from evolve_admin import cli as _cli
    from evolve_admin.deploy import SmokeAuditResult

    monkeypatch.setattr(
        _cli, "run_smoke_audit",
        lambda *_a, **_kw: SmokeAuditResult(error="audit_oc_security raised RuntimeError: boom"),
    )
    _cli._run_post_deploy_smoke_audit("admin_bot", Path("/tmp/fake-shared"))

    out = capsys.readouterr().out
    assert "SMOKE AUDIT FAILED" in out or "FAILED TO RUN" in out
    assert "RuntimeError" in out
