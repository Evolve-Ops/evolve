#!/usr/bin/env python3
"""
evolve/app_session_correlator.py — Phase 6: App-session association

Associates Claude sessions with the apps (manifests) they invoked using a
three-tier attribution strategy:

  1. capabilities_invoked  (primary, session-level)
     session_summary records carry a `capabilities_invoked` list produced by
     the SessionSummarizer LLM.  Each capability name is fuzzy-matched against
     manifest display names and capability_tags.  This works even when no files
     are modified during a session — covering the common case of conversational
     app use.

  2. File mtime correlation  (secondary)
     For sessions not matched by capabilities, compare the session's time window
     against modification times of the files listed in each manifest.  Catches
     bot-triggered background tasks and automated writes.

  3. session_keywords fallback  (tertiary)
     If a manifest declares explicit session_keywords, match them against the
     session's outcome text.

Usage (standalone):
    python3 app_session_correlator.py --bot admin_bot --date 2026-04-15
    python3 app_session_correlator.py --bot admin_bot --date 2026-04-15 --shared-dir /Users/Shared/evolve
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from evolve_config import bot_home as _bot_home


# ── Timestamp helpers ─────────────────────────────────────────────────────────

def _parse_ts(ts_str: str) -> float:
    """Parse an ISO-8601 timestamp string to a Unix float. Returns 0 on failure."""
    if not ts_str:
        return 0.0
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


# ── Manifest loading ──────────────────────────────────────────────────────────

def _load_manifests(bot_id: str, shared_dir: str) -> list[dict]:
    """
    Load all app manifests for a bot.

    Primary path:  /Users/{bot_id}/.openclaw/workspace/manifests/{app_id}.json
    Fallback path: {shared_dir}/applications/{bot_id}/{app_id}.json

    Manifests with status "paused" or "deprecated" are excluded.
    """
    primary_dir = _bot_home(bot_id) / ".openclaw" / "workspace" / "manifests"
    fallback_dir = Path(shared_dir) / "applications" / bot_id

    manifests: list[dict] = []
    seen_app_ids: set[str] = set()

    for search_dir in (primary_dir, fallback_dir):
        if not search_dir.exists():
            continue
        for mf in search_dir.glob("*.json"):
            try:
                data = json.loads(mf.read_text())
            except Exception:
                continue
            # Skip paused/deprecated apps
            status = data.get("status", "")
            if status in ("paused", "deprecated"):
                continue
            # Use app_id from the manifest if present, otherwise use filename stem
            app_id = data.get("app_id") or data.get("id") or mf.stem
            if app_id in seen_app_ids:
                continue  # primary already loaded this app
            seen_app_ids.add(app_id)
            data["_app_id"] = app_id  # ensure app_id is accessible
            manifests.append(data)

    return manifests


def _collect_file_paths(manifest: dict, workspace_root: Path) -> list[Path]:
    """
    Extract absolute file paths for mtime correlation.

    Resolution order:
    1. `files` field (v5: list[dict] with "path"; v4: list[str])
    2. `crons` field (v5: list[dict] with "script"; v4: list[str])
    3. `evidence_files` fallback — scanner writes these but forge hasn't run yet
       to promote them into the formal `files` registry.  Using them as a proxy
       is safe: they are the actual on-disk files the scanner detected.
    """
    paths: list[Path] = []

    # ── files field ───────────────────────────────────────────────────────────
    for entry in manifest.get("files", []):
        if isinstance(entry, dict):
            rel = entry.get("path", "")
        elif isinstance(entry, str):
            rel = entry
        else:
            continue
        if rel:
            paths.append(workspace_root / rel)

    # ── crons field ───────────────────────────────────────────────────────────
    for entry in manifest.get("crons", []):
        if isinstance(entry, dict):
            rel = entry.get("script", "")
        elif isinstance(entry, str):
            rel = entry
        else:
            continue
        if rel:
            paths.append(workspace_root / rel)

    # ── evidence_files fallback (scanner-created manifests before forge runs) ─
    if not paths:
        for raw in manifest.get("evidence_files", []):
            if not isinstance(raw, str) or not raw:
                continue
            # Strip leading tag like "directory: foo/" or "memory: bar.md"
            rel = re.sub(r"^[a-z_]+:\s*", "", raw.strip()).rstrip("/")
            if not rel:
                continue
            # evidence_files may be relative OR absolute paths
            p = Path(rel) if Path(rel).is_absolute() else workspace_root / rel
            paths.append(p)

    return paths


# ── Session window building ───────────────────────────────────────────────────

def _build_session_windows(annotations: list[dict]) -> dict[str, tuple[float, float]]:
    """
    Derive [start, end] time windows for each session from turn_annotation records.

    Rules:
    - All sessions with at least one turn_annotation are included.
    - session_start = min turn timestamp
    - session_end   = max turn timestamp + 5-minute buffer (300 s)

    Returns: {session_id: (start_unix, end_unix)}
    """
    BUFFER_SECS = 300  # 5 minutes

    all_times: dict[str, list[float]] = {}

    for ann in annotations:
        if ann.get("type") != "turn_annotation":
            continue
        sid = ann.get("session_id")
        if not sid:
            continue
        ts = _parse_ts(ann.get("ts", ""))
        if ts == 0.0:
            continue
        all_times.setdefault(sid, []).append(ts)

    windows: dict[str, tuple[float, float]] = {}
    for sid, times in all_times.items():
        start = min(times)
        end   = max(times) + BUFFER_SECS
        windows[sid] = (start, end)

    return windows


# ── Capabilities-based correlation (primary) ──────────────────────────────────

def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for fuzzy name matching."""
    text = text.lower()
    text = re.sub(r"[_\-/\\]", " ", text)     # treat separators as spaces
    text = re.sub(r"[^a-z0-9 ]", "", text)    # strip remaining punctuation
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _build_name_index(manifests: list[dict]) -> dict[str, str]:
    """
    Build a {normalized_name: app_id} lookup from manifest display names,
    snake_case names, and capability_tags.

    When multiple manifests would produce the same normalized key the first one
    (primary directory order) wins.
    """
    index: dict[str, str] = {}

    for m in manifests:
        app_id = m["_app_id"]
        candidates: list[str] = []

        # display_name is the canonical human-readable name
        dn = m.get("display_name") or m.get("name") or ""
        if dn:
            candidates.append(dn)

        # capability_tags (strings) also appear in capabilities_invoked text
        for tag in m.get("capability_tags", []):
            if isinstance(tag, str) and tag:
                candidates.append(tag)

        # identity.name / identity.display_name (nested format)
        identity = m.get("identity", {})
        if isinstance(identity, dict):
            for key in ("name", "display_name"):
                v = identity.get(key)
                if v and isinstance(v, str):
                    candidates.append(v)

        for c in candidates:
            key = _normalize(c)
            if key and key not in index:
                index[key] = app_id

    return index


def _session_capabilities(annotations: list[dict]) -> dict[str, list[str]]:
    """
    Build {session_id: [capability_name, ...]} from session_summary records.

    Uses the last session_summary for a given session_id if there are duplicates.
    """
    caps: dict[str, list[str]] = {}
    for ann in annotations:
        if ann.get("type") != "session_summary":
            continue
        sid = ann.get("session_id")
        if not sid:
            continue
        invoked = ann.get("capabilities_invoked", [])
        if isinstance(invoked, list):
            caps[sid] = [c for c in invoked if isinstance(c, str)]
    return caps


def _correlate_by_capabilities(
    session_ids: set[str],
    session_caps: dict[str, list[str]],
    name_index: dict[str, str],
) -> dict[str, set[str]]:
    """
    Match sessions to apps using the capabilities_invoked list in session_summary.

    For each session, each capability name is:
    1. Exact-normalized match against the name index.
    2. Substring match: the normalized name appears inside the normalized capability
       string, or vice versa (handles "Health Tracking App" ↔ "Health Tracking").

    Returns: {session_id: {app_id, ...}}  — only sessions with ≥1 match included.
    """
    result: dict[str, set[str]] = {}

    for sid in session_ids:
        caps = session_caps.get(sid, [])
        if not caps:
            continue

        matched: set[str] = set()
        for cap in caps:
            cap_norm = _normalize(cap)
            if not cap_norm:
                continue

            # Exact match
            if cap_norm in name_index:
                matched.add(name_index[cap_norm])
                continue

            # Substring match: walk every known name.
            # Require both sides to be at least 4 chars to avoid short tokens
            # (e.g. "log", "web") causing false-positive matches.
            if len(cap_norm) >= 4:
                for known_name, app_id in name_index.items():
                    if len(known_name) >= 4 and (known_name in cap_norm or cap_norm in known_name):
                        matched.add(app_id)

        if matched:
            result[sid] = matched

    return result


# ── Keyword fallback ──────────────────────────────────────────────────────────

def _session_outcomes(annotations: list[dict]) -> dict[str, str]:
    """Build a {session_id: outcome_text} map from session_summary records."""
    outcomes: dict[str, str] = {}
    for ann in annotations:
        if ann.get("type") != "session_summary":
            continue
        sid = ann.get("session_id")
        if sid:
            # Use last summary if there are duplicates
            outcomes[sid] = (ann.get("outcome") or "").lower()
    return outcomes


# ── Core correlator ───────────────────────────────────────────────────────────

def correlate_sessions(
    annotations: list[dict],
    bot_id: str,
    shared_dir: str,
) -> dict[str, list[str]]:
    """
    Associate sessions with apps via three-tier attribution:

    Tier 1 — capabilities_invoked (primary)
        session_summary records carry a `capabilities_invoked` list produced by
        the SessionSummarizer LLM.  Fuzzy-match each capability name against
        manifest display names and tags.  Works for any conversational app use,
        even when no files are modified.

    Tier 2 — file mtime correlation (secondary)
        For sessions not matched by Tier 1, check whether any manifest file was
        modified during the session's time window.  Catches background tasks and
        automated writes.

    Tier 3 — session_keywords fallback (tertiary)
        If a manifest declares explicit session_keywords, match them against the
        session outcome text.

    Returns: {session_id: [app_id, ...]}
    """
    # Build time windows for qualifying sessions
    windows = _build_session_windows(annotations)
    if not windows:
        return {}

    # Load manifests
    manifests = _load_manifests(bot_id, shared_dir)
    if not manifests:
        return {}

    bot_workspace = _bot_home(bot_id) / ".openclaw" / "workspace"
    result: dict[str, set[str]] = {sid: set() for sid in windows}

    # ── Tier 1: capabilities_invoked matching ─────────────────────────────────
    session_caps = _session_capabilities(annotations)
    name_index   = _build_name_index(manifests)
    cap_matches  = _correlate_by_capabilities(set(windows.keys()), session_caps, name_index)
    for sid, app_ids in cap_matches.items():
        result[sid].update(app_ids)

    # Sessions not matched by Tier 1 fall through to mtime + keyword checks
    unmatched = {sid for sid, apps in result.items() if not apps}

    # ── Tier 2 & 3: mtime + keyword fallback ─────────────────────────────────
    if unmatched:
        outcomes = _session_outcomes(annotations)

        for manifest in manifests:
            app_id = manifest["_app_id"]
            file_paths = _collect_file_paths(manifest, bot_workspace)
            session_keywords: list[str] = [
                kw.lower() for kw in manifest.get("session_keywords", []) if isinstance(kw, str)
            ]

            # Collect all mtimes (ignore missing/unreadable files)
            mtimes: list[float] = []
            for fp in file_paths:
                try:
                    mtimes.append(fp.stat().st_mtime)
                except OSError:
                    pass

            for sid in unmatched:
                if app_id in result[sid]:
                    continue  # already matched

                start, end = windows[sid]
                matched = False

                # Tier 2: mtime-based
                for mtime in mtimes:
                    if start <= mtime <= end:
                        matched = True
                        break

                # Tier 3: keyword match against session outcome
                if not matched and session_keywords:
                    outcome = outcomes.get(sid, "")
                    if any(kw in outcome for kw in session_keywords):
                        matched = True

                if matched:
                    result[sid].add(app_id)

    return {sid: sorted(apps) for sid, apps in result.items() if apps}


# ── Standalone main ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Correlate sessions to apps for a given bot and date"
    )
    parser.add_argument("--bot", required=True, help="Bot ID (e.g. admin_bot)")
    parser.add_argument("--date", required=True, help="Date in YYYY-MM-DD format")
    parser.add_argument(
        "--shared-dir",
        default="/Users/Shared/evolve",
        help="Path to shared evolve directory (default: /Users/Shared/evolve)",
    )
    args = parser.parse_args()

    from datetime import date as _date
    target_date = _date.fromisoformat(args.date)
    shared_dir = args.shared_dir

    # Load annotations
    ann_file = Path(shared_dir) / "annotations" / args.bot / f"{target_date}.jsonl"
    if not ann_file.exists():
        print(f"No annotation file found: {ann_file}")
        sys.exit(1)

    annotations: list[dict] = []
    with open(ann_file) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    annotations.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    print(f"Loaded {len(annotations)} annotation records for {args.bot} on {target_date}")

    session_app_map = correlate_sessions(annotations, args.bot, shared_dir)

    sessions_with_match = len(session_app_map)
    total_sessions = len(_build_session_windows(annotations))
    all_apps: set[str] = set()
    for apps in session_app_map.values():
        all_apps.update(apps)

    print(f"\nSummary:")
    print(f"  Total qualifying sessions:             {total_sessions}")
    print(f"  Sessions matched to at least one app:  {sessions_with_match}")
    print(f"  Unique apps matched:                   {len(all_apps)}")

    if session_app_map:
        # Show tier attribution in standalone mode
        session_caps = _session_capabilities(annotations)
        name_index = _build_name_index(_load_manifests(args.bot, shared_dir))

        print(f"\nSession → App associations:")
        for sid, apps in sorted(session_app_map.items()):
            # Indicate which tier matched
            caps_matched = bool(_correlate_by_capabilities({sid}, session_caps, name_index).get(sid))
            tier = "caps" if caps_matched else "mtime/kw"
            print(f"  {sid[:16]}…  [{tier}]  →  {', '.join(apps)}")
    else:
        print("\nNo session-app associations found.")


if __name__ == "__main__":
    main()
