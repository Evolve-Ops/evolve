"""incursion.owned_jobs — the registry of scheduled-job labels Evolve installs.

Why a registry and not a prefix
===============================

The first cut of :mod:`incursion.job_inventory` excused a brand-new scheduled
job whose label merely *started with* ``ai.evolve.`` or ``ai.openclaw.``. The
review of PR #3967 named the cost of that: an attacker's LaunchDaemon called
``ai.evolve.helper`` is blessed and baselined for the price of one filename.
The label is attacker-chosen data; the prefix is not a fact about who installed
the job.

So ownership is settled by a file the INSTALLER writes, listing the labels it
put there:

    {shared_dir}/security/evolve-owned-jobs.json

    {"version": 1,
     "recorded_at": "2026-09-04T10:15:00Z",
     "labels": ["ai.evolve.evolve.admin-ui", "ai.openclaw.evolve.measure", …]}

A label in that file is Evolve's. A label with the right prefix and no entry is
a job Evolve does not know about, and the detector pages for it.

Where the content comes from — and where it deliberately does not
=================================================================

Every writer is on the install side (``deploy.py``): the label of each JobSpec
as it goes through the Scheduler seam, plus a whole-set re-assert from
``deploy.expected_plist_labels()`` at the end of ``install_evolve_infra_jobs``.
Both are statements of what the installer INTENDS to own.

Nothing here ever reads ``/Library/LaunchDaemons`` (or the systemd unit dir) to
populate the registry. Seeding it from what is on disk would launder whatever
an intruder had already installed into "Evolve owns this" — the exact evasion
this module exists to close, re-introduced through the back door of a backfill.

Merge, never prune
==================

:func:`record` unions the labels it is given into what is already there. The
alternative — replacing the set on every write — would mean any partial deploy
(one bot, one app, an infra-jobs run on a pod whose network.json failed to
load) silently un-owns every label it did not happen to mention, and the next
audit tick pages for a dozen of Evolve's own daemons at once. A false page
storm is worse than the residual this leaves: a RETIRED Evolve label lingering
as owned. That residual is narrow (the label has to have been genuinely
Evolve's at some point) and is still bounded by the program check — a job whose
program changes pages whoever owns the label.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

REGISTRY_VERSION = 1


def registry_path(shared_dir: Path | str) -> Path:
    """Where the installer records the job labels it owns."""
    return Path(shared_dir) / "security" / "evolve-owned-jobs.json"


def load(shared_dir: Path | str) -> set[str] | None:
    """The recorded labels, or ``None`` when no usable registry exists.

    ``None`` is deliberately not ``set()``: the caller has to be able to tell
    "the installer has recorded nothing here yet" (a coverage gap it should
    say out loud) from "the installer recorded an empty set". Both cases
    happen to make every prefixed label unowned, but only one of them is worth
    a row in the operator's table.
    """
    path = registry_path(shared_dir)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("incursion: unreadable owned-jobs registry %s (%s)", path, exc)
        return None
    labels = data.get("labels") if isinstance(data, dict) else None
    if not isinstance(labels, list):
        return None
    return {str(label) for label in labels if str(label)}


def record(shared_dir: Path | str, labels: Iterable[str]) -> bool:
    """Union ``labels`` into the registry. Returns False (and warns) on failure.

    Atomic — temp file plus :func:`os.replace`, the same shape ``audit.py``'s
    snapshot writes use. A torn registry would read back as "no registry", and
    the detector would then treat every Evolve label as unowned.

    Best-effort by contract: an install must not fail because this file could
    not be written. The cost of a missed write is a coverage-gap row and, at
    worst, one false page on the next new label — not a failed deploy.
    """
    wanted = {str(label).strip() for label in labels}
    wanted.discard("")
    if not wanted:
        return True

    path = registry_path(shared_dir)
    merged = sorted((load(shared_dir) or set()) | wanted)
    payload = {
        "version": REGISTRY_VERSION,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "labels": merged,
    }
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("incursion: cannot write owned-jobs registry %s: %s", path, exc)
        try:
            tmp.unlink()
        except OSError as cleanup_exc:
            logger.debug("incursion: staged temp %s left behind: %s", tmp, cleanup_exc)
        return False
