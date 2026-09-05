"""incursion.logins — a session from a source this pod has never seen pages.

The pod has no interactive users in the normal course of a day, which is what
makes its own login record high signal: a new ``(account, source)`` pair is
either the operator on a new machine or it is somebody else. Nothing read that
record before this detector.

The scenarios below are the four the brief pins, plus the two properties that
decide whether the detector survives a month on a real host: ``last``'s
optional remote-host field must be parsed by structure rather than position,
and wtmp rotation must never look like a new source.
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

from incursion import baseline as baseline_store  # noqa: E402
from incursion import logins  # noqa: E402

BASELINE_OUTPUT = """\
pod_admin_user ttys000  198.51.100.10   Mon Sep  1 09:12   still logged in
pod_admin_user console                  Mon Sep  1 08:00 - 08:30  (00:30)
reboot    ~                             Mon Sep  1 07:59

wtmp begins Mon Sep  1 07:59
"""

NEW_SOURCE_OUTPUT = BASELINE_OUTPUT + (
    "pod_admin_user ttys001  203.0.113.77    Tue Sep  2 02:41 - 03:10  (00:29)\n"
)


@pytest.fixture
def pod(tmp_path):
    shared = tmp_path / "shared"
    (shared / "security" / "baselines").mkdir(parents=True)
    return shared


def _patch_last(stdout: str, returncode: int = 0, stderr: str = ""):
    def fake(cmd, **kw):
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout=stdout, stderr=stderr,
        )
    return patch.object(logins.subprocess, "run", side_effect=fake)


def _criticals(observations):
    return [o for o in observations if o.level == "critical"]


def test_first_run_records_the_baseline_and_does_not_page(pod):
    """Every historical login on a pod being set up would otherwise page at
    once — the worst possible first impression of a security check."""
    with _patch_last(BASELINE_OUTPUT):
        observations = logins.check(pod, None)

    assert [o.level for o in observations] == ["ok"]
    assert "baseline recorded, 2 entries" in observations[0].message


def test_unchanged_pass_reports_nothing_actionable(pod):
    with _patch_last(BASELINE_OUTPUT):
        logins.check(pod, None)
        observations = logins.check(pod, None)

    assert [o.level for o in observations] == ["ok"]
    assert "known (user, source) pair(s)" in observations[0].message


def test_a_login_from_a_new_source_is_an_event(pod):
    with _patch_last(BASELINE_OUTPUT):
        logins.check(pod, None)
    with _patch_last(NEW_SOURCE_OUTPUT):
        observations = logins.check(pod, None)

    criticals = _criticals(observations)
    assert len(criticals) == 1, [o.message for o in observations]
    assert criticals[0].finding_kind == "event"
    assert "pod_admin_user" in criticals[0].message
    assert "203.0.113.77" in criticals[0].message


def test_the_finding_stands_until_the_operator_acts(pod):
    """A login is a past event, so there is a temptation to absorb the pair
    and move on. That would clear the Alerts row on the next 15-minute cycle
    while the operator was still deciding what to do about it. The finding
    repeats; page-on-transition (R-1) is what keeps it from re-paging."""
    with _patch_last(BASELINE_OUTPUT):
        logins.check(pod, None)
    with _patch_last(NEW_SOURCE_OUTPUT):
        logins.check(pod, None)
        again = logins.check(pod, None)

    assert len(_criticals(again)) == 1


def test_log_rotation_does_not_invent_a_new_source(pod):
    """wtmp rotates. If the baseline were re-recorded from "what ``last``
    reports today", a rotation would drop known-good pairs and the
    operator's own laptop would page as brand new the next time they
    connected — the detector crying wolf about its own log."""
    with _patch_last(NEW_SOURCE_OUTPUT):
        logins.check(pod, None)

    rotated = "pod_admin_user ttys002  198.51.100.10   Wed Sep  3 10:00   still logged in\n"
    with _patch_last(rotated):
        after_rotation = logins.check(pod, None)
    with _patch_last(NEW_SOURCE_OUTPUT):
        when_it_returns = logins.check(pod, None)

    assert _criticals(after_rotation) == []
    assert _criticals(when_it_returns) == []


def test_last_missing_is_a_coverage_gap_not_a_crash(pod):
    """A minimal container image may not ship util-linux at all. The detector
    says the login record is unreadable rather than reporting a clean pod."""
    with patch.object(logins.subprocess, "run", side_effect=FileNotFoundError()):
        observations = logins.check(pod, None)

    assert [o.level for o in observations] == ["warn"]
    assert "coverage gap" in observations[0].message
    assert "no `last` command at /usr/bin/last" in observations[0].detail
    assert baseline_store.load(pod, "logins") is None


def test_last_is_invoked_by_absolute_path(pod):
    """CLAUDE.md's path table asks for it, and a security detector has the
    stronger reason: a bare ``last`` resolves through PATH, so whoever can put
    a file earlier on this process's PATH chooses what "read the login record"
    executes."""
    with patch.object(logins.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=BASELINE_OUTPUT, stderr="",
        )
        logins.check(pod, None)

    assert run.call_args.args[0][0] == "/usr/bin/last"


def test_a_failing_last_reports_its_own_exit_status(pod):
    with _patch_last("", returncode=1, stderr="last: /var/log/wtmp: Permission denied"):
        observations = logins.check(pod, None)

    assert observations[0].level == "warn"
    assert "Permission denied" in observations[0].detail


def test_the_optional_remote_host_field_is_parsed_by_structure():
    """``last`` puts the remote host between the tty and the date and
    delimits neither. The date always starts with a weekday, so the fields
    before it ARE the source — and when there are none the session was
    local. A positional parser gets one of those two shapes wrong."""
    pairs = logins.parse_last(BASELINE_OUTPUT)

    assert ("pod_admin_user", "198.51.100.10") in pairs
    assert ("pod_admin_user", "local") in pairs
    # Boots and the trailing banner are not logins.
    assert all(user == "pod_admin_user" for user, _ in pairs)


def test_read_only_pass_writes_no_baseline(pod):
    with _patch_last(BASELINE_OUTPUT):
        logins.check(pod, None, read_only=True)

    assert baseline_store.load(pod, "logins") is None
