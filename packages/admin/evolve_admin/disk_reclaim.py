"""disk_reclaim — pure-Python scanner for reclaimable space on the pod host.

The report half of pod-host disk hygiene (PR1 of the 3-PR plan in
docs/spec-delta-disk-reclaim-2026-06-21.md). This module only *sizes*
what is reclaimable — it never deletes or truncates anything. Deletion
is a separate, privileged PR (PR2) that imports the category/path/cap
constants from here so "what is reclaimable" has a single source of
truth.

Cheap by design: no LLM, no sudo, no subprocess. The admin server runs
as the ``evolve`` user and bot homes are mode 0755, so ``evolve`` can
stat/walk other accounts' ``.npm`` and ``.openclaw/logs`` directly —
sizing needs no privilege escalation. (Deletion does; that is PR2.)

Categories (each ``/Users/*`` home is globbed):
  - ``npm_cache``           — ``<home>/.npm/_cacache`` + ``<home>/.npm/_npx``
                              whole-dir sizes. Fully regenerable → ``rm``.
  - ``evolve_logs_oversized`` — ``<home>/.evolve/logs/*.log`` ≥ EVOLVE_LOG_CAP.
                              Truncate-in-place → ``truncate``.
  - ``oc_rotated_logs``     — ``<home>/.openclaw/logs/openclaw*.log`` whose
                              name ends ``.1.log`` / ``.2.log`` / ``.3.log``
                              and is ≥ OC_LOG_CAP. Truncate → ``truncate``.

Tri-state sizing: a category whose tree was readable and genuinely empty
reports ``bytes: 0, partial: False`` ("checked, nothing to reclaim"); a
category whose tree could not be read reports ``partial: True`` so the
operator never mistakes "couldn't read" for "0 bytes reclaimable". Per
path, PermissionError / FileNotFoundError are skipped (best-effort) but
flip the owning category's ``partial`` flag.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

# ── Reclaim contract — the single source of truth shared with PR2 ─────────────
#
# PR2 (the privileged reclaimer) and the tests import THESE constants rather
# than re-deriving the globs/caps, so "what is reclaimable" can never drift
# between the scanner that reports it and the reaper that acts on it.

DEFAULT_ROOTS: tuple[str, ...] = ("/Users",)

# An Evolve log only counts as reclaimable once it is genuinely oversized;
# below the cap it is normal operational logging and left alone.
EVOLVE_LOG_CAP = 50 * 1024 * 1024   # 50 MiB
# OpenClaw keeps a small live log plus rotated siblings; only the rotated
# ones (and only once non-trivial) are reclaimable.
OC_LOG_CAP = 5 * 1024 * 1024        # 5 MiB

# Category identifiers (stable; PR2 keys its handlers off these).
CAT_NPM_CACHE = "npm_cache"
CAT_EVOLVE_LOGS = "evolve_logs_oversized"
CAT_OC_ROTATED = "oc_rotated_logs"

# Operator-facing label per category.
CATEGORY_LABELS: dict[str, str] = {
    CAT_NPM_CACHE: "npm cache (regenerable)",
    CAT_EVOLVE_LOGS: "Oversized Evolve logs",
    CAT_OC_ROTATED: "Rotated OpenClaw logs",
}

# Reclaim method per category — "rm" = delete whole tree (regenerable),
# "truncate" = zero the file in place (keep the inode/handle, drop bytes).
CATEGORY_METHOD: dict[str, str] = {
    CAT_NPM_CACHE: "rm",
    CAT_EVOLVE_LOGS: "truncate",
    CAT_OC_ROTATED: "truncate",
}

# Path templates relative to a home directory.
NPM_CACHE_RELDIRS: tuple[str, ...] = (".npm/_cacache", ".npm/_npx")
EVOLVE_LOG_GLOB = ".evolve/logs/*.log"
OC_LOG_GLOB = ".openclaw/logs/openclaw*.log"
OC_ROTATED_SUFFIXES: tuple[str, ...] = (".1.log", ".2.log", ".3.log")


# ── Low-level sizing (no symlink following, best-effort) ──────────────────────


def _dir_size(path: Path) -> tuple[int, bool]:
    """Sum the byte size of a directory tree.

    Returns ``(total_bytes, had_error)``. Walks with ``os.scandir`` and never
    follows symlinks (neither into subdirs nor when stat-ing files), so a
    symlink out of the tree can't inflate the total or cause double-counting.
    Any unreadable entry/dir is skipped and flips ``had_error`` — the caller
    turns that into the category's ``partial`` flag.
    """
    total = 0
    had_error = False
    stack = [str(path)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except (PermissionError, FileNotFoundError, OSError):
                        had_error = True
            # end scandir
        except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
            had_error = True
    return total, had_error


def _iter_homes(roots) -> tuple[list[Path], bool]:
    """List candidate home directories under each root.

    Returns ``(homes, root_unreadable)``. ``root_unreadable`` is True when a
    root that exists couldn't be enumerated at all — that is the signal that
    callers must NOT report a clean 0, because the tree was opaque rather
    than empty.
    """
    homes: list[Path] = []
    root_unreadable = False
    for root in roots:
        rp = Path(root)
        try:
            with os.scandir(rp) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            homes.append(Path(entry.path))
                    except OSError:
                        root_unreadable = True
        except FileNotFoundError:
            # An absent root is genuinely "nothing there", not "couldn't read".
            continue
        except (PermissionError, NotADirectoryError, OSError):
            root_unreadable = True
    return homes, root_unreadable


def _file_size(path: Path) -> tuple[int, bool]:
    """Size of a single regular file. Returns ``(bytes, had_error)``."""
    try:
        if path.is_symlink():
            return 0, False
        return path.stat(follow_symlinks=False).st_size, False
    except (PermissionError, FileNotFoundError, OSError):
        return 0, True


# ── Per-category scanners ─────────────────────────────────────────────────────


def _make_category(cat: str) -> dict[str, Any]:
    return {
        "category": cat,
        "label": CATEGORY_LABELS[cat],
        "bytes": 0,
        "method": CATEGORY_METHOD[cat],
        "entries": [],
        "partial": False,
    }


def _scan_npm_cache(homes: list[Path], root_unreadable: bool) -> dict[str, Any]:
    cat = _make_category(CAT_NPM_CACHE)
    cat["partial"] = root_unreadable
    for home in homes:
        for reldir in NPM_CACHE_RELDIRS:
            d = home / reldir
            try:
                if not d.is_dir() or d.is_symlink():
                    continue
            except OSError:
                cat["partial"] = True
                continue
            size, had_error = _dir_size(d)
            if had_error:
                cat["partial"] = True
            if size > 0:
                cat["entries"].append({"path": str(d), "bytes": size})
                cat["bytes"] += size
    return cat


def _scan_evolve_logs(homes: list[Path], root_unreadable: bool) -> dict[str, Any]:
    cat = _make_category(CAT_EVOLVE_LOGS)
    cat["partial"] = root_unreadable
    for home in homes:
        log_dir = home / ".evolve" / "logs"
        try:
            if not log_dir.is_dir():
                continue
            files = sorted(log_dir.glob("*.log"))
        except OSError:
            cat["partial"] = True
            continue
        for f in files:
            size, had_error = _file_size(f)
            if had_error:
                cat["partial"] = True
                continue
            if size >= EVOLVE_LOG_CAP:
                cat["entries"].append({"path": str(f), "bytes": size})
                cat["bytes"] += size
    return cat


def _scan_oc_rotated(homes: list[Path], root_unreadable: bool) -> dict[str, Any]:
    cat = _make_category(CAT_OC_ROTATED)
    cat["partial"] = root_unreadable
    for home in homes:
        log_dir = home / ".openclaw" / "logs"
        try:
            if not log_dir.is_dir():
                continue
            files = sorted(log_dir.glob("openclaw*.log"))
        except OSError:
            cat["partial"] = True
            continue
        for f in files:
            if not f.name.endswith(OC_ROTATED_SUFFIXES):
                continue
            size, had_error = _file_size(f)
            if had_error:
                cat["partial"] = True
                continue
            if size >= OC_LOG_CAP:
                cat["entries"].append({"path": str(f), "bytes": size})
                cat["bytes"] += size
    return cat


# ── Public API ────────────────────────────────────────────────────────────────


def scan_reclaimable(roots=DEFAULT_ROOTS) -> dict[str, Any]:
    """Size everything reclaimable under ``roots`` without deleting anything.

    Returns::

        {
          "total_bytes": int,
          "scanned_at": float,           # epoch seconds
          "categories": [
            {"category": str, "label": str, "bytes": int,
             "method": "rm"|"truncate", "entries": [{"path", "bytes"}, ...],
             "partial": bool},
            ...
          ],
        }

    ``partial`` distinguishes "0 bytes reclaimable" (readable, clean) from
    "couldn't read this tree" — never silently report 0 for an opaque root.
    Pure-Python and best-effort: per-path read errors are skipped, not raised.
    """
    scanned_at = time.time()
    homes, root_unreadable = _iter_homes(roots)
    categories = [
        _scan_npm_cache(homes, root_unreadable),
        _scan_evolve_logs(homes, root_unreadable),
        _scan_oc_rotated(homes, root_unreadable),
    ]
    total = sum(c["bytes"] for c in categories)
    return {
        "total_bytes": total,
        "scanned_at": scanned_at,
        "categories": categories,
    }


# ── Cached hot-path wrapper ───────────────────────────────────────────────────
#
# host_health calls the scan only when the disk Signal fires (≥85% full), but
# that path is a dashboard-polled endpoint — an operator investigating a full
# disk would have the page open and re-poll. A stat walk over a multi-GB npm
# cache on every poll is wasteful, so the hot path goes through a short TTL
# cache. The pure scan_reclaimable above stays uncached for deterministic tests
# and for PR2, which wants a fresh read before it deletes.

_SCAN_CACHE_TTL = 120.0
_scan_cache: tuple[float, tuple, dict[str, Any]] | None = None


def scan_reclaimable_cached(roots=DEFAULT_ROOTS) -> dict[str, Any]:
    """``scan_reclaimable`` with a short TTL cache, for per-poll callers."""
    global _scan_cache
    key = tuple(roots)
    now = time.time()
    if (
        _scan_cache is not None
        and _scan_cache[1] == key
        and (now - _scan_cache[0]) < _SCAN_CACHE_TTL
    ):
        return _scan_cache[2]
    result = scan_reclaimable(roots)
    _scan_cache = (now, key, result)
    return result


def reset_scan_cache() -> None:
    """Drop the TTL cache so the next ``scan_reclaimable_cached`` re-walks.

    Stays pure (no sudo): PR2's reaper calls this after acting so the
    dashboard-polled ``/api/host-health`` reflects the freed space on the
    operator's next poll instead of serving the pre-reclaim total for up to
    the TTL window.
    """
    global _scan_cache
    _scan_cache = None


def trim_categories_for_signal(categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the long per-entry ``entries[]`` lists so the Signal stays lean.

    The Signal store keeps category + label + bytes + method (+ partial); the
    full per-path breakdown belongs to the reclaim UI / PR2, which re-scans.
    """
    trimmed = []
    for c in categories:
        t = {
            "category": c["category"],
            "label": c["label"],
            "bytes": c["bytes"],
            "method": c["method"],
        }
        if c.get("partial"):
            t["partial"] = True
        trimmed.append(t)
    return trimmed


def human_bytes(n: float) -> str:
    """Compact human size, e.g. 14.2 GB. Decimal (GB) to match Finder."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000.0 or unit == "TB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1000.0
    return f"{n:.1f} TB"
