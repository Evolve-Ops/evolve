#!/usr/bin/env python3
"""
usage_logger.py — App activity sweep via manifest file mtimes

For each installed app manifest, walk the file paths it claims as evidence
(evidence_files / files / crons) and stat them for modification times.
The signal is "when was anything this app cares about last touched?" — a
direct, deterministic proxy for whether the app is still in active use.

This replaces the prior session-correlation approach, which depended on a
brittle keyword-match between SessionSummarizer output and manifest names
and produced near-zero attribution for most apps.

Output: {sharedDir}/{botId}/recommendations/usage-stats.json
  {
    "schema_version": 2,
    "bot_id": "admin_bot",
    "computed_at": "...",
    "apps": {
      "health-tracking": {
        "last_modified_ts": "2026-05-02T14:32:00+00:00",
        "days_since_modified": 3,
        "active_files_30d": 4,
        "active_files_60d": 6,
        "total_files": 12,
        "evidence_present": true
      },
      "stale-app": {
        "last_modified_ts": "2025-08-15T...",
        "days_since_modified": 263,
        "active_files_30d": 0,
        "active_files_60d": 0,
        "total_files": 5,
        "evidence_present": true
      },
      "no-files-app": {
        "last_modified_ts": null,
        "days_since_modified": null,
        "active_files_30d": 0,
        "active_files_60d": 0,
        "total_files": 0,
        "evidence_present": false
      }
    }
  }

Read by:
  - api_analytics_applications (admin UI — shows last-touched on app cards)

Privacy invariant: processes one bot at a time. No cross-bot reads ever occur.

Schedule: daily at 03:30.

Usage:
    python3 usage_logger.py --network /Users/Shared/evolve/network.json
    python3 usage_logger.py --bot admin_bot --shared-dir /Users/Shared/evolve
    python3 usage_logger.py --bot admin_bot --shared-dir /Users/Shared/evolve --report
    python3 usage_logger.py --bot admin_bot --shared-dir /Users/Shared/evolve --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from evolve_config import CANONICAL_SHARED_DIR
from evolve_config import bot_home as _bot_home
from evolve_util import now_iso_micro as _now_iso

# ── Constants ─────────────────────────────────────────────────────────────────

SCHEMA_VERSION = 2
LOOKBACK_DAYS = 90
UNUSED_FLAG_DAYS = 60   # apps inactive this long get a reactivation rec
MAX_FILES_PER_APP = 2000
MAX_WALK_DEPTH = 6

# Paths that produce no useful signal: caches, VCS metadata, OS junk.
IGNORED_DIR_NAMES = frozenset({
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", ".DS_Store", ".ipynb_checkpoints",
})
IGNORED_FILE_SUFFIXES = (".pyc", ".pyo", ".swp", ".tmp")
IGNORED_FILE_NAMES = frozenset({".DS_Store"})


# ── Manifest loading ──────────────────────────────────────────────────────────

def load_manifests(shared_dir: Path, bot_id: str) -> list[dict]:
    """Load all active app manifests for a bot.

    Primary path:  /Users/{bot_id}/.openclaw/workspace/manifests/{app_id}.json
    Fallback path: {sharedDir}/applications/{bot_id}/manifest-*.json

    Manifests with status "paused" or "deprecated" are excluded.
    """
    manifests: list[dict] = []
    seen: set[str] = set()

    primary = _bot_home(bot_id) / ".openclaw" / "workspace" / "manifests"
    if primary.exists():
        for f in primary.glob("*.json"):
            try:
                m = json.loads(f.read_text())
            except Exception:
                continue
            if m.get("status") in ("paused", "deprecated"):
                continue
            app_id = m.get("id") or f.stem
            if app_id in seen:
                continue
            seen.add(app_id)
            m["_app_id"] = app_id
            manifests.append(m)

    fallback = shared_dir / "applications" / bot_id
    if fallback.exists():
        for f in fallback.glob("manifest-*.json"):
            try:
                m = json.loads(f.read_text())
            except Exception:
                continue
            if m.get("status") in ("paused", "deprecated"):
                continue
            app_id = m.get("id") or f.stem.replace("manifest-", "")
            if app_id in seen:
                continue
            seen.add(app_id)
            m["_app_id"] = app_id
            manifests.append(m)

    return manifests


# ── Evidence path resolution ──────────────────────────────────────────────────

def _strip_evidence_prefix(raw: str) -> str:
    """Strip leading tag like 'directory: foo/' or 'memory: bar.md'."""
    return re.sub(r"^[a-z_]+:\s*", "", raw.strip()).rstrip("/")


def collect_evidence_paths(manifest: dict, workspace_root: Path) -> list[Path]:
    """Resolve every file/directory path the manifest claims as evidence.

    Resolution order, deduplicated:
      1. `files` field (v5+: list[dict] with "path"; v4: list[str])
      2. `crons` field (v5+: list[dict] with "script"; v4: list[str])
      3. `evidence_files` (raw scanner output with optional 'kind:' prefixes)
      4. `realized_files` (v7-arc Instances — on-disk JSON has empty files[];
         the hydrator grafts realized_files into files[] for the UI, but
         this code reads the raw manifest. Mirrors the v7-arc fallback in
         evolve_admin/app_permissions/reconciler.py:_file_paths.)
    """
    paths: list[Path] = []
    seen: set[str] = set()

    def _add(rel: str) -> None:
        if not rel:
            return
        p = Path(rel) if Path(rel).is_absolute() else workspace_root / rel
        key = str(p)
        if key in seen:
            return
        seen.add(key)
        paths.append(p)

    saw_files_entry = False
    for entry in manifest.get("files", []) or []:
        if isinstance(entry, dict):
            path = entry.get("path", "")
            if path:
                saw_files_entry = True
                _add(path)
        elif isinstance(entry, str):
            saw_files_entry = True
            _add(entry)

    for entry in manifest.get("crons", []) or []:
        if isinstance(entry, dict):
            _add(entry.get("script", ""))
        elif isinstance(entry, str):
            _add(entry)

    for raw in manifest.get("evidence_files", []) or []:
        if isinstance(raw, str):
            _add(_strip_evidence_prefix(raw))

    shape = manifest.get("manifest_shape", "")
    realized = manifest.get("realized_files") or []
    if (shape == "v7-arc" or not saw_files_entry) and realized:
        for r in realized:
            if isinstance(r, dict):
                _add(r.get("path", ""))

    return paths


def _iter_files(path: Path, max_files: int) -> list[Path]:
    """Return files reachable under path (recursive for dirs), capped + filtered."""
    if not path.exists():
        return []

    if path.is_file():
        return [path] if path.name not in IGNORED_FILE_NAMES else []

    if not path.is_dir():
        return []

    out: list[Path] = []
    root_depth = len(path.parts)
    try:
        for cur, dirs, files in os.walk(path, followlinks=False):
            cur_path = Path(cur)
            depth = len(cur_path.parts) - root_depth
            if depth > MAX_WALK_DEPTH:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
            for fn in files:
                if fn in IGNORED_FILE_NAMES:
                    continue
                if fn.endswith(IGNORED_FILE_SUFFIXES):
                    continue
                out.append(cur_path / fn)
                if len(out) >= max_files:
                    return out
    except OSError:
        pass
    return out


# ── Per-app sweep ─────────────────────────────────────────────────────────────

def sweep_app(manifest: dict, workspace_root: Path, now_dt: datetime) -> dict:
    """Compute activity stats for one app from its evidence paths.

    Returns the per-app stats dict that gets written into usage-stats.json.
    """
    cutoff_30 = (now_dt - timedelta(days=30)).timestamp()
    cutoff_60 = (now_dt - timedelta(days=60)).timestamp()

    declared = collect_evidence_paths(manifest, workspace_root)
    evidence_present = bool(declared)

    files: list[Path] = []
    for p in declared:
        files.extend(_iter_files(p, MAX_FILES_PER_APP - len(files)))
        if len(files) >= MAX_FILES_PER_APP:
            break

    if not files:
        return {
            "last_modified_ts": None,
            "days_since_modified": None,
            "active_files_30d": 0,
            "active_files_60d": 0,
            "total_files": 0,
            "evidence_present": evidence_present,
        }

    max_mtime = 0.0
    active_30 = 0
    active_60 = 0
    for f in files:
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        if mt > max_mtime:
            max_mtime = mt
        if mt >= cutoff_30:
            active_30 += 1
        if mt >= cutoff_60:
            active_60 += 1

    if max_mtime == 0.0:
        last_ts = None
        days_since = None
    else:
        last_dt = datetime.fromtimestamp(max_mtime, tz=timezone.utc)
        last_ts = last_dt.isoformat()
        days_since = max(0, (now_dt - last_dt).days)

    return {
        "last_modified_ts": last_ts,
        "days_since_modified": days_since,
        "active_files_30d": active_30,
        "active_files_60d": active_60,
        "total_files": len(files),
        "evidence_present": evidence_present,
    }


# ── Output ────────────────────────────────────────────────────────────────────

def write_usage_stats(
    bot_id: str,
    shared_dir: Path,
    stats: dict[str, dict],
    dry_run: bool = False,
) -> None:
    """Write usage-stats.json to {sharedDir}/{botId}/recommendations/."""
    recs_dir = shared_dir / bot_id / "recommendations"
    recs_dir.mkdir(parents=True, exist_ok=True)
    out_path = recs_dir / "usage-stats.json"

    payload = {
        "schema_version": SCHEMA_VERSION,
        "bot_id": bot_id,
        "computed_at": _now_iso(),
        "window_days": LOOKBACK_DAYS,
        "apps": stats,
    }

    if dry_run:
        print(f"[usage] [dry-run] would write {out_path}")
        for app_id, s in sorted(stats.items()):
            last = (s.get("last_modified_ts") or "never")[:10]
            print(f"  {app_id}: last={last} 30d_active={s['active_files_30d']} "
                  f"total={s['total_files']}")
        return

    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix="evolve-usage-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, indent=2)
    try:
        shutil.move(tmp, str(out_path))
    except (PermissionError, OSError):
        import subprocess
        subprocess.run(["sudo", "/bin/cp", tmp, str(out_path)],
                       capture_output=True, check=False)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load_usage_stats(bot_id: str, shared_dir: Path) -> dict:
    """Load usage-stats.json. Returns empty dict if absent."""
    path = shared_dir / bot_id / "recommendations" / "usage-stats.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run_usage_logger(
    bot_id: str,
    shared_dir: Path,
    dry_run: bool = False,
    report: bool = False,
) -> dict[str, dict]:
    """Sweep evidence-file mtimes for one bot and write usage-stats.json.

    Returns the per-app stats dict (keyed by app_id).
    """
    print(f"[usage] {bot_id}: sweeping manifest evidence paths…")

    manifests = load_manifests(shared_dir, bot_id)
    if not manifests:
        print(f"[usage] {bot_id}: no manifests found")
        return {}

    workspace_root = _bot_home(bot_id) / ".openclaw" / "workspace"
    now_dt = datetime.now(timezone.utc)

    stats: dict[str, dict] = {}
    for m in manifests:
        app_id = m["_app_id"]
        stats[app_id] = sweep_app(m, workspace_root, now_dt)

    if report:
        print(f"\n{'App':<32} {'Last touched':<14} {'30d':>5} {'60d':>5} {'Total':>6} {'Ev':>3}")
        print("-" * 72)
        for app_id, s in sorted(stats.items(),
                                key=lambda kv: kv[1].get("days_since_modified") or 99999):
            last = (s.get("last_modified_ts") or "never")[:10]
            ev = "Y" if s["evidence_present"] else "—"
            print(f"{app_id:<32} {last:<14} {s['active_files_30d']:>5} "
                  f"{s['active_files_60d']:>5} {s['total_files']:>6} {ev:>3}")
        return stats

    write_usage_stats(bot_id, shared_dir, stats, dry_run=dry_run)
    if not dry_run:
        active = sum(1 for s in stats.values() if (s.get("days_since_modified") or 99999) <= 30)
        print(f"[usage] {bot_id}: usage-stats.json written — {len(stats)} apps "
              f"({active} active in 30d)")

    return stats


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="App activity sweep via manifest file mtimes")
    parser.add_argument("--shared-dir", default=str(CANONICAL_SHARED_DIR))
    parser.add_argument("--network", help="Path to network.json (processes all bots)")
    parser.add_argument("--bot", dest="bot_id", help="Single bot to process")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be written without writing")
    parser.add_argument("--report", action="store_true",
                        help="Print activity table, no file writes")
    args = parser.parse_args()

    shared_dir = Path(args.shared_dir)
    bots: list[str] = []

    if args.bot_id:
        bots = [args.bot_id]
    elif args.network:
        try:
            net = json.loads(Path(args.network).read_text())
            shared_dir = Path(net.get("sharedDir", str(shared_dir)))
            bots = net.get("members", [])
        except Exception as e:
            print(f"[usage] Failed to read network.json: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        net_path = shared_dir / "network.json"
        if net_path.exists():
            try:
                net = json.loads(net_path.read_text())
                bots = net.get("members", [])
            except Exception:
                pass

    if not bots:
        parser.error("Specify --bot BOT_ID, --network PATH, or ensure network.json exists")

    for bot in bots:
        run_usage_logger(bot, shared_dir, args.dry_run, args.report)


if __name__ == "__main__":
    main()
