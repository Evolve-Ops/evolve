"""Primary-gateway Telegram channel registration (FIND-B).

``_register_primary_telegram_channel`` runs ``openclaw channels status``/``add``
as the gateway account. Two rules, both learned on the live ``evolve-vsp-pod``:

  * cwd MUST be the gateway home — ``openclaw`` is a Node binary; Node calls
    ``uv_cwd()`` at startup and EACCES-dies if it inherits the root-run wizard's
    cwd (e.g. ``/var/lib/evolve/repo``, which ``evo`` cannot traverse).
  * a slow/hung gateway must degrade to a "register manually" warning — never an
    unhandled ``TimeoutExpired`` crashing the wizard after "Setup Complete".

The subprocess + ``user_home`` are injected, so no test shells out to openclaw.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import setup_wizard  # noqa: E402
from evolve_admin.setup_wizard import _register_primary_telegram_channel  # noqa: E402

GW_HOME = Path("/home/evo")
ACCOUNT = "evo"
BOT = "primary-bot"
TOKEN = "123:abc"


class _Res:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def captured(monkeypatch):
    msgs: list[str] = []
    for fn in ("_ok", "_warn", "_info"):
        monkeypatch.setattr(setup_wizard, fn, lambda m, _p=msgs: _p.append(str(m)))
    return msgs


def _run(monkeypatch, runner):
    calls: list[dict] = []

    def wrapped(cmd, **kwargs):
        calls.append({"cmd": list(cmd), "cwd": kwargs.get("cwd"),
                      "timeout": kwargs.get("timeout")})
        return runner(list(cmd))

    status = _register_primary_telegram_channel(
        ACCOUNT, BOT, TOKEN, home_fn=lambda _a: GW_HOME, runner=wrapped,
    )
    return status, calls


def test_status_and_add_use_gateway_home_as_cwd(monkeypatch, captured):
    """Both openclaw calls run with cwd == the gateway home (Node uv_cwd)."""
    def runner(cmd):
        if "status" in cmd:
            return _Res(stdout="no channels configured")
        return _Res(returncode=0)  # add succeeds

    status, calls = _run(monkeypatch, runner)
    assert status == "registered"
    assert len(calls) == 2
    for c in calls:
        assert c["cwd"] == str(GW_HOME), f"cwd not the gateway home: {c}"
    # The token never leaks into a warning (only the placeholder hint does).
    assert TOKEN not in "\n".join(captured)


def test_already_registered_skips_add(monkeypatch, captured):
    def runner(cmd):
        # status reports a running telegram channel
        return _Res(stdout="telegram: running")

    status, calls = _run(monkeypatch, runner)
    assert status == "already"
    assert len(calls) == 1  # only the status probe, no add


def test_timeout_on_status_degrades_to_warning(monkeypatch, captured):
    """A hung `channels status` must be caught, not propagate (the live crash)."""
    def runner(cmd):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

    # The call MUST NOT raise.
    status, calls = _run(monkeypatch, runner)
    assert status == "timeout"
    joined = "\n".join(captured).lower()
    assert "timed out" in joined
    assert "register manually" in joined


def test_timeout_on_add_degrades_to_warning(monkeypatch, captured):
    def runner(cmd):
        if "status" in cmd:
            return _Res(stdout="no channels")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=15)

    status, _ = _run(monkeypatch, runner)
    assert status == "timeout"


def test_add_failure_is_loud_but_returns(monkeypatch, captured):
    def runner(cmd):
        if "status" in cmd:
            return _Res(stdout="no channels")
        return _Res(returncode=1, stderr="bad token")

    status, _ = _run(monkeypatch, runner)
    assert status == "failed"
    assert "manually" in "\n".join(captured).lower()


def test_oserror_degrades_to_warning(monkeypatch, captured):
    def runner(cmd):
        raise OSError("sudo not found")

    status, _ = _run(monkeypatch, runner)
    assert status == "error"
