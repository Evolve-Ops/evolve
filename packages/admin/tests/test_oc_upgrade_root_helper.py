"""The report-scoped root helper — `oc_upgrade_apply.py --report-id <id>`.

Spec: internal/spec-oc-upgrade-from-ui-2026-07-28.md §4 + verification items
**H1–H3**.

The helper is the whole reason there is no npm sudoers grant. sudo's `*` is
fnmatch-style and matches npm's alias syntax, so
``openclaw@npm:some-other-package`` would satisfy an `openclaw@*` grant and run
an arbitrary package's postinstall as root. Granting one interpreter running
one script moves the decision inside the script — which means the script has to
earn that trust:

* **H1** it rejects a malformed report id *before touching the filesystem*
  (``--report-id *`` still admits an arbitrary string, including a traversal).
* **H2** it re-derives freshness itself and refuses when the report no longer
  describes the pod — it does not trust that the caller checked.
* **H3** it installs the version named in the report, not whatever ``latest``
  resolves to at install time. That is also the race the CLI has today, where
  the preflight and the npm install each resolve ``latest`` independently.

Plus the unprivileged half's contract: a missing sudoers grant is reported as
a missing grant (sudo exits 1 for that, the same code a real failure uses), and
the unattended npm temp-dir cleanup refuses the one case the CLI resolves with
an operator confirm rather than guessing.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import config as ecfg  # noqa: E402
from evolve_admin import oc_upgrade_apply as m  # noqa: E402
from evolve_admin import ocadmin  # noqa: E402
from evolve_admin import safe_upgrade as su  # noqa: E402

INSTALLED = "2026.7.0"
TARGET = "2026.7.1"
REPORT_ID = "20260728T120000Z-abcd1234"

NETWORK = {"sharedDir": "/Users/Shared/evolve", "bots": {}, "members": []}


def _report(**over) -> dict:
    base = {
        "report_id": REPORT_ID,
        "ok": True,
        "current": {"installed_version": INSTALLED},
        "candidate": {"resolved_version": TARGET},
        "gates": {},
        "requirements": [],
    }
    base.update(over)
    return base


def _events(capsys) -> list[dict]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


@pytest.fixture
def wired(monkeypatch):
    """Every leaf the helper reaches, stubbed. ``seen`` records what it did."""
    seen: dict = {"loaded_reports": [], "networks": 0, "run_upgrade": []}

    def _load_network(_path):
        seen["networks"] += 1
        return NETWORK

    def _load_report(report_id, shared_dir=None):
        seen["loaded_reports"].append(report_id)
        return _report()

    monkeypatch.setattr(ecfg, "load_network", _load_network)
    monkeypatch.setattr(su, "load_report", _load_report)
    monkeypatch.setattr(ocadmin, "_installed_version", lambda: INSTALLED)
    monkeypatch.setattr(ocadmin, "_latest_version", lambda: TARGET)

    def _run_upgrade(**kwargs):
        seen["run_upgrade"].append(kwargs)
        return m.UpgradeOutcome(ok=True, exit_code=0,
                                installed_before=INSTALLED, installed_after=TARGET)

    monkeypatch.setattr(m, "run_upgrade", _run_upgrade)
    return seen


# ── H1: malformed report id ──────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [
    "../../../etc/passwd",
    "20260728T120000Z-abcd1234/../../evil",
    "*",
    "latest",
    "20260728T120000Z-ABCD1234",       # uppercase hex — not the minted shape
    "20260728T120000Z-abcd123",        # 7 hex chars
    "20260728T120000-abcd1234",        # missing the Z
    "",
    "20260728T120000Z-abcd1234\n../x",
])
def test_h1_malformed_report_id_is_refused_without_touching_the_filesystem(
    bad, monkeypatch, capsys,
):
    def _boom(*a, **k):
        raise AssertionError("the helper touched the filesystem before validating")

    monkeypatch.setattr(ecfg, "load_network", _boom)
    monkeypatch.setattr(su, "load_report", _boom)
    monkeypatch.setattr(su, "reports_dir", _boom)

    assert m.main(["--report-id", bad]) == m.EXIT_REFUSED
    events = _events(capsys)
    assert events and "malformed report id" in events[-1]["message"]


def test_h1_bad_usage_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(ecfg, "load_network",
                        lambda *a, **k: pytest.fail("must not load"))
    for argv in ([], ["--report-id"], ["--target", "latest"],
                 ["--report-id", REPORT_ID, "--force"]):
        assert m.main(argv) == m.EXIT_REFUSED
    assert "usage:" in _events(capsys)[0]["message"]


def test_h1_well_formed_id_is_accepted(wired, capsys):
    assert m.main(["--report-id", REPORT_ID]) == m.EXIT_OK
    assert wired["loaded_reports"] == [REPORT_ID]


# ── H2: freshness re-derived, not trusted ────────────────────────────────────


def test_h2_refuses_when_the_installed_version_moved(wired, monkeypatch, capsys):
    monkeypatch.setattr(ocadmin, "_installed_version", lambda: "2026.7.0.5")
    assert m.main(["--report-id", REPORT_ID]) == m.EXIT_REFUSED
    assert "installed OpenClaw version moved" in _events(capsys)[-1]["message"]
    assert wired["run_upgrade"] == []


def test_h2_refuses_when_npm_latest_moved(wired, monkeypatch, capsys):
    monkeypatch.setattr(ocadmin, "_latest_version", lambda: "2026.7.2")
    assert m.main(["--report-id", REPORT_ID]) == m.EXIT_REFUSED
    assert "npm latest moved" in _events(capsys)[-1]["message"]
    assert wired["run_upgrade"] == []


def test_h2_fails_closed_when_the_registry_is_unreachable(wired, monkeypatch, capsys):
    """Cannot PROVE the gates still describe reality → refuse."""
    monkeypatch.setattr(ocadmin, "_latest_version",
                        lambda: "(error: getaddrinfo ENOTFOUND)")
    assert m.main(["--report-id", REPORT_ID]) == m.EXIT_REFUSED
    assert "could not re-check the npm registry" in _events(capsys)[-1]["message"]
    assert wired["run_upgrade"] == []


def test_h2_refuses_a_red_report_even_though_the_endpoint_already_checked(
    wired, monkeypatch, capsys,
):
    """The helper does not trust that the caller checked (spec §4.2)."""
    monkeypatch.setattr(su, "load_report",
                        lambda rid, shared_dir=None: _report(ok=False))
    assert m.main(["--report-id", REPORT_ID]) == m.EXIT_REFUSED
    assert "did not pass its gates" in _events(capsys)[-1]["message"]
    assert wired["run_upgrade"] == []


def test_h2_refuses_a_missing_report(wired, monkeypatch, capsys):
    monkeypatch.setattr(su, "load_report", lambda rid, shared_dir=None: None)
    assert m.main(["--report-id", REPORT_ID]) == m.EXIT_REFUSED
    assert "no safety report" in _events(capsys)[-1]["message"]
    assert wired["run_upgrade"] == []


def test_h2_refuses_a_report_with_no_resolved_candidate(wired, monkeypatch, capsys):
    monkeypatch.setattr(
        su, "load_report",
        lambda rid, shared_dir=None: _report(candidate={"resolved_version": None}))
    assert m.main(["--report-id", REPORT_ID]) == m.EXIT_REFUSED
    assert "never resolved a candidate version" in _events(capsys)[-1]["message"]
    assert wired["run_upgrade"] == []


# ── H3: the report names the version, not `latest` ───────────────────────────


def test_h3_target_version_comes_from_the_report(wired):
    assert m.main(["--report-id", REPORT_ID]) == m.EXIT_OK
    kwargs = wired["run_upgrade"][0]
    assert kwargs["target_version"] == TARGET
    assert kwargs["interactive"] is False
    # The operator confirmed in the UI; the helper never blocks on a prompt.
    assert kwargs["confirm"]("  Proceed?", False) is True


def test_h3_npm_argv_pins_the_report_version_and_never_refetches_latest(monkeypatch):
    """End to end through run_upgrade: with target_version pinned, the registry
    is never consulted and the install argv names exactly that version."""
    monkeypatch.setattr(ocadmin, "_latest_version",
                        lambda: pytest.fail("the pinned path must not resolve latest"))
    monkeypatch.setattr(ocadmin, "_installed_version",
                        _versions(INSTALLED, TARGET))
    monkeypatch.setattr(su, "run_preflight",
                        lambda **kw: _PreflightReport(kw.get("target_spec")))
    monkeypatch.setattr(m, "clean_stale_npm_temp_dirs_unattended", lambda _run: True)
    monkeypatch.setattr(ocadmin, "_remove_conflicting_user_agents", lambda _n: None)
    monkeypatch.setattr(ocadmin, "_verify_send_surface_post_upgrade",
                        lambda *a, **k: None)
    monkeypatch.setattr(ocadmin, "_verify_oc_dependencies_post_upgrade",
                        lambda *a, **k: None)
    monkeypatch.setattr(ocadmin, "_emit_runtime_notes_review_signal",
                        lambda *a, **k: None)

    argvs: list[list[str]] = []
    real_run = subprocess.run

    def _fake_run(cmd, **kw):
        if cmd and cmd[0] == "npm":
            argvs.append(list(cmd))

            class _R:
                returncode = 0
                stdout = ""
                stderr = ""
            return _R()
        return real_run(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    events: list[m.StepEvent] = []
    outcome = m.run_upgrade(
        network=NETWORK, emit=events.append, confirm=lambda p, d: True,
        target_version=TARGET, interactive=False,
    )
    assert outcome.ok and outcome.installed_after == TARGET
    assert argvs == [[
        "npm", "install", "-g",
        f"--prefix={ocadmin.OPENCLAW_NPM_PREFIX}", f"openclaw@{TARGET}",
    ]]
    # The gates ran against the pinned version too, not against "latest".
    assert any(f"Target:     {TARGET}" in e.message for e in events)


def _versions(before, after):
    state = {"n": 0}

    def _read():
        state["n"] += 1
        return before if state["n"] == 1 else after
    return _read


class _PreflightReport:
    def __init__(self, target_spec):
        assert target_spec == TARGET, f"gates ran against {target_spec!r}, not the report's version"
        self.ok = True
        self.requirements = []
        self.gates = {}


# ── Step events ──────────────────────────────────────────────────────────────


def test_events_carry_phase_step_total_level_message():
    ev = m.StepEvent(phase="npm_install", step=8, total=m.TOTAL_PHASES,
                     level=m.LEVEL_SUCCESS, message="  [green]✅ Installed: 1.2.3[/]")
    payload = ev.to_json()
    assert payload["phase"] == "npm_install"
    assert payload["step"] == 8 and payload["total"] == m.TOTAL_PHASES
    assert payload["level"] == "success"
    # Rich markup is stripped for the JSON/job-log consumers; the raw event
    # keeps it so the CLI renderer stays byte-identical.
    assert payload["message"] == "  ✅ Installed: 1.2.3"
    assert payload["label"] == m.PHASE_LABELS["npm_install"]


def test_plain_text_leaves_genuine_brackets_alone():
    """`    1. [config_references] …` is a gate name, not markup."""
    assert m.plain_text("    1. [config_references] plugin missing") == \
        "    1. [config_references] plugin missing"


# ── No `sudo …` hint survives into the SPA ───────────────────────────────────
#
# The failure tails are the CLI's verbatim (that's what makes the terminal
# output byte-identical) and several hand the operator a `sudo …` command. In a
# terminal that's right; in the admin SPA it's the dead end this whole change
# removes — the in-app Terminal runs as the passwordless `evolve` user, so a
# pasted `sudo` prompts for a password nobody can enter. The RENDERING fixes
# it, not the message.

_SUDO_HINT_RE = re.compile(
    r"(?<![\w-])sudo\s+(?:-\w+\s+)*"
    r"(?:/\S+|evolve-admin|openclaw|npm|launchctl|systemctl|rm|cp|mv|chmod|chown)\b"
)


@pytest.mark.parametrize("cli_message", [
    "  Try running the command directly:\n    sudo npm install -g --prefix=/opt/homebrew openclaw@2026.7.1",
    "\n  [yellow]→[/] Found stale npm temp dir at /x/.openclaw-abc.\n"
    "     Remove it and retry:\n"
    "       sudo rm -rf /x/.openclaw-abc\n"
    "       sudo evolve-admin menu upgrade",
    "       [yellow]→[/] Roll back: sudo npm install -g --prefix=/opt/homebrew openclaw@2026.7.0",
    "       [yellow]→[/] Run `sudo evolve-admin menu safe-upgrade` directly to see the full traceback.",
    "\n  [yellow]⚠️  --no-restart: repaired CLI device scopes on a take effect only after a "
    "gateway restart (sudo launchctl kickstart -k system/ai.openclaw.<bot>-gateway).[/]",
])
def test_web_rendering_strips_sudo_command_hints(cli_message):
    ev = m.StepEvent(phase="npm_install", step=8, total=m.TOTAL_PHASES,
                     level=m.LEVEL_ERROR, message=cli_message)
    rendered = ev.to_json()["message"]
    assert not _SUDO_HINT_RE.search(rendered), rendered
    # The command itself survives — the operator still gets it, they're just
    # told where it has to run.
    assert "run them from a host terminal" in rendered
    # And the CLI's own bytes are untouched: the event still carries them.
    assert ev.message == cli_message


def test_web_rendering_leaves_non_command_uses_of_the_word_alone():
    """"sudoers" and a parenthetical "(no sudo)" are prose, not a hint."""
    for text in ("/etc/sudoers.d/evolve has no grant for it",
                 "runs the release promote in-process (no sudo)"):
        ev = m.StepEvent(phase="preflight", step=2, total=m.TOTAL_PHASES,
                         level=m.LEVEL_INFO, message=text)
        assert ev.to_json()["message"] == text


def test_web_rendering_is_a_noop_for_ordinary_lines():
    ev = m.StepEvent(phase="npm_install", step=8, total=m.TOTAL_PHASES,
                     level=m.LEVEL_SUCCESS, message="  [green]✅ Installed: 1.2.3[/]")
    assert ev.to_json()["message"] == "  ✅ Installed: 1.2.3"


# ── The unprivileged invoker ─────────────────────────────────────────────────


def test_missing_helper_script_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(m, "helper_script_path", lambda: "/nonexistent/helper.py")
    ok, detail = m.stream_privileged_upgrade(REPORT_ID, lambda e: None)
    assert ok is False
    assert "not present" in detail


def test_sudo_denial_is_reported_as_a_missing_grant():
    """`sudo -n` exits 1 for "a password is required" — the same code a genuine
    upgrade failure uses — so the missing-grant case is keyed on the message."""
    assert m.is_sudo_denial("sudo: a password is required")
    assert m.is_sudo_denial("sudo: a terminal is required to read the password")
    assert m.is_sudo_denial(
        "sudo: user evolve is not allowed to execute '/x/python3 ...' as root")
    assert not m.is_sudo_denial("  ✅ Installed: 2026.7.1")
    assert not m.is_sudo_denial('{"message": "sudo: something"}')


def test_no_grant_detail_names_the_manual_step_without_a_sudo_hint():
    """Refreshing sudoers is manual by design (spec §10) — the endpoint should
    SAY the grant is missing rather than fail opaquely, and must not hand the
    SPA a `sudo …` command it cannot run."""
    detail = m.NO_GRANT_DETAIL
    assert "refresh-sudoers" in detail
    assert "not authorized" in detail
    assert "sudo " not in detail


def test_helper_path_matches_the_sudoers_grant_shape():
    path = m.helper_script_path()
    assert path.endswith("/packages/admin/evolve_admin/oc_upgrade_apply.py")
    assert Path(path).is_absolute()


# ── Unattended npm temp-dir cleanup (§4.3) ───────────────────────────────────


def _run_sink() -> tuple[m._Run, list[m.StepEvent]]:
    events: list[m.StepEvent] = []
    return m._Run(events.append, lambda p, d: True), events


def test_unattended_cleanup_removes_residue(tmp_path, monkeypatch):
    nm = tmp_path / "node_modules"
    (nm / "openclaw").mkdir(parents=True)
    (nm / "openclaw" / "package.json").write_text('{"version": "2026.7.0"}')
    stale = nm / ".openclaw-deadbeef"
    stale.mkdir()
    (stale / "junk").write_text("x")
    monkeypatch.setattr(ocadmin, "_npm_node_modules_dir", lambda: nm)

    run, events = _run_sink()
    assert m.clean_stale_npm_temp_dirs_unattended(run) is True
    assert not stale.exists()
    assert any("Removed .openclaw-deadbeef" in e.message for e in events)


def test_unattended_cleanup_refuses_to_guess_a_promote(tmp_path, monkeypatch):
    """The one case the CLI resolves with an operator confirm: the live install
    is a husk while a COMPLETE staged sibling holds the only resolvable copy.
    An unattended run declines and aborts BEFORE the install, leaving the pod
    exactly as it was."""
    nm = tmp_path / "node_modules"
    (nm / "openclaw").mkdir(parents=True)          # husk: no package.json
    staged = nm / ".openclaw-good"
    (staged / "dist").mkdir(parents=True)
    (staged / "package.json").write_text('{"version": "2026.7.1"}')
    (staged / "openclaw.mjs").write_text("//")
    (staged / "dist" / "index.js").write_text("//")
    monkeypatch.setattr(ocadmin, "_npm_node_modules_dir", lambda: nm)

    run, events = _run_sink()
    assert m.clean_stale_npm_temp_dirs_unattended(run) is False
    assert staged.exists()                          # never destroyed
    assert any("will not guess" in e.message for e in events)


def test_unattended_cleanup_is_a_noop_with_no_residue(tmp_path, monkeypatch):
    nm = tmp_path / "node_modules"
    (nm / "openclaw").mkdir(parents=True)
    (nm / "openclaw" / "package.json").write_text('{"version": "2026.7.0"}')
    monkeypatch.setattr(ocadmin, "_npm_node_modules_dir", lambda: nm)

    run, events = _run_sink()
    assert m.clean_stale_npm_temp_dirs_unattended(run) is True
    assert events == []
