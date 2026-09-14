"""incursion.baseline — the baseline store and diff every detector shares.

One file per detector under ``{shared_dir}/security/baselines/``, named
``incursion-<detector>.json``, alongside the audit's existing per-bot
baselines (``scripts.json``, ``cron-jobs.json``, …) so an operator looking
for "where is the state this alert compares against" finds all of it in one
directory.

Shape::

    {"version": 1,
     "recorded_at": "2026-09-02T10:15:00Z",
     "entries": {"<key>": "<value>"}}

``entries`` is deliberately a flat string→string map: every detector's key is
already a composite it built itself (``<user>:SHA256:…``, ``pam.d/sudo``,
``launchd:ai.evolve.evolve.heal``, ``pod-admin-user@203.0.113.7``), so the
store never has to know what any of them mean. That is what lets one diff and
one first-run path serve all four.

Absorption rule (the part that decides whether a finding ever clears)
=====================================================================

An **explained** change is written into the baseline. The gate said an
authorized event accounts for it, so the new state IS the expected state; not
absorbing it would re-page the moment the gate's window closed, which is the
exact "explained today, unexplained tomorrow" trap that would make these
detectors unrunnable.

An **unexplained** change is NOT written. It keeps firing on every audit cycle
until the operator removes the thing or reblesses the baseline. A detector
that quietly adopted an unexplained key would page once and then help the
attacker keep it.

A torn baseline is a coverage gap, never a fresh start
=====================================================

Two rules keep a half-written file from re-baselining a compromised host.

*Writes are atomic* — temp file plus :func:`os.replace`, the pattern
``audit.py``'s snapshot writes already use. A crash between the two leaves the
previous baseline intact and the temp file beside it, so there is no window in
which the detector's own state file is half a JSON document.

*A file that will not parse is reported, not replaced.* :func:`read`
distinguishes "no baseline yet" from "a baseline exists and is unusable". The
first is the fresh-pod path that records and says so; the second is a coverage
gap — the detector refuses to re-record, because a corrupt file that silently
became a first run would bake whatever is on the host RIGHT NOW into the new
baseline, with one info row to show for it. Starting over stays a deliberate
act: the operator deletes the baseline file.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from incursion import Observation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from incursion import CoverageGap, Survey

logger = logging.getLogger(__name__)

BASELINE_VERSION = 1


def baseline_path(shared_dir: Path, name: str) -> Path:
    """Where detector ``name`` keeps its baseline."""
    return Path(shared_dir) / "security" / "baselines" / f"incursion-{name}.json"


@dataclass(frozen=True)
class BaselineRead:
    """The three states a detector's baseline file can be in.

    ``entries`` is the stored map, or ``None`` when there is nothing usable.
    ``corrupt`` is empty unless a file IS there and cannot be used — then it
    carries the reason, in the words the coverage-gap row will print. The two
    fields are never both meaningful: a corrupt read has ``entries is None``.
    """

    entries: dict[str, str] | None
    corrupt: str = ""


def read(shared_dir: Path, name: str) -> BaselineRead:
    """Read the baseline, keeping "absent" and "unusable" apart.

    Absent (``BaselineRead(None)``) is the fresh-pod path: the caller records
    and says so. Unusable (``BaselineRead(None, "<reason>")``) is a coverage
    gap: the caller must NOT re-record, or a torn file becomes a silent
    re-baseline of whatever state the host is in.
    """
    path = baseline_path(shared_dir, name)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return BaselineRead(None)
    except OSError as exc:
        return BaselineRead(None, f"{type(exc).__name__} reading the file: {exc}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return BaselineRead(None, f"the file is not valid JSON ({exc})")
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return BaselineRead(
            None,
            "the file parsed but has no \"entries\" map — it is not a baseline",
        )
    return BaselineRead({str(k): str(v) for k, v in entries.items()})


def load(shared_dir: Path, name: str) -> dict[str, str] | None:
    """The stored entries, or ``None`` when there is no usable baseline.

    The presence-only view of :func:`read`, kept for callers that only need
    "is a baseline established here" — the report's coverage column and the
    tests. A detector must use :func:`read` instead: it is the one that can
    tell a fresh pod from a torn file.
    """
    return read(shared_dir, name).entries


def save(shared_dir: Path, name: str, entries: dict[str, str]) -> bool:
    """Write the baseline atomically. Returns False (and warns) on failure.

    Temp file plus :func:`os.replace`, so a crash mid-write leaves the previous
    baseline whole rather than a half-document that the next pass would read as
    "no baseline" and re-record from current state.
    """
    path = baseline_path(shared_dir, name)
    payload = {
        "version": BASELINE_VERSION,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entries": dict(sorted(entries.items())),
    }
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=False))
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("incursion: cannot write baseline %s: %s", path, exc)
        try:
            tmp.unlink()
        except OSError as cleanup_exc:
            logger.debug("incursion: staged temp %s left behind: %s", tmp, cleanup_exc)
        return False


@dataclass
class Diff:
    """Keys that appeared, vanished, or kept their key and changed value."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def diff(baseline: dict[str, str], current: dict[str, str]) -> Diff:
    return Diff(
        added=sorted(set(current) - set(baseline)),
        removed=sorted(set(baseline) - set(current)),
        changed=sorted(
            k for k in set(baseline) & set(current) if baseline[k] != current[k]
        ),
    )


def first_run_observation(
    name: str, survey: "Survey", *, read_only: bool = False,
) -> Observation:
    """The explicit "baseline recorded" row a fresh pod gets instead of a page.

    A read-only pass says "would record" instead: the row appears in the
    operator's coverage table, where claiming a write that did not happen
    would be the one lie the whole read-only mode exists to avoid.
    """
    verb = "would record a baseline of" if read_only else "baseline recorded,"
    return Observation(
        level="ok",
        message=(
            f"incursion: {name} {verb} {len(survey.entries)} "
            f"entries from {survey.read} source(s)"
        ),
        detail=", ".join(sorted(survey.labels.values())[:20]),
    )


def corrupt_observation(name: str, shared_dir: Path, why: str) -> Observation:
    """The row a detector emits instead of treating a torn baseline as new.

    Warn and gap-shaped, so it lands in the report's "what this pod is NOT
    watching" section rather than paging: the detector has no evidence of an
    incursion, it has lost its ability to look. What it must never do is
    re-record — the state it would record is the state an intruder may have
    just put there, and the only row saying so would be an informational
    "baseline recorded".
    """
    path = baseline_path(shared_dir, name)
    return Observation(
        level="warn",
        message=(
            f"incursion: {name} coverage gap: baseline corrupt — "
            f"not re-recording"
        ),
        detail=f"{path}: {why}",
        what_it_means=(
            f"The {name} detector's baseline exists but cannot be read, so "
            f"this pass had nothing to compare against. It deliberately did "
            f"NOT record a new one: a baseline written now would adopt the "
            f"host's current state as expected, including anything an "
            f"intruder has already installed, and the detector would go quiet "
            f"about it forever. Until the file is dealt with, this detector "
            f"is covering nothing."
        ),
        fix_steps=(
            f"1. Look at {path} — a truncated or empty file is a crash "
            f"mid-write; anything else is worth understanding before you "
            f"delete it\n"
            f"2. Check what else changed around its mtime (new SSH keys, new "
            f"accounts, new scheduled jobs, `last`) before you re-record\n"
            f"3. Starting over is deliberate and manual: delete {path} and "
            f"the next audit pass records a fresh baseline from current "
            f"state — which is only safe once you believe current state\n"
            f"4. `python3 -m incursion.report` re-reads without writing "
            f"anything, so it is safe to run at any point above"
        ),
    )


def gap_observations(name: str, gaps: "list[CoverageGap]") -> list[Observation]:
    """One warn per unreadable source — the anti-vacuity row.

    Warn, not critical: a gap is missing coverage, not evidence of an
    incursion, and paging on it would train the operator to dismiss the
    detector's real findings. It still becomes a Signal on the Alerts board,
    which is where "this check is only half-covering the pod" belongs.
    """
    return [
        Observation(
            level="warn",
            message=f"incursion: {name} coverage gap: {gap.source} unreadable",
            detail=gap.why,
            what_it_means=(
                f"The {name} detector could not read {gap.source}, so anything "
                f"an attacker put there is invisible to this pod's audit. This "
                f"is a hole in the coverage, not a detection: the check ran and "
                f"reported honestly that part of its source is out of reach."
            ),
            fix_steps=(
                f"1. Confirm the reason: {gap.why}\n"
                f"2. If the source exists but the audit user cannot read it, "
                f"that read grant is a privileged change and is deliberately "
                f"NOT made by this detector — raise it as its own change\n"
                f"3. If the source does not exist on this host, no action is "
                f"needed; the gap row records that the check has nothing to "
                f"look at rather than pretending it looked"
            ),
        )
        for gap in gaps
    ]


def ok_observation(name: str, survey: "Survey", extra: str = "") -> list[Observation]:
    """The "nothing changed" row — omitted entirely when nothing was read.

    A pass that read zero sources has proved nothing, and an "OK" row for it
    would be exactly the empty green the anti-vacuity rule forbids: the gaps
    stand on their own.
    """
    if survey.read == 0:
        return []
    suffix = f" — {extra}" if extra else ""
    return [Observation(
        level="ok",
        message=(
            f"incursion: {name} OK ({len(survey.entries)} entries, "
            f"{survey.read} source(s)){suffix}"
        ),
    )]
