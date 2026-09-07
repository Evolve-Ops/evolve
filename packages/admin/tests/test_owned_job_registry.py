"""The install side of the incursion detector's owned-job registry.

``incursion.job_inventory`` decides whether a newly appeared LaunchDaemon or
systemd unit is Evolve's by looking its label up in
``{shared_dir}/security/evolve-owned-jobs.json``. Before this, it decided by
string prefix — so an attacker's ``ai.evolve.helper`` was blessed and baselined
for the price of one filename (review of PR #3967, finding 1).

The detector reads that file and never writes it. These tests pin the writer:
that a job installed through the Scheduler seam records its label, that the
whole expected set is re-asserted on a deploy, and — the property the fix turns
on — that nothing in the writer ever derives a label from what is on disk.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
sys.path.insert(0, str(_ANALYZER_DIR))

from evolve_admin import deploy  # noqa: E402
from incursion import owned_jobs  # noqa: E402


@pytest.fixture
def pod(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    return shared


def test_recording_a_label_lands_in_the_registry_the_detector_reads(pod):
    deploy.record_owned_job_labels(["ai.evolve.evolve.admin-ui"], pod)

    assert owned_jobs.load(pod) == {"ai.evolve.evolve.admin-ui"}
    written = json.loads(owned_jobs.registry_path(pod).read_text())
    assert written["labels"] == ["ai.evolve.evolve.admin-ui"]


def test_the_expected_label_set_is_recordable_as_a_whole(pod):
    """The deploy-time re-assert — what backfills a pod upgrading into the
    registry and what recovers one that was deleted. It is derived from
    ``expected_plist_labels`` (installer intent), never from
    /Library/LaunchDaemons."""
    network = {"members": ["team_bot_a"], "bots": {}}
    labels = deploy.expected_plist_labels(network)

    deploy.record_owned_job_labels(labels, pod)

    recorded = owned_jobs.load(pod)
    assert recorded is not None
    for required in (
        "ai.evolve.evolve.admin-ui",
        "ai.evolve.evolve.heal",
        "ai.evolve.evolve.audit",
        "ai.openclaw.evolve.measure",
    ):
        assert required in recorded, f"{required} missing from the registry"


def test_a_write_failure_never_raises_into_the_install(pod, monkeypatch):
    """Best-effort by contract. An install that failed because a detector's
    lookup file could not be written would trade a coverage-gap row for a
    broken deploy."""
    monkeypatch.setattr(
        owned_jobs, "record",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only file system")),
    )

    deploy.record_owned_job_labels(["ai.evolve.evolve.heal"], pod)  # no raise


def test_installing_through_the_seam_records_the_label(pod, monkeypatch):
    """``_install_spec_via_seam`` is the path the admin-ui, mcp-bridge,
    signal-subscriber and better-engine daemons take."""
    from runtime.scheduler import FakeScheduler, JobSpec, set_scheduler

    set_scheduler(FakeScheduler())
    monkeypatch.setattr(deploy, "_CANONICAL_SHARED_DIR", pod)
    monkeypatch.setattr(
        deploy, "load_network", lambda *a, **k: {"sharedDir": str(pod)},
    )
    try:
        result = deploy.DeployResult(bot_id="evolve", success=True)
        deploy._install_spec_via_seam(
            JobSpec(label="ai.evolve.evolve.admin-ui", user="evolve",
                    program_args=["/usr/bin/true"], keep_alive=True),
            result,
        )
    finally:
        set_scheduler(None)

    assert owned_jobs.load(pod) == {"ai.evolve.evolve.admin-ui"}


def test_the_writer_never_reads_the_daemon_directory(pod, monkeypatch, tmp_path):
    """The one property the whole fix rests on: a backfill seeded from what is
    on disk would launder an intruder's already-installed job into "Evolve owns
    this" — the evasion re-entering through its own repair. Nothing an
    unrecorded plist puts in /Library/LaunchDaemons may reach the registry."""
    daemons = tmp_path / "LaunchDaemons"
    daemons.mkdir()
    (daemons / "ai.evolve.impostor.plist").write_text("<plist/>")
    monkeypatch.setattr(deploy, "LAUNCHD_DIR", daemons)

    network = {"members": ["team_bot_a"], "bots": {}}
    deploy.record_owned_job_labels(deploy.expected_plist_labels(network), pod)

    assert "ai.evolve.impostor" not in (owned_jobs.load(pod) or set())
