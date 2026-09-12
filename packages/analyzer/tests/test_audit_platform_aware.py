"""tests/test_audit_platform_aware.py — platform-awareness of the machine audit.

Covers the [META:platform] bleed-stop: the pod security-audit monitor must
stop firing false macOS CRITICALs on a Linux/VPS pod.

  * ``audit.audit_machine`` runs each check only when the pod's running
    profile (``platform_profile.get_profile().name``, autodetected from
    ``sys.platform`` — the monitor runs ON the host it audits) is in the
    check's ``applies_to`` set. On Linux the five macOS-only checks are
    SILENTLY skipped (no Signal); only the platform-neutral checks run. On
    macOS every check runs, in the pinned order (see ``_MACOS_ORDER``).
  * ``sysadmin_watchdog.signals.user_missing_signal_kwargs`` derives its OS
    noun from the running profile, so the message no longer says "macOS
    account" on a Linux pod (the detection itself is platform-valid).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402
import platform_profile  # noqa: E402
from generators.sysadmin_watchdog import signals as sa_signals  # noqa: E402


# ── machine-audit registry ───────────────────────────────────────────────────

# module attr name -> registry short name, for every machine check.
_CHECK_ATTRS = {
    "_check_firewall": "firewall",
    "_check_filevault": "filevault",
    "_check_admin_user_gateway": "admin_user_gateway",
    "_check_bot_user_privilege": "bot_user_privilege",
    "_check_ssh_config": "ssh_config",
    "_check_macos_updates": "macos_updates",
    "_check_user_accounts": "user_accounts",
    "_check_listening_ports": "listening_ports",
    "_check_oc_binary_mtime": "oc_binary_mtime",
    "_check_linux_firewall": "linux_firewall",
    "_check_linux_disk_encryption": "linux_disk_encryption",
    "_check_linux_os_updates": "linux_os_updates",
}

# The four incursion detectors share ONE adapter (``audit._incursion_check``)
# behind four registry rows, so they are stubbed by name rather than by
# module attr. All four are platform-neutral by design: each resolves its own
# sources per profile internally, so there is no macOS-only binary to
# FileNotFound into a false critical on a Linux pod.
_INCURSION = {
    "incursion_authorized_keys", "incursion_pam",
    "incursion_job_inventory", "incursion_logins",
}

_MACOS_ONLY = {"firewall", "filevault", "macos_updates", "user_accounts", "oc_binary_mtime"}
_LINUX_ONLY = {"linux_firewall", "linux_disk_encryption", "linux_os_updates"}
_NEUTRAL = {
    "admin_user_gateway", "bot_user_privilege", "ssh_config", "listening_ports",
} | _INCURSION

# The canonical macOS run order. Originally pinned to keep the registry
# refactor byte-identical to the prior fixed call list; it now also pins
# WHERE a new check was inserted — bot_user_privilege sits next to its twin
# admin_user_gateway (the two halves of the same privilege invariant).
_MACOS_ORDER = [
    "firewall", "filevault", "admin_user_gateway", "bot_user_privilege",
    "ssh_config", "macos_updates", "user_accounts", "listening_ports",
    "oc_binary_mtime",
    "incursion_authorized_keys", "incursion_pam",
    "incursion_job_inventory", "incursion_logins",
]


def _stub_all_checks(monkeypatch):
    """Replace every ``_check_*`` with a stub that returns one marker finding.

    The stub accepts any args (the per-check signatures diverge) and returns a
    ``RAN:<name>`` finding so we can observe exactly which checks the registry
    invoked — without depending on real subprocess behaviour on the CI host.
    Patching the module attrs also proves monkeypatch still reaches the checks
    through the registry's call-time name resolution.
    """
    for attr, name in _CHECK_ATTRS.items():
        monkeypatch.setattr(
            audit, attr,
            lambda *_a, _n=name, **_k: [audit.Finding(
                level="ok", category="machine", bot_id=None, message=f"RAN:{_n}",
            )],
        )
    monkeypatch.setattr(
        audit, "_incursion_check",
        lambda name, *_a, **_k: [audit.Finding(
            level="ok", category="machine", bot_id=None,
            message=f"RAN:incursion_{name}",
        )],
    )


def _ran(findings):
    return [f.message[len("RAN:"):] for f in findings if f.message.startswith("RAN:")]


def test_registry_applies_to_sets_are_as_specified():
    """The applies_to table is the single declaration of platform
    applicability (a follow-up lint keys off it) — pin it explicitly."""
    table = {name: applies_to for name, _run, applies_to in audit._MACHINE_CHECKS}
    assert table == {
        "firewall": frozenset({"macos"}),
        "filevault": frozenset({"macos"}),
        "admin_user_gateway": frozenset({"macos", "linux"}),
        "bot_user_privilege": frozenset({"macos", "linux"}),
        "ssh_config": frozenset({"macos", "linux"}),
        "macos_updates": frozenset({"macos"}),
        "user_accounts": frozenset({"macos"}),
        "listening_ports": frozenset({"macos", "linux"}),
        "oc_binary_mtime": frozenset({"macos"}),
        "linux_firewall": frozenset({"linux"}),
        "linux_disk_encryption": frozenset({"linux"}),
        "linux_os_updates": frozenset({"linux"}),
        "incursion_authorized_keys": frozenset({"macos", "linux"}),
        "incursion_pam": frozenset({"macos", "linux"}),
        "incursion_job_inventory": frozenset({"macos", "linux"}),
        "incursion_logins": frozenset({"macos", "linux"}),
    }


def test_audit_machine_linux_runs_neutral_plus_linux_checks(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(audit, "get_profile", lambda: platform_profile.LINUX)
    _stub_all_checks(monkeypatch)

    ran = set(_ran(audit.audit_machine(tmp_path, {})))

    # The platform-neutral checks (four originals plus the four incursion
    # detectors) plus the three Linux machine checks ...
    assert ran == _NEUTRAL | _LINUX_ONLY
    # ... and NONE of the macOS-only checks emit anything (silence is correct).
    assert not (ran & _MACOS_ONLY)


def test_audit_machine_macos_runs_all_checks(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(audit, "get_profile", lambda: platform_profile.MACOS)
    _stub_all_checks(monkeypatch)

    ran = set(_ran(audit.audit_machine(tmp_path, {})))

    # macOS runs the macOS-only + neutral checks; the Linux-only checks are
    # silently skipped.
    assert ran == _MACOS_ONLY | _NEUTRAL
    assert not (ran & _LINUX_ONLY)


def test_audit_machine_macos_order_is_byte_identical(tmp_path: Path, monkeypatch):
    """macOS behaviour must be unchanged — same checks, same order, as the
    prior fixed ``findings.extend(...)`` call list."""
    monkeypatch.setattr(audit, "get_profile", lambda: platform_profile.MACOS)
    _stub_all_checks(monkeypatch)

    assert _ran(audit.audit_machine(tmp_path, {})) == _MACOS_ORDER


def test_unknown_platform_resolves_to_macos(tmp_path: Path, monkeypatch):
    """get_profile() resolves unknown platforms to MACOS, so a pod whose
    sys.platform is unrecognised keeps running the full macOS check set
    (the conservative default — never silently drops security checks)."""
    monkeypatch.setattr(audit, "get_profile", lambda: platform_profile.get_profile("freebsd13"))
    _stub_all_checks(monkeypatch)

    assert set(_ran(audit.audit_machine(tmp_path, {}))) == _MACOS_ONLY | _NEUTRAL


# ── user_missing wording ──────────────────────────────────────────────────────


def test_user_missing_wording_macos(monkeypatch):
    monkeypatch.setattr(sa_signals, "get_profile", lambda: platform_profile.MACOS)
    spec = sa_signals.user_missing_signal_kwargs("team_bot_a", user="team_bot_a-bot")

    assert "macOS account" in spec["body"]
    assert "macOS user" in spec["title"]
    assert "Linux" not in spec["body"]
    assert "Linux" not in spec["title"]
    # Remediation hint + shared-account note must survive the wording fix.
    assert "sudo evolve-admin deploy team_bot_a" in spec["body"]
    assert "shares an account" in spec["body"]
    assert spec["details"]["expected_user"] == "team_bot_a-bot"


def test_user_missing_wording_linux(monkeypatch):
    monkeypatch.setattr(sa_signals, "get_profile", lambda: platform_profile.LINUX)
    spec = sa_signals.user_missing_signal_kwargs("team_bot_b", user="team_bot_b-bot")

    assert "Linux account" in spec["body"]
    assert "Linux user" in spec["title"]
    assert "macOS" not in spec["body"]
    assert "macOS" not in spec["title"]
    # Remediation hint + shared-account note must survive the wording fix.
    assert "sudo evolve-admin deploy team_bot_b" in spec["body"]
    assert "shares an account" in spec["body"]
    assert spec["details"]["expected_user"] == "team_bot_b-bot"
