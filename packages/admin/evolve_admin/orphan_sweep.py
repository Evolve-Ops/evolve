"""Orphan service-definition sweeper — platform-aware.

Extracted from ``deploy.py`` (which is frozen at its size cap) so the Linux
systemd path could be added. ``deploy.py`` re-exports ``find_orphaned_plists``
and ``remove_orphaned_plists`` from here, so every historical import site
(cli.py upgrade, web/server.py maintenance, tests that call
``deploy.find_orphaned_plists``) keeps working unchanged.

Two platforms, one contract:

  * macOS — glob ``/Library/LaunchDaemons`` for orphaned ``ai.openclaw.evolve.*``
    / ``ai.evolve.*`` ``.plist`` files (byte-identical to the pre-extraction
    behaviour).
  * Linux — enumerate the platform daemon dir (``/etc/systemd/system``) for
    Evolve-owned systemd units (those two shapes **plus** ``ai.openclaw.*-gateway``,
    which neither macOS glob catches) and return one representative unit per
    orphaned label.

The macOS sweeper was launchd-only, so a stale unit (e.g. the legacy
``ai.openclaw.evolve-gateway.service`` left on an evo-primary pod whose real
gateway is ``ai.openclaw.evo-gateway`` — EVO-LINUX-PHANTOM-GATEWAY) sat on disk
forever with no automatic cleanup. This restores parity on Linux.

``Path.stem`` of any plist OR rendered systemd unit name is the label, so
``remove_orphaned_plists`` drives the platform Scheduler seam
(``get_scheduler().remove(label)``) without caring which OS produced the path:
launchd bootout + ``rm`` on macOS, ``systemctl disable --now`` + delete-unit-set
+ ``daemon-reload`` on Linux.
"""

from __future__ import annotations

import logging
from pathlib import Path

from platform_profile import get_profile as _get_profile

_log = logging.getLogger(__name__)

# systemd unit-file suffixes the SystemdScheduler renders (mirrors
# runtime.scheduler._UNIT_KINDS without importing a private name). Unit-name →
# label is the inverse of runtime.scheduler.systemd_unit_name: strip the suffix.
# ``.service`` always exists; ``.timer`` / ``.path`` exist only for
# interval/calendar/watch jobs.
_SYSTEMD_UNIT_SUFFIXES = (".service", ".timer", ".path")


def _is_evolve_owned_label(label: str) -> bool:
    """True iff ``label`` is one the orphan-sweeper is allowed to consider.

    The shapes the sweeper owns — the same set the macOS plist globs match,
    plus the OpenClaw gateway shape (which neither macOS glob catches: the
    ``ai.openclaw.evolve.*`` glob needs a dot after ``evolve``, but a gateway
    label is ``ai.openclaw.<bot>-gateway`` with a hyphen):

      * ``ai.openclaw.evolve.*``  — pod-wide + per-bot Evolve infra daemons
      * ``ai.evolve.*``           — pod-wide + per-bot Evolve infra daemons
      * ``ai.openclaw.*-gateway`` — per-bot OpenClaw gateways

    This is the belt-and-suspenders ownership gate: any unit NOT matching one
    of these shapes (sshd, nginx, a hand-rolled operator unit, …) can never
    enter the orphan set, so the sweep can never disable a non-Evolve unit.
    """
    if label.startswith("ai.openclaw.evolve.") or label.startswith("ai.evolve."):
        return True
    return label.startswith("ai.openclaw.") and label.endswith("-gateway")


def _all_configured_bot_ids(network: dict) -> set[str]:
    """Every bot id this pod is configured for — members ∪ bots-map keys ∪
    the resolved primary. Used to build the gateway keep-set (below)."""
    ids: set[str] = set(network.get("members", []) or [])
    ids.update((network.get("bots") or {}).keys())
    try:  # primary may sit outside members on evo-primary pods
        from primary_bot import primary_bot_id as _primary_bot_id  # type: ignore
        pid = _primary_bot_id(network)
        if pid:
            ids.add(pid)
    except Exception as exc:  # noqa: BLE001 — best-effort keep-set widening
        _log.debug("_all_configured_bot_ids: primary_bot_id unavailable: %s", exc)
    return ids


def _orphan_keep_set(network: dict) -> set[str]:
    """The labels the sweeper must SPARE: ``expected_plist_labels`` plus every
    configured bot's gateway.

    ``expected_plist_labels`` lists only the PRIMARY gateway (it is consumed by
    pod_health, which must not FAIL on a member gateway that is momentarily
    absent). But the Linux scan walks the ``ai.openclaw.*-gateway`` shape, so
    without this widening every member bot's LIVE gateway would be flagged as an
    orphan and disabled. Add each configured bot's gateway here — these SHOULD
    exist, so they belong in the keep-set for sweep purposes. This is scan-side
    only; it deliberately does NOT perturb ``expected_plist_labels`` (and thus
    pod_health / the macOS sweep).
    """
    from . import deploy  # lazy: deploy re-exports from here (avoid import cycle)
    keep = set(deploy.expected_plist_labels(network))
    for bot_id in _all_configured_bot_ids(network):
        keep.add(deploy.per_bot_gateway_plist_label(bot_id))
    return keep


def _warn_if_per_bot_app_shape(label: str, members: set[str]) -> None:
    """Belt-and-suspenders WARN, shared by both platform scans: a label matching
    the per-bot app pattern (``ai.evolve.<bot>.<rest>``, ``<bot>`` a member) that
    ends up an orphan means its manifest is missing/corrupt. Log BEFORE the
    sweep removes it so the regression surfaces in logs, not in a silent cron
    disappearance — the 2026-06-05 application-installed cron / ea-pack mode."""
    parts = label.split(".")
    if (len(parts) >= 4 and parts[0] == "ai" and parts[1] == "evolve"
            and parts[2] in members):
        _log.warning(
            "find_orphaned_plists: %s matches the per-bot app pattern "
            "(bot=%s) but isn't in the expected keep-set. Likely the bot's "
            "manifest is missing or doesn't record installed_artifact for this "
            "scheduled_action. Investigate before sweep removes it — see "
            "PR #2164's install_launchd_command_action path.",
            label, parts[2],
        )


def _find_orphaned_units_systemd(network: dict) -> list[Path]:
    """Linux sibling of the macOS plist scan: enumerate Evolve-owned systemd
    units under the platform daemon dir (``/etc/systemd/system``), diff against
    the keep-set, and return one representative unit Path per orphaned label.

    One representative path per LABEL (not per unit file): a label may own
    ``.service`` + ``.timer`` + ``.path``, but :meth:`SystemdScheduler.remove`
    (which ``remove_orphaned_plists`` drives) deletes the whole set atomically,
    so returning all three would double-count the removal. The ``.service`` is
    preferred as the representative when present.
    """
    unit_dir = Path(_get_profile().daemon_dir)
    if not unit_dir.is_dir():
        return []
    keep = _orphan_keep_set(network)
    members = set(network.get("members", []) or [])
    # label → representative unit Path (dedup across .service/.timer/.path).
    orphan_rep: dict[str, Path] = {}
    for entry in sorted(unit_dir.iterdir()):
        # Top-level entries only: `systemctl enable` creates symlinks under
        # `*.target.wants/` subdirs, which iterdir() does not descend into, so
        # we only ever see the real unit files our _write_unit cp'd here.
        if not entry.is_file():
            continue
        suffix = entry.suffix
        if suffix not in _SYSTEMD_UNIT_SUFFIXES:
            continue
        label = entry.name[: -len(suffix)]
        if not _is_evolve_owned_label(label):
            continue  # ownership gate: never touch a non-Evolve unit
        if label in keep:
            continue
        _warn_if_per_bot_app_shape(label, members)
        # Prefer the .service as the representative for display/dedup; the
        # remove verb deletes the whole set regardless of which we keep.
        if label not in orphan_rep or suffix == ".service":
            orphan_rep[label] = entry
    return sorted(orphan_rep.values())


def find_orphaned_plists(network: dict) -> list[Path]:
    """Scan the platform daemon dir for Evolve service definitions that are no
    longer expected by the current version and network configuration.

    Returns a sorted list of Path objects — orphaned ``.plist`` files under
    ``/Library/LaunchDaemons`` on macOS, or one representative ``.service`` /
    ``.timer`` / ``.path`` unit per orphaned label under ``/etc/systemd/system``
    on Linux (see :func:`_find_orphaned_units_systemd`). ``Path.stem`` is the
    label on both platforms, so :func:`remove_orphaned_plists` consumes the
    result without caring which OS produced it.

    Belt-and-suspenders: a label matching the per-bot app pattern
    (``ai.evolve.<bot>.<rest>``, with ``<bot>`` resolving to a network member)
    is logged at WARN. The expected-set pass already walks
    scheduled_actions[].installed_artifact, so anything matching this shape that
    ends up in orphans means the manifest was deleted/corrupted (or the install
    was never recorded) and the operator should investigate BEFORE the sweep
    removes the unit — this is the 2026-06-05 application-installed cron /
    ea-pack failure mode written down so the next regression surfaces in logs,
    not in a silent cron disappearance.
    """
    if _get_profile().name == "linux":
        return _find_orphaned_units_systemd(network)
    from . import deploy  # lazy: deploy re-exports from here (avoid import cycle)
    expected = deploy.expected_plist_labels(network)
    members = set(network.get("members", []))
    orphans: list[Path] = []
    for pattern in ("ai.openclaw.evolve.*.plist", "ai.evolve.*.plist"):
        for plist in deploy.LAUNCHD_DIR.glob(pattern):
            if plist.stem not in expected:
                orphans.append(plist)
                _warn_if_per_bot_app_shape(plist.stem, members)
    return sorted(set(orphans))  # deduplicate in case patterns overlap


def remove_orphaned_plists(
    orphans: list[Path], dry_run: bool = False
) -> tuple[list[str], list[str]]:
    """Unload and remove each orphaned service definition through the platform
    Scheduler seam.

    Platform-agnostic by construction: every orphan Path's ``.stem`` is the
    label on both OSes, and ``get_scheduler().remove(label)`` resolves to
    :meth:`LaunchdScheduler.remove` (bootout + ``rm`` the plist) on macOS and
    :meth:`SystemdScheduler.remove` (``systemctl disable --now`` + delete the
    whole unit set + ``daemon-reload``) on Linux. So the same callers
    (cli.py upgrade, web/server.py maintenance) get the Linux behaviour with
    no change here.

    Returns ``(removed_labels, failures)``. A label is in ``removed`` only
    if the actual removal succeeded (or in dry-run mode, where the
    intent is reported). Failures carry a one-line reason from stderr —
    surfaced by the upgrade CLI so the operator can spot sudoers gaps,
    NFS-style stuck files, etc.

    The previous shape silently ignored ``rm`` failure (no return-code
    check) and unconditionally appended to a flat ``removed`` list, so
    the upgrade reported "✓ Removed: <label>" even when the file was
    still on disk. Operators saw the same orphans flagged on every
    upgrade run with no indication something was wrong.
    """
    from .runtime import get_scheduler  # lazy: keep module import-cheap

    removed: list[str] = []
    failures: list[str] = []
    scheduler = get_scheduler()
    for unit in orphans:
        label = unit.stem
        if dry_run:
            removed.append(label)
            continue
        # Scheduler.remove() boots out / disables best-effort (the daemon may
        # not be loaded right now — "could not find specified service" is fine)
        # and then deletes the file(s); ok is True only if the delete succeeded.
        ok, msg = scheduler.remove(label)
        if ok:
            removed.append(label)
        else:
            failures.append(f"{label} — {msg}")
    return removed, failures
