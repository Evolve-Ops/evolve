"""One-time purge of the ``_ingested`` audit-outbox backlog.

Background (META:footprint, internal/footprint-disk-output-audit-2026-06-28.md):
The audit poller archives every processed outbox record into
``audit_outbox/_ingested/<YYYY-MM-DD>/`` (and the shared infra outbox into
``infra_audit_outbox/_ingested/<YYYY-MM-DD>/``). Nothing in production reads
``_ingested`` — only tests — so the archive grew without bound until the
go-forward source-cut (#3315 selective-delete, #3319 emit-on-change) stopped
new accumulation. The merged 30-day retention (#3311) only trims the
>30-day tail; the bulk of the existing backlog (measured 2026-06-28 on the
mini = 131,321 files / 525M across all bots) is younger than 30 days and
must be cleared by an explicit one-time sweep.

This module is that sweep: a guarded, idempotent purge wired into the CLI as
``evolve-admin purge-ingested-backlog``. The live outbox roots
(``audit_outbox/``, ``infra_audit_outbox/``) are NEVER touched — only the
``_ingested/`` archive subtree under each.

GUARDS (auditor-grade — this deletes across every bot's workspace):

* Bot homes are enumerated via ``platform_profile`` (``config.bot_home``),
  never hardcoded ``/Users/<bot>``.
* The only paths ever ``rmtree``/unlinked are ones whose final component is
  literally ``_ingested`` OR a ``<YYYY-MM-DD>`` date-dir directly under an
  ``_ingested`` dir. Anything else raises ``UnsafePurgeTarget`` and is
  refused. Symlinks are refused. The resolved parent is containment-checked
  against the expected ``_ingested`` root, the same shape as
  ``signals/retention.py``'s archived-bot-dir removal.
* The ``evolve`` user has rw ACL on ``workspace/evolve`` and owns the shared
  dir, so direct ``rmtree``/``unlink`` works — there is deliberately NO
  ``sudo /bin/rm`` fallback (sudo-rm grants are anti-footprint; see the
  sibling F-5-CLEAN chip).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


class UnsafePurgeTarget(Exception):
    """Raised when a path is not a literal ``_ingested`` dir or a
    ``<YYYY-MM-DD>`` date-dir directly under one. The purge refuses it."""


@dataclass
class _DirStat:
    files: int = 0
    bytes: int = 0


@dataclass
class BotPurgeResult:
    """Per-target outcome (one bot's audit outbox, or the shared infra outbox)."""

    label: str  # bot_id, or "<infra>" for the shared infra outbox
    ingested_dir: str
    files_removed: int = 0
    bytes_removed: int = 0
    date_dirs_removed: int = 0
    skipped_reason: str = ""  # non-empty ⇒ nothing was removed (e.g. "absent")
    errors: list[str] = field(default_factory=list)


@dataclass
class PurgeResult:
    dry_run: bool
    older_than_days: int | None
    targets: list[BotPurgeResult] = field(default_factory=list)

    @property
    def total_files(self) -> int:
        return sum(t.files_removed for t in self.targets)

    @property
    def total_bytes(self) -> int:
        return sum(t.bytes_removed for t in self.targets)

    @property
    def total_date_dirs(self) -> int:
        return sum(t.date_dirs_removed for t in self.targets)


# ── Guards ──────────────────────────────────────────────────────────────────


def assert_safe_ingested_target(path: Path, ingested_root: Path) -> None:
    """Raise ``UnsafePurgeTarget`` unless *path* is safe to remove.

    Safe means exactly one of:

    * *path* is the ``_ingested`` root itself (final component ``_ingested``),
      resolving to the same inode as *ingested_root*; or
    * *path* is a ``<YYYY-MM-DD>`` date-dir whose resolved parent IS
      *ingested_root* (final component ``_ingested``).

    Symlinks are refused outright. Resolution + parent containment mirror
    ``signals/retention.py::_remove_archived_bot_dir`` so a crafted or
    drifted path can't escape the archive subtree.
    """
    if path.is_symlink():
        raise UnsafePurgeTarget(f"refusing symlink target: {path}")

    root_resolved = ingested_root.resolve()
    if root_resolved.name != "_ingested":
        raise UnsafePurgeTarget(
            f"ingested_root final component is not '_ingested': {ingested_root}"
        )

    resolved = path.resolve()

    # Case 1: the whole _ingested dir.
    if resolved == root_resolved:
        return

    # Case 2: a <YYYY-MM-DD> date-dir directly under _ingested.
    if resolved.parent != root_resolved:
        raise UnsafePurgeTarget(
            f"target is not directly under {ingested_root!s}: {path}"
        )
    try:
        date.fromisoformat(resolved.name)
    except ValueError:
        raise UnsafePurgeTarget(
            f"target final component is not a YYYY-MM-DD date-dir: {path}"
        )


# ── Sizing ──────────────────────────────────────────────────────────────────


def _measure(path: Path) -> _DirStat:
    """Count files + bytes under *path* (a directory). Best-effort: unreadable
    entries are skipped, never raise."""
    stat = _DirStat()
    if not path.is_dir():
        return stat
    for child in path.rglob("*"):
        try:
            if child.is_file() or child.is_symlink():
                stat.files += 1
                if not child.is_symlink():
                    stat.bytes += child.stat().st_size
        except OSError:
            continue
    return stat


# ── Per-target purge ────────────────────────────────────────────────────────


def _purge_one(
    label: str,
    ingested_dir: Path,
    *,
    dry_run: bool,
    cutoff_date: date | None,
) -> BotPurgeResult:
    """Purge a single ``_ingested`` dir.

    When *cutoff_date* is None, the whole ``_ingested`` dir is removed. When
    set, only ``<YYYY-MM-DD>`` date-dirs strictly older than it are removed
    (the recent window is kept). Loose non-date entries directly under
    ``_ingested`` are left untouched in age-filtered mode (the guard only
    admits date-dirs there).
    """
    res = BotPurgeResult(label=label, ingested_dir=str(ingested_dir))

    if ingested_dir.is_symlink():
        res.skipped_reason = "symlink (refused)"
        res.errors.append(f"refusing symlink _ingested dir: {ingested_dir}")
        return res
    if not ingested_dir.exists():
        res.skipped_reason = "absent"
        return res
    if not ingested_dir.is_dir():
        res.skipped_reason = "not a directory"
        return res

    if cutoff_date is None:
        # Remove the entire _ingested dir.
        try:
            assert_safe_ingested_target(ingested_dir, ingested_dir)
        except UnsafePurgeTarget as exc:
            res.errors.append(str(exc))
            return res
        stat = _measure(ingested_dir)
        res.files_removed = stat.files
        res.bytes_removed = stat.bytes
        # Count immediate date-dir children for reporting.
        res.date_dirs_removed = sum(1 for c in ingested_dir.iterdir() if c.is_dir())
        if not dry_run:
            try:
                shutil.rmtree(ingested_dir)
            except OSError as exc:
                res.errors.append(f"rmtree failed: {exc}")
        return res

    # Age-filtered: only date-dirs strictly older than the cutoff.
    for child in sorted(ingested_dir.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        try:
            dir_date = date.fromisoformat(child.name)
        except ValueError:
            continue  # not a date-dir — leave it
        if dir_date >= cutoff_date:
            continue  # within the kept window
        try:
            assert_safe_ingested_target(child, ingested_dir)
        except UnsafePurgeTarget as exc:
            res.errors.append(str(exc))
            continue
        stat = _measure(child)
        res.files_removed += stat.files
        res.bytes_removed += stat.bytes
        res.date_dirs_removed += 1
        if not dry_run:
            try:
                shutil.rmtree(child)
            except OSError as exc:
                res.errors.append(f"rmtree failed for {child}: {exc}")
    return res


# ── Target enumeration ──────────────────────────────────────────────────────


def _bot_ingested_dir(home: Path) -> Path:
    return home / ".openclaw" / "workspace" / "evolve" / "audit_outbox" / "_ingested"


def _enumerate_targets(
    shared_dir: Path, network: dict | None
) -> list[tuple[str, Path]]:
    """Return ``[(label, ingested_dir), ...]`` for every purge target,
    pod-wide: one per account that has an ``audit_outbox/_ingested`` dir, plus
    the shared infra outbox.

    Enumeration is filesystem-driven — every entry under the platform's
    ``user_home_root`` (``/Users`` on macOS, ``/home`` on Linux) that holds an
    ``.openclaw/workspace/evolve/audit_outbox/_ingested`` dir is a target. This
    catches RETIRED bots whose account + dead archive still exist on disk but
    are no longer in ``network.json`` (e.g. the mini's ``ledger``) — a
    network-only enumeration would strand those. The home-root prefix comes
    from ``platform_profile``, never hardcoded.

    Network-resolved bot homes are unioned in for the rare case a bot's home
    lives outside ``user_home_root`` (a shared/personal account). Targets are
    deduped by resolved ``_ingested`` path so an account reached both ways is
    processed once.
    """
    # Absolute imports (not ``from .config``) so this module also runs as a
    # standalone top-level script on a pod — the one-time sweep is invoked via
    # ``python3 -m`` against the installed ``evolve_admin`` package before this
    # branch is deployed.
    from evolve_admin.config import bot_home, load_network
    from evolve_admin.applications.infra_audit import infra_audit_outbox_ingested
    from platform_profile import get_profile

    if network is None:
        try:
            network = load_network()
        except Exception:
            network = {}

    targets: list[tuple[str, Path]] = []
    seen: set[str] = set()

    def _add(label: str, ingested: Path) -> None:
        key = str(ingested.resolve()) if ingested.exists() else str(ingested)
        if key in seen:
            return
        seen.add(key)
        targets.append((label, ingested))

    # 1. Filesystem scan of every account home (the authoritative pod-wide set).
    home_root = Path(get_profile().user_home_root)
    try:
        entries = sorted(p for p in home_root.iterdir() if p.is_dir())
    except OSError:
        entries = []
    for home in entries:
        ingested = _bot_ingested_dir(home)
        if ingested.exists():
            _add(home.name, ingested)

    # 2. Union in network-resolved bot homes (homes outside user_home_root).
    bots = (network or {}).get("bots") or {}
    for bot_id, cfg in bots.items():
        if not isinstance(cfg, dict):
            continue
        try:
            home = bot_home(bot_id, network)
        except Exception:
            continue
        ingested = _bot_ingested_dir(home)
        if ingested.exists():
            _add(bot_id, ingested)

    # 3. Shared, pod-wide infra audit outbox (one, not per-bot).
    _add("<infra>", infra_audit_outbox_ingested(Path(shared_dir)))
    return targets


# ── Public entry point ──────────────────────────────────────────────────────


def purge_ingested_backlog(
    shared_dir: Path,
    *,
    network: dict | None = None,
    dry_run: bool = False,
    older_than_days: int | None = None,
    now: datetime | None = None,
) -> PurgeResult:
    """Purge the ``_ingested`` audit-archive backlog pod-wide.

    With ``older_than_days=None`` (default) the entire ``_ingested`` subtree
    under each bot's ``audit_outbox`` and the shared ``infra_audit_outbox`` is
    removed. With ``older_than_days=N`` only date-dirs strictly older than N
    days are removed, keeping a recent window.

    The live outbox roots are never touched. See module docstring for guards.
    """
    cutoff_date: date | None = None
    if older_than_days is not None:
        cutoff_now = now or datetime.now(timezone.utc)
        cutoff_date = (cutoff_now - timedelta(days=older_than_days)).date()

    result = PurgeResult(dry_run=dry_run, older_than_days=older_than_days)
    for label, ingested_dir in _enumerate_targets(Path(shared_dir), network):
        result.targets.append(
            _purge_one(
                label, ingested_dir, dry_run=dry_run, cutoff_date=cutoff_date
            )
        )
    return result


def _fmt_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if f < 1024 or unit == "T":
            return f"{f:.1f}{unit}" if unit != "B" else f"{int(f)}B"
        f /= 1024
    return f"{f:.1f}T"


def _main(argv: list[str]) -> int:
    from platform_profile import get_profile

    parser = argparse.ArgumentParser(
        description=(
            "One-time purge of the audit-outbox _ingested backlog "
            "(audit_outbox/_ingested + infra_audit_outbox/_ingested), pod-wide."
        )
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=Path(get_profile().shared_dir_default),
        help="Pod shared dir (default: platform profile shared_dir_default).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what WOULD be removed (counts + bytes per target) "
        "without deleting anything.",
    )
    parser.add_argument(
        "--older-than-days",
        type=int,
        default=None,
        help="Only remove date-dirs strictly older than N days "
        "(default: remove ALL _ingested content).",
    )
    args = parser.parse_args(argv)

    result = purge_ingested_backlog(
        args.shared_dir,
        dry_run=args.dry_run,
        older_than_days=args.older_than_days,
    )

    verb = "would remove" if result.dry_run else "removed"
    for t in result.targets:
        if t.skipped_reason and not t.files_removed:
            line = f"  {t.label:<20} {t.skipped_reason}"
        else:
            line = (
                f"  {t.label:<20} {verb} {t.files_removed} files / "
                f"{_fmt_bytes(t.bytes_removed)} ({t.date_dirs_removed} date-dirs)"
            )
        print(line)
        for err in t.errors:
            print(f"    ! {err}")

    print(
        f"{'DRY-RUN — ' if result.dry_run else ''}total {verb}: "
        f"{result.total_files} files / {_fmt_bytes(result.total_bytes)} "
        f"across {len(result.targets)} targets "
        f"({result.total_date_dirs} date-dirs)"
    )
    any_errors = any(t.errors for t in result.targets)
    return 1 if any_errors else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
