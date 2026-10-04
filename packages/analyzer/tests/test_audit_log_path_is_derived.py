"""The audit log path is derived from ``shared_dir``, not a module constant.

``audit._LOG_FILE = Path("/Users/Shared/evolve/logs/audit.log")`` was a
hard-coded module constant, so every test that drove ``dispatch_findings``
appended its fixture findings to the REAL operator-facing log whatever
``tmp_path`` it was handed. Measured on a maintainer's laptop on 2026-09-01:
420 accumulated lines reading "CRITICAL: sshd PasswordAuthentication enabled"
and "CRITICAL: team_bot_a: Telegram token in .env" — indistinguishable from
genuine findings in the log an operator greps during an incident, and ten
audit suites added another 265 in a single run. Benign on Linux CI (``/Users``
is not creatable by a non-root user, so the ``mkdir`` raises and ``_log``
swallows the OSError) and invisible there for the same reason.

These tests pin the *executed* path, not just the presence of a helper: a
real dispatch must land in the caller's ``shared_dir``. A later change that
reintroduces a module-level default — under any name — leaves the tmp log
empty and turns this file red.

The executed-path tests spell their expected location out as a literal
``tmp_path / "logs" / "audit.log"`` rather than reading it back from
``audit_log_path``. Asserting through the resolver would let a resolver that
ignored ``shared_dir`` move both sides of the comparison and pass vacuously —
which is exactly the defect these tests exist to catch. Only
:func:`test_resolved_log_path_lives_under_the_given_shared_dir`, whose subject
IS the resolver, calls it.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


@pytest.fixture(autouse=True)
def _silence_telegram(monkeypatch):
    """Delivery is not under test here — keep the run off the network."""
    monkeypatch.setattr("audit._send_security_alert", lambda *a, **kw: True)
    monkeypatch.setattr("audit._send_telegram_direct", lambda *a, **kw: True)


def _critical(message: str) -> audit.Finding:
    return audit.Finding(level="critical", finding_kind="event",
                         category="machine", bot_id=None,
                         message=message, detail="")


def _warn(message: str) -> audit.Finding:
    return audit.Finding(level="warn", category="machine", bot_id=None,
                         message=message, detail="")


def test_resolved_log_path_lives_under_the_given_shared_dir(tmp_path: Path):
    log_file = audit.audit_log_path(tmp_path)

    assert log_file == tmp_path / "logs" / "audit.log"
    assert tmp_path in log_file.parents, (
        f"audit log resolved to {log_file}, outside the caller's shared_dir "
        f"{tmp_path} — a run would write to some other pod's log"
    )


def test_log_requires_a_shared_dir_with_no_production_fallback():
    """No default ⇒ a new call site cannot silently write to the real log.

    The containment only holds because omitting ``shared_dir`` is a type
    error rather than a fallback to a module constant.
    """
    param = inspect.signature(audit._log).parameters["shared_dir"]

    assert param.default is inspect.Parameter.empty, (
        "_log grew a default shared_dir — a call site that forgets the "
        "argument would silently write to whatever that default points at"
    )


def test_dispatch_findings_writes_the_log_under_shared_dir(tmp_path: Path):
    """The executed path, not just the helper: a real dispatch lands in tmp.

    Literal path on purpose — see the module docstring.
    """
    log_file = tmp_path / "logs" / "audit.log"
    assert not log_file.exists()

    audit.dispatch_findings(
        [_critical("sshd PasswordAuthentication enabled"),
         _warn("bot_a: world-readable workspace file")],
        tmp_path, config={}, dry_run=False,
    )

    assert log_file.exists(), (
        "dispatch_findings wrote no log under shared_dir — the log path is "
        "no longer derived from the caller's shared_dir"
    )
    body = log_file.read_text()
    assert "CRITICAL: sshd PasswordAuthentication enabled" in body
    assert "WARN: bot_a: world-readable workspace file" in body


def test_two_shared_dirs_do_not_share_a_log(tmp_path: Path):
    """Two pods, two logs — the proof that nothing is process-global."""
    pod_a, pod_b = tmp_path / "a", tmp_path / "b"

    audit.dispatch_findings([_critical("finding for pod a")], pod_a,
                            config={}, dry_run=False)
    audit.dispatch_findings([_critical("finding for pod b")], pod_b,
                            config={}, dry_run=False)

    log_a = (pod_a / "logs" / "audit.log").read_text()
    log_b = (pod_b / "logs" / "audit.log").read_text()
    assert "finding for pod a" in log_a and "finding for pod b" not in log_a
    assert "finding for pod b" in log_b and "finding for pod a" not in log_b
