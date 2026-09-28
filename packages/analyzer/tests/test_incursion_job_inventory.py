"""incursion.job_inventory — a scheduled job Evolve did not install pages;
one it did is settled by ownership, not by a timing window.

``audit_cron_health`` reports on the health of Evolve's OWN cron entries and
``audit_process`` catches a bad binary only while it happens to be running.
Neither notices a LaunchDaemon or systemd timer that re-launches an
attacker's code every boot. These tests pin the four brief-mandated
scenarios plus the two judgements specific to this detector: label ownership
(a new ``ai.evolve.*`` job is expected) and program identity (the same label
now running something else is the quietest takeover there is, and is NOT
excused by ownership).
"""

from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import platform_profile  # noqa: E402
from incursion import baseline as baseline_store  # noqa: E402
from incursion import job_inventory  # noqa: E402
from incursion import owned_jobs  # noqa: E402
from incursion.job_inventory import JobRoot  # noqa: E402

#: The labels a deployed pod's installer has recorded as its own. Everything
#: else with an Evolve-shaped name is a stranger wearing the name.
_RECORDED = (
    "ai.evolve.evolve.heal",
    "ai.evolve.evolve.signal-subscriber",
)


@pytest.fixture
def pod(tmp_path):
    shared = tmp_path / "shared"
    (shared / "security" / "baselines").mkdir(parents=True)
    # A deployed pod: deploy.record_owned_job_labels has written the labels
    # the installer put on this host. Ownership is a lookup in that file.
    owned_jobs.record(shared, _RECORDED)
    return shared


@pytest.fixture
def undeployed_pod(tmp_path):
    """A pod whose installer has not recorded anything yet."""
    shared = tmp_path / "undeployed"
    (shared / "security" / "baselines").mkdir(parents=True)
    return shared


@pytest.fixture
def daemons(tmp_path):
    d = tmp_path / "Library" / "LaunchDaemons"
    d.mkdir(parents=True)
    _plist(d, "ai.evolve.evolve.heal", ["/opt/evolve-venv/bin/python3", "-m", "heal"])
    _plist(d, "com.example.updater", ["/Applications/Example.app/updater"])
    return d


def _plist(directory: Path, label: str, argv: list[str]) -> Path:
    path = directory / f"{label}.plist"
    path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": argv}))
    return path


def _roots(daemons: Path) -> list[JobRoot]:
    return [JobRoot("launchd", daemons, ("*.plist",))]


def _criticals(observations):
    return [o for o in observations if o.level == "critical"]


def test_first_run_records_the_baseline_and_does_not_page(pod, daemons):
    observations = job_inventory.check(pod, {}, roots=_roots(daemons))

    assert [o.level for o in observations] == ["ok"]
    assert "baseline recorded, 2 entries" in observations[0].message


def test_unchanged_pass_reports_nothing_actionable(pod, daemons):
    job_inventory.check(pod, {}, roots=_roots(daemons))

    observations = job_inventory.check(pod, {}, roots=_roots(daemons))

    assert [o.level for o in observations] == ["ok"]


def test_a_new_job_evolve_did_not_install_is_an_event(pod, daemons):
    """The persistence move: a definition the host will run on its own
    schedule, at boot, with nobody logged in."""
    job_inventory.check(pod, {}, roots=_roots(daemons))

    _plist(daemons, "com.unknown.helper", ["/tmp/helper", "--daemon"])
    observations = job_inventory.check(pod, {}, roots=_roots(daemons))

    criticals = _criticals(observations)
    assert len(criticals) == 1, [o.message for o in observations]
    assert criticals[0].finding_kind == "event"
    assert "com.unknown.helper" in criticals[0].message
    assert "/tmp/helper --daemon" in criticals[0].detail


def test_a_new_job_the_installer_recorded_is_explained_by_ownership(pod, daemons):
    """Ownership settles this without a time window: the installer WROTE this
    label into its registry, so there is no deploy stamp to time the way a
    content change would need."""
    job_inventory.check(pod, {}, roots=_roots(daemons))

    _plist(daemons, "ai.evolve.evolve.signal-subscriber",
           ["/opt/evolve-venv/bin/python3", "-m", "signals.subscriber"])
    observations = job_inventory.check(pod, {}, roots=_roots(daemons))

    assert _criticals(observations) == []
    assert any("new Evolve-owned scheduled job" in o.message for o in observations)
    # Absorbed — the next pass is quiet.
    assert [o.level for o in job_inventory.check(pod, {}, roots=_roots(daemons))] == ["ok"]


def test_an_evolve_named_job_the_installer_never_recorded_pages(pod, daemons):
    """The prefix-bless evasion, review #3967 finding 1.

    ``ai.evolve.helper`` costs an attacker one filename. Before the registry
    it was absorbed as "new Evolve-owned scheduled job" and written into the
    baseline, so it never paged again. Ownership is now membership in the list
    the installer wrote — and nothing put this label in it.
    """
    job_inventory.check(pod, {}, roots=_roots(daemons))

    _plist(daemons, "ai.evolve.helper", ["/tmp/helper", "--daemon"])
    observations = job_inventory.check(pod, {}, roots=_roots(daemons))

    criticals = _criticals(observations)
    assert len(criticals) == 1, [o.message for o in observations]
    assert criticals[0].finding_kind == "event"
    assert "ai.evolve.helper" in criticals[0].message
    # NOT absorbed: it keeps firing until the operator deals with it.
    assert len(_criticals(job_inventory.check(pod, {}, roots=_roots(daemons)))) == 1


def test_an_openclaw_named_job_the_installer_never_recorded_pages(pod, daemons):
    """The second blessed prefix gets the same treatment as the first."""
    job_inventory.check(pod, {}, roots=_roots(daemons))

    _plist(daemons, "ai.openclaw.evolve.exfil", ["/tmp/x"])
    observations = job_inventory.check(pod, {}, roots=_roots(daemons))

    assert len(_criticals(observations)) == 1, [o.message for o in observations]
    assert "ai.openclaw.evolve.exfil" in _criticals(observations)[0].message


def test_the_registry_is_matched_exactly_not_by_prefix(pod, daemons):
    """A recorded label must not bless the labels UNDER it. Otherwise
    ``ai.evolve.evolve.heal`` in the registry re-opens the whole evasion for
    ``ai.evolve.evolve.heal.helper``."""
    job_inventory.check(pod, {}, roots=_roots(daemons))

    _plist(daemons, "ai.evolve.evolve.heal.helper", ["/tmp/helper"])
    observations = job_inventory.check(pod, {}, roots=_roots(daemons))

    assert len(_criticals(observations)) == 1, [o.message for o in observations]


def test_no_registry_means_nothing_is_owned_and_the_gap_is_named(
    undeployed_pod, daemons,
):
    """A pod that has not deployed since this landed cannot tell its own jobs
    apart from anyone else's, and says so. Falling back to the prefix would
    hand the evasion back to whoever can delete one file, so the fallback is
    to page — with a gap row explaining why the next Evolve daemon will."""
    job_inventory.check(undeployed_pod, {}, roots=_roots(daemons))

    _plist(daemons, "ai.evolve.evolve.signal-subscriber", ["/opt/venv/bin/python3"])
    observations = job_inventory.check(undeployed_pod, {}, roots=_roots(daemons))

    gaps = [o for o in observations if "coverage gap" in o.message]
    assert len(gaps) == 1, [o.message for o in observations]
    assert "no Evolve-owned job registry recorded yet" in gaps[0].message
    assert gaps[0].level == "warn"
    assert len(_criticals(observations)) == 1


def test_the_registry_never_absorbs_a_label_it_only_observed(pod, daemons):
    """The detector reads the registry; it never writes one. A backfill from
    what is on disk would launder an intruder's already-installed job into
    "Evolve owns this" — the evasion re-entering through the fix."""
    _plist(daemons, "ai.evolve.impostor", ["/tmp/x"])

    job_inventory.check(pod, {}, roots=_roots(daemons))
    job_inventory.check(pod, {}, roots=_roots(daemons))

    assert owned_jobs.load(pod) == set(_RECORDED)


def test_a_repointed_program_pages_even_under_an_evolve_label(pod, daemons):
    """Ownership excuses a new label, never a new program. "ai.evolve.evolve.heal
    now runs something else" is among the most alarming lines this audit can
    produce, and nothing in a normal upgrade repoints a daemon."""
    job_inventory.check(pod, {}, roots=_roots(daemons))

    _plist(daemons, "ai.evolve.evolve.heal", ["/tmp/not-heal"])
    observations = job_inventory.check(pod, {}, roots=_roots(daemons))

    criticals = _criticals(observations)
    assert len(criticals) == 1, [o.message for o in observations]
    assert criticals[0].finding_kind == "event"
    assert "runs a different program" in criticals[0].message
    assert "/tmp/not-heal" in criticals[0].detail


def test_a_scheduling_change_alone_does_not_page(pod, daemons):
    """The stored value is the PROGRAM, not the whole file — so retiming a
    job or editing a comment is not an alert, and the detector survives
    contact with ordinary maintenance."""
    job_inventory.check(pod, {}, roots=_roots(daemons))

    path = daemons / "com.example.updater.plist"
    data = plistlib.loads(path.read_bytes())
    data["StartInterval"] = 7200
    path.write_bytes(plistlib.dumps(data))

    assert _criticals(job_inventory.check(pod, {}, roots=_roots(daemons))) == []


def test_an_uninstalled_job_is_information(pod, daemons):
    job_inventory.check(pod, {}, roots=_roots(daemons))

    (daemons / "com.example.updater.plist").unlink()
    observations = job_inventory.check(pod, {}, roots=_roots(daemons))

    assert _criticals(observations) == []
    assert any("no longer installed" in o.message for o in observations)


def test_systemd_units_and_cron_files_are_surveyed_on_the_linux_shapes(pod, tmp_path):
    """The Linux half of the source table: unit ExecStart lines and the cron
    surfaces. Same diff, different parsers."""
    units = tmp_path / "etc" / "systemd" / "system"
    units.mkdir(parents=True)
    (units / "ai.evolve.evolve.heal.service").write_text(
        "[Service]\nExecStart=/opt/evolve-venv/bin/python3 -m heal\n"
    )
    cron_d = tmp_path / "etc" / "cron.d"
    cron_d.mkdir(parents=True)
    (cron_d / "backup").write_text("# comment\n0 3 * * * root /usr/local/bin/backup\n")
    roots = [
        JobRoot("systemd", units, ("*.service", "*.timer")),
        JobRoot("cron", cron_d),
    ]

    job_inventory.check(pod, {}, roots=roots)
    (units / "com.unknown.helper.service").write_text(
        "[Service]\nExecStart=/tmp/helper\n"
    )
    (cron_d / "backup").write_text(
        "# comment\n0 3 * * * root /usr/local/bin/backup\n"
        "* * * * * root /tmp/beacon\n"
    )
    observations = job_inventory.check(pod, {}, roots=roots)

    messages = " | ".join(o.message for o in _criticals(observations))
    assert "systemd:com.unknown.helper.service" in messages
    assert "cron:" in messages and "backup" in messages


def test_a_recorded_label_is_owned_through_its_systemd_unit_suffix(pod, tmp_path):
    """The registry holds the label the installer passed to the Scheduler
    seam; Linux renders that as ``<label>.timer`` / ``<label>.service``. Left
    unmapped, no job on a Linux pod would ever be owned and every Evolve
    daemon installed after the baseline would page."""
    units = tmp_path / "etc" / "systemd" / "system"
    units.mkdir(parents=True)
    (units / "ai.evolve.evolve.heal.service").write_text(
        "[Service]\nExecStart=/opt/evolve-venv/bin/python3 -m heal\n"
    )
    roots = [JobRoot("systemd", units, ("*.service", "*.timer"))]
    job_inventory.check(pod, {}, roots=roots)

    # `ai.evolve.evolve.signal-subscriber` IS in the registry (as a bare
    # label); Linux installs it as a .timer unit.
    (units / "ai.evolve.evolve.signal-subscriber.timer").write_text(
        "[Service]\nExecStart=/opt/evolve-venv/bin/python3 -m signals.subscriber\n"
    )
    # ...and one that is not in the registry, under the same suffix.
    (units / "ai.evolve.helper.timer").write_text(
        "[Service]\nExecStart=/tmp/helper\n"
    )
    observations = job_inventory.check(pod, {}, roots=roots)

    criticals = _criticals(observations)
    assert len(criticals) == 1, [o.message for o in observations]
    assert "ai.evolve.helper.timer" in criticals[0].message


def test_a_cron_comment_change_alone_does_not_page(pod, tmp_path):
    """Cron files have no label/program split to make, so they are hashed
    whole — but comment and blank lines are dropped first, or every routine
    annotation would be an alert."""
    cron_d = tmp_path / "etc" / "cron.d"
    cron_d.mkdir(parents=True)
    (cron_d / "backup").write_text("0 3 * * * root /usr/local/bin/backup\n")
    roots = [JobRoot("cron", cron_d)]
    job_inventory.check(pod, {}, roots=roots)

    (cron_d / "backup").write_text(
        "# nightly backup, see runbook\n\n0 3 * * * root /usr/local/bin/backup\n"
    )

    assert _criticals(job_inventory.check(pod, {}, roots=roots)) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root traverses 0000 directories")
def test_an_unreadable_root_is_a_coverage_gap_not_a_crash(pod, tmp_path):
    """The per-user crontab spool is mode 1730 root:crontab on Debian and
    friends, so this is the root most likely to come back as a gap. Naming it
    is the point: the alternative is a green table that quietly excludes it."""
    spool = tmp_path / "var" / "spool" / "cron" / "crontabs"
    spool.mkdir(parents=True)
    os.chmod(spool, 0o000)
    try:
        observations = job_inventory.check(pod, {}, roots=[JobRoot("cron", spool)])
    finally:
        os.chmod(spool, 0o700)

    gaps = [o for o in observations if "coverage gap" in o.message]
    assert len(gaps) == 1
    assert gaps[0].level == "warn"
    assert "crontabs" in gaps[0].message


def test_a_malformed_definition_is_a_gap_not_a_swallowed_job(pod, daemons):
    """A plist the parser cannot read is a job whose program is NOT being
    watched. Skipping it silently would leave a blind spot shaped exactly
    like "write a plist the parser chokes on"."""
    (daemons / "com.broken.plist").write_bytes(b"this is not a plist")

    observations = job_inventory.check(pod, {}, roots=_roots(daemons))

    gaps = [o for o in observations if "coverage gap" in o.message]
    assert len(gaps) == 1
    assert "com.broken.plist" in gaps[0].message
    assert "not being watched" in gaps[0].detail


def test_a_root_this_host_does_not_have_is_covered_not_a_gap(pod, tmp_path):
    """Every Linux host is missing some cron surface and every macOS host is
    missing others. "There is no such directory" is a place the detector
    looked, not a place it failed to look."""
    observations = job_inventory.check(
        pod, {}, roots=[JobRoot("cron", tmp_path / "etc" / "cron.weekly")],
    )

    assert [o.level for o in observations] == ["ok"]
    assert "0 entries from 1 source(s)" in observations[0].message


def test_job_roots_are_keyed_to_the_running_platform(monkeypatch, tmp_path):
    """One detector, two source tables. The macOS side surveys the launchd
    directories third-party software actually writes to (Apple's own jobs
    under /System/Library are SIP-protected and churn on every OS update);
    the Linux side surveys the unit dir and the cron surfaces."""
    home = tmp_path / "homes" / "team_bot_a"
    homes = {"team_bot_a": home}

    monkeypatch.setattr(job_inventory, "get_profile",
                        lambda *a, **k: platform_profile.MACOS)
    macos = job_inventory.job_roots({}, homes=homes)
    macos_paths = [str(r.path) for r in macos]
    assert {r.kind for r in macos} == {"launchd"}
    assert "/Library/LaunchDaemons" in macos_paths
    assert "/Library/LaunchAgents" in macos_paths
    assert str(home / "Library" / "LaunchAgents") in macos_paths
    assert not any(p.startswith("/System/Library") for p in macos_paths)

    monkeypatch.setattr(job_inventory, "get_profile",
                        lambda *a, **k: platform_profile.LINUX)
    linux = job_inventory.job_roots({}, homes=homes)
    linux_paths = [str(r.path) for r in linux]
    assert {r.kind for r in linux} == {"systemd", "cron"}
    assert platform_profile.LINUX.daemon_dir in linux_paths
    assert "/etc/crontab" in linux_paths
    assert "/etc/cron.d" in linux_paths
    assert "/var/spool/cron/crontabs" in linux_paths
    assert str(home / ".config" / "systemd" / "user") in linux_paths


def test_read_only_pass_writes_no_baseline(pod, daemons):
    job_inventory.check(pod, {}, read_only=True, roots=_roots(daemons))

    assert baseline_store.load(pod, "job_inventory") is None
