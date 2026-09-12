"""incursion.job_inventory — the scheduled job nobody installed.

Two audit checks already look at scheduling and neither one would notice a
stranger's job. ``audit_cron_health`` reads each bot's ``.openclaw/cron/jobs.json``
and reports on the health of Evolve's OWN entries — error counts, quarantine,
silent-exec patterns. ``audit_process`` walks ``ps`` and catches a suspicious
binary only while it happens to be running. A LaunchDaemon or a systemd timer
that re-launches an attacker's code every boot is invisible to both: it is not
in ``jobs.json``, and between runs there is no process to see.

This detector takes the inventory instead — every job-definition surface the
host schedules from — and diffs it label by label.

Sources
=======

macOS
    ``/Library/LaunchDaemons`` and ``/Library/LaunchAgents`` (root-owned,
    world-readable, and where anything that wants to survive a reboot has to
    put itself), plus each pod account's ``~/Library/LaunchAgents``. Apple's
    own jobs under ``/System/Library`` are deliberately NOT surveyed: they are
    SIP-protected, so an attacker who can write there has already won a bigger
    fight than this detector adjudicates, and they churn on every OS update.

Linux
    The system unit directory (``platform_profile``'s ``daemon_dir``), the
    cron surfaces (``/etc/crontab``, ``/etc/cron.d``, the ``cron.daily``-style
    run-parts directories), the per-user crontab spool, and each pod
    account's ``~/.config/systemd/user``.

What counts as a change
=======================

The stored value is the job's PROGRAM — the ``ProgramArguments`` of a plist,
the ``ExecStart=`` lines of a unit — not the whole file. So a scheduling
tweak or a comment does not page, and a job whose label stayed the same while
its program was repointed does. Cron files have no label/program split to
make, so those are hashed whole.

Evolve's own labels are explained by ownership when they APPEAR — the
installer put them there, and that is settled without a time window to be
timed against. What settles it is the installer's own registry
(:mod:`incursion.owned_jobs`), NOT the shape of the label: a job called
``ai.evolve.helper`` that no installer ever recorded is a stranger's job with
a familiar name, and it pages. A repointed program under a genuinely owned
label is not excused either: "``ai.evolve.evolve.heal`` now runs something
else" is among the most alarming lines this audit could produce.

When the registry has not been written yet — a pod that has not deployed since
this landed — nothing is owned, and the detector says so with a coverage-gap
row. That fails toward paging on the next new ``ai.evolve.*`` job, which is the
right direction: the alternative (fall back to the prefix) would hand the
evasion back to anyone who can delete one file.
"""

from __future__ import annotations

import hashlib
import plistlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import drift_authorization
from platform_profile import get_profile
from runtime.scheduler import label_from_unit_name

from incursion import Observation, Survey
from incursion import baseline as baseline_store
from incursion import gate
from incursion import owned_jobs
from incursion.users import pod_users

NAME = "job_inventory"


@dataclass(frozen=True)
class JobRoot:
    """One directory (or single file) of job definitions to survey."""

    kind: str      # "launchd" | "systemd" | "cron"
    path: Path
    patterns: tuple[str, ...] = ("*",)
    #: True when this root is one many hosts simply do not have, so its
    #: absence is a covered source rather than a coverage gap.
    optional: bool = True


def job_roots(
    config: dict[str, Any] | None = None,
    *,
    homes: dict[str, Path] | None = None,
) -> list[JobRoot]:
    """Every job-definition surface on this host, keyed by platform profile."""
    profile = get_profile()
    accounts = pod_users(config) if homes is None else homes
    roots: list[JobRoot] = []

    if profile.name == "macos":
        roots.append(JobRoot("launchd", Path(profile.daemon_dir), ("*.plist",)))
        roots.append(JobRoot("launchd", Path("/Library/LaunchAgents"), ("*.plist",)))
        for home in sorted(accounts.values()):
            roots.append(JobRoot(
                "launchd", home / "Library" / "LaunchAgents", ("*.plist",),
            ))
        return roots

    roots.append(JobRoot(
        "systemd", Path(profile.daemon_dir), ("*.service", "*.timer"),
    ))
    roots.append(JobRoot("cron", Path("/etc/crontab"), ()))
    roots.append(JobRoot("cron", Path("/etc/cron.d")))
    for period in ("hourly", "daily", "weekly", "monthly"):
        roots.append(JobRoot("cron", Path(f"/etc/cron.{period}")))
    # Per-user crontabs. The spool is mode 1730 root:crontab on Debian and
    # friends, so this is the root most likely to come back as a gap — which
    # is the honest answer, not a reason to skip it.
    roots.append(JobRoot("cron", Path("/var/spool/cron/crontabs")))
    for home in sorted(accounts.values()):
        roots.append(JobRoot(
            "systemd", home / ".config" / "systemd" / "user",
            ("*.service", "*.timer"),
        ))
    return roots


def _launchd_entry(path: Path) -> tuple[str, str]:
    """``(label, program)`` for a launchd job definition."""
    data = plistlib.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise ValueError("plist root is not a dictionary")
    label = str(data.get("Label") or path.stem)
    argv = data.get("ProgramArguments")
    if isinstance(argv, list) and argv:
        program = " ".join(str(a) for a in argv)
    else:
        program = str(data.get("Program") or "(no program)")
    return label, program


def _systemd_entry(path: Path) -> tuple[str, str]:
    """``(unit name, ExecStart lines)`` for a systemd unit."""
    execs: list[str] = []
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if line.startswith(("ExecStart=", "ExecStartPre=", "ExecStartPost=")):
            execs.append(line.split("=", 1)[1].strip())
    return path.name, " ; ".join(execs) or "(no ExecStart)"


def _cron_entry(path: Path) -> tuple[str, str]:
    """``(path, content hash)`` — cron files have no label to key on."""
    lines = [
        line.strip()
        for line in path.read_text(errors="replace").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return str(path), hashlib.sha256("\n".join(lines).encode()).hexdigest()


_PARSERS = {
    "launchd": _launchd_entry,
    "systemd": _systemd_entry,
    "cron": _cron_entry,
}


def _files_in(root: JobRoot, survey: Survey) -> list[Path] | None:
    """The files this root contributes, or ``None`` when it could not be read."""
    if not root.patterns:                      # a single-file root (/etc/crontab)
        return [root.path]
    try:
        names = sorted(p for p in root.path.iterdir() if p.is_file())
    except FileNotFoundError:
        # A root this host does not have is a covered source: the detector
        # looked where jobs could live and there was no such place.
        survey.read += 1
        return None
    except OSError as exc:
        survey.gap(str(root.path), f"{type(exc).__name__} listing directory: {exc}")
        return None
    survey.read += 1
    if root.patterns == ("*",):
        return names
    return [p for p in names if any(p.match(pat) for pat in root.patterns)]


def _survey(roots: list[JobRoot]) -> Survey:
    survey = Survey()
    for root in roots:
        files = _files_in(root, survey)
        if files is None:
            continue
        for path in files:
            try:
                key_part, value = _PARSERS[root.kind](path)
            except FileNotFoundError:
                if not root.patterns:          # single-file root, simply absent
                    survey.read += 1
                continue
            except OSError as exc:
                survey.gap(str(path), f"{type(exc).__name__}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 — a malformed definition
                survey.gap(
                    str(path),
                    f"cannot parse this job definition ({type(exc).__name__}: "
                    f"{exc}) — its program is not being watched",
                )
                continue
            if not root.patterns:
                survey.read += 1
            survey.add(
                f"{root.kind}:{key_part}",
                value,
                f"{root.kind}:{key_part} → {value[:120]}",
            )
    return survey


def _is_owned(key: str, registry: set[str]) -> bool:
    """True only when the INSTALLER recorded this exact label as its own.

    ``key`` is the survey key: ``launchd:<Label>``, ``systemd:<unit file>``,
    ``cron:<path>``. The registry holds the bare labels the installer passed
    to the Scheduler seam, so the systemd side has its unit-type suffix
    stripped by the seam's own inverse mapping — ``ai.evolve.evolve.heal``
    installs as ``ai.evolve.evolve.heal.timer`` on Linux, and comparing the
    unit filename would mean no Linux job was ever owned.

    Beyond that one platform mapping, membership is EXACT: no prefix, no
    normalization, no case folding. Every relaxation of this comparison is a
    filename an attacker gets to pick.
    """
    key = key.split(":", 1)[1] if ":" in key else key
    if key in registry:
        return True
    label = label_from_unit_name(key)
    return label is not None and label in registry


def _registry_gap() -> Observation:
    """The row for a pod whose installer has not recorded its labels yet.

    Not a critical: nothing has been detected, the detector has lost a way to
    tell its own jobs apart from anyone else's. The consequence is stated
    plainly, because it is a real one — the next Evolve daemon this pod
    installs will page until the registry catches up.
    """
    return Observation(
        level="warn",
        message=(
            f"incursion: {NAME} coverage gap: no Evolve-owned job registry "
            f"recorded yet"
        ),
        detail=(
            "the installer writes security/evolve-owned-jobs.json on deploy; "
            "until it does, no scheduled job counts as Evolve's own"
        ),
        what_it_means=(
            "This detector decides whether a new scheduled job is Evolve's by "
            "looking it up in a list the installer writes — never by the shape "
            "of its name, because a name is something an attacker chooses. "
            "That list has not been written on this pod yet, so until the next "
            "deploy every newly appearing job is treated as unrecognised, "
            "including Evolve's own. Nothing here says an incursion happened."
        ),
        fix_steps=(
            "1. Deploy the pod (`evolve-admin install-infra-jobs`, or any "
            "bot deploy) — the installer records the labels it owns as part "
            "of the run\n"
            "2. Until then, treat a new `ai.evolve.*` / `ai.openclaw.*` "
            "finding as a name to CHECK against the deploy you just ran, not "
            "as a false positive to dismiss"
        ),
    )


def check(
    shared_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    read_only: bool = False,
    roots: list[JobRoot] | None = None,
    homes: dict[str, Path] | None = None,
) -> list[Observation]:
    """One pass. ``roots`` overrides the surveyed surfaces (tests)."""
    shared_dir = Path(shared_dir)
    survey = _survey(job_roots(config, homes=homes) if roots is None else roots)
    observations = baseline_store.gap_observations(NAME, survey.gaps)

    # Nothing was readable, so there is nothing to baseline and nothing to
    # compare against. Recording an empty baseline here would POISON the
    # detector: the next pass that could read its sources would call every
    # entry on the host brand new. The gaps stand alone.
    if survey.read == 0:
        return observations

    state = baseline_store.read(shared_dir, NAME)
    if state.corrupt:
        # A baseline that exists and will not parse is a coverage gap, not a
        # fresh start. Re-recording here would adopt whatever is on the host
        # right now as the expected state — an intruder's additions included —
        # and the only trace would be an "ok" row saying a baseline was
        # recorded. Starting over stays the operator's deliberate act.
        observations.append(baseline_store.corrupt_observation(
            NAME, shared_dir, state.corrupt,
        ))
        return observations

    stored = state.entries
    if stored is None:
        if not read_only:
            baseline_store.save(shared_dir, NAME, survey.entries)
        observations.append(baseline_store.first_run_observation(
            NAME, survey, read_only=read_only,
        ))
        return observations

    # Who owns a label is decided by the installer's registry, never by the
    # label's shape. A pod that has not deployed since this landed has no
    # registry: nothing is owned, and the gap row says why the next
    # ai.evolve.* job may page.
    registry = owned_jobs.load(shared_dir)
    if registry is None:
        observations.append(_registry_gap())
        registry = set()

    delta = baseline_store.diff(stored, survey.entries)
    absorbed = dict(stored)

    for key in delta.added:
        if _is_owned(key, registry):
            absorbed[key] = survey.entries[key]
            observations.append(Observation(
                level="ok",
                message=f"incursion: new Evolve-owned scheduled job {key}",
                detail=survey.labels.get(key, ""),
            ))
            continue
        observations.append(Observation(
            level="critical",
            # event: it is installed, it is scheduled, and it will run.
            finding_kind="event",
            message=f"🔴 CRITICAL: new scheduled job {key} that Evolve did not install",
            detail=survey.labels.get(key, ""),
            what_it_means=(
                f"A job definition that was not in the baseline now exists at "
                f"{key}. The host will run it on its own schedule — at boot, "
                f"on a timer, or on a trigger — whether or not anyone is "
                f"logged in, which is what makes a scheduled job the standard "
                f"way to make access to a machine survive a reboot. Its "
                f"program is in the detail line. Evolve's own jobs are "
                f"recognised by looking the label up in the list its installer "
                f"writes ({owned_jobs.registry_path(shared_dir)}) — never by "
                f"the label looking Evolve-ish — so a name beginning "
                f"ai.evolve. or ai.openclaw. does NOT make this one of ours."
            ),
            fix_steps=(
                f"1. Read the definition and see what it runs — the program "
                f"is in the detail line above; the file is named in the key\n"
                f"2. If you or an application you installed put it there "
                f"(a browser updater, a backup agent, a VPN), it is expected: "
                f"rebless the baseline at step 4\n"
                f"3. If you do not recognise it, do NOT just delete it — "
                f"capture the file first, then look at what else changed "
                f"around the same time (new SSH keys, new accounts, `last`). "
                f"An Evolve-looking name here is a REASON to look, not a "
                f"reason to relax: the installer never recorded this label\n"
                f"4. Rebless: delete "
                f"{baseline_store.baseline_path(shared_dir, NAME)} to "
                f"re-record every job from current state, or remove this one "
                f"entry from its \"entries\" map"
            ),
        ))

    for key in delta.changed:
        explanation = gate.explain(
            drift_authorization.KIND_JOB_INVENTORY, key, shared_dir,
            content_hash=survey.entries[key], read_only=read_only,
        )
        if explanation is not None:
            absorbed[key] = survey.entries[key]
            observations.append(Observation(
                level="ok",
                message=f"incursion: scheduled job {key} repointed — {explanation.evidence}",
                detail=survey.labels.get(key, ""),
            ))
            continue
        observations.append(Observation(
            level="critical",
            finding_kind="event",
            message=f"🔴 CRITICAL: scheduled job {key} now runs a different program",
            detail=f"was: {stored[key][:150]} | now: {survey.entries[key][:150]}",
            what_it_means=(
                f"The job {key} kept its name and changed what it executes. "
                f"That is the quietest way to take over a scheduled task: "
                f"every list of jobs still looks familiar, and the host runs "
                f"the new program on the old schedule with the old privilege. "
                f"It applies to Evolve's own labels too — if this is one of "
                f"them, nothing in a normal upgrade repoints a daemon."
            ),
            fix_steps=(
                f"1. Compare the two programs in the detail line above\n"
                f"2. If the new one is not something you changed, stop the "
                f"job and restore its definition from the package or repo "
                f"that owns it\n"
                f"3. Check what the new program did while it was scheduled\n"
                f"4. If the repoint was intentional (an upgrade you ran), "
                f"rebless the baseline at "
                f"{baseline_store.baseline_path(shared_dir, NAME)}"
            ),
        ))

    for key in delta.removed:
        absorbed.pop(key, None)
        observations.append(Observation(
            level="ok",
            message=f"incursion: scheduled job {key} is no longer installed",
            detail=stored[key][:200],
        ))

    if absorbed != stored and not read_only:
        baseline_store.save(shared_dir, NAME, absorbed)

    if not delta:
        observations.extend(baseline_store.ok_observation(NAME, survey))
    return observations
