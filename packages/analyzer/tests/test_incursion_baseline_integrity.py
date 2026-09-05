"""The two state files the incursion detectors depend on, and how they fail.

A detector is only as trustworthy as the state it compares against. Review
#3967 found both of these state files failing in the same direction — toward
silently adopting whatever is on the host right now as expected:

* a baseline written with a bare ``write_text`` tears on a crash, and a torn
  baseline read back as "not established" makes the next pass a *first run*
  that records current state with an informational row (finding 3);
* the owned-job registry did not exist at all, so a scheduled job was blessed
  by the shape of its name (finding 1).

These tests pin the state files themselves. The detector-level consequences
live in ``test_incursion_job_inventory.py``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from incursion import authorized_keys, job_inventory, logins, pam  # noqa: E402
from incursion import baseline as baseline_store  # noqa: E402
from incursion import owned_jobs  # noqa: E402


@pytest.fixture
def pod(tmp_path):
    shared = tmp_path / "shared"
    (shared / "security" / "baselines").mkdir(parents=True)
    return shared


# ── baseline: atomic write ───────────────────────────────────────────────────

def test_a_saved_baseline_is_readable_and_leaves_no_temp_behind(pod):
    assert baseline_store.save(pod, "demo", {"a": "1"})

    assert baseline_store.load(pod, "demo") == {"a": "1"}
    assert list(baseline_store.baseline_path(pod, "demo").parent.glob("*.tmp")) == []


def test_a_crash_between_the_temp_and_the_replace_leaves_the_old_baseline(
    pod, monkeypatch,
):
    """The whole point of temp + os.replace. With a bare ``write_text`` the
    same crash leaves a truncated document under the real name, which reads
    back as "no baseline" and re-records the host's current state."""
    baseline_store.save(pod, "demo", {"original": "value"})
    path = baseline_store.baseline_path(pod, "demo")

    def die(src, dst):
        raise KeyboardInterrupt("power cut between the write and the rename")

    monkeypatch.setattr(baseline_store.os, "replace", die)
    with pytest.raises(KeyboardInterrupt):
        baseline_store.save(pod, "demo", {"replacement": "value"})

    # The real file is untouched and still parses.
    assert baseline_store.load(pod, "demo") == {"original": "value"}
    assert json.loads(path.read_text())["entries"] == {"original": "value"}


def test_a_failed_write_removes_its_own_temp_file(pod, monkeypatch):
    """A temp file left beside the baseline would be read by nothing and
    pruned by nothing — the sediment the footprint contract exists to stop."""
    baseline_store.save(pod, "demo", {"a": "1"})

    def boom(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(baseline_store.os, "replace", boom)
    assert baseline_store.save(pod, "demo", {"b": "2"}) is False

    assert list(baseline_store.baseline_path(pod, "demo").parent.glob("*.tmp")) == []


# ── baseline: a torn file is a gap, not a fresh start ────────────────────────

@pytest.mark.parametrize("content", [
    "",                                    # crash before any bytes landed
    '{"version": 1, "entries": {"a": ',    # crash mid-document
    "not json at all",
    '{"version": 1}',                      # parses, but is not a baseline
    '["a", "b"]',                          # parses, wrong shape entirely
])
def test_an_unparseable_baseline_is_reported_and_not_replaced(pod, content):
    path = baseline_store.baseline_path(pod, "demo")
    path.write_text(content)

    state = baseline_store.read(pod, "demo")

    assert state.entries is None
    assert state.corrupt
    # Untouched: reading a broken baseline must not repair it silently.
    assert path.read_text() == content


def test_an_absent_baseline_is_not_a_corrupt_one(pod):
    """The distinction the whole fix turns on. Both make ``entries`` None; only
    one of them is allowed to record."""
    state = baseline_store.read(pod, "demo")

    assert state.entries is None
    assert state.corrupt == ""


@pytest.mark.parametrize("detector,name,kwargs", [
    (authorized_keys.check, "authorized_keys", {"homes": {}}),
    (pam.check, "pam", {}),
    (job_inventory.check, "job_inventory", {}),
    (logins.check, "logins", {}),
])
def test_every_detector_treats_a_torn_baseline_as_a_coverage_gap(
    pod, tmp_path, monkeypatch, detector, name, kwargs,
):
    """Named per detector, because the failure is per detector: whichever one
    tears is the one that goes quiet about its own surface."""
    # Give each detector one readable source so it gets past the read == 0
    # guard and actually reaches its baseline.
    home = tmp_path / "home" / "pod_admin_user"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "authorized_keys").write_text("")
    kwargs = dict(kwargs)
    if name == "authorized_keys":
        kwargs["homes"] = {"pod_admin_user": home}
    if name == "pam":
        pam_dir = tmp_path / "pam.d"
        pam_dir.mkdir()
        (pam_dir / "sudo").write_text("auth required pam_opendirectory.so\n")
        kwargs.update(pam_dir=pam_dir, pam_conf=tmp_path / "no-pam.conf")
    if name == "job_inventory":
        daemons = tmp_path / "LaunchDaemons"
        daemons.mkdir()
        kwargs["roots"] = [job_inventory.JobRoot("cron", daemons)]
    if name == "logins":
        import subprocess as _sp
        monkeypatch.setattr(logins.subprocess, "run", lambda *a, **k: _sp.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        ))

    path = baseline_store.baseline_path(pod, name)
    path.write_text('{"version": 1, "entries": {"a": ')

    observations = detector(pod, {}, **kwargs)

    corrupt_rows = [o for o in observations if "baseline corrupt" in o.message]
    assert len(corrupt_rows) == 1, [o.message for o in observations]
    assert corrupt_rows[0].level == "warn"
    assert "coverage gap" in corrupt_rows[0].message
    assert "not re-recording" in corrupt_rows[0].message
    # And, the point of the whole thing: the file was NOT re-recorded.
    assert path.read_text() == '{"version": 1, "entries": {"a": '


def test_the_corrupt_row_tells_the_operator_how_to_start_over(pod):
    """A gap with no way out trains the operator to ignore it. Starting over
    stays deliberate — delete the file — because the state a re-record would
    adopt is exactly what is in question."""
    observation = baseline_store.corrupt_observation("demo", pod, "the file is empty")

    assert str(baseline_store.baseline_path(pod, "demo")) in observation.fix_steps
    assert "delete" in observation.fix_steps
    assert "incursion.report" in observation.fix_steps


# ── owned-job registry ───────────────────────────────────────────────────────

def test_an_absent_registry_reads_as_none_not_as_empty(pod):
    """``None`` is "the installer has recorded nothing here"; ``set()`` would
    be "it recorded an empty list". Only the first is worth a gap row."""
    assert owned_jobs.load(pod) is None


def test_recording_unions_rather_than_replacing(pod):
    """A partial deploy — one bot, one app, a run whose network.json failed to
    load — must not un-own every label it did not happen to mention. That would
    page for a dozen of Evolve's own daemons on the next audit tick."""
    owned_jobs.record(pod, ["ai.evolve.evolve.heal", "ai.evolve.evolve.audit"])
    owned_jobs.record(pod, ["ai.evolve.evolve.admin-ui"])

    assert owned_jobs.load(pod) == {
        "ai.evolve.evolve.heal",
        "ai.evolve.evolve.audit",
        "ai.evolve.evolve.admin-ui",
    }


def test_a_crash_between_the_temp_and_the_replace_keeps_the_old_registry(
    pod, monkeypatch,
):
    """A torn registry would read as "no registry", and the detector would
    then treat every one of Evolve's own labels as unowned."""
    owned_jobs.record(pod, ["ai.evolve.evolve.heal"])

    def die(src, dst):
        raise KeyboardInterrupt("power cut between the write and the rename")

    monkeypatch.setattr(owned_jobs.os, "replace", die)
    with pytest.raises(KeyboardInterrupt):
        owned_jobs.record(pod, ["ai.evolve.evolve.audit"])

    assert owned_jobs.load(pod) == {"ai.evolve.evolve.heal"}


def test_the_temp_name_is_fixed_so_a_crash_cannot_accumulate_files(pod, monkeypatch):
    """A real power cut leaves the staged temp behind — nothing can clean up
    after a kill -9. What it must not do is leave a NEW file each time: one
    deterministic name means the residue is bounded at a single stale blob
    that the next successful write reuses."""
    def die(src, dst):
        raise KeyboardInterrupt("power cut")

    monkeypatch.setattr(owned_jobs.os, "replace", die)
    for label in ("ai.evolve.a", "ai.evolve.b", "ai.evolve.c"):
        with pytest.raises(KeyboardInterrupt):
            owned_jobs.record(pod, [label])

    leftovers = list(owned_jobs.registry_path(pod).parent.glob("*.tmp"))
    assert len(leftovers) == 1
    # And it is not mistaken for the registry itself.
    assert owned_jobs.load(pod) is None


def test_a_torn_registry_reads_as_no_registry(pod):
    """Which is the fail-toward-paging direction: nothing is owned, the
    detector says so, and a new ai.evolve.* job pages instead of being
    absorbed by a file nobody can parse."""
    owned_jobs.registry_path(pod).parent.mkdir(parents=True, exist_ok=True)
    owned_jobs.registry_path(pod).write_text('{"version": 1, "labels": [')

    assert owned_jobs.load(pod) is None


def test_recording_nothing_does_not_create_a_registry(pod):
    """An empty write would turn "not recorded yet" into "recorded, empty" and
    silence the gap row without recording a single label."""
    assert owned_jobs.record(pod, []) is True

    assert not owned_jobs.registry_path(pod).exists()


def test_an_unwritable_registry_is_a_false_return_not_an_exception(pod, monkeypatch):
    """The write is best-effort by contract: a deploy must never fail because
    a detector's lookup file could not be written."""
    monkeypatch.setattr(owned_jobs.os, "replace", lambda *a: (_ for _ in ()).throw(
        OSError("read-only file system"),
    ))

    assert owned_jobs.record(pod, ["ai.evolve.evolve.heal"]) is False
