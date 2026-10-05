"""Tests for launchd_reload — the on-disk-but-unloaded daemon repair sweep.

Origin: 2026-09-14, mini pod. 8 of 9 ``ai.openclaw.evolve.doctor-pass.<bot>``
LaunchDaemons had been booted out since 2026-09-07 — plists present and
untouched on disk, absent from ``launchctl list`` — and nothing repaired them
because ``health.py::_check_launchd`` only REPORTS that state. Seven nights of
per-bot ``openclaw doctor --fix`` silently did not run.

Every collaborator is injected (``_deps``); no subprocess is ever spawned.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "analyzer"))

import launchd_reload  # noqa: E402


# ── doubles ──────────────────────────────────────────────────────────────────

class FakeProfile:
    def __init__(self, name="macos"):
        self.name = name


class FakeScheduler:
    """Minimal Scheduler stand-in: a loaded set + an on-disk artifact dir."""

    def __init__(self, *, loaded, artifact_dir, bootstrap_loads=True,
                 status_errors=()):
        self._loaded = set(loaded)
        self._dir = Path(artifact_dir)
        self._bootstrap_loads = bootstrap_loads
        self._status_errors = set(status_errors)
        self.bootstrapped: list[str] = []

    def artifact_path(self, label):
        return str(self._dir / f"{label}.plist")

    def status(self, label, **_kw):
        if label in self._status_errors:
            return {"installed": False, "managed": False,
                    "status_error": "cannot_escalate"}
        return {"installed": (self._dir / f"{label}.plist").exists(),
                "managed": label in self._loaded,
                "status_error": None}

    # Stand-in for health._execute_fix's launchctl_bootstrap branch.
    def fake_execute_fix(self, fix_args):
        label = fix_args["label"]
        assert fix_args["action"] == "launchctl_bootstrap"
        self.bootstrapped.append(label)
        if not (self._dir / f"{label}.plist").exists():
            return False, "Plist not found"
        if self._bootstrap_loads:
            self._loaded.add(label)
            return True, f"bootstrap {label}"
        return False, "Load failed: 5: Input/output error"


def _mk(tmp_path, labels_on_disk):
    d = tmp_path / "LaunchDaemons"
    d.mkdir(exist_ok=True)
    for lab in labels_on_disk:
        (d / f"{lab}.plist").write_text("<plist/>")
    return d


def _deps(scheduler, expected, *, profile="macos", gated=()):
    return {
        "profile": FakeProfile(profile),
        "scheduler": scheduler,
        "expected_labels": set(expected),
        "is_gated_off": lambda label, _ij=None: label in gated,
        "execute_fix": scheduler.fake_execute_fix,
    }


DP = "ai.openclaw.evolve.doctor-pass.{}"


# ── the incident, reproduced ─────────────────────────────────────────────────

def test_repairs_the_2026_09_07_doctor_pass_bootout(tmp_path):
    """8 of 9 on disk but unloaded → all 8 bootstrapped, the 9th left alone."""
    bots = ["bot-a", "bot-b", "bot-c", "bot-d", "bot-e", "bot-f", "bot-g",
            "bot-h", "bot-i"]
    labels = [DP.format(b) for b in bots]
    d = _mk(tmp_path, labels)
    sched = FakeScheduler(loaded=[DP.format("bot-a")], artifact_dir=d)

    res = launchd_reload.sweep(
        {"sharedDir": str(tmp_path)}, tmp_path,
        _deps=_deps(sched, labels),
    )

    assert res.skipped is None
    assert len(res.unloaded) == 8
    assert DP.format("bot-a") not in res.unloaded
    assert sorted(res.repaired) == sorted(l for l in labels
                                          if l != DP.format("bot-a"))
    assert res.newly_parked == []


def test_all_loaded_is_a_quiet_noop(tmp_path):
    labels = [DP.format(b) for b in ("bot-a", "bot-b")]
    d = _mk(tmp_path, labels)
    sched = FakeScheduler(loaded=labels, artifact_dir=d)

    res = launchd_reload.sweep({}, tmp_path, _deps=_deps(sched, labels))

    assert res.unloaded == []
    assert res.checked == 2
    assert sched.bootstrapped == []


# ── the boundaries that keep this from doing damage ──────────────────────────

def test_missing_artifact_is_deploys_problem_not_ours(tmp_path):
    """A label with no plist on disk is never bootstrapped.

    "Not installed" is a deploy concern. Re-running deploy from a 5-minute
    heal cycle would be a loop, not a repair.
    """
    present, absent = DP.format("bot-a"), DP.format("bot-gone")
    d = _mk(tmp_path, [present])
    sched = FakeScheduler(loaded=[], artifact_dir=d)

    res = launchd_reload.sweep(
        {}, tmp_path, _deps=_deps(sched, [present, absent]),
    )

    assert res.unloaded == [present]
    assert sched.bootstrapped == [present]
    assert absent not in sched.bootstrapped


def test_feature_gated_off_label_is_skipped(tmp_path):
    """An intentionally-absent feature plist is not a fault to repair."""
    gated = "ai.openclaw.evolve.upstream-issues-watcher"
    live = DP.format("bot-a")
    d = _mk(tmp_path, [live, gated])
    sched = FakeScheduler(loaded=[], artifact_dir=d)

    res = launchd_reload.sweep(
        {}, tmp_path,
        _deps=_deps(sched, [live, gated], gated=(gated,)),
    )

    assert res.unloaded == [live]
    assert res.checked == 1          # the gated label isn't even counted
    assert gated not in sched.bootstrapped


def test_disabled_job_is_not_resurrected(tmp_path):
    """A deliberately ``launchctl disable``-d job stays down.

    The repair is a PLAIN bootstrap, and launchd refuses to bootstrap a
    disabled label — so the boundary is enforced by launchd itself. This test
    pins the consequence: if someone "simplifies" the repair to
    ``Scheduler.enable()`` (which clears the disable override DB first), a
    paused job would be silently un-paused and this goes red.
    """
    label = DP.format("bot-a")
    d = _mk(tmp_path, [label])
    # bootstrap_loads=False models launchd's refusal for a disabled label.
    sched = FakeScheduler(loaded=[], artifact_dir=d, bootstrap_loads=False)

    res = launchd_reload.sweep({}, tmp_path, _deps=_deps(sched, [label]))

    assert res.repaired == []
    assert [o.result for o in res.outcomes] == ["failed"]


def test_probe_failure_is_reported_not_acted_on(tmp_path):
    """``status_error`` means "unknown", never "absent".

    Acting on a failed probe is how a sudo hiccup becomes a pod-wide
    bootstrap storm. Same rule as PR #1579: keep a tooling failure
    distinguishable from a finding. This matters concretely here — a bare
    ``launchctl list`` is NOT in the evolve user's sudo grant, so probe
    failures are a live possibility, not a theoretical one.
    """
    labels = [DP.format(b) for b in ("bot-a", "bot-b", "bot-c")]
    d = _mk(tmp_path, labels)
    sched = FakeScheduler(loaded=[], artifact_dir=d, status_errors=labels)

    res = launchd_reload.sweep({}, tmp_path, _deps=_deps(sched, labels))

    assert res.unloaded == []
    assert len(res.unprobed) == 3
    assert sched.bootstrapped == []


def test_one_unprobeable_label_does_not_block_the_others(tmp_path):
    """A single failed probe must not stop the rest of the sweep."""
    good, bad = DP.format("bot-a"), DP.format("bot-b")
    d = _mk(tmp_path, [good, bad])
    sched = FakeScheduler(loaded=[], artifact_dir=d, status_errors=[bad])

    res = launchd_reload.sweep({}, tmp_path, _deps=_deps(sched, [good, bad]))

    assert res.repaired == [good]
    assert len(res.unprobed) == 1


def test_non_macos_profile_skips(tmp_path):
    label = DP.format("bot-a")
    d = _mk(tmp_path, [label])
    sched = FakeScheduler(loaded=[], artifact_dir=d)

    res = launchd_reload.sweep(
        {}, tmp_path, _deps=_deps(sched, [label], profile="linux"),
    )

    assert res.skipped is not None and "linux" in res.skipped
    assert sched.bootstrapped == []


# ── honesty: attempted ≠ repaired ────────────────────────────────────────────

def test_bootstrap_success_without_load_is_not_reported_as_repaired(tmp_path):
    """rc=0 is not "the job is loaded" — verify, then report."""
    label = DP.format("bot-a")
    d = _mk(tmp_path, [label])
    sched = FakeScheduler(loaded=[], artifact_dir=d)
    # Succeeds, but the label never becomes managed.
    sched.fake_execute_fix = lambda fa: (True, "bootstrap ok")

    res = launchd_reload.sweep({}, tmp_path, _deps=_deps(sched, [label]))

    assert res.repaired == []
    assert [o.result for o in res.outcomes] == ["attempted"]


# ── attempt cap ──────────────────────────────────────────────────────────────

def test_parks_after_max_attempts_and_escalates_once(tmp_path):
    """A unit that cannot load stops being hammered, and alerts exactly once."""
    label = DP.format("bot-a")
    d = _mk(tmp_path, [label])
    sched = FakeScheduler(loaded=[], artifact_dir=d, bootstrap_loads=False)
    deps = _deps(sched, [label])

    parked_on = []
    for cycle in range(launchd_reload.MAX_ATTEMPTS + 2):
        res = launchd_reload.sweep({}, tmp_path, _deps=deps)
        if res.newly_parked:
            parked_on.append(cycle)

    # Escalated on exactly one cycle — the one that exhausted the budget.
    assert parked_on == [launchd_reload.MAX_ATTEMPTS - 1]
    # And stopped trying after that.
    assert len(sched.bootstrapped) == launchd_reload.MAX_ATTEMPTS
    assert res.outcomes[0].result == "parked"


def test_recovery_clears_the_strike_ledger(tmp_path):
    """A label that comes back healthy gets a fresh budget next time."""
    label = DP.format("bot-a")
    d = _mk(tmp_path, [label])

    failing = FakeScheduler(loaded=[], artifact_dir=d, bootstrap_loads=False)
    launchd_reload.sweep({}, tmp_path, _deps=_deps(failing, [label]))
    assert launchd_reload.load_state(tmp_path)[label]["attempts"] == 1

    healthy = FakeScheduler(loaded=[], artifact_dir=d)
    res = launchd_reload.sweep({}, tmp_path, _deps=_deps(healthy, [label]))

    assert res.repaired == [label]
    assert label not in launchd_reload.load_state(tmp_path)


def test_dry_run_touches_nothing(tmp_path):
    label = DP.format("bot-a")
    d = _mk(tmp_path, [label])
    sched = FakeScheduler(loaded=[], artifact_dir=d)

    res = launchd_reload.sweep(
        {}, tmp_path, dry_run=True, _deps=_deps(sched, [label]),
    )

    assert sched.bootstrapped == []
    assert [o.result for o in res.outcomes] == ["attempted"]
    assert not (tmp_path / launchd_reload.STATE_RELPATH).exists()


# ── degradation ──────────────────────────────────────────────────────────────

def test_unavailable_dependencies_skip_rather_than_raise(tmp_path, monkeypatch):
    """heal must survive a missing admin package, not crash mid-cycle."""
    def boom():
        raise ImportError("evolve_admin not on path")

    monkeypatch.setattr(launchd_reload, "_load_admin_deps", boom)
    sched = FakeScheduler(loaded=[], artifact_dir=_mk(tmp_path, []))

    res = launchd_reload.sweep(
        {}, tmp_path,
        _deps={"profile": FakeProfile(), "scheduler": sched},
    )
    assert res.skipped is not None and "evolve_admin" in res.skipped


def test_corrupt_state_file_fails_open(tmp_path):
    (tmp_path / "launchd_reload").mkdir()
    (tmp_path / launchd_reload.STATE_RELPATH).write_text("{not json")

    assert launchd_reload.load_state(tmp_path) == {}


def test_state_roundtrips(tmp_path):
    launchd_reload.save_state(tmp_path, {"a": {"attempts": 2}})
    assert launchd_reload.load_state(tmp_path)["a"]["attempts"] == 2
    # Written atomically as readable JSON, not a pickle or a bare string.
    assert json.loads((tmp_path / launchd_reload.STATE_RELPATH).read_text())


# ── catalog wiring ───────────────────────────────────────────────────────────

def test_catalog_events_exist_and_render_from_sample_payloads():
    """Both heal-side events must render — a missing placeholder is a
    runtime KeyError on a pod at the exact moment it is trying to report."""
    from evolve_admin.alerts.catalog import CATALOG

    by_key = {e.key: e for e in CATALOG}
    for key in ("system.daemon_reloaded", "system.daemon_reload_failed"):
        ev = by_key[key]
        assert ev.producer_source == "heal"
        ev.body_template.format(**ev.sample_payload)
        if ev.action is not None:
            getattr(ev.action, "command", "").format(**ev.sample_payload)
