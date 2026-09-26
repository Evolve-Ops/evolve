#!/usr/bin/env python3
"""launchd_reload — re-register Evolve daemons that are on disk but not loaded.

The gap this closes
-------------------
``health.py::_check_launchd`` already DETECTS the "plist present, service not
loaded" state and hands the operator a ``fix_cmd`` (plus the Maintenance-page
"Fix" button). Nothing ever ran that fix on its own, so the finding sat until
a human opened the page.

Observed 2026-09-14 on the mini pod: 8 of 9
``ai.openclaw.evolve.doctor-pass.<bot>`` LaunchDaemons had been booted out
since 2026-09-07 — plists untouched on disk since June, absent from
``launchctl list``, absent from the disable override DB. Seven nights of
per-bot ``openclaw doctor --fix`` silently did not happen, and the pod
reported healthy on every other axis. (The root cause of that particular
bootout was never established; the 2026-09-07 OC 2026.9.2 hand-recovery of
all nine bots, which re-bootstrapped only the *gateways*, is the leading
candidate. This module deliberately does not depend on knowing — "on disk,
not loaded, not deliberately disabled" is a repairable state whatever
produced it.)

What it does NOT do
-------------------
* **It does not install anything.** A label with no artifact on disk is left
  alone — that is ``deploy``'s job, and re-running deploy from a 5-minute
  heal cycle is not a repair, it is a loop.
* **It does not resurrect a paused job.** The repair is a PLAIN
  ``launchctl bootstrap`` (via :func:`evolve_admin.health._execute_fix`),
  never ``Scheduler.enable`` — ``enable`` clears the disable override DB
  first, which would un-pause a job an operator deliberately
  ``launchctl disable``-d. A disabled label refuses to bootstrap, so launchd
  itself enforces the boundary and this module needs no disable-list of its
  own. Do not "simplify" this to ``enable()``; see
  :func:`test_disabled_job_is_not_resurrected`.
* **It does not hide a recurring failure.** Every repair is reported
  (``system.daemon_reloaded``). A self-heal nobody is told about converts a
  visible outage into an invisible one — if doctor-pass had been silently
  re-bootstrapped every night since 2026-09-07, the underlying bug would
  have been *less* discoverable, not more.
* **It does not retry forever.** A label that fails to bootstrap
  :data:`MAX_ATTEMPTS` times stops being attempted and escalates once
  (``system.daemon_reload_failed``). A malformed or genuinely broken unit is
  an operator problem, not something to hammer every 5 minutes.

Platform
--------
macOS/launchd only for now; :func:`sweep` returns a ``skipped`` result on any
other profile. The Linux failure mode is not the same shape — systemd loads
unit files from disk on ``daemon-reload`` and expresses breakage as ``failed``
state rather than absence — so the systemd analogue needs its own design
rather than a guessed transliteration of this one. Keyed off
``platform_profile.get_profile()``, never ``sys.platform``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# How many consecutive failed bootstrap attempts before a label is parked and
# escalated once. Three 5-minute heal cycles ≈ 15 minutes of trying, which is
# long enough to ride out a transient (a sudo grant momentarily unavailable,
# a plist mid-rewrite by a concurrent deploy) and short enough that a real
# breakage reaches the operator promptly.
MAX_ATTEMPTS = 3

# State lives beside the other heal-side bookkeeping under shared_dir. Owned by
# the evolve user; plain writes, no sudo staging needed.
STATE_RELPATH = "launchd_reload/state.json"


@dataclass
class LabelOutcome:
    """What happened to one label this sweep."""

    label: str
    # "repaired"      — bootstrap ran AND the label is loaded afterwards
    # "attempted"     — bootstrap returned success but the label is still not
    #                   loaded (honest: the command ran, the outcome did not
    #                   follow — never reported as a repair)
    # "failed"        — bootstrap returned failure
    # "parked"        — already at MAX_ATTEMPTS; not attempted this cycle
    result: str
    detail: str = ""
    attempts: int = 0


@dataclass
class SweepResult:
    """Everything one sweep did, for the caller to log and alert on."""

    skipped: str | None = None          # non-None ⇒ nothing ran; why
    checked: int = 0                    # expected labels considered
    unloaded: list[str] = field(default_factory=list)
    # Labels whose load state could not be determined this sweep (sudo could
    # not escalate, launchctl errored). Reported, never acted on.
    unprobed: list[str] = field(default_factory=list)
    outcomes: list[LabelOutcome] = field(default_factory=list)
    # Labels that crossed MAX_ATTEMPTS on THIS sweep — escalate once each.
    newly_parked: list[str] = field(default_factory=list)

    @property
    def repaired(self) -> list[str]:
        return [o.label for o in self.outcomes if o.result == "repaired"]

    @property
    def unrepaired(self) -> list[LabelOutcome]:
        return [o for o in self.outcomes if o.result != "repaired"]


# ── state ────────────────────────────────────────────────────────────────────

def _state_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / STATE_RELPATH


def load_state(shared_dir: Path) -> dict:
    """Read the per-label attempt ledger. Fail-open: any error ⇒ empty.

    An unreadable state file must not stop the sweep — the worst case of a
    lost ledger is that a parked label gets MAX_ATTEMPTS more tries, which is
    strictly better than declining to repair a pod because a bookkeeping file
    got corrupted.
    """
    try:
        raw = json.loads(_state_path(shared_dir).read_text())
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state(shared_dir: Path, state: dict) -> None:
    """Atomically persist the ledger (temp file + rename). Best-effort.

    A write failure is logged, never raised: losing the ledger costs extra
    retries on the next sweep, but failing here would take down the heal
    cycle that restarts gateways — a far worse trade.
    """
    path = _state_path(shared_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception as exc:            # noqa: BLE001 — bookkeeping, not the operation
        print(f"[launchd_reload] could not persist attempt ledger "
              f"({path}): {exc}", file=sys.stderr)


# ── detection ────────────────────────────────────────────────────────────────

def find_unloaded(
    network: dict,
    *,
    scheduler,
    expected_labels: set[str],
    is_gated_off,
    install_json: Path | None = None,
) -> tuple[list[str], int, list[str]]:
    """Labels whose artifact is on disk but which launchd has not loaded.

    Returns ``(unloaded_labels, checked_count, unprobed_labels)``.

    Probing is per-label via ``scheduler.status()``, NOT a single bare
    ``scheduler.list()``. Two reasons, and the first is decisive:

    1. **A bare ``launchctl list`` is not in the evolve user's sudo grant.**
       Verified on the mini pod 2026-09-14: as ``evolve`` it returns
       ``sudo: a password is required`` and the seam yields an empty list —
       which, taken at face value, reads as "nothing is loaded" for every
       label on the pod. ``launchctl list <label>`` IS granted (it is what
       ``health.py::_launchd_loaded`` has always used).
    2. ``status()`` carries the ``status_error`` tri-state, so a label whose
       probe failed stays distinguishable from one that is genuinely absent
       (PR #1579's rule). A bare listing collapses that distinction.

    The cost objection does not survive measurement: ~13 ms per probe on the
    reference pod, so ~2 s for a ~140-label pod, against a 5-minute cycle.

    A label is a candidate only when ALL of:
      * it is in ``expected_labels`` (the same
        ``expected_plist_labels(realized_only=True)`` set the health check
        uses, so the healer and the reporter cannot drift apart);
      * its feature is not gated off (an intentionally-absent plist is not a
        fault — the health check skips these too);
      * its probe was authoritative (``status_error`` is None);
      * its artifact EXISTS on disk (no artifact ⇒ a deploy concern, not ours);
      * launchd does not have it registered (``managed`` is False).
    """
    unloaded: list[str] = []
    unprobed: list[str] = []
    checked = 0
    for label in sorted(expected_labels):
        if is_gated_off(label, install_json):
            continue
        checked += 1
        try:
            st = scheduler.status(label)
        except Exception as exc:        # noqa: BLE001 — a raise is a probe failure
            unprobed.append(f"{label}: {exc}")
            continue
        # Tooling failure ⇒ "unknown", never "absent". Acting on a failed
        # probe is how a monitor turns a sudo hiccup into a bootstrap storm.
        if st.get("status_error"):
            unprobed.append(f"{label}: {st['status_error']}")
            continue
        if st.get("managed"):
            continue
        if not st.get("installed"):
            continue        # missing artifact ⇒ deploy's problem, not ours
        unloaded.append(label)
    return unloaded, checked, unprobed


# ── repair ───────────────────────────────────────────────────────────────────

def sweep(
    network: dict,
    shared_dir: Path,
    *,
    dry_run: bool = False,
    _deps: dict | None = None,
) -> SweepResult:
    """Detect and repair on-disk-but-unloaded Evolve daemons. Idempotent.

    ``_deps`` injects the collaborators in tests (``profile``, ``scheduler``,
    ``expected_labels``, ``is_gated_off``, ``execute_fix``); production
    resolves them lazily so heal.py stays importable in slim subprocess
    contexts where ``evolve_admin`` may be absent.
    """
    deps = _deps or {}
    # Presence, not truthiness: a test may legitimately inject an empty
    # expected set, and ``or`` would silently fall through to the real pod.
    _get = lambda k, build: deps[k] if k in deps else build()   # noqa: E731

    try:
        profile = _get("profile", _get_profile)
        if getattr(profile, "name", None) != "macos":
            return SweepResult(
                skipped=f"platform {getattr(profile, 'name', '?')} "
                        f"— launchd-only repair, see module docstring",
            )

        scheduler = _get("scheduler", _get_scheduler)
        if {"expected_labels", "is_gated_off", "execute_fix"} <= deps.keys():
            expected_labels = deps["expected_labels"]
            is_gated_off = deps["is_gated_off"]
            execute_fix = deps["execute_fix"]
        else:
            admin = _load_admin_deps()
            expected_labels = deps.get(
                "expected_labels",
                admin["expected_plist_labels"](network, realized_only=True),
            )
            is_gated_off = deps.get(
                "is_gated_off", admin["is_label_feature_gated_off"])
            execute_fix = deps.get("execute_fix", admin["_execute_fix"])
    except Exception as exc:            # noqa: BLE001 — degrade, never crash heal
        return SweepResult(skipped=f"dependencies unavailable: {exc}")

    install_json = Path(network.get("sharedDir", str(shared_dir))) / "install.json"
    unloaded, checked, unprobed = find_unloaded(
        network,
        scheduler=scheduler,
        expected_labels=set(expected_labels),
        is_gated_off=is_gated_off,
        install_json=install_json,
    )
    result = SweepResult(
        checked=checked, unloaded=list(unloaded), unprobed=list(unprobed),
    )
    if not unloaded:
        # Nothing wrong — clear the ledger so a label that breaks again later
        # gets a fresh MAX_ATTEMPTS budget rather than inheriting old strikes.
        if not dry_run:
            save_state(shared_dir, {})
        return result

    state = load_state(shared_dir)
    now = datetime.now(timezone.utc).isoformat()

    for label in unloaded:
        entry = state.get(label) or {}
        attempts = int(entry.get("attempts", 0) or 0)

        if attempts >= MAX_ATTEMPTS:
            result.outcomes.append(LabelOutcome(
                label, "parked", "at attempt cap — not retried", attempts,
            ))
            continue

        if dry_run:
            result.outcomes.append(LabelOutcome(
                label, "attempted", "[dry-run] would bootstrap", attempts,
            ))
            continue

        # PLAIN bootstrap — see the module docstring on why this must not
        # become Scheduler.enable().
        try:
            ok, msg = execute_fix({"action": "launchctl_bootstrap", "label": label})
        except Exception as exc:        # noqa: BLE001 — a raise is an outcome
            ok, msg = False, f"bootstrap raised: {exc}"

        # Verify rather than trust: "the command returned 0" is not "the job
        # is loaded". Report what is true, not what was attempted.
        reloaded = False
        if ok:
            try:
                reloaded = bool(scheduler.status(label).get("managed"))
            except Exception:
                reloaded = False

        if reloaded:
            state.pop(label, None)
            result.outcomes.append(LabelOutcome(label, "repaired", msg or "", attempts))
            continue

        attempts += 1
        state[label] = {"attempts": attempts, "last_attempt_at": now,
                        "last_error": (msg or "")[:300]}
        result.outcomes.append(LabelOutcome(
            label,
            "attempted" if ok else "failed",
            msg or ("bootstrap reported success but the job is still not loaded"
                    if ok else "bootstrap failed"),
            attempts,
        ))
        if attempts >= MAX_ATTEMPTS:
            result.newly_parked.append(label)

    if not dry_run:
        save_state(shared_dir, state)
    return result


# ── lazy dependency resolution ───────────────────────────────────────────────

def _get_profile():
    from platform_profile import get_profile
    return get_profile()


def _get_scheduler():
    from runtime.scheduler import get_scheduler
    return get_scheduler()


def _load_admin_deps() -> dict:
    """Import the admin-side collaborators. Raises if unavailable.

    heal.py runs in contexts where ``evolve_admin`` may not be importable, so
    every caller treats a raise here as "skip the sweep", never as fatal. The
    same lazy-import-with-fallback pattern heal already uses for
    ``evolve_admin.breakers_enforce`` / ``alerts.dispatcher``.
    """
    from evolve_admin.deploy import (           # type: ignore[import]
        expected_plist_labels, is_label_feature_gated_off,
    )
    from evolve_admin.health import _execute_fix  # type: ignore[import]
    return {
        "expected_plist_labels": expected_plist_labels,
        "is_label_feature_gated_off": is_label_feature_gated_off,
        "_execute_fix": _execute_fix,
    }
