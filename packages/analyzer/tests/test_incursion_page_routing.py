"""Each incursion detector's critical actually reaches the operator.

Same standard as ``test_audit_critical_page_routing.py``, applied to the four
new detectors: the REAL dispatcher, the REAL catalog, the REAL signal store —
only the wire send is stubbed. A detector that produces a beautifully worded
``event`` critical which the delivery layer then routes into a daily digest
has detected nothing anybody will act on for a day.

Each case drives ``audit._incursion_check`` (the production adapter, with its
production source defaults redirected at a fixture) so the whole path is
exercised: detector → Observation → Finding → signal → page-on-transition →
dispatcher → channel.
"""

from __future__ import annotations

import base64
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
_ADMIN_DIR = _ANALYZER_DIR.parent / "admin"
for _p in (str(_ANALYZER_DIR), str(_ADMIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import audit  # noqa: E402
import drift_authorization  # noqa: E402
from incursion import authorized_keys, job_inventory, logins, pam  # noqa: E402
from incursion.job_inventory import JobRoot  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Real dispatcher and store, stubbed wire — a default-configured pod."""
    from evolve_admin.alerts import dispatcher

    wire: list[tuple[str, str]] = []

    def fake_wire(chat_id, message):
        wire.append((chat_id, message))
        return True, None

    monkeypatch.setattr(dispatcher, "_dispatch_via_telegram_http", fake_wire)

    shared = tmp_path / "evolve"
    (shared / "security" / "baselines").mkdir(parents=True)
    return {
        "wire": wire,
        "shared_dir": shared,
        "config": {
            "alerts": {"channel": "telegram", "chatId": "12345"},
            "sharedDir": str(shared),
        },
    }


def _key_line(comment: str, seed: bytes) -> str:
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + seed.ljust(32, b"\x00")[:32]
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}"


def _arm_authorized_keys(tmp_path, monkeypatch):
    home = tmp_path / "homes" / "pod_admin_user"
    (home / ".ssh").mkdir(parents=True)
    keys = home / ".ssh" / "authorized_keys"
    keys.write_text(_key_line("operator@laptop", b"A"))
    monkeypatch.setattr(
        authorized_keys, "pod_users", lambda config: {"pod_admin_user": home},
    )

    def introduce():
        keys.write_text(
            _key_line("operator@laptop", b"A") + "\n" + _key_line("elsewhere", b"B")
        )

    return introduce


def _arm_pam(tmp_path, monkeypatch):
    pam_dir = tmp_path / "etc" / "pam.d"
    pam_dir.mkdir(parents=True)
    (pam_dir / "sudo").write_text("auth required pam_opendirectory.so\n")
    monkeypatch.setattr(pam, "PAM_DIR", pam_dir)
    monkeypatch.setattr(pam, "PAM_CONF", tmp_path / "etc" / "pam.conf")
    # No install record on either platform, so the change is unexplained.
    monkeypatch.setattr(drift_authorization, "_DPKG_LOGS", ())
    monkeypatch.setattr(
        drift_authorization, "_MACOS_INSTALL_HISTORY", tmp_path / "no-history",
    )

    def introduce():
        (pam_dir / "sudo").write_text("auth sufficient pam_permit.so\n")

    return introduce


def _arm_job_inventory(tmp_path, monkeypatch):
    daemons = tmp_path / "Library" / "LaunchDaemons"
    daemons.mkdir(parents=True)
    (daemons / "ai.evolve.evolve.heal.plist").write_bytes(plistlib.dumps({
        "Label": "ai.evolve.evolve.heal",
        "ProgramArguments": ["/opt/evolve-venv/bin/python3", "-m", "heal"],
    }))
    monkeypatch.setattr(
        job_inventory, "job_roots",
        lambda config=None, homes=None: [JobRoot("launchd", daemons, ("*.plist",))],
    )

    def introduce():
        (daemons / "com.unknown.helper.plist").write_bytes(plistlib.dumps({
            "Label": "com.unknown.helper",
            "ProgramArguments": ["/tmp/helper", "--daemon"],
        }))

    return introduce


def _arm_logins(tmp_path, monkeypatch):
    del tmp_path
    state = {"out": "pod_admin_user ttys000  198.51.100.10   Mon Sep  1 09:12   still logged in\n"}

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=state["out"], stderr="",
        )

    monkeypatch.setattr(logins.subprocess, "run", fake_run)

    def introduce():
        state["out"] += (
            "pod_admin_user ttys001  203.0.113.77    Tue Sep  2 02:41 - 03:10  (00:29)\n"
        )

    return introduce


ARMERS = {
    "authorized_keys": _arm_authorized_keys,
    "pam": _arm_pam,
    "job_inventory": _arm_job_inventory,
    "logins": _arm_logins,
}


@pytest.mark.parametrize("detector", sorted(ARMERS))
def test_each_detectors_critical_pages_immediately(env, tmp_path, monkeypatch, detector):
    """The whole path, per detector, on a default pod: the finding is an
    ``event`` critical and the batch reaches the channel rather than a digest
    queue."""
    introduce = ARMERS[detector](tmp_path, monkeypatch)
    shared_dir, config = env["shared_dir"], env["config"]

    # First pass establishes the baseline; nothing should page from it.
    audit.dispatch_findings(
        audit._incursion_check(detector, shared_dir, config),
        shared_dir, config, dry_run=False,
    )
    assert not env["wire"], "a fresh pod's first pass must never page"

    introduce()
    findings = audit._incursion_check(detector, shared_dir, config)
    criticals = [f for f in findings if f.level == "critical"]
    assert len(criticals) == 1, [f.message for f in findings]
    assert criticals[0].finding_kind == "event"
    assert criticals[0].category == "machine"

    audit.dispatch_findings(findings, shared_dir, config, dry_run=False)

    assert env["wire"], (
        f"the {detector} critical never reached the channel — it was routed "
        f"to a digest queue instead of paging"
    )
    assert "CRITICAL" in env["wire"][0][1]
    assert not (shared_dir / "alerts" / "digest-pending").exists()


@pytest.mark.parametrize("detector", sorted(ARMERS))
def test_a_standing_finding_does_not_re_page_every_cycle(env, tmp_path, monkeypatch, detector):
    """Page-on-transition (R-1). The unexplained change is deliberately not
    absorbed, so the finding repeats on every 15-minute run — which is only
    survivable because a Signal that is already firing does not page again.
    Both halves have to hold or the detector is either forgetful or a pager
    storm."""
    introduce = ARMERS[detector](tmp_path, monkeypatch)
    shared_dir, config = env["shared_dir"], env["config"]
    audit.dispatch_findings(
        audit._incursion_check(detector, shared_dir, config),
        shared_dir, config, dry_run=False,
    )
    introduce()
    audit.dispatch_findings(
        audit._incursion_check(detector, shared_dir, config),
        shared_dir, config, dry_run=False,
    )
    assert len(env["wire"]) == 1

    repeat = audit._incursion_check(detector, shared_dir, config)
    assert [f for f in repeat if f.level == "critical"], (
        "the finding must stand until the operator acts on it"
    )
    audit.dispatch_findings(repeat, shared_dir, config, dry_run=False)

    assert len(env["wire"]) == 1, "a standing finding must not re-page"


def test_a_detector_that_raises_degrades_to_one_skipped_finding(env, monkeypatch):
    """One broken detector must not end the audit run. ``skipped`` (not
    ``warn``) because a detector that did not run is audit infrastructure
    failing, not a pod condition — and skipped findings are kept off the
    Alerts page by ``_emit_signals_from_findings``."""
    def boom(*a, **kw):
        raise RuntimeError("source went away")

    monkeypatch.setattr(pam, "check", boom)

    findings = audit._incursion_check("pam", env["shared_dir"], env["config"])

    assert [f.level for f in findings] == ["skipped"]
    assert "incursion pam detector did not run" in findings[0].message
    assert "source went away" in findings[0].detail
