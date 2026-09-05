"""The L2 gate's ``os_update`` source — the host's own install record.

Added 2026-09-02 for ``incursion.pam``: an OS update legitimately rewrites
``/etc/pam.d``, and a PAM detector that paged for every patch cycle would be
muted inside a month. This is the only source in the allow-set that reads
outside ``{shared_dir}``, and the only one whose record Evolve does not
write — which is what makes it usable, because laundering a change through it
means forging a root-owned system log.

Two properties get their own tests because both are silent when wrong: the
narrowness of the package match (a source that answers "something was
installed" explains nothing about a specific file) and the timestamp
conversion (``dpkg`` stamps local wall-clock with no offset, so reading it as
UTC misplaces every event by the host's offset).
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import drift_authorization as da  # noqa: E402

PAM_CHANGE = da.DriftChange(kind=da.KIND_PAM_CONFIG, target="pam.d/sudo")


@pytest.fixture
def tokyo_tz():
    """Run the body in a non-UTC zone, so "local" and "UTC" differ.

    Without this the timestamp test is vacuous on a UTC CI runner: a naive
    stamp read as UTC and a naive stamp read as local are the same instant.
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Tokyo"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def _dpkg_log(tmp_path: Path, *lines: str) -> Path:
    log = tmp_path / "dpkg.log"
    log.write_text("\n".join(lines) + "\n")
    return log


def test_a_pam_package_upgrade_inside_the_window_explains_the_change(
    tmp_path, monkeypatch, tokyo_tz,
):
    installed = datetime.now().astimezone() - timedelta(hours=2)
    monkeypatch.setattr(da, "_DPKG_LOGS", (_dpkg_log(
        tmp_path,
        f"{installed.strftime('%Y-%m-%d %H:%M:%S')} upgrade libpam-modules:arm64 1.5.2 1.5.3",
    ),))

    events = da._dpkg_update_events(PAM_CHANGE, installed + timedelta(hours=1))

    assert len(events) == 1
    assert "libpam-modules" in events[0].evidence


def test_the_same_upgrade_outside_the_window_explains_nothing(
    tmp_path, monkeypatch, tokyo_tz,
):
    """The window is what keeps a single patch from accounting for a change
    made days later. Read as UTC on this non-UTC host, the stamp would land
    nine hours off and this boundary would move with it."""
    installed = datetime.now().astimezone() - timedelta(hours=40)
    monkeypatch.setattr(da, "_DPKG_LOGS", (_dpkg_log(
        tmp_path,
        f"{installed.strftime('%Y-%m-%d %H:%M:%S')} upgrade libpam-modules:arm64 1.5.2 1.5.3",
    ),))
    now = installed + da.OS_UPDATE_WINDOW + timedelta(minutes=30)

    assert da._dpkg_update_events(PAM_CHANGE, now) == []
    # …and one minute inside the same window still answers, so the test above
    # is about the boundary rather than about the log being unreadable.
    assert da._dpkg_update_events(
        PAM_CHANGE, installed + da.OS_UPDATE_WINDOW - timedelta(minutes=1),
    )


def test_an_unrelated_package_is_not_an_explanation(tmp_path, monkeypatch):
    """Narrow by kind: the source has to name the package that OWNS the
    changed file. "An apt run happened recently" is true most days on a
    patched host and would excuse anything."""
    stamp = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr(da, "_DPKG_LOGS", (_dpkg_log(
        tmp_path, f"{stamp} upgrade curl:arm64 8.5.0 8.6.0",
    ),))

    assert da._dpkg_update_events(PAM_CHANGE, datetime.now(timezone.utc)) == []


def test_a_removal_is_not_an_explanation(tmp_path, monkeypatch):
    """``remove``/``purge`` are deliberately absent from the credited
    actions: a removal that deletes a PAM policy is exactly the change the
    detector must still page for."""
    stamp = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr(da, "_DPKG_LOGS", (_dpkg_log(
        tmp_path, f"{stamp} remove libpam-modules:arm64 1.5.3 <none>",
    ),))

    assert da._dpkg_update_events(PAM_CHANGE, datetime.now(timezone.utc)) == []


def test_a_kind_with_no_package_hint_gets_no_os_update_explanation(tmp_path, monkeypatch):
    """Registering the source for a kind whose owning package cannot be named
    would make it a source that answers every question — the anti-pattern
    this module's docstring calls out. Only ``pam_config`` has a hint."""
    stamp = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr(da, "_DPKG_LOGS", (_dpkg_log(
        tmp_path, f"{stamp} upgrade libpam-modules:arm64 1.5.2 1.5.3",
    ),))

    other = da.DriftChange(kind=da.KIND_JOB_INVENTORY, target="launchd:x")
    assert da._dpkg_update_events(other, datetime.now(timezone.utc)) == []


def test_an_unreadable_install_record_explains_nothing(tmp_path, monkeypatch):
    """Fails toward paging: a source that cannot be read is not a source that
    said yes."""
    monkeypatch.setattr(da, "_DPKG_LOGS", (tmp_path / "absent.log",))
    monkeypatch.setattr(da, "_MACOS_INSTALL_HISTORY", tmp_path / "absent.plist")

    assert da._dpkg_update_events(PAM_CHANGE, datetime.now(timezone.utc)) == []
    assert da._macos_update_events(PAM_CHANGE, datetime.now(timezone.utc)) == []


def test_the_macos_receipts_date_is_read_as_utc(tmp_path, monkeypatch, tokyo_tz):
    """plistlib hands back a NAIVE datetime that the format defines as UTC.
    Letting it adopt the host's zone would shift every OS-update record by
    the offset — nine hours here."""
    import plistlib

    installed = datetime.now(timezone.utc) - timedelta(hours=2)
    history = tmp_path / "InstallHistory.plist"
    history.write_bytes(plistlib.dumps([{
        "displayName": "System Update",
        "date": installed.replace(tzinfo=None),
        "packageIdentifiers": ["com.apple.pkg.update.os"],
    }]))
    monkeypatch.setattr(da, "_MACOS_INSTALL_HISTORY", history)

    events = da._macos_update_events(PAM_CHANGE, datetime.now(timezone.utc))

    assert len(events) == 1
    assert abs((events[0].at - installed).total_seconds()) < 60


def _history(tmp_path, *package_ids, display="An Update", hours_ago=1):
    """A receipts file with one entry, ``hours_ago`` old, inside the window."""
    import plistlib

    path = tmp_path / "InstallHistory.plist"
    path.write_bytes(plistlib.dumps([{
        "displayName": display,
        "date": (
            datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        ).replace(tzinfo=None),
        "packageIdentifiers": list(package_ids),
    }]))
    return path


@pytest.mark.parametrize("receipt", da._MACOS_DATA_ONLY_RECEIPT_IDS)
def test_a_data_only_apple_receipt_explains_no_pam_edit(
    tmp_path, monkeypatch, receipt,
):
    """Review #3967 finding 2, by name.

    The predicate used to be "any com.apple.* receipt in the last 24h".
    XProtect / MRT / Gatekeeper definition refreshes land near-daily on a
    healthy Mac and rewrite nothing in /etc, so that predicate explained away
    a ``/etc/pam.d`` edit on most days of the year. A data refresh is not the
    OS updating its own software.
    """
    monkeypatch.setattr(da, "_MACOS_INSTALL_HISTORY", _history(tmp_path, receipt))

    assert da._macos_update_events(PAM_CHANGE, datetime.now(timezone.utc)) == []


def test_an_os_software_update_receipt_still_explains_a_pam_edit(
    tmp_path, monkeypatch,
):
    """The narrowing must not close the source: a real OS update rewrites
    /etc/pam.d, and paging for every patch cycle is how a detector gets
    muted."""
    monkeypatch.setattr(da, "_MACOS_INSTALL_HISTORY", _history(
        tmp_path, "com.apple.pkg.update.os.15.1.0.patch", display="macOS 15.1",
    ))

    events = da._macos_update_events(PAM_CHANGE, datetime.now(timezone.utc))

    assert len(events) == 1
    # The evidence names the receipt, so the operator can check the claim.
    assert "com.apple.pkg.update.os.15.1.0.patch" in events[0].evidence
    assert "macOS 15.1" in events[0].evidence


def test_a_data_receipt_alongside_an_os_update_still_explains(
    tmp_path, monkeypatch,
):
    """One receipt can carry several identifiers. The allow-list asks whether
    ANY of them is OS software, not whether ALL of them are — otherwise a
    bundled definitions payload would suppress a genuine update."""
    monkeypatch.setattr(da, "_MACOS_INSTALL_HISTORY", _history(
        tmp_path, "com.apple.pkg.XProtectPayloads", "com.apple.pkg.update.os",
    ))

    assert len(da._macos_update_events(PAM_CHANGE, datetime.now(timezone.utc))) == 1


def test_every_allowed_prefix_carries_its_reason(tmp_path, monkeypatch):
    """The allow-list is a widening surface: each entry says why it qualifies,
    so the next edit to it is a reviewable claim rather than a string."""
    assert da._MACOS_OS_UPDATE_PACKAGE_PREFIXES
    for prefix, reason in da._MACOS_OS_UPDATE_PACKAGE_PREFIXES.items():
        assert prefix.startswith("com.apple."), prefix
        assert len(reason.split()) >= 8, prefix
        # A data-only receipt must not be reachable through any allowed prefix.
        for data_receipt in da._MACOS_DATA_ONLY_RECEIPT_IDS:
            assert not data_receipt.startswith(prefix), (prefix, data_receipt)


def test_a_third_party_installer_receipt_is_not_an_os_update(tmp_path, monkeypatch):
    """Every ``.pkg`` a user double-clicks lands in the same receipts file.
    Only Apple's own package identifiers count as the OS updating itself."""
    import plistlib

    history = tmp_path / "InstallHistory.plist"
    history.write_bytes(plistlib.dumps([{
        "displayName": "Some Vendor Tool",
        "date": (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None),
        "packageIdentifiers": ["com.vendor.tool"],
    }]))
    monkeypatch.setattr(da, "_MACOS_INSTALL_HISTORY", history)

    assert da._macos_update_events(PAM_CHANGE, datetime.now(timezone.utc)) == []
