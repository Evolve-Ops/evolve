"""incursion.pam — drift in the authentication stack itself.

``/etc/pam.d`` is where the host decides what "authenticated" means. One
edited line in ``sudo`` or ``sshd`` — a ``pam_permit.so`` where a check used
to be, a module swapped for one on a path the attacker controls — turns every
credential check on the box into a formality, and leaves no process, no port
and no new account for the rest of the audit to notice.

Same path on both platforms, which is the whole reason this detector is one
module and not two: macOS and Linux both keep the per-service policies in
``/etc/pam.d/*`` and both look for the legacy monolithic ``/etc/pam.conf``
first. Files are mode 0644 root-owned on both, so the audit user reads them
directly — no grant, no sudo, no gap on a healthy host.

What is stored is the SHA256 of each file's bytes, keyed by name. Not the
contents: a baseline that carried the policies would be a map of exactly which
service to attack, and the hash answers the only question the detector asks.

The one kind with a real authorized-change source
=================================================

An OS update legitimately rewrites these files, and a detector that paged for
every patch cycle would be off within a month. So ``pam_config`` is the one
incursion kind registered in ``drift_authorization._KIND_SOURCES``, against
``SOURCE_OS_UPDATE`` — the host's own install record (``dpkg.log`` naming a
``pam`` package on Linux; ``InstallHistory.plist`` naming an OS update on
macOS). An explained change is absorbed into the baseline; anything else is an
``event`` and pages.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import drift_authorization

from incursion import Observation, Survey
from incursion import baseline as baseline_store
from incursion import gate

NAME = "pam"

#: The PAM configuration surface, in the order the stack itself consults it.
#: ``pam.conf`` is legacy and normally absent; when it IS present it takes
#: precedence over the directory on most implementations, which makes its
#: sudden appearance one of the more interesting things this detector can see.
PAM_CONF = Path("/etc/pam.conf")
PAM_DIR = Path("/etc/pam.d")


def _survey(pam_dir: Path, pam_conf: Path) -> Survey:
    survey = Survey()

    try:
        survey.add(
            "pam.conf",
            hashlib.sha256(pam_conf.read_bytes()).hexdigest(),
            f"{pam_conf} present",
        )
        survey.read += 1
    except FileNotFoundError:
        # Absent is the normal case and a covered one: the detector looked.
        survey.read += 1
    except OSError as exc:
        survey.gap(str(pam_conf), f"{type(exc).__name__}: {exc}")

    try:
        names = sorted(p.name for p in pam_dir.iterdir() if p.is_file())
    except FileNotFoundError:
        survey.gap(
            str(pam_dir),
            f"{pam_dir} does not exist on this host — the PAM policy surface "
            f"this detector was built to watch is not where it should be",
        )
        return survey
    except OSError as exc:
        survey.gap(str(pam_dir), f"{type(exc).__name__} listing {pam_dir}: {exc}")
        return survey

    survey.read += 1
    for name in names:
        path = pam_dir / name
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            survey.gap(str(path), f"{type(exc).__name__}: {exc}")
            continue
        survey.add(f"pam.d/{name}", digest, f"pam.d/{name}")
    return survey


def _observation(kind: str, key: str, detail: str) -> Observation:
    verb = {
        "added": "appeared",
        "changed": "was modified",
        "removed": "was deleted",
    }[kind]
    return Observation(
        level="critical",
        # event: whoever changed the auth stack can use it right now, and
        # the change is live for every login until it is reverted.
        finding_kind="event",
        message=f"🔴 CRITICAL: PAM policy {key} {verb} with nothing to authorize it",
        detail=detail,
        what_it_means=(
            f"{key} is part of the host's authentication stack — the rules "
            f"that decide whether a password, a key or a sudo attempt counts "
            f"as valid. It {verb} since the baseline was recorded, and no OS "
            f"or package update on this host accounts for it. A single "
            f"changed line here can make sudo or ssh accept anything, and it "
            f"applies to every account on the box, immediately."
        ),
        fix_steps=(
            f"1. Read the file before changing anything:\n"
            f"   cat /etc/{key}\n"
            f"2. Compare it against the same file on a host you trust, or "
            f"against the package's shipped copy\n"
            f"3. If it was edited, treat every account on this host as "
            f"exposed for the period since the change: rotate credentials "
            f"and review `last` and the sudo log\n"
            f"4. If YOU made the change (hardening, an MFA module), rebless "
            f"the baseline — see the incursion-pam.json entry named in the "
            f"detail line"
        ),
    )


def check(
    shared_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    read_only: bool = False,
    pam_dir: Path | None = None,
    pam_conf: Path | None = None,
) -> list[Observation]:
    """One pass. ``pam_dir`` / ``pam_conf`` override the surface (tests)."""
    del config  # the PAM surface is host-wide; the bot roster says nothing
    shared_dir = Path(shared_dir)
    survey = _survey(pam_dir or PAM_DIR, pam_conf or PAM_CONF)
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

    delta = baseline_store.diff(stored, survey.entries)
    absorbed = dict(stored)

    for kind, keys in (
        ("added", delta.added), ("changed", delta.changed), ("removed", delta.removed),
    ):
        for key in keys:
            explanation = gate.explain(
                drift_authorization.KIND_PAM_CONFIG, key, shared_dir,
                content_hash=survey.entries.get(key, ""), read_only=read_only,
            )
            if explanation is not None:
                if kind == "removed":
                    absorbed.pop(key, None)
                else:
                    absorbed[key] = survey.entries[key]
                observations.append(Observation(
                    level="ok",
                    message=(
                        f"incursion: PAM policy {key} {kind} — "
                        f"{explanation.evidence}"
                    ),
                    detail=explanation.line(),
                ))
                continue
            observations.append(_observation(
                kind, key,
                f"baseline {stored.get(key, '(absent)')[:16]} → live "
                f"{survey.entries.get(key, '(absent)')[:16]}; baseline file "
                f"{baseline_store.baseline_path(shared_dir, NAME)}",
            ))

    if absorbed != stored and not read_only:
        baseline_store.save(shared_dir, NAME, absorbed)

    if not delta:
        observations.extend(baseline_store.ok_observation(NAME, survey))
    return observations
