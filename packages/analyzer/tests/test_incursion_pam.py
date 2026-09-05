"""incursion.pam — drift in the authentication stack pages unless the host's
own install record accounts for it.

PAM is the one incursion kind with a real entry in the L2 allow-set
(``drift_authorization._KIND_SOURCES``), because an OS update legitimately
rewrites ``/etc/pam.d`` and a detector that paged for every patch cycle would
be muted within a month. These tests pin both halves: the unexplained change
is an ``event`` critical, and the one an install record explains is absorbed
so it never comes back when the gate's window closes.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import drift_authorization  # noqa: E402
import platform_profile  # noqa: E402
from incursion import baseline as baseline_store  # noqa: E402
from incursion import pam  # noqa: E402


@pytest.fixture
def pod(tmp_path):
    shared = tmp_path / "shared"
    (shared / "security" / "baselines").mkdir(parents=True)
    return shared


@pytest.fixture
def pam_dir(tmp_path):
    d = tmp_path / "etc" / "pam.d"
    d.mkdir(parents=True)
    (d / "sudo").write_text("auth       required       pam_opendirectory.so\n")
    (d / "sshd").write_text("auth       required       pam_unix.so\n")
    return d


@pytest.fixture
def missing_conf(tmp_path):
    return tmp_path / "etc" / "pam.conf"


def _check(pod, pam_dir, missing_conf, **kw):
    return pam.check(pod, None, pam_dir=pam_dir, pam_conf=missing_conf, **kw)


def _criticals(observations):
    return [o for o in observations if o.level == "critical"]


def test_first_run_records_the_baseline_and_does_not_page(pod, pam_dir, missing_conf):
    observations = _check(pod, pam_dir, missing_conf)

    assert [o.level for o in observations] == ["ok"]
    assert "baseline recorded, 2 entries" in observations[0].message


def test_unchanged_pass_reports_nothing_actionable(pod, pam_dir, missing_conf):
    _check(pod, pam_dir, missing_conf)

    observations = _check(pod, pam_dir, missing_conf)

    assert [o.level for o in observations] == ["ok"]
    assert "OK (2 entries" in observations[0].message


def test_an_edited_policy_with_nothing_to_authorize_it_is_an_event(
    pod, pam_dir, missing_conf, monkeypatch,
):
    """One changed line in ``sudo`` can make every privilege check on the box
    a formality. With no install record to account for it this pages."""
    monkeypatch.setattr(drift_authorization, "_DPKG_LOGS", ())
    monkeypatch.setattr(
        drift_authorization, "_MACOS_INSTALL_HISTORY", pam_dir / "no-such-file",
    )
    _check(pod, pam_dir, missing_conf)

    (pam_dir / "sudo").write_text("auth       sufficient     pam_permit.so\n")
    observations = _check(pod, pam_dir, missing_conf)

    criticals = _criticals(observations)
    assert len(criticals) == 1, [o.message for o in observations]
    assert criticals[0].finding_kind == "event"
    assert "pam.d/sudo" in criticals[0].message
    assert "was modified" in criticals[0].message


def test_a_new_policy_file_and_a_deleted_one_both_page(
    pod, pam_dir, missing_conf, monkeypatch,
):
    """Deleting ``/etc/pam.d/sudo`` removes a requirement just as effectively
    as editing it, so removal is an event too — not the information-level row
    a removed SSH key gets."""
    monkeypatch.setattr(drift_authorization, "_DPKG_LOGS", ())
    monkeypatch.setattr(
        drift_authorization, "_MACOS_INSTALL_HISTORY", pam_dir / "no-such-file",
    )
    _check(pod, pam_dir, missing_conf)

    (pam_dir / "backdoor").write_text("auth sufficient pam_permit.so\n")
    (pam_dir / "sshd").unlink()
    observations = _check(pod, pam_dir, missing_conf)

    messages = " | ".join(o.message for o in _criticals(observations))
    assert "pam.d/backdoor appeared" in messages
    assert "pam.d/sshd was deleted" in messages


def test_a_change_a_package_upgrade_explains_is_absorbed(
    pod, pam_dir, missing_conf, monkeypatch, tmp_path,
):
    """The false-positive case that would otherwise retire this detector.

    A dpkg upgrade of a pam package inside the window explains the change —
    and the new state is WRITTEN to the baseline. Absorbing is what stops the
    same change re-paging the moment the gate's 24h window closes, which is
    the "explained today, unexplained tomorrow" trap.
    """
    monkeypatch.setattr(platform_profile, "get_profile",
                        lambda *a, **k: platform_profile.LINUX)
    log = tmp_path / "dpkg.log"
    monkeypatch.setattr(drift_authorization, "_DPKG_LOGS", (log,))
    _check(pod, pam_dir, missing_conf)

    stamp = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    log.write_text(f"{stamp} upgrade libpam-modules:arm64 1.5.2 1.5.3\n")
    (pam_dir / "sudo").write_text("auth       required       pam_unix.so\n")

    observations = _check(pod, pam_dir, missing_conf)

    assert _criticals(observations) == []
    assert any("libpam-modules" in o.message for o in observations)
    # Absorbed: the next pass, with the log now outside the window, is quiet.
    log.write_text("")
    assert _criticals(_check(pod, pam_dir, missing_conf)) == []


def test_an_unrelated_package_upgrade_explains_nothing(
    pod, pam_dir, missing_conf, monkeypatch, tmp_path,
):
    """The source is narrow by design: it has to say "the package that owns
    this file", not merely "something was installed an hour ago"."""
    monkeypatch.setattr(platform_profile, "get_profile",
                        lambda *a, **k: platform_profile.LINUX)
    log = tmp_path / "dpkg.log"
    monkeypatch.setattr(drift_authorization, "_DPKG_LOGS", (log,))
    _check(pod, pam_dir, missing_conf)

    stamp = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    log.write_text(f"{stamp} upgrade curl:arm64 8.5.0 8.6.0\n")
    (pam_dir / "sudo").write_text("auth       sufficient     pam_permit.so\n")

    assert len(_criticals(_check(pod, pam_dir, missing_conf))) == 1


def test_a_macos_os_update_explains_the_change(
    pod, pam_dir, missing_conf, monkeypatch, tmp_path,
):
    """Apple ships no per-file package identifier for ``/etc/pam.d``, so the
    receipts database can only say "the OS was updated at T". That is still a
    dated, host-written record of an authorized event — and it is why the
    window is a day rather than a week."""
    import plistlib

    monkeypatch.setattr(platform_profile, "get_profile",
                        lambda *a, **k: platform_profile.MACOS)
    history = tmp_path / "InstallHistory.plist"
    history.write_bytes(plistlib.dumps([{
        "displayName": "System Update",
        "date": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2),
        "packageIdentifiers": ["com.apple.pkg.update.os"],
    }]))
    monkeypatch.setattr(drift_authorization, "_MACOS_INSTALL_HISTORY", history)
    _check(pod, pam_dir, missing_conf)

    (pam_dir / "sudo").write_text("auth       required       pam_smartcard.so\n")
    observations = _check(pod, pam_dir, missing_conf)

    assert _criticals(observations) == []
    assert any("System Update" in o.message for o in observations)


def test_a_macos_data_receipt_does_not_explain_the_change(
    pod, pam_dir, missing_conf, monkeypatch, tmp_path,
):
    """The end-to-end shape of review #3967 finding 2, and one of the drill's
    scenarios: an edit to ``/etc/pam.d/sudo`` made while a fresh XProtect
    definitions receipt sits in the install history must still page. Those
    receipts land near-daily, so the old "any com.apple.* receipt" rule meant
    the authentication stack could be rewritten on almost any day of the year
    and the audit would file it as maintenance."""
    import plistlib

    monkeypatch.setattr(platform_profile, "get_profile",
                        lambda *a, **k: platform_profile.MACOS)
    history = tmp_path / "InstallHistory.plist"
    history.write_bytes(plistlib.dumps([{
        "displayName": "XProtectPlistConfigData",
        "date": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2),
        "packageIdentifiers": ["com.apple.pkg.XProtectPlistConfigData"],
    }]))
    monkeypatch.setattr(drift_authorization, "_MACOS_INSTALL_HISTORY", history)
    _check(pod, pam_dir, missing_conf)

    (pam_dir / "sudo").write_text("auth       sufficient     pam_permit.so\n")
    observations = _check(pod, pam_dir, missing_conf)

    criticals = _criticals(observations)
    assert len(criticals) == 1, [o.message for o in observations]
    assert criticals[0].finding_kind == "event"
    assert "pam.d/sudo" in criticals[0].message


def test_an_unreadable_pam_directory_is_a_coverage_gap_not_a_crash(pod, tmp_path):
    """A host where the detector cannot see its own source says so."""
    observations = pam.check(
        pod, None,
        pam_dir=tmp_path / "nowhere" / "pam.d",
        pam_conf=tmp_path / "nowhere" / "pam.conf",
    )

    gaps = [o for o in observations if "coverage gap" in o.message]
    assert len(gaps) == 1
    assert gaps[0].level == "warn"
    assert "does not exist on this host" in gaps[0].detail


def test_read_only_pass_writes_no_baseline(pod, pam_dir, missing_conf):
    _check(pod, pam_dir, missing_conf, read_only=True)

    assert baseline_store.load(pod, "pam") is None
