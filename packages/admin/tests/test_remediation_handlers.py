"""Tests for the three Phase 4 PR-1 action handlers.

Each handler runs side-effectful operations (subprocess calls, file
writes). The tests mock subprocess.run + filesystem boundaries to keep
them hermetic — the real on-mini behavior is verified by manually
running the action from the UI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ADMIN = Path(__file__).parent.parent
_ANALYZER = _ADMIN.parent / "analyzer"
for p in (_ADMIN, _ANALYZER):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from evolve_admin.remediation.handlers import (  # noqa: E402
    HANDLERS,
    get_handler,
    handle_flip_cron_session_target,
    handle_install_infra_jobs,
    handle_reset_baseline,
)


# ── Registry ─────────────────────────────────────────────────────────────────


def test_registry_includes_three_phase4_kinds():
    """PR-1 (Phase 4) shipped these three kinds. Later phases add more
    (Phase 5 adds set_exec_allowlist + set_exec_security); the new
    test_registry_now_has_five_kinds in test_remediation_exec_handlers.py
    is the current full-set assertion. This test stays narrow — it pins
    the original three so they can't accidentally be dropped."""
    for required in ("install_infra_jobs", "reset_baseline", "flip_cron_session_target"):
        assert required in HANDLERS


def test_get_handler_known_kind_returns_callable():
    h = get_handler("install_infra_jobs")
    assert callable(h)


def test_get_handler_unknown_kind_raises_keyerror_with_available_list():
    with pytest.raises(KeyError) as ei:
        get_handler("destroy_pod")
    msg = str(ei.value)
    assert "destroy_pod" in msg
    assert "install_infra_jobs" in msg  # the message lists the available set


# ── install_infra_jobs ──────────────────────────────────────────────────────


def test_install_infra_jobs_success(tmp_path: Path):
    fake_result = MagicMock(returncode=0, stdout="Installed launchd: foo\n", stderr="")
    with patch("evolve_admin.remediation.handlers.subprocess.run",
               return_value=fake_result) as mock_run:
        out = handle_install_infra_jobs({}, tmp_path)
    assert out["exit_code"] == 0
    assert "Installed" in out["stdout"]
    # Sanity-check the command shape
    args = mock_run.call_args.args[0]
    assert args[0] == "sudo"
    assert "install-infra-jobs" in args


def test_install_infra_jobs_nonzero_exit_raises(tmp_path: Path):
    fake_result = MagicMock(returncode=1, stdout="", stderr="permission denied")
    with patch("evolve_admin.remediation.handlers.subprocess.run",
               return_value=fake_result):
        with pytest.raises(RuntimeError) as ei:
            handle_install_infra_jobs({}, tmp_path)
    assert "permission denied" in str(ei.value)


# ── reset_baseline ──────────────────────────────────────────────────────────


def test_reset_baseline_missing_params_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        handle_reset_baseline({}, tmp_path)
    with pytest.raises(ValueError):
        handle_reset_baseline({"bot_id": "security_bot"}, tmp_path)
    with pytest.raises(ValueError):
        handle_reset_baseline({"kind": "scripts"}, tmp_path)


def test_reset_baseline_clears_entry(tmp_path: Path):
    # Seed a baseline so reset has something to clear
    baseline = tmp_path / "security" / "baselines" / "scripts.json"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(json.dumps({"security_bot": ["a.py"], "admin_bot": ["b.py"]}))
    out = handle_reset_baseline(
        {"bot_id": "security_bot", "kind": "scripts"}, tmp_path,
    )
    assert out["cleared"] is True
    assert out["bot_id"] == "security_bot"
    # admin_bot's entry survived
    remaining = json.loads(baseline.read_text())
    assert "security_bot" not in remaining
    assert "admin_bot" in remaining


def test_reset_baseline_idempotent_when_no_entry(tmp_path: Path):
    """No entry to clear → cleared=False but doesn't raise. The UI shows
    'nothing to do' rather than an error."""
    baseline = tmp_path / "security" / "baselines" / "scripts.json"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(json.dumps({"admin_bot": ["b.py"]}))
    out = handle_reset_baseline(
        {"bot_id": "security_bot", "kind": "scripts"}, tmp_path,
    )
    assert out["cleared"] is False
    assert "note" in out


# ── flip_cron_session_target ────────────────────────────────────────────────


@pytest.fixture
def fake_cron_path(tmp_path: Path, monkeypatch):
    """Patch the user resolver to return ``security_bot`` and stub subprocess.run
    so it reads/writes a tmp_path-local cron file instead of /Users/security_bot/..."""
    cron_file = tmp_path / "security_bot-cron-jobs.json"
    cron_file.write_text(json.dumps({"jobs": [
        {"name": "security_bot-task-runner", "sessionTarget": "main",
         "payload": {"kind": "exec", "command": "x"}},
        {"name": "security_bot-other", "sessionTarget": "isolated",
         "payload": {"kind": "exec", "command": "y"}},
    ]}, indent=2))

    monkeypatch.setattr(
        "evolve_admin.remediation.handlers._resolve_bot_user",
        lambda bot_id: "security_bot",
    )

    real_run = __import__("subprocess").run

    def fake_run(args, **kw):
        """Intercept the sudo /bin/cat and sudo /bin/cp calls that target the
        real /Users/security_bot path; let chmod/chown calls become no-ops."""
        if len(args) >= 3 and args[0] == "sudo" and args[1] == "/bin/cat":
            target = Path(args[2])
            if "security_bot" in str(target) and "cron" in str(target):
                return MagicMock(returncode=0, stdout=cron_file.read_text(),
                                 stderr="")
        if len(args) >= 4 and args[0] == "sudo" and args[1] == "/bin/cp":
            src = Path(args[2])
            dst = Path(args[3])
            if "security_bot" in str(dst) and "cron" in str(dst):
                cron_file.write_text(src.read_text())
                return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "evolve_admin.remediation.handlers.subprocess.run", fake_run,
    )
    return cron_file


def test_flip_cron_main_to_isolated(tmp_path: Path, fake_cron_path):
    out = handle_flip_cron_session_target(
        {"bot_id": "security_bot", "cron_name": "security_bot-task-runner"}, tmp_path,
    )
    assert out["flipped"] is True
    assert out["from"] == "main"
    assert out["to"] == "isolated"
    # Verify the file was actually rewritten
    data = json.loads(fake_cron_path.read_text())
    target = next(j for j in data["jobs"] if j["name"] == "security_bot-task-runner")
    assert target["sessionTarget"] == "isolated"
    # Other cron untouched
    other = next(j for j in data["jobs"] if j["name"] == "security_bot-other")
    assert other["sessionTarget"] == "isolated"  # was already isolated


def test_flip_cron_already_isolated_is_noop(tmp_path: Path, fake_cron_path):
    out = handle_flip_cron_session_target(
        {"bot_id": "security_bot", "cron_name": "security_bot-other"}, tmp_path,
    )
    assert out["flipped"] is False
    assert "already" in out["note"].lower()


def test_flip_cron_missing_name_raises(tmp_path: Path, fake_cron_path):
    with pytest.raises(RuntimeError) as ei:
        handle_flip_cron_session_target(
            {"bot_id": "security_bot", "cron_name": "nope"}, tmp_path,
        )
    assert "no cron named" in str(ei.value)


def test_flip_cron_missing_params_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        handle_flip_cron_session_target({}, tmp_path)
    with pytest.raises(ValueError):
        handle_flip_cron_session_target({"bot_id": "security_bot"}, tmp_path)
    with pytest.raises(ValueError):
        handle_flip_cron_session_target({"cron_name": "x"}, tmp_path)


def test_flip_cron_unexpected_session_target_refuses(
    tmp_path: Path, monkeypatch,
):
    """If sessionTarget is neither 'main' nor 'isolated', refuse the flip
    rather than silently doing nothing — surfaces the case where someone
    manually set it to something exotic."""
    cron_file = tmp_path / "cron.json"
    cron_file.write_text(json.dumps({"jobs": [
        {"name": "weird", "sessionTarget": "background",
         "payload": {"kind": "exec", "command": "x"}},
    ]}))
    monkeypatch.setattr(
        "evolve_admin.remediation.handlers._resolve_bot_user",
        lambda bid: "security_bot",
    )

    def fake_run(args, **kw):
        if args[0] == "sudo" and args[1] == "/bin/cat":
            return MagicMock(returncode=0, stdout=cron_file.read_text(),
                             stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "evolve_admin.remediation.handlers.subprocess.run", fake_run,
    )

    with pytest.raises(RuntimeError) as ei:
        handle_flip_cron_session_target(
            {"bot_id": "security_bot", "cron_name": "weird"}, tmp_path,
        )
    assert "background" in str(ei.value)
