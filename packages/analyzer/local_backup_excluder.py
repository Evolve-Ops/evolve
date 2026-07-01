"""local_backup_excluder — sync ephemeral classification to Time Machine.

Phase 4c of the backup architecture rework
(spec docs/spec-backup-and-data-classification-2026-05-28.md
§"Phase 4 polish: TM exclusion reconciliation").

When an operator marks a directory ``ephemeral`` in the Data tab,
they're saying "don't back this up — anywhere." Phase 3a already
makes the cloud backup respect that. This reconciler makes the local
backup (Time Machine) respect it too: every ephemeral path gets
``tmutil addexclusion``-ed so TM stops wasting destination space on
regenerable caches.

**Opt-in.** This module no-ops unless ``network.json::backup.
tm_exclusion_sync`` is set to ``true``. Time Machine exclusions are
mutations of macOS system state — even additive mutations — and we
want operators to consciously enable cross-plane sync rather than have
it just happen on the first backup architecture upgrade.

**Additive only in v1.** When a path's classification flips OUT of
ephemeral (operator changed the tier, or removed the data_paths
entry), this reconciler does **not** remove the TM exclusion. Removing
exclusions could surprise an operator who set them by hand for other
reasons. Removal is a separate UX problem and deferred.

Pure Python; subprocess seam (``_run``) for tests. Non-macOS
environments return cleanly without acting.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from data_classification import (
    PRIVACY_EPHEMERAL,
    _normalise_path,
    load_bot_manifests,
)
from evolve_config import (
    bot_home,
    get_members,
    get_primary,
    get_shared_dir,
    load_config,
)

_TMUTIL = "/usr/bin/tmutil"
_TM_TIMEOUT_S = 8.0


# ─── Result shape ─────────────────────────────────────────────────────────


@dataclass
class ExcluderResult:
    enabled: bool                                    # opt-in flag
    available: bool                                  # tmutil exists
    candidates: int = 0                              # ephemeral paths discovered
    already_excluded: int = 0
    newly_excluded: int = 0
    skipped_nonexistent: int = 0
    errors: list[str] = field(default_factory=list)  # path-specific failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "candidates": self.candidates,
            "already_excluded": self.already_excluded,
            "newly_excluded": self.newly_excluded,
            "skipped_nonexistent": self.skipped_nonexistent,
            "errors": list(self.errors),
        }


# ─── Subprocess defaults (test seam) ──────────────────────────────────────


def _default_run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=_TM_TIMEOUT_S, check=False,
    )


def _is_macos() -> bool:
    return Path(_TMUTIL).exists()


# ─── Path collection ──────────────────────────────────────────────────────


def _ephemeral_paths_from_manifest(
    manifest: dict, workspace: Path,
) -> list[Path]:
    """Return absolute paths declared ephemeral by one app manifest."""
    out: list[Path] = []
    for entry in manifest.get("data_paths") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("privacy") != PRIVACY_EPHEMERAL:
            continue
        norm = _normalise_path(entry.get("path") or "")
        if norm is None:
            continue
        out.append(workspace / norm)
    # default_for_unclassified being ``ephemeral`` is too coarse a hammer to
    # interpret as "exclude the whole workspace from TM" — that case skips.
    return out


def _ephemeral_paths_from_pod(
    network: dict, shared_dir: Path,
) -> list[Path]:
    """Return absolute paths declared ephemeral pod-wide via network.json."""
    out: list[Path] = []
    backup = network.get("backup") if isinstance(network.get("backup"), dict) else {}
    for entry in backup.get("data_paths") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("privacy") != PRIVACY_EPHEMERAL:
            continue
        norm = _normalise_path(entry.get("path") or "")
        if norm is None:
            continue
        out.append(shared_dir / norm)
    return out


def collect_ephemeral_paths(
    network: dict,
    *,
    bots: list[str] | None = None,
    manifest_loader: Callable[[Path], list[dict]] = load_bot_manifests,
    include_per_bot: bool = False,
) -> list[Path]:
    """Walk the pod-wide section (and optionally per-bot manifests).

    **v1 scope (2026-05-29 review fix).** By default this only collects
    paths under ``{shared_dir}/`` — paths the ``evolve`` user actually
    owns and can act on. Per-bot ephemeral paths live under each bot's
    ``.openclaw/workspace/`` directory, and the ``evolve`` user has
    read-ACL but not write-ACL there: ``tmutil addexclusion`` would
    fail with EACCES on every per-bot path. Including those is opt-in
    via ``include_per_bot=True`` for future use (when the daemon
    converts to per-bot scheduling under each bot's user, or when a
    sudo wrapper lands), but the v1 reconciler keeps it off.

    Sorted + de-duplicated so callers can rely on stable ordering even
    when the same path is declared in multiple manifests.
    """
    shared_dir = get_shared_dir(network)
    paths: set[Path] = set()

    # Pod-wide — under {shared_dir}, evolve-owned, safe to mutate.
    for p in _ephemeral_paths_from_pod(network, shared_dir):
        paths.add(p)

    if not include_per_bot:
        return sorted(paths, key=str)

    # Per-bot — disabled by default; see docstring above.
    if bots is None:
        primary = get_primary(network)
        members = get_members(network)
        bots = ([primary] if primary and primary not in members else []) + members
        bots = [b for b in bots if b]
    for bot_id in bots:
        try:
            workspace = bot_home(bot_id, network) / ".openclaw" / "workspace"
        except Exception:  # noqa: BLE001 — defensive on malformed config
            continue
        try:
            manifests = manifest_loader(workspace)
        except Exception:  # noqa: BLE001
            continue
        for m in manifests:
            for p in _ephemeral_paths_from_manifest(m, workspace):
                paths.add(p)

    return sorted(paths, key=str)


# ─── tmutil wrappers ──────────────────────────────────────────────────────


def _tmutil_is_excluded(
    path: Path, *, _run: Callable[..., subprocess.CompletedProcess] = _default_run,
) -> bool | None:
    """Return True/False, or None on error."""
    try:
        r = _run([_TMUTIL, "isexcluded", str(path)])
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    if "[Excluded]" in (r.stdout or ""):
        return True
    if "[Included]" in (r.stdout or ""):
        return False
    return None


def _tmutil_add_exclusion(
    path: Path, *, _run: Callable[..., subprocess.CompletedProcess] = _default_run,
) -> str | None:
    """Returns None on success, error string otherwise."""
    try:
        r = _run([_TMUTIL, "addexclusion", str(path)])
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"tmutil addexclusion timed out / failed: {exc!s}"
    if r.returncode != 0:
        return (r.stderr or r.stdout or "tmutil addexclusion failed").strip()[:200]
    return None


# ─── Reconciler ───────────────────────────────────────────────────────────


def _opt_in_enabled(network: dict) -> bool:
    backup = network.get("backup") if isinstance(network.get("backup"), dict) else {}
    return bool(backup.get("tm_exclusion_sync"))


def reconcile(
    network: dict,
    *,
    bots: list[str] | None = None,
    dry_run: bool = False,
    _run: Callable[..., subprocess.CompletedProcess] = _default_run,
    manifest_loader: Callable[[Path], list[dict]] = load_bot_manifests,
) -> ExcluderResult:
    """Reconcile TM exclusions with ephemeral data_paths. Returns an ExcluderResult.

    Behaviour matrix:
      - Opt-in disabled         → enabled=False, no work performed
      - tmutil missing          → available=False, no work performed
      - Path doesn't exist      → counted in skipped_nonexistent, not excluded
      - Already excluded        → counted in already_excluded, no action
      - Not yet excluded        → tmutil addexclusion (or noted if dry_run)
      - tmutil call errors      → recorded in errors[] but loop continues

    A single bad path doesn't blank the whole sync — each path is handled
    independently. Operators see partial progress in the result.
    """
    if not _opt_in_enabled(network):
        return ExcluderResult(enabled=False, available=_is_macos())

    if not _is_macos():
        return ExcluderResult(enabled=True, available=False)

    result = ExcluderResult(enabled=True, available=True)
    candidates = collect_ephemeral_paths(
        network, bots=bots, manifest_loader=manifest_loader,
    )
    result.candidates = len(candidates)

    for path in candidates:
        if not path.exists():
            result.skipped_nonexistent += 1
            continue

        state = _tmutil_is_excluded(path, _run=_run)
        if state is True:
            result.already_excluded += 1
            continue
        # state is False or None → try to exclude. If state was None
        # (lookup error), the addexclusion attempt is the operator's
        # remediation either way; the call's own error gets recorded.

        if dry_run:
            result.newly_excluded += 1
            continue

        err = _tmutil_add_exclusion(path, _run=_run)
        if err is not None:
            result.errors.append(f"{path}: {err}")
            continue
        result.newly_excluded += 1

    return result


# ─── CLI entrypoint ───────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "local_backup_excluder — sync ephemeral-classified paths to "
            "Time Machine exclusions"
        ),
    )
    parser.add_argument("--network", default=None)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print would-be additions; don't call tmutil addexclusion",
    )
    args = parser.parse_args()

    config = load_config(args.network)
    result = reconcile(config, dry_run=args.dry_run)
    summary = result.as_dict()
    print(f"[local_backup_excluder] {json.dumps(summary, default=str)}", flush=True)
    if not summary["enabled"]:
        print(
            "[local_backup_excluder] opt-in flag "
            "network.json::backup.tm_exclusion_sync=true required",
            flush=True,
        )


if __name__ == "__main__":
    main()
