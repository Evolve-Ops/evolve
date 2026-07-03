"""tests/test_audit_linux_machine_checks.py — the Linux machine-audit checks.

Covers the [META:reports] Linux equivalents of the macOS firewall/FileVault/
softwareupdate checks (audit._check_linux_firewall / _check_linux_disk_encryption
/ _check_linux_os_updates).

The hard part of this work is the SEVERITY bar, not the probing, so the tests
drive the decision branches by monkeypatching the probe helpers and assert:
  * the cloud/provider-firewall down-rank (the DigitalOcean trap: ufw inactive
    behind a DO Cloud Firewall must be WARN, never a CRITICAL phantom),
  * the provider-managed-disk down-rank for LUKS-absent on a VPS,
  * security-update CRITICAL vs the unattended-upgrades WARN down-rank,
  * operator policy-acceptance demotion to ok,
  * skipped (no tooling) cases emit nothing alert-worthy,
  * stable signal signatures (no per-run-varying counts in the core message).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402


def _one(findings):
    assert len(findings) == 1, findings
    return findings[0]


def _levels(findings):
    return {f.level for f in findings}


# ── firewall ──────────────────────────────────────────────────────────────────


def test_firewall_active_is_ok(monkeypatch):
    monkeypatch.setattr(audit, "_linux_firewall_state", lambda: ("active", "ufw service active"))
    f = _one(audit._check_linux_firewall({}))
    assert f.level == "ok"
    assert "firewall OK" in f.message


def test_firewall_inactive_bare_metal_is_critical(monkeypatch):
    monkeypatch.setattr(audit, "_linux_firewall_state", lambda: ("inactive", "ufw ENABLED=no"))
    monkeypatch.setattr(audit, "_detect_managed_host", lambda: None)
    f = _one(audit._check_linux_firewall({}))
    assert f.level == "critical"
    assert "CRITICAL" in f.message
    assert f.what_it_means and f.fix_steps


def test_firewall_inactive_on_digitalocean_is_warn_not_critical(monkeypatch):
    """The DigitalOcean trap: ufw inactive behind a DO Cloud Firewall must NOT
    emit a CRITICAL phantom — it down-ranks to WARN with the provider named."""
    monkeypatch.setattr(audit, "_linux_firewall_state", lambda: ("inactive", "ufw ENABLED=no"))
    monkeypatch.setattr(audit, "_detect_managed_host", lambda: "a DigitalOcean Cloud Firewall")
    f = _one(audit._check_linux_firewall({}))
    assert f.level == "warn"
    assert "CRITICAL" not in f.message
    assert "DigitalOcean" in f.detail or "DigitalOcean" in (f.what_it_means or "")


def test_firewall_inactive_operator_accepted_is_ok(monkeypatch):
    monkeypatch.setattr(audit, "_linux_firewall_state", lambda: ("inactive", "ufw ENABLED=no"))
    # Acceptance wins even on a bare-metal host (no managed-host detection).
    monkeypatch.setattr(audit, "_detect_managed_host", lambda: None)
    cfg = {"policy_acceptances": {"machine.firewall_off": {"reason": "isolated lab net"}}}
    f = _one(audit._check_linux_firewall(cfg))
    assert f.level == "ok"
    assert "operator-accepted" in f.message
    assert "isolated lab net" in f.message


def test_firewall_unknown_tooling_is_skipped(monkeypatch):
    monkeypatch.setattr(audit, "_linux_firewall_state", lambda: ("unknown", "no tooling"))
    f = _one(audit._check_linux_firewall({}))
    assert f.level == "skipped"


# ── firewall state probe (the non-root signal reading) ────────────────────────


def _fake_which(present):
    """Return a shutil.which stub: a name in *present* resolves to /usr/bin/<name>."""
    return lambda name: f"/usr/bin/{name}" if name in present else None


def test_firewall_state_ufw_enabled_no_is_inactive(monkeypatch, tmp_path):
    """The live DigitalOcean shape: ufw installed, ufw.conf ENABLED=no, service
    not active → a positive inactive signal (which then down-ranks to WARN
    because the host is a managed VPS)."""
    conf = tmp_path / "ufw.conf"
    conf.write_text("# comment\nENABLED=no\nLOGLEVEL=low\n")
    monkeypatch.setattr(audit, "_UFW_CONF_PATH", str(conf))
    monkeypatch.setattr(audit.shutil, "which", _fake_which({"ufw"}))
    monkeypatch.setattr(audit, "_systemctl_is_active", lambda u: "inactive")
    state, _detail = audit._linux_firewall_state()
    assert state == "inactive"


def test_firewall_state_ufw_enabled_yes_is_active(monkeypatch, tmp_path):
    conf = tmp_path / "ufw.conf"
    conf.write_text("ENABLED=yes\n")
    monkeypatch.setattr(audit, "_UFW_CONF_PATH", str(conf))
    monkeypatch.setattr(audit.shutil, "which", _fake_which({"ufw"}))
    monkeypatch.setattr(audit, "_systemctl_is_active", lambda u: "inactive")
    state, _detail = audit._linux_firewall_state()
    assert state == "active"


def test_firewall_state_nftables_service_active_is_active(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "_UFW_CONF_PATH", str(tmp_path / "absent"))
    monkeypatch.setattr(audit.shutil, "which", _fake_which({"nft"}))
    monkeypatch.setattr(audit, "_systemctl_is_active",
                        lambda u: "active" if u == "nftables" else None)
    state, _detail = audit._linux_firewall_state()
    assert state == "active"


def test_firewall_state_iptables_only_is_unknown(monkeypatch, tmp_path):
    """Only the iptables binary present, no readable service state → unknown,
    NOT inactive — we cannot read iptables rules without root, so claiming
    'off' would risk a false CRITICAL on a host firewalled via raw iptables."""
    monkeypatch.setattr(audit, "_UFW_CONF_PATH", str(tmp_path / "absent"))
    monkeypatch.setattr(audit.shutil, "which", _fake_which({"iptables"}))
    monkeypatch.setattr(audit, "_systemctl_is_active", lambda u: None)
    state, _detail = audit._linux_firewall_state()
    assert state == "unknown"


def test_firewall_state_no_tooling_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "_UFW_CONF_PATH", str(tmp_path / "absent"))
    monkeypatch.setattr(audit.shutil, "which", _fake_which(set()))
    monkeypatch.setattr(audit, "_systemctl_is_active", lambda u: None)
    state, _detail = audit._linux_firewall_state()
    assert state == "unknown"


# ── disk encryption ───────────────────────────────────────────────────────────


def test_disk_luks_present_is_ok(monkeypatch):
    monkeypatch.setattr(audit, "_linux_luks_present", lambda: True)
    f = _one(audit._check_linux_disk_encryption({}))
    assert f.level == "ok"
    assert "disk encryption OK" in f.message


def test_disk_absent_on_managed_vps_is_warn_not_critical(monkeypatch):
    """LUKS-absent on a provider-managed VPS is NOT critical — the disk is the
    provider's and you cannot boot external media to read it."""
    monkeypatch.setattr(audit, "_linux_luks_present", lambda: False)
    monkeypatch.setattr(audit, "_detect_managed_host", lambda: "a DigitalOcean Cloud Firewall")
    f = _one(audit._check_linux_disk_encryption({}))
    assert f.level == "warn"
    assert "CRITICAL" not in f.message


def test_disk_absent_bare_metal_is_critical(monkeypatch):
    """Parity with the macOS FileVault CRITICAL — a physically-accessible host
    whose stolen disk reveals every secret."""
    monkeypatch.setattr(audit, "_linux_luks_present", lambda: False)
    monkeypatch.setattr(audit, "_detect_managed_host", lambda: None)
    f = _one(audit._check_linux_disk_encryption({}))
    assert f.level == "critical"
    assert "CRITICAL" in f.message


def test_disk_absent_operator_accepted_is_ok(monkeypatch):
    monkeypatch.setattr(audit, "_linux_luks_present", lambda: False)
    monkeypatch.setattr(audit, "_detect_managed_host", lambda: None)
    cfg = {"policy_acceptances": {"machine.disk_encryption_off": {"reason": "secured rack"}}}
    f = _one(audit._check_linux_disk_encryption(cfg))
    assert f.level == "ok"
    assert "operator-accepted" in f.message


def test_disk_lsblk_unavailable_is_skipped(monkeypatch):
    monkeypatch.setattr(audit, "_linux_luks_present", lambda: None)
    f = _one(audit._check_linux_disk_encryption({}))
    assert f.level == "skipped"


# ── OS updates ────────────────────────────────────────────────────────────────


def test_updates_none_pending_is_ok(monkeypatch):
    monkeypatch.setattr(audit, "_linux_pending_updates", lambda: (0, 0, "apt-check"))
    monkeypatch.setattr(audit, "_linux_unattended_upgrades_enabled", lambda: False)
    f = _one(audit._check_linux_os_updates({}))
    assert f.level == "ok"


def test_updates_security_pending_no_unattended_is_critical(monkeypatch):
    monkeypatch.setattr(audit, "_linux_pending_updates", lambda: (2, 5, "apt-check"))
    monkeypatch.setattr(audit, "_linux_unattended_upgrades_enabled", lambda: False)
    f = _one(audit._check_linux_os_updates({}))
    assert f.level == "critical"
    assert "security update" in f.message.lower()
    # counts live in detail, not the (signature-bearing) core message
    assert "2 security" in f.detail


def test_updates_security_pending_with_unattended_is_warn(monkeypatch):
    """unattended-upgrades auto-applies security patches → down-rank to WARN
    (not a standing exposure)."""
    monkeypatch.setattr(audit, "_linux_pending_updates", lambda: (3, 0, "apt-check"))
    monkeypatch.setattr(audit, "_linux_unattended_upgrades_enabled", lambda: True)
    f = _one(audit._check_linux_os_updates({}))
    assert f.level == "warn"
    assert "unattended-upgrades" in f.message


def test_updates_regular_only_is_warn(monkeypatch):
    monkeypatch.setattr(audit, "_linux_pending_updates", lambda: (0, 7, "apt list"))
    monkeypatch.setattr(audit, "_linux_unattended_upgrades_enabled", lambda: False)
    f = _one(audit._check_linux_os_updates({}))
    assert f.level == "warn"
    assert "CRITICAL" not in f.message


def test_updates_regular_accepted_is_ok_but_security_unaffected(monkeypatch):
    cfg = {"policy_acceptances": {"machine.os_updates_pending": {"reason": "freeze"}}}
    # regular-only → accepted demotes to ok
    monkeypatch.setattr(audit, "_linux_pending_updates", lambda: (0, 4, "apt-check"))
    monkeypatch.setattr(audit, "_linux_unattended_upgrades_enabled", lambda: False)
    assert _one(audit._check_linux_os_updates(cfg)).level == "ok"
    # but a pending security update still fires regardless of the acceptance
    monkeypatch.setattr(audit, "_linux_pending_updates", lambda: (1, 4, "apt-check"))
    assert _one(audit._check_linux_os_updates(cfg)).level == "critical"


def test_updates_no_apt_is_skipped(monkeypatch):
    monkeypatch.setattr(audit, "_linux_pending_updates", lambda: (None, None, "no apt"))
    f = _one(audit._check_linux_os_updates({}))
    assert f.level == "skipped"


# ── parse helpers ─────────────────────────────────────────────────────────────


def test_parse_apt_upgradable_counts_security_vs_regular():
    out = (
        "Listing...\n"
        "openssl/jammy-security 3.0.2-0ubuntu1.15 amd64 [upgradable from: 3.0.2-0ubuntu1.12]\n"
        "libc6/jammy-updates 2.35-0ubuntu3.8 amd64 [upgradable from: 2.35-0ubuntu3.6]\n"
        "curl/jammy-security 7.81.0-1ubuntu1.16 amd64 [upgradable from: 7.81.0-1ubuntu1.15]\n"
        "vim/jammy 2:8.2.3995 amd64 [upgradable from: 2:8.2.3000]\n"
    )
    security, regular, source = audit._parse_apt_upgradable(out)
    assert security == 2
    assert regular == 2
    assert source == "apt list"


def test_parse_apt_upgradable_empty():
    assert audit._parse_apt_upgradable("Listing...\n") == (0, 0, "apt list")


# ── managed-host detection ────────────────────────────────────────────────────


def test_detect_managed_host_reads_dmi(monkeypatch, tmp_path):
    vendor = tmp_path / "sys_vendor"
    vendor.write_text("DigitalOcean\n")
    monkeypatch.setattr(audit, "_DMI_ID_PATHS", (str(vendor),))
    assert audit._detect_managed_host() == "a DigitalOcean Cloud Firewall"


def test_detect_managed_host_bare_metal_returns_none(monkeypatch, tmp_path):
    vendor = tmp_path / "sys_vendor"
    vendor.write_text("Dell Inc.\n")
    monkeypatch.setattr(audit, "_DMI_ID_PATHS", (str(vendor),))
    assert audit._detect_managed_host() is None


def test_detect_managed_host_unreadable_dmi_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "_DMI_ID_PATHS", (str(tmp_path / "absent"),))
    assert audit._detect_managed_host() is None


# ── signal signature stability ────────────────────────────────────────────────


def test_security_update_signature_stable_across_counts(monkeypatch):
    """The mirrored Signal signature must not churn as the pending count drifts
    run-to-run — counts live in detail, the core message is stable."""
    monkeypatch.setattr(audit, "_linux_unattended_upgrades_enabled", lambda: False)

    monkeypatch.setattr(audit, "_linux_pending_updates", lambda: (2, 0, "apt-check"))
    sig_a = audit._audit_signature(_one(audit._check_linux_os_updates({})))
    monkeypatch.setattr(audit, "_linux_pending_updates", lambda: (5, 0, "apt-check"))
    sig_b = audit._audit_signature(_one(audit._check_linux_os_updates({})))

    assert sig_a == sig_b
