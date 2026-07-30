"""Reap stale OpenClaw gateway units left by a renamed primary bot.

Phase E.6 of docs/spec-evo-account-separation-2026-05-25.md. When the primary
bot was renamed ``evolve`` → ``evo``, the leftover ``ai.openclaw.evolve-gateway``
unit survived a fresh install and crash-looped against the real
``ai.openclaw.evo-gateway`` over the shared gateway port (:19030) — a production
incident on the live evo-primary Linux pod, with the label-keyed health monitor
watching the wrong label so no alert fired.

The general orphan sweep (``deploy.find_orphaned_plists`` /
``remove_orphaned_plists``) does NOT cover this: its patterns are
``ai.openclaw.evolve.*`` (infra daemons) and ``ai.evolve.*`` (per-bot apps),
neither of which matches the gateway shape ``ai.openclaw.<bot>-gateway`` (note
the dash); and it scans ``/Library/LaunchDaemons`` only — there is no Linux
systemd equivalent. This module closes both gaps for the gateway case on both
platforms, keyed off the RESOLVED primary id (never the literal ``"evolve"``).

Lives in its own module (not ``deploy.py``) so the install/upgrade call sites
can reap stale gateways without growing the frozen ``deploy.py`` / ``cli.py``
hot files (tools/file-size-baseline.txt).
"""

from __future__ import annotations

from pathlib import Path

# The launchd system-domain dir (macOS) and the systemd system-unit dir (Linux).
# Local copies (matching deploy.LAUNCHD_DIR) keep this module decoupled from the
# capped deploy.py beyond the one label helper it borrows.
_LAUNCHD_DIR = Path("/Library/LaunchDaemons")
_SYSTEMD_DIR = Path("/etc/systemd/system")


def _gateway_label(bot_id: str) -> str:
    """``ai.openclaw.<bot>-gateway`` — the per-bot OpenClaw gateway unit label.

    Borrowed from ``deploy.per_bot_gateway_plist_label`` (single source of truth)
    so the shape can never drift between the provisioner and the reaper."""
    from .deploy import per_bot_gateway_plist_label
    return per_bot_gateway_plist_label(bot_id)


def installed_gateway_labels(
    *,
    profile_name: "str | None" = None,
    launchd_dir: "Path | None" = None,
    systemd_dir: "Path | None" = None,
) -> list[str]:
    """Discover OpenClaw gateway unit labels installed ON DISK on this host.

    Returns labels of the shape ``ai.openclaw.<bot>-gateway``, platform-aware:
      - macOS:  ``/Library/LaunchDaemons/ai.openclaw.*-gateway.plist`` stems.
      - Linux:  ``/etc/systemd/system/ai.openclaw.*-gateway.service`` (label =
        unit-file name minus the ``.service`` suffix).

    A filesystem scan (not ``systemctl list-units`` / ``launchctl list``) so a
    stale unit that exists on disk but isn't currently loaded is still found.
    Empty list when the unit directory is absent (e.g. a partial/test host).

    The keyword args exist so tests can exercise both platform branches without
    monkeypatching globals; production calls take the active platform + dirs.
    """
    if profile_name is None:
        from platform_profile import get_profile  # type: ignore
        profile_name = get_profile().name
    ld = launchd_dir if launchd_dir is not None else _LAUNCHD_DIR
    sd = systemd_dir if systemd_dir is not None else _SYSTEMD_DIR
    labels: list[str] = []
    if profile_name == "linux":
        if sd.is_dir():
            for unit in sd.glob("ai.openclaw.*-gateway.service"):
                labels.append(unit.name[: -len(".service")])
    else:
        if ld.is_dir():
            for plist in ld.glob("ai.openclaw.*-gateway.plist"):
                labels.append(plist.stem)
    return sorted(set(labels))


def find_orphaned_gateway_labels(network: dict) -> list[str]:
    """Installed gateway units whose bot is neither a current member nor the
    RESOLVED primary — i.e. a stale gateway left behind by a renamed primary.

    The expected set is built from the RESOLVED primary id (via
    ``primary_bot.primary_bot_id``) plus the current members — never the literal
    ``"evolve"``. **Conservative on a degenerate network:** when the primary
    can't be resolved we return ``[]`` rather than guess, so we never risk
    reaping the real primary's gateway when we don't know which one it is.
    """
    try:
        from primary_bot import primary_bot_id  # type: ignore
        primary = primary_bot_id(network)
    except Exception:
        primary = None
    if not primary:
        return []
    expected = {_gateway_label(primary)}
    for m in (network.get("members") or []):
        if isinstance(m, str) and m:
            expected.add(_gateway_label(m))
    return [lab for lab in installed_gateway_labels() if lab not in expected]


def reap_orphaned_gateways(
    labels: list[str], dry_run: bool = False
) -> tuple[list[str], list[str]]:
    """Bootout/disable + remove each stale gateway unit via the scheduler seam.

    Same ``(removed, failures)`` contract as
    ``deploy.remove_orphaned_plists``: ``removed`` carries only labels whose
    removal succeeded (or, in dry-run, the intended set); ``failures`` carry a
    one-line reason. The scheduler seam picks launchd vs systemd by platform, so
    ``scheduler.remove`` boots out / disables the unit and deletes its unit file
    on whichever host this runs.
    """
    removed: list[str] = []
    failures: list[str] = []
    from runtime.scheduler import get_scheduler  # type: ignore
    scheduler = get_scheduler()
    for label in labels:
        if dry_run:
            removed.append(label)
            continue
        ok, msg = scheduler.remove(label)
        if ok:
            removed.append(label)
        else:
            failures.append(f"{label} — {msg}")
    return removed, failures


def reap_stale_gateways(
    network: dict, *, dry_run: bool = False
) -> tuple[list[str], list[str], list[str]]:
    """Find + reap stale gateway units in one call.

    Returns ``(found, removed, failures)`` so a caller can render a compact
    report without re-deriving the set. ``found == []`` means nothing stale.
    """
    found = find_orphaned_gateway_labels(network)
    if not found:
        return [], [], []
    removed, failures = reap_orphaned_gateways(found, dry_run=dry_run)
    return found, removed, failures
