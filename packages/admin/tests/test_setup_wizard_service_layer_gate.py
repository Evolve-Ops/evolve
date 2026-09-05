"""`evolve-admin setup` (no --fresh) must refuse a pod with no service layer.

Brief: internal/dispatch/done/adopt-wizard-cannot-stand-up-a-pod.md.

``wizard.run_wizard`` is a CONFIG wizard: it rewrites network.json and
re-deploys each member bot. It never creates the ``evolve`` service account
and never calls ``deploy.install_evolve_infra_jobs``, so on a machine that has
OpenClaw bots but no Evolve service layer it printed "Setup Complete" over a
pod with nothing running — and then pointed the operator at a port no server
was listening on.

Resolution C (re-scope): keep the established-pod flow exactly as it was, and
refuse the from-scratch case with the command that does the job. The gate is
tri-state and must fail toward DOING the work: an unreadable probe proceeds.

Every assertion here observes a real call — the wizard's own prompt function,
``deploy_bot``, ``save_network``, the scheduler seam — never a raising
sentinel planted inside the code under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evolve_admin import wizard
from evolve_admin.deploy import (
    DeployResult,
    _admin_ui_jobspec,
    _install_launchd_admin_ui,
)
from evolve_admin.wizard import PrereqResult
from runtime.isolation import FakeIsolation, get_isolation, set_isolation
from runtime.scheduler import FakeScheduler, get_scheduler, set_scheduler

_ADMIN_DIR = Path(__file__).resolve().parent.parent


class _ReachedPrompts(Exception):
    """Raised by the stubbed ``_ask`` — the gate let the wizard through."""


@pytest.fixture
def seams():
    """Fresh Fake isolation + scheduler seams, restored afterwards."""
    prev_iso, prev_sched = get_isolation(), get_scheduler()
    iso, sched = FakeIsolation(), FakeScheduler()
    set_isolation(iso)
    set_scheduler(sched)
    try:
        yield iso, sched
    finally:
        set_isolation(prev_iso)
        set_scheduler(prev_sched)


@pytest.fixture
def no_evolve_account(monkeypatch):
    """``pwd`` reports no ``evolve`` account (authoritative absent)."""
    import pwd as _pwd

    real = _pwd.getpwnam

    def fake(name):
        if name == wizard.EVOLVE_SERVICE_USER:
            raise KeyError(f"getpwnam(): name not found: {name!r}")
        return real(name)

    monkeypatch.setattr(_pwd, "getpwnam", fake)


@pytest.fixture
def evolve_account_present(monkeypatch, seams):
    """``pwd`` reports the ``evolve`` account exists."""
    import pwd as _pwd

    real = _pwd.getpwnam

    def fake(name):
        if name == wizard.EVOLVE_SERVICE_USER:
            return real("root")  # any real record — only "did it raise" is read
        return real(name)

    monkeypatch.setattr(_pwd, "getpwnam", fake)


def _wizard_reaches_prompts(monkeypatch, tmp_path: Path):
    """Arrange run_wizard so the first operator prompt raises _ReachedPrompts.

    Everything between the gate and that prompt is stubbed to the boring
    success path, so whether the exception escapes is a direct read on
    "did the gate let the wizard through?".
    """
    monkeypatch.setattr(
        wizard, "check_prerequisites",
        lambda: [PrereqResult("Running as root", True, "OK")],
    )
    monkeypatch.setattr(wizard, "find_oc_candidates", lambda network=None: [])

    def _boom(*a, **kw):
        raise _ReachedPrompts()

    monkeypatch.setattr(wizard, "_ask", _boom)
    return tmp_path / "network.json"


# ── the label the gate probes must be the label deploy actually installs ─────

def test_admin_ui_label_matches_what_deploy_installs(seams):
    """ADMIN_UI_LABEL is not a guess — install the real job and read it back.

    A drifting label would make the probe report "absent" on a healthy pod and
    refuse a working wizard, so pin it against the production install path.
    """
    _iso, sched = seams
    _install_launchd_admin_ui("evolve", DeployResult(bot_id="evolve", success=True))
    assert wizard.ADMIN_UI_LABEL in sched.jobs


# ── probe tri-state ──────────────────────────────────────────────────────────

def test_account_probe_reports_absent(seams, no_evolve_account):
    assert wizard._probe_evolve_account() is False


def test_account_probe_reports_present(evolve_account_present):
    assert wizard._probe_evolve_account() is True


def test_account_probe_unknown_when_pwd_lookup_fails(monkeypatch, seams):
    """A probe failure is not an answer — it must read as None, not absent."""
    import pwd as _pwd

    def explode(_name):
        raise OSError("directory service unavailable")

    monkeypatch.setattr(_pwd, "getpwnam", explode)
    assert wizard._probe_evolve_account() is None


def test_admin_ui_probe_reports_absent(seams):
    assert wizard._probe_admin_ui_unit() is False


def test_admin_ui_probe_reports_present(seams):
    _iso, sched = seams
    sched.seed_job(_admin_ui_jobspec(wizard.ADMIN_UI_LABEL))
    assert wizard._probe_admin_ui_unit() is True


def test_admin_ui_probe_unknown_when_status_errors(seams):
    """status_error means `managed: False` is UNKNOWN, not absent."""
    _iso, sched = seams
    sched.status_errors[wizard.ADMIN_UI_LABEL] = "cannot_escalate"
    assert wizard._probe_admin_ui_unit() is None


def test_admin_ui_probe_trusts_artifact_on_disk_despite_status_error(seams):
    """A unit file we can see is present even when the status probe failed."""
    _iso, sched = seams
    sched.seed_job(_admin_ui_jobspec(wizard.ADMIN_UI_LABEL))
    sched.status_errors[wizard.ADMIN_UI_LABEL] = "cannot_escalate"
    assert wizard._probe_admin_ui_unit() is True


def test_admin_ui_probe_unknown_when_scheduler_raises(seams, monkeypatch):
    _iso, sched = seams

    def explode(_label, **_kw):
        raise RuntimeError("launchctl missing")

    monkeypatch.setattr(sched, "status", explode)
    assert wizard._probe_admin_ui_unit() is None


# ── the refusal ──────────────────────────────────────────────────────────────

def test_run_wizard_refuses_when_pod_has_no_service_layer(
    monkeypatch, tmp_path, capsys, seams, no_evolve_account,
):
    """No evolve account + no admin-ui unit ⇒ exit 1 before doing ANY work.

    The spies are the point: deploy_bot and save_network are the two things a
    completed non-fresh run performs, and the prompt stub is what a run that
    got past the gate would hit first. None of them may fire.
    """
    net_path = _wizard_reaches_prompts(monkeypatch, tmp_path)
    deployed: list = []
    saved: list = []
    monkeypatch.setattr(
        wizard, "deploy_bot", lambda *a, **kw: deployed.append(a) or DeployResult(
            bot_id="x", success=True,
        ),
    )
    monkeypatch.setattr(wizard, "save_network", lambda *a, **kw: saved.append(a))

    with pytest.raises(SystemExit) as exc:
        wizard.run_wizard(net_path)

    assert exc.value.code == 1
    assert deployed == []
    assert saved == []
    assert not net_path.exists()

    out = capsys.readouterr().out
    assert "sudo evolve-admin setup --fresh" in out
    # A Linux operator must never be told to run `setup evolve-user` — that
    # subcommand is macOS-only by design and exits telling them to use --fresh.
    assert "setup evolve-user" not in out


def test_refusal_is_silent_about_ports(seams, no_evolve_account, capsys):
    """The refusal replaces the banner that sent operators at a dead port."""
    with pytest.raises(SystemExit):
        wizard._refuse_without_service_layer()
    assert "19099" not in capsys.readouterr().out


# ── everything else proceeds (fail toward doing the work) ────────────────────

def test_run_wizard_proceeds_when_admin_ui_unit_present(
    monkeypatch, tmp_path, seams, no_evolve_account,
):
    """An established pod's wizard is untouched — reaching the prompts proves it."""
    _iso, sched = seams
    sched.seed_job(_admin_ui_jobspec(wizard.ADMIN_UI_LABEL))
    net_path = _wizard_reaches_prompts(monkeypatch, tmp_path)

    with pytest.raises(_ReachedPrompts):
        wizard.run_wizard(net_path)


def test_run_wizard_proceeds_when_account_present(
    monkeypatch, tmp_path, seams, evolve_account_present,
):
    net_path = _wizard_reaches_prompts(monkeypatch, tmp_path)

    with pytest.raises(_ReachedPrompts):
        wizard.run_wizard(net_path)


def test_run_wizard_proceeds_when_probes_are_unreadable(
    monkeypatch, tmp_path, seams, no_evolve_account,
):
    """An unlucky permission must not become a wizard that refuses to run.

    Account authoritatively absent, admin-ui probe unreadable ⇒ proceed.
    """
    _iso, sched = seams
    sched.status_errors[wizard.ADMIN_UI_LABEL] = "cannot_escalate"
    net_path = _wizard_reaches_prompts(monkeypatch, tmp_path)

    with pytest.raises(_ReachedPrompts):
        wizard.run_wizard(net_path)


def test_partial_pod_warns_but_does_not_refuse(
    seams, evolve_account_present, capsys,
):
    """Account without daemons: name the repair, then get out of the way."""
    wizard._refuse_without_service_layer()  # must NOT raise SystemExit
    out = capsys.readouterr().out
    assert "sudo evolve-admin install-infra-jobs" in out


# ── the banner ───────────────────────────────────────────────────────────────

def test_wizard_names_no_gateway_port_as_the_admin_ui():
    """No operator-facing string may point at the provisioning-range port.

    The summary resolves the admin URL through the one helper every other
    operator-facing surface uses, so it can never contradict the 5050 daemon.
    """
    src = (_ADMIN_DIR / "evolve_admin" / "wizard.py").read_text()
    assert "19099" not in src
    assert "resolve_admin_base_url(network_data)" in src
