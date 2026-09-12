"""incursion — baseline-diff detectors for the persistence moves nobody watched.

Brief: internal/dispatch/done/incursion-baseline-detectors.md (operator
priority, 2026-09-01: "not missing major incursions").

What this package closes
========================

A coverage read of ``audit.py`` on 2026-09-01 showed what pages today for a
real intrusion — a gateway running as the pod admin, a NEW local user account,
a credential sitting in a bot workspace, upstream OpenClaw criticals — and,
next to it, four moves that cost an attacker nothing and were watched by
nothing at all:

1. **An added ``~/.ssh/authorized_keys`` line.** One appended line buys
   passwordless re-entry as that user, forever, and survives every password
   rotation. Nothing on the pod looked at those files.
2. **``/etc/pam.d`` drift.** The authentication stack itself. A single edited
   module line turns ``sudo`` or ``sshd`` into a no-op check.
3. **A stranger's scheduled job.** ``audit_cron_health`` checks the health of
   *Evolve's own* cron entries; ``audit_process`` catches a suspicious process
   only while it happens to be running. Neither notices a new LaunchDaemon or
   systemd unit that re-launches the attacker's code on every boot.
4. **A login from somewhere new.** Nothing read the host's own interactive
   login records, so an SSH session from an unfamiliar address was invisible
   even in hindsight.

Each detector here follows the shape ``_check_user_accounts`` already
established: survey a source, diff it against a stored baseline, and emit an
``event``-classified critical for an unexplained addition. None of them
mutates the host.

The four properties every detector in here holds to
===================================================

* **Read-only.** Nothing runs a repair, kills a process, edits a key file or
  asks for a privilege it does not already have. The only writes are this
  package's own baselines under ``{shared_dir}/security/baselines/``.
* **Never vacuous.** A source the detector cannot read produces a *coverage
  gap* observation naming the source and the reason — never an empty green.
  A pass where every source was unreadable emits gaps and no "OK" row at all,
  so "we saw nothing" can never be misread as "there was nothing to see".
* **Never a page on a fresh pod.** The first run of each detector records the
  baseline and says so ("baseline recorded, N entries"). A pod that has just
  been installed is not an incursion.
* **Explained changes are absorbed, unexplained ones page.** Every difference
  goes to the L2 authorized-change gate (``drift_authorization``). An
  explained change becomes an informational row AND is written into the
  baseline — the authorized state is now the expected state, so it never
  re-pages once the gate's window closes. An unexplained change is an
  ``event``-classified critical and is deliberately NOT absorbed, so it keeps
  firing every cycle until the operator removes it or reblesses the baseline.

Why ``Observation`` and not ``audit.Finding``
=============================================

``Finding`` lives in ``audit.py``, and ``audit.py`` is what calls these
detectors — importing it back would be a cycle. More usefully, keeping the
detectors free of that import is what lets ``incursion.report`` run a full
read-only pass on a live pod without touching the audit's alert dispatch,
signal store or baselines. ``audit._incursion_check`` does the one-line
translation at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "CoverageGap",
    "Observation",
    "Survey",
    "detectors",
    "run_all",
]


@dataclass(frozen=True)
class Observation:
    """One thing a detector has to say, in the terms ``audit.Finding`` needs.

    ``level`` uses the audit's own vocabulary — ``"critical"`` (pages),
    ``"warn"`` (Alerts board), ``"ok"`` (audit log only). ``finding_kind`` is
    R-3's event-vs-posture classification and is REQUIRED on a critical;
    ``audit.Finding.__post_init__`` rejects a critical without one, so an
    unclassified critical here is a loud failure rather than a quiet one.
    """

    level: str
    message: str
    detail: str = ""
    what_it_means: str = ""
    fix_steps: str = ""
    finding_kind: str = ""


@dataclass(frozen=True)
class CoverageGap:
    """A source the detector was supposed to read and could not.

    ``source`` is the path or command as the operator would type it;
    ``why`` is the reason in plain words. Both end up in the finding, because
    a gap the operator cannot act on is only marginally better than silence.
    """

    source: str
    why: str


@dataclass
class Survey:
    """What one pass over a detector's sources found.

    ``entries``  stable key → value. The key identifies the thing (a user's
                 key fingerprint, a PAM file's name, a job label, a
                 ``user@source`` login pair); the value is what changing it
                 would mean (a key comment, a file hash, a program argv).
                 Both halves are diffed: a new key is an addition, a new
                 value under a known key is a change.
    ``labels``   key → one operator-legible line about that entry, used in
                 the finding text so the operator does not have to go read
                 the baseline to find out what fired.
    ``gaps``     sources that could not be read.
    ``read``     how many sources were successfully read. Zero means the pass
                 proves nothing, and the caller must not emit an "OK" row.
    """

    entries: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    gaps: list[CoverageGap] = field(default_factory=list)
    read: int = 0

    def gap(self, source: str, why: str) -> None:
        self.gaps.append(CoverageGap(source, why))

    def add(self, key: str, value: str, label: str = "") -> None:
        self.entries[key] = value
        if label:
            self.labels[key] = label


def detectors() -> tuple[tuple[str, Callable[..., list[Observation]]], ...]:
    """``(name, check)`` for every detector, in the order a full pass runs them.

    ``name`` is the stable id used by the baseline filename, the audit's
    machine-check table and the operator report's coverage table — renaming
    one orphans its baseline, so don't.
    """
    # Imported lazily so a broken detector module degrades to "that one
    # detector is missing" rather than taking the whole audit's import down.
    from incursion import authorized_keys, job_inventory, logins, pam

    return (
        ("authorized_keys", authorized_keys.check),
        ("pam", pam.check),
        ("job_inventory", job_inventory.check),
        ("logins", logins.check),
    )


def run_all(
    shared_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    read_only: bool = False,
) -> list[tuple[str, list[Observation]]]:
    """Run every detector, returning ``(name, observations)`` per detector.

    ``read_only`` suppresses BOTH writes a pass would otherwise make: the
    baseline update and the authorized-change gate's explanation memo. It is
    what ``incursion.report`` uses so an operator can rehearse a full pass on
    a live pod and change nothing.

    A detector that raises is reported as its own coverage gap rather than
    aborting the pass — one broken source must not blind the other three.
    """
    results: list[tuple[str, list[Observation]]] = []
    for name, check in detectors():
        try:
            results.append((name, check(shared_dir, config, read_only=read_only)))
        except Exception as exc:  # noqa: BLE001 — a detector fault is a coverage gap
            results.append((name, [Observation(
                level="warn",
                message=(
                    f"incursion: {name} coverage gap: the detector itself "
                    f"failed ({type(exc).__name__})"
                ),
                detail=str(exc)[:300],
            )]))
    return results
