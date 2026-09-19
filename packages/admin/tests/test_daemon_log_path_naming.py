"""Regression tests for per-bot daemon log filenames (``_job_spec_for``).

Found 2026-09-14 while resolving 8 unloaded ``ai.openclaw.evolve.doctor-pass.*``
LaunchDaemons on the mini pod. The log stem used to be derived with an
UNANCHORED substring strip of the account name::

    label.replace("ai.openclaw.evolve.", "").replace("ai.evolve.", "").replace(f".{user}", "")

``network.json`` maps the bot id to a macOS account that need not share its
name (bot ``evolve`` runs on account ``evo``; a bot may be given an account
named for the person who uses it), so the strip had three different outcomes
depending on the relationship:

* account == bot id                 → ``evolve-doctor-pass.log``      (intended)
* account is a prefix of the bot id → ``evolve-doctor-passlve.log``   (MANGLED —
  ``doctor-pass.evolve`` minus the substring ``.evo`` eats ``.evo`` out of the
  MIDDLE of ``.evolve``; live on the reference pod for doctor-pass,
  audit-runner, audit-runner-t3 and cost-converter)
* account unrelated to the bot id   → ``evolve-doctor-pass.<bot>.log`` (bot
  segment never stripped)

The fix strips the trailing ``.<bot_id>`` with an anchored ``removesuffix``, so
all three collapse to ``evolve-<job>.log``. ``stdout_path``/``stderr_path`` feed
BOTH scheduler adapters (``render_launchd_plist`` → ``StandardOutPath`` on
macOS, ``render_systemd_units`` → ``StandardOutput=append:`` on Linux), so the
one derivation covers both pod platforms.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import deploy  # noqa: E402

# (bot_id, macOS account) — the three user/bot-id relationships in play.
_BOT_ACCOUNTS = [
    pytest.param("team_bot_a", "team_bot_a", id="account-equals-bot-id"),
    pytest.param("evolve", "evo", id="account-is-prefix-of-bot-id"),
    pytest.param("team_bot_b", "bot-b-account", id="account-unrelated-to-bot-id"),
]


def _stems(spec) -> tuple[str, str]:
    """(stdout basename, stderr basename) for a built JobSpec."""
    return Path(spec.stdout_path).name, Path(spec.stderr_path).name


@pytest.mark.parametrize("bot_id,account", _BOT_ACCOUNTS)
@pytest.mark.parametrize("job", ["doctor-pass", "audit-runner", "audit-runner-t3",
                                 "cost-converter", "analyze", "outcome"])
def test_per_bot_log_stem_is_account_independent(bot_id, account, job):
    """``evolve-<job>.log`` for every bot, whatever its account is called."""
    spec = deploy._job_spec_for(
        f"ai.openclaw.evolve.{job}.{bot_id}", account,
        Path("/dev/null"), {"Hour": 3, "Minute": 17}, bot_id=bot_id,
    )
    assert _stems(spec) == (f"evolve-{job}.log", f"evolve-{job}.err.log")


def test_prefix_collision_does_not_mangle_the_job_name():
    """The specific 2026-09-14 defect: bot ``evolve`` on account ``evo``."""
    spec = deploy._job_spec_for(
        "ai.openclaw.evolve.doctor-pass.evolve", "evo",
        Path("/dev/null"), {"Hour": 3, "Minute": 17}, bot_id="evolve",
    )
    assert "passlve" not in spec.stdout_path
    assert spec.stdout_path == "/Users/evo/.openclaw/logs/evolve-doctor-pass.log"


def test_log_dir_still_follows_the_account_not_the_bot_id():
    """Only the FILENAME is bot-id-derived; the directory is the account's home.
    A bot writes to its own account's ~/.openclaw/logs — the job runs as that
    user and has no access to a directory named after the bot id."""
    spec = deploy._job_spec_for(
        "ai.openclaw.evolve.cost-converter.team_bot_b", "bot-b-account",
        Path("/dev/null"), {"interval": 900}, bot_id="team_bot_b",
    )
    assert spec.stdout_path == "/Users/bot-b-account/.openclaw/logs/evolve-cost-converter.log"


@pytest.mark.parametrize("label,expected", [
    # Pod-wide jobs pass no bot_id — unchanged by the fix.
    ("ai.openclaw.evolve.tuples", "evolve-tuples.log"),
    ("ai.openclaw.evolve.measure", "evolve-measure.log"),
    ("ai.openclaw.evolve.model_liveness_monitor", "evolve-model_liveness_monitor.log"),
    # ``ai.evolve.<bot>.<job>`` carries the bot id as an INFIX, not a suffix.
    # removesuffix is a no-op there and these keep the names they have on disk
    # today — deliberately out of scope, see the PR body.
    ("ai.evolve.evolve.backup", "evolve-evolve.backup.log"),
    ("ai.evolve.team_bot_a.heal", "evolve-team_bot_a.heal.log"),
])
def test_pod_wide_and_infix_labels_are_unchanged(label, expected):
    spec = deploy._job_spec_for(
        label, "evolve", Path("/dev/null"), {"Hour": 1, "Minute": 30},
    )
    assert Path(spec.stdout_path).name == expected


def test_prefix_strip_is_anchored_not_a_substring_replace():
    """A job name that merely CONTAINS the label prefix keeps it.
    ``str.replace`` would have eaten the embedded copy too."""
    spec = deploy._job_spec_for(
        "ai.openclaw.evolve.verify-ai.evolve.-links", "evolve",
        Path("/dev/null"), {"Hour": 2, "Minute": 0},
    )
    assert Path(spec.stdout_path).name == "evolve-verify-ai.evolve.-links.log"


# ── The threading contract: the installers must actually PASS bot_id ─────────

# (installer, job segment, call shape). The first four take an explicit
# ``user=`` override, so they can be driven with the mini's real bot-evolve /
# account-evo split and checked on the log path alone. The last three pin
# ``user=bot_id`` internally, where every account name produces the same path —
# there the only observable is the kwarg itself, so both are asserted.
_INSTALLERS = [
    ("_install_launchd_doctor_pass", "doctor-pass",
     lambda f, b, u, r: f(b, r, user=u)),
    ("_install_launchd_audit_runner", "audit-runner",
     lambda f, b, u, r: f(b, Path("/tmp"), r, user=u)),
    ("_install_launchd_audit_runner_tier3", "audit-runner-t3",
     lambda f, b, u, r: f(b, Path("/tmp"), r, user=u)),
    ("_install_launchd_cost_converter", "cost-converter",
     lambda f, b, u, r: f(b, r, user=u)),
    ("_install_launchd_analyze", "analyze",
     lambda f, b, u, r: f(b, Path("/tmp"), r)),
    ("_install_launchd_outcome", "outcome",
     lambda f, b, u, r: f(b, Path("/tmp"), r)),
    ("_install_launchd_expansion", "expansion",
     lambda f, b, u, r: f(b, Path("/tmp"), r)),
]


@pytest.mark.parametrize("fn_name,job,call", _INSTALLERS)
def test_installers_thread_bot_id_through_to_the_log_path(fn_name, job, call, monkeypatch):
    """End-to-end through the real installer: a helper that forgets
    ``bot_id=bot_id`` silently reverts that job to an account-derived name."""
    seen_bot_ids: list = []
    captured: list = []
    real_job_spec_for = deploy._job_spec_for

    def _spy(*a, **kw):
        seen_bot_ids.append(kw.get("bot_id"))
        return real_job_spec_for(*a, **kw)

    monkeypatch.setattr(deploy, "VENV_PYTHON", sys.executable)
    monkeypatch.setattr(deploy, "ANALYZER_DIR", _ADMIN_DIR.parent / "analyzer")
    monkeypatch.setattr(deploy, "_job_spec_for", _spy)
    monkeypatch.setattr(
        deploy, "_install_job_ensuring_restart",
        lambda spec: (captured.append(spec), (True, "ok"))[1],
    )
    call(getattr(deploy, fn_name), "evolve", "evo",
         deploy.DeployResult(bot_id="evolve", success=True))

    assert captured, f"{fn_name} did not reach the scheduler seam"
    assert seen_bot_ids == ["evolve"], (
        f"{fn_name} must pass bot_id=<bot id> to _job_spec_for; got {seen_bot_ids}"
    )
    assert Path(captured[0].stdout_path).name == f"evolve-{job}.log"
