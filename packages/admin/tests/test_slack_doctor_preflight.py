"""tests/test_slack_doctor_preflight.py — cli rendering of slack-doctor preflight.

These cover ``_run_slack_doctor_preflight`` — the presentation layer that
runs slack-doctor during deploy and prints findings to the console. The
doctor itself is exercised by ``test_slack_doctor.py``; here we only
care about which findings make it onto the screen.

The case that motivated this file: PR #1697 made the doctor short-circuit
right after SLK012 (info, "slack administratively disabled") so admin_bot's
Telegram-only deploys stopped emitting SLK001/SLK007 FAILs. But the
preflight only printed fails+warns, so SLK012 was silently dropped — the
dead slack block in openclaw.json kept sitting there with nothing in the
deploy log to point at it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))


def _stub_run_doctor(monkeypatch, tmp_path, fake_result):
    """Stub _bot_home + run_doctor so the preflight reaches the renderer."""
    from evolve_admin import cli as _cli
    from evolve_admin.integrations.slack import doctor as _doctor

    monkeypatch.setattr(_cli, "_bot_home", lambda _b, _n: tmp_path)
    monkeypatch.setattr(_doctor, "run_doctor", lambda *_a, **_kw: fake_result)


def test_preflight_surfaces_info_findings(monkeypatch, tmp_path, capsys):
    """SLK012 (info) must reach the deploy output — otherwise the
    operator can't see the dead-config notice post-#1697."""
    from evolve_admin import cli as _cli
    from evolve_admin.integrations.slack.doctor import DoctorResult, Finding

    _stub_run_doctor(monkeypatch, tmp_path, DoctorResult(
        bot_id="admin_bot",
        findings=[Finding(
            code="SLK012", severity="info",
            title="admin_bot: Slack integration is administratively disabled",
            detail="channels.slack.enabled is false…",
        )],
    ))

    _cli._run_slack_doctor_preflight("admin_bot", {"sharedDir": str(tmp_path)})
    out = capsys.readouterr().out
    assert "slack-doctor pre-flight" in out
    assert "SLK012" in out
    assert "administratively disabled" in out


def test_preflight_silent_when_no_findings(monkeypatch, tmp_path, capsys):
    """A clean bot produces no preflight output — operators shouldn't
    see a header for an empty section."""
    from evolve_admin import cli as _cli
    from evolve_admin.integrations.slack.doctor import DoctorResult

    _stub_run_doctor(monkeypatch, tmp_path, DoctorResult(bot_id="team_bot_a", findings=[]))

    _cli._run_slack_doctor_preflight("team_bot_a", {"sharedDir": str(tmp_path)})
    out = capsys.readouterr().out
    assert "slack-doctor pre-flight" not in out


def test_preflight_info_only_does_not_exit(monkeypatch, tmp_path, capsys):
    """Info findings must not trigger the FAIL gate sys.exit. Only
    fails-with-policy block the deploy."""
    from evolve_admin import cli as _cli
    from evolve_admin.integrations.slack.doctor import DoctorResult, Finding

    _stub_run_doctor(monkeypatch, tmp_path, DoctorResult(
        bot_id="admin_bot",
        findings=[Finding(
            code="SLK012", severity="info", title="disabled", detail="",
        )],
    ))

    # Must return None without raising SystemExit.
    assert _cli._run_slack_doctor_preflight(
        "admin_bot", {"sharedDir": str(tmp_path)},
    ) is None


def test_preflight_renders_fails_warns_and_infos_together(
    monkeypatch, tmp_path, capsys,
):
    """All three severities show in stable order: fails → warns → infos."""
    from evolve_admin import cli as _cli
    from evolve_admin.integrations.slack.doctor import DoctorResult, Finding

    _stub_run_doctor(monkeypatch, tmp_path, DoctorResult(
        bot_id="team_bot_a",
        findings=[
            Finding(code="SLK001", severity="fail", title="channel name not ID", detail=""),
            Finding(code="SLK010", severity="warn", title="empty allowlist", detail=""),
            Finding(code="SLK012", severity="info", title="disabled", detail=""),
        ],
    ))

    # Stub the policy lookup so the FAIL gate doesn't try to read disk.
    from evolve_admin.integrations.slack import policy as _policy
    monkeypatch.setattr(_policy, "policy_path", lambda *_a, **_kw: tmp_path / "missing")

    _cli._run_slack_doctor_preflight("team_bot_a", {"sharedDir": str(tmp_path)})
    out = capsys.readouterr().out

    for code in ("SLK001", "SLK010", "SLK012"):
        assert code in out, f"{code} missing from preflight output"
    # Stable ordering: fail before warn before info.
    assert out.index("SLK001") < out.index("SLK010") < out.index("SLK012")
