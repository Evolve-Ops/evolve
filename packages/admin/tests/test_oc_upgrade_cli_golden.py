"""Golden-output test — `evolve-admin menu upgrade` is byte-identical after
the headless extraction.

Spec: docs/spec-oc-upgrade-from-ui-2026-07-28.md §5 + verification item **C1**.

``oc_upgrade``'s ~255-line body moved to ``evolve_admin.oc_upgrade_apply`` so
the admin UI's "Run upgrade now" button and the root helper can run the same
sequence headless. The click command is now a renderer over that module's
:class:`StepEvent` stream. The regression bar the spec sets is that the
*terminal* output does not move: "The CLI must come out byte-identical in its
output; that is the regression bar."

The fixtures under ``fixtures/oc_upgrade_cli_golden/`` were captured from the
**pre-extraction** command (origin/main at 3cb4417) driving the scenarios
below, so equality here proves the extraction changed zero bytes of operator-
visible output. Regenerating one is a conscious act: an intentional wording
change must move the fixture in the same PR and say why.

Scenario coverage — every terminal branch of the body plus the full happy path:

* ``already_latest``   — installed == latest, no --force
* ``registry_error``   — the npm registry is unreachable
* ``preflight_raised`` — run_preflight itself blew up
* ``blockers``         — the preflight reports blocking requirements
* ``cancelled``        — gates pass, operator answers "no" at Proceed?
* ``dance_dry_run``    — --neutralize-externalized --dry-run (the plan preview)
* ``happy_path``       — the whole sequence, install through verification
* ``npm_failed``       — npm install exits non-zero (ENOTEMPTY hint path)
* ``install_did_not_move`` — npm claims success, package.json still stale
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import ocadmin  # noqa: E402

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "oc_upgrade_cli_golden"

NETWORK = {
    "sharedDir": "/Users/Shared/evolve",
    "bots": {"team_bot_a": {"user": "team_bot_a"}, "team_bot_b": {"user": "team_bot_b"}},
    "members": ["team_bot_a", "team_bot_b"],
}


# ── Report doubles ───────────────────────────────────────────────────────────


class _Gate:
    def __init__(self, ok: bool, details: dict | None = None) -> None:
        self.ok = ok
        self.details = details or {}


class _Req:
    def __init__(self, gate: str, summary: str, remediation: str, blocking: bool) -> None:
        self.source_gate = gate
        self.summary = summary
        self.remediation = remediation
        self.blocking = blocking


class _Report:
    def __init__(self, ok: bool, requirements=None, gates=None) -> None:
        self.ok = ok
        self.requirements = requirements or []
        self.gates = gates or {}


def _clean_report() -> _Report:
    return _Report(ok=True, gates={"config_references": _Gate(True, {"bots": []})})


def _blocked_report() -> _Report:
    return _Report(
        ok=False,
        requirements=[
            _Req("config_references", "brave plugin missing from candidate",
                 "Install @openclaw/brave-plugin on team_bot_a first.", True),
            _Req("node_version", "node 18 < engines.node >=20",
                 "Upgrade node before openclaw.", True),
            _Req("plist_paths", "advisory only", "nothing to do", False),
        ],
        gates={"config_references": _Gate(False, {"bots": []})},
    )


def _dance_report() -> _Report:
    """Preflight whose only blockers are externalized plugins — the shape that
    makes --neutralize-externalized the prescribed workaround."""
    return _Report(
        ok=False,
        requirements=[
            _Req("config_references", "brave plugin missing from candidate",
                 "Install @openclaw/brave-plugin on team_bot_a first.", True),
        ],
        gates={
            "config_references": _Gate(False, {
                "missing_by_bot": {"team_bot_a": ["brave"]},
                "bots": [{"bot_id": "team_bot_b", "phantom_installs": ["discord"]}],
            }),
        },
    )


# ── Harness ──────────────────────────────────────────────────────────────────


class _Outcome:
    def __init__(self, changed=False, ok=True, detail="") -> None:
        self.changed = changed
        self.ok = ok
        self.detail = detail


def _run_cli(monkeypatch, *, args, installed, latest, preflight, confirm=True,
             npm_rc=0, npm_stderr="", npm_stdout="", installed_after=None):
    """Drive `evolve-admin menu upgrade` with every leaf stubbed, capturing the
    Rich console into a StringIO. Returns (captured_text, exit_code, calls)."""
    buf = io.StringIO()
    # no_color + fixed width so the fixture is a stable, diffable plain-text
    # transcript rather than an ANSI blob whose wrapping tracks the terminal.
    monkeypatch.setattr(
        ocadmin, "console",
        Console(file=buf, width=100, no_color=True, highlight=False, soft_wrap=False),
    )

    calls: dict = {"confirm": [], "npm": [], "restarts": [], "signals": []}

    monkeypatch.setattr(ocadmin, "_load_network_safe", lambda _p: NETWORK)
    monkeypatch.setattr(ocadmin, "_installed_version",
                        _version_sequence(installed, installed_after))
    monkeypatch.setattr(ocadmin, "_latest_version", lambda: latest)

    from evolve_admin import safe_upgrade as _su

    def _preflight(**_kw):
        if isinstance(preflight, Exception):
            raise preflight
        return preflight

    monkeypatch.setattr(_su, "run_preflight", _preflight)

    def _confirm(prompt, **kw):
        calls["confirm"].append((prompt, kw))
        return confirm

    monkeypatch.setattr(ocadmin.click, "confirm", _confirm)
    monkeypatch.setattr(ocadmin, "_check_and_clean_stale_npm_temp_dirs", lambda: True)
    monkeypatch.setattr(ocadmin, "_remove_conflicting_user_agents",
                        lambda _n: ocadmin.console.print("  (no conflicting agents)"))
    monkeypatch.setattr(ocadmin, "_restart_gateway",
                        lambda b: calls["restarts"].append(b))
    monkeypatch.setattr(ocadmin, "_verify_send_surface_post_upgrade",
                        lambda *a, **k: ocadmin.console.print("  (send-surface stub)"))
    monkeypatch.setattr(ocadmin, "_verify_oc_dependencies_post_upgrade",
                        lambda *a, **k: ocadmin.console.print("  (oc-deps stub)"))
    monkeypatch.setattr(ocadmin, "_emit_runtime_notes_review_signal",
                        lambda *a, **k: calls["signals"].append(a))

    from evolve_admin import oc_cli_device
    monkeypatch.setattr(oc_cli_device, "ensure_cli_device_scopes",
                        lambda *a, **k: _Outcome())

    real_run = subprocess.run

    def _fake_run(cmd, **kwargs):
        if cmd and cmd[0] in ("npm",):
            calls["npm"].append(list(cmd))

            class _R:
                returncode = 0 if cmd[1:2] == ["root"] else npm_rc
                stdout = "/opt/homebrew/lib/node_modules" if cmd[1:2] == ["root"] else npm_stdout
                stderr = "" if cmd[1:2] == ["root"] else npm_stderr
            return _R()
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = CliRunner().invoke(
        ocadmin.menu_group, ["upgrade", *args], obj={"network_path": Path("/dev/null")},
    )
    return buf.getvalue(), result.exit_code, calls


def _version_sequence(first, second):
    """`_installed_version` is read twice — before the install and after it."""
    state = {"n": 0}

    def _read():
        state["n"] += 1
        if state["n"] == 1 or second is None:
            return first
        return second
    return _read


SCENARIOS = {
    "already_latest": dict(
        args=[], installed="2026.7.1", latest="2026.7.1", preflight=_clean_report(),
    ),
    "registry_error": dict(
        args=[], installed="2026.7.0", latest="(error: getaddrinfo ENOTFOUND)",
        preflight=_clean_report(),
    ),
    "preflight_raised": dict(
        args=[], installed="2026.7.0", latest="2026.7.1",
        preflight=RuntimeError("gate exploded"),
    ),
    "blockers": dict(
        args=[], installed="2026.7.0", latest="2026.7.1", preflight=_blocked_report(),
    ),
    "cancelled": dict(
        args=[], installed="2026.7.0", latest="2026.7.1", preflight=_clean_report(),
        confirm=False,
    ),
    "dance_dry_run": dict(
        args=["--neutralize-externalized", "--dry-run"],
        installed="2026.7.0", latest="2026.7.1", preflight=_dance_report(),
    ),
    "happy_path": dict(
        args=[], installed="2026.7.0", latest="2026.7.1", preflight=_clean_report(),
        installed_after="2026.7.1",
    ),
    "npm_failed": dict(
        args=[], installed="2026.7.0", latest="2026.7.1", preflight=_clean_report(),
        npm_rc=1,
        npm_stderr=(
            "npm error code ENOTEMPTY\n"
            "npm error dest /opt/homebrew/lib/node_modules/.openclaw-2N5mgx4q\n"
        ),
    ),
    "install_did_not_move": dict(
        args=[], installed="2026.7.0", latest="2026.7.1", preflight=_clean_report(),
        installed_after="2026.7.0", npm_stdout="added 1 package",
    ),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_cli_output_matches_pre_extraction_golden(name, monkeypatch):
    out, _rc, _calls = _run_cli(monkeypatch, **SCENARIOS[name])
    # Capture mode — used ONCE, with origin/main's pre-extraction ocadmin.py
    # checked out, to mint the fixtures. Never set in CI.
    if os.environ.get("EVOLVE_REGEN_OC_UPGRADE_GOLDEN") == "1":
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        (GOLDEN_DIR / f"{name}.txt").write_text(out)
        return
    golden = (GOLDEN_DIR / f"{name}.txt").read_text()
    assert out == golden, (
        f"`evolve-admin menu upgrade` output drifted for scenario {name!r}. "
        "The extraction's contract (spec §5) is byte-identical CLI output; if "
        "the change is intentional, regenerate the fixture in the same PR."
    )


def test_exit_codes_preserved(monkeypatch):
    """The command's exit codes are part of its contract — 1 on every refusal,
    0 on nothing-to-do / cancelled / success."""
    expected = {
        "already_latest": 0, "registry_error": 1, "preflight_raised": 1,
        "blockers": 1, "cancelled": 0, "dance_dry_run": 0, "happy_path": 0,
        "npm_failed": 1, "install_did_not_move": 1,
    }
    for name, code in expected.items():
        _out, rc, _calls = _run_cli(monkeypatch, **SCENARIOS[name])
        assert rc == code, f"{name}: exit {rc}, expected {code}"


def test_proceed_confirm_still_defaults_to_no(monkeypatch):
    """The `Proceed?` gate keeps its default=False — an accidental <enter>
    must not start an upgrade."""
    _out, _rc, calls = _run_cli(monkeypatch, **SCENARIOS["happy_path"])
    assert calls["confirm"] == [("  Proceed?", {"default": False})]


def test_happy_path_pins_the_resolved_version_in_the_npm_argv(monkeypatch):
    _out, _rc, calls = _run_cli(monkeypatch, **SCENARIOS["happy_path"])
    install = [c for c in calls["npm"] if c[1:2] == ["install"]]
    assert install == [[
        "npm", "install", "-g",
        f"--prefix={ocadmin.OPENCLAW_NPM_PREFIX}", "openclaw@2026.7.1",
    ]]
    assert calls["restarts"] == ["team_bot_a", "team_bot_b"]
    assert calls["signals"] == [("2026.7.0", "2026.7.1", None)]
