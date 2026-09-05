"""The operator's read-only pass, and the audit wiring behind it.

The PR that adds these detectors can honestly claim they are wired in; it
cannot claim anything about what they SEE on a live pod, because no test has
that pod's ``/etc``, homes or login record. ``incursion.report`` is how the
operator produces that answer themselves — which only works if the command is
genuinely inert, so that is what these tests pin, alongside the two pieces of
audit wiring that decide whether the detectors run at all.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import audit  # noqa: E402
from incursion import baseline as baseline_store  # noqa: E402
from incursion import detectors, logins, report, run_all  # noqa: E402


@pytest.fixture
def pod(tmp_path):
    shared = tmp_path / "shared"
    (shared / "security" / "baselines").mkdir(parents=True)
    return shared


def _quiet_last():
    def fake(cmd, **kw):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
    return patch.object(logins.subprocess, "run", side_effect=fake)


def test_a_read_only_pass_writes_nothing_at_all(pod):
    """The property that makes this safe to hand an operator for a live pod:
    after a full pass there is still no baseline, no gate memo, no signal."""
    with _quiet_last():
        run_all(pod, {}, read_only=True)

    written = sorted(p for p in pod.rglob("*") if p.is_file())
    assert written == [], [str(p) for p in written]
    assert all(baseline_store.load(pod, name) is None for name, _ in detectors())


def test_the_table_says_which_detectors_have_no_baseline_yet(pod):
    """"NOT YET" is the row an operator needs most on a pod where the audit
    has not run: it distinguishes "nothing changed" from "nothing is being
    compared against anything"."""
    with _quiet_last():
        results = run_all(pod, {}, read_only=True)

    rendered = report.render(
        results, pod,
        baselines={name: False for name, _ in detectors()},
    )

    for name, _ in detectors():
        assert name in rendered
    assert "NOT YET" in rendered
    assert "wrote nothing" in rendered


def test_a_torn_baseline_reads_as_CORRUPT_not_as_a_fresh_pod(pod, monkeypatch):
    """The presence check alone cannot tell them apart — a torn file reads as
    "no baseline". NOT YET on a pod that has been running for months is the
    row an operator scrolls past; CORRUPT is the one they act on."""
    for name, _ in detectors():
        baseline_store.baseline_path(pod, name).parent.mkdir(
            parents=True, exist_ok=True)
        baseline_store.baseline_path(pod, name).write_text('{"entries": {')

    with patch.object(logins.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="")
        results = run_all(pod, {}, read_only=True)

    rendered = report.render(
        results, pod, baselines={name: False for name, _ in detectors()},
    )

    assert "CORRUPT" in rendered
    assert "baseline corrupt — not re-recording" in rendered


def test_the_table_lists_every_coverage_gap_by_name(pod):
    """A gap that is only a count in a table is a gap the operator cannot
    act on. Each one is printed with the reason underneath."""
    with patch.object(logins.subprocess, "run", side_effect=FileNotFoundError()):
        results = run_all(pod, {}, read_only=True)

    rendered = report.render(
        results, pod, baselines={name: True for name, _ in detectors()},
    )

    assert "coverage gaps — what this pod is NOT watching:" in rendered
    assert "[logins] incursion: logins coverage gap" in rendered
    assert "no `last` command at /usr/bin/last on this host" in rendered


def test_a_detector_that_raises_does_not_abort_the_pass(pod, monkeypatch):
    """One broken source must not blind the other three."""
    from incursion import pam

    monkeypatch.setattr(pam, "check", lambda *a, **kw: 1 / 0)
    with _quiet_last():
        results = dict(run_all(pod, {}, read_only=True))

    assert len(results) == 4
    assert "the detector itself failed" in results["pam"][0].message
    assert results["logins"], "the other detectors still ran"


# ── audit wiring ─────────────────────────────────────────────────────────────


def test_every_detector_is_registered_as_a_machine_check_on_both_platforms():
    """The detectors resolve their own sources per platform (``/etc/pam.d``
    and ``last`` are identical on both; the job inventory swaps launchd roots
    for systemd + cron), so none of them is macOS-only — and a check gated to
    one platform is silently skipped on the other, which is how a detector
    ships and never runs."""
    registered = {
        name: applies for name, _run, applies in audit._MACHINE_CHECKS
        if name.startswith("incursion_")
    }

    assert set(registered) == {
        f"incursion_{name}" for name, _ in detectors()
    }
    for name, applies in registered.items():
        assert applies == audit._ALL_PLATFORMS, name


def test_reset_baselines_reaches_the_incursion_baselines(pod):
    """Every detector's fix_steps tell the operator that deleting its
    baseline is the "accept current state" path. ``--reset-baselines`` has to
    reach them too, or the flag that exists to do exactly that leaves the
    four newest baselines behind."""
    for name, _ in detectors():
        baseline_store.save(pod, name, {"whatever": "x"})

    audit._reset_baselines(pod)

    assert all(baseline_store.load(pod, name) is None for name, _ in detectors())
