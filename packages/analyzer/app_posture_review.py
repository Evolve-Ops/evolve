#!/usr/bin/env python3
"""
app_posture_review.py — Weekly per-bot inventory of application state.

Spec: docs/spec-manifest-reflex.md §"App posture review (PR4)".

PR4 ships only the gather + render layer — no LLM. The output is a
structured markdown document at ``{shared_dir}/app_posture/{bot_id}.md``
that gets injected into each session's systemAppend by session_surface.py.
PR5 will add an LLM reflection section ("why didn't I anticipate this?",
cluster/split candidates, forward guidance) on top of this snapshot.

What gets gathered, per bot, weekly:

  1. Manifests changed in the last 7 days (mtime-based diff)
  2. ``bot_created_app`` Signals from PR #899 within the window — apps
     the bot self-recorded via the manifest reflex
  3. ``unmanifested_app`` Signals from PR #902 within the window — apps
     the periodic scanner discovered un-manifested
  4. Workspace file orphans — persistent files under the bot's workspace
     not present in any manifest's ``files[]``
  5. A summary of total app inventory

What is NOT gathered (deferred):

  - Cron orphans. The evolve user can't ``sudo -u <bot> crontab -l``
    (CLAUDE.md), so cron orphan detection requires either a new sudoers
    grant or a bot-side dump. Out of scope here. The script the cron
    runs would still appear as a workspace orphan if un-manifested, so
    most signal is captured via file-orphan detection.
  - Transcripts. The "why didn't I anticipate" question (PR5) needs
    transcript access; PR4 only does inventory.

Runs as the ``evolve`` user via launchd (weekly Sunday 04:30). The
shared_dir signal/manifest reads work via existing ACLs; the bot
workspace walk works via ``set_evolve_read_acl`` on ``.openclaw/``.

Usage:
    app_posture_review.py [--network NETWORK_JSON] [--bot BOT_ID]
                          [--shared-dir DIR] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evolve_config import resolve_network_path
from evolve_util import atomic_write_text as _atomic_write


WINDOW_DAYS = 7

# System surfaces — files under these paths in the bot's workspace are
# never considered orphans because they're infrastructure rather than
# user-created apps. Same intent as scanner.OC_DEFAULT_FILES /
# OC_DEFAULT_DIRS / OC_INFRA_DIRS, but we keep the predicate local so a
# future change to the scanner's policy doesn't silently change posture.
_SYSTEM_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".DS_Store",
    "evolve",        # workspace/evolve/ — the eval/manifest staging dir
    "credentials",
    "logs",
}
_SYSTEM_FILES = {
    "openclaw.json",
    "openclaw.plugin.json",
    "AGENTS.md",
    "HEARTBEAT.md",
    "IDENTITY.md",
    "BOOTSTRAP.md",
    "COST_OPS.md",
    "EMAIL_POLICY.md",
    "EMAIL_WHITELIST.md",
    "MAGIC_COMMANDS.md",
    "SECURITY_PROTOCOLS.md",
    "TOOLS.md",
    "POD_CONDUCT.md",
    # MEMORY.md / SOUL.md / USER.md are user-content; intentionally NOT
    # treated as system files. Bots that manage notes via these are
    # surfacing content the posture review wants to see.
}


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ManifestSummary:
    app_id: str
    name: str
    source: str
    status: str
    purpose: str
    files: list[str]
    crons_count: int
    updated_at: str  # ISO from manifest, falls back to mtime
    is_recent: bool  # within window


@dataclass
class SignalSummary:
    # Signal.id (e.g. "sig-abc123"). Populated when reading from the
    # store; default empty to keep older callers / synthetic test
    # fixtures working without the field set explicitly.
    id: str = ""
    type: str = ""
    signature: str = ""
    title: str = ""
    body: str = ""
    first_observed_at: str = ""
    last_observed_at: str = ""
    observation_count: int = 0
    bot_id: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class OrphanFile:
    path: str            # workspace-relative
    size: int
    mtime_iso: str


@dataclass
class BotPosture:
    bot_id: str
    generated_at: str
    window_start: str
    window_end: str
    manifests: list[ManifestSummary]
    bot_created_signals: list[SignalSummary]
    unmanifested_signals: list[SignalSummary]
    orphan_files: list[OrphanFile]
    workspace_path: str | None = None
    notes: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Time helpers
# ─────────────────────────────────────────────────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except (ValueError, AttributeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Network / bot enumeration
# ─────────────────────────────────────────────────────────────────────────────


def _bot_ids(network_config: dict) -> list[str]:
    """Same shape-tolerance as defer_runner / manifest_reflex_runner."""
    bots = network_config.get("bots") or {}
    if isinstance(bots, dict):
        return sorted(bots.keys())
    if isinstance(bots, list):
        return sorted(b.get("id") or b.get("bot_id") for b in bots if isinstance(b, dict))
    return []


def _shared_dir_from_network(network_config: dict) -> Path:
    sd = network_config.get("sharedDir") or network_config.get("shared_dir")
    if sd:
        return Path(sd)
    return Path("/Users/Shared/evolve")


def _resolve_bot_workspace(bot_id: str) -> Path | None:
    """Best-effort workspace resolver. Falls back to None if anything fails;
    callers degrade gracefully to "no orphan walk performed"."""
    try:
        from evolve_admin.config import get_bot_workspace  # type: ignore
        ws = get_bot_workspace(bot_id)
        return ws if ws and ws.exists() else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Manifest gather
# ─────────────────────────────────────────────────────────────────────────────


def _file_paths_from_manifest(data: dict) -> list[str]:
    """Extract file paths from a manifest dict (handles v4 list[str] and
    v5+ list[dict] shapes — same logic as scanner._emit_unmanifested_signals
    and manifest.file_paths())."""
    out: list[str] = []
    for entry in (data.get("files") or []):
        if isinstance(entry, str):
            if entry.strip():
                out.append(entry.strip())
        elif isinstance(entry, dict):
            p = (entry.get("path") or "").strip()
            if p:
                out.append(p)
    return out


def _collect_manifests(shared_dir: Path, bot_id: str, window_start: datetime) -> list[ManifestSummary]:
    apps_dir = shared_dir / "applications" / bot_id
    if not apps_dir.exists():
        return []

    out: list[ManifestSummary] = []
    for mf in sorted(apps_dir.glob("*.json")):
        if mf.name.startswith(".") or "_history" in mf.parts:
            continue
        try:
            data = json.loads(mf.read_text())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue

        updated_at = (data.get("updated_at") or data.get("created_at") or "").strip()
        updated_dt = _parse_iso(updated_at)
        if updated_dt is None:
            try:
                updated_dt = datetime.fromtimestamp(mf.stat().st_mtime, tz=timezone.utc)
                updated_at = updated_dt.isoformat(timespec="seconds")
            except OSError:
                updated_dt = None

        is_recent = bool(updated_dt and updated_dt >= window_start)

        out.append(ManifestSummary(
            app_id=data.get("id") or mf.stem,
            name=data.get("display_name") or data.get("name") or (data.get("id") or mf.stem),
            source=data.get("source") or "",
            status=data.get("status") or "active",
            purpose=(data.get("purpose") or data.get("description") or "").strip(),
            files=_file_paths_from_manifest(data),
            crons_count=len(data.get("crons") or []),
            updated_at=updated_at,
            is_recent=is_recent,
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Signal gather
# ─────────────────────────────────────────────────────────────────────────────


def _collect_signals(
    shared_dir: Path,
    bot_id: str,
    window_start: datetime,
    *,
    producer: str,
) -> list[SignalSummary]:
    """Walk firing/snoozed/archived signals for this bot+producer with
    last_observed_at within the window. Uses iter_signals so archived
    (resolved/dismissed) signals from earlier in the week still surface."""
    try:
        from signals.store import iter_signals
    except Exception as e:
        print(f"[app_posture] signals.store unavailable: {e}", file=sys.stderr)
        return []

    out: list[SignalSummary] = []
    for sig in iter_signals(shared_dir, subdirs=("firing", "snoozed", "archived")):
        if sig.producer != producer:
            continue
        if sig.bot_id != bot_id:
            continue
        last_dt = _parse_iso(sig.last_observed_at)
        if last_dt is None or last_dt < window_start:
            continue
        out.append(SignalSummary(
            id=sig.id,
            type=sig.type,
            signature=sig.signature,
            title=sig.title or "",
            body=sig.body or "",
            first_observed_at=sig.created_at,
            last_observed_at=sig.last_observed_at,
            observation_count=sig.observation_count,
            bot_id=sig.bot_id or bot_id,
            details=dict(sig.details or {}),
        ))

    out.sort(key=lambda s: s.first_observed_at)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Workspace orphan detection
# ─────────────────────────────────────────────────────────────────────────────


def _is_system_path(path: Path, workspace_root: Path) -> bool:
    """True for paths the posture review should ignore — OS/OC infrastructure
    rather than user-created apps."""
    try:
        rel = path.relative_to(workspace_root)
    except ValueError:
        return True
    parts = set(rel.parts)
    if parts & _SYSTEM_DIRS:
        return True
    if path.name in _SYSTEM_FILES:
        return True
    if path.name.startswith("."):
        return True
    return False


def _is_meaningful_file(path: Path) -> bool:
    """Only flag files with material content. A 0-byte placeholder isn't an
    app. Spec §"Trigger conditions" §2 ('material content')."""
    try:
        size = path.stat().st_size
    except OSError:
        return False
    return size > 0


def _load_orphan_exclusions(bot_id: str, shared_dir: Path) -> set[str]:
    """Load the bot's orphan_exclusions.json — paths the operator
    previously approved a RetireOrphan proposal on (PR9). Posture
    reviews skip matching files so the LLM doesn't propose retiring
    them again every week.

    Failure-soft: missing file or malformed JSON → empty set."""
    try:
        from arbiter.appliers.retire_orphan import load_exclusions
        return load_exclusions(bot_id, shared_dir=shared_dir)
    except Exception as e:
        # The applier module shouldn't fail to import; if it does, we
        # just skip the exclusion filter rather than crash the gather.
        print(f"[app_posture] orphan exclusions unavailable: {e}", file=sys.stderr)
        return set()


def _collect_orphan_files(
    workspace: Path,
    manifests: list[ManifestSummary],
    *,
    excluded_paths: set[str] | None = None,
) -> list[OrphanFile]:
    """Walk the workspace, return persistent files not in any manifest's
    files[]. Manifest files are stored workspace-relative in v5+; we
    normalize to workspace-relative for comparison.

    ``excluded_paths`` (PR9): workspace-relative paths the operator has
    previously approved a RetireOrphan proposal on. The walk skips
    these so the LLM doesn't keep proposing the same retirement every
    week. The actual file is still on disk (we never unlink), but
    posture review treats it as out-of-scope.

    Caps the number of orphans returned so the rendered doc stays bounded.
    A bot with thousands of orphan files needs a different intervention
    (probably scanner-driven cleanup) than one with three."""
    if not workspace.exists():
        return []

    manifest_paths: set[str] = set()
    for m in manifests:
        for fp in m.files:
            # Normalize: strip any leading ./ or workspace/ prefix so the
            # comparison matches whether the manifest stored "ops/x.py" or
            # "workspace/ops/x.py".
            n = fp.lstrip("./").lstrip("/")
            if n.startswith("workspace/"):
                n = n[len("workspace/"):]
            manifest_paths.add(n)

    excluded = set(excluded_paths or ())

    orphans: list[OrphanFile] = []
    MAX_ORPHANS = 50  # rendered-doc safety cap; the count is reported separately
    truncated = 0

    try:
        for p in workspace.rglob("*"):
            if not p.is_file():
                continue
            if _is_system_path(p, workspace):
                continue
            if not _is_meaningful_file(p):
                continue
            try:
                rel = str(p.relative_to(workspace))
            except ValueError:
                continue
            if rel in manifest_paths:
                continue
            if rel in excluded:
                continue

            if len(orphans) >= MAX_ORPHANS:
                truncated += 1
                continue

            try:
                st = p.stat()
                mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
                orphans.append(OrphanFile(path=rel, size=st.st_size, mtime_iso=mtime_iso))
            except OSError:
                continue
    except (PermissionError, OSError) as e:
        # ACL gap or filesystem hiccup — return what we have, log to stderr
        # so the launchd log shows the partial-walk.
        print(f"[app_posture] workspace walk for {workspace} interrupted: {e}", file=sys.stderr)

    orphans.sort(key=lambda o: o.mtime_iso, reverse=True)

    # If we truncated, append a synthetic note via the path field. Keeps the
    # caller honest about completeness without complicating the dataclass.
    if truncated:
        orphans.append(OrphanFile(
            path=f"… and {truncated} more (truncated; see scanner output for full list)",
            size=0,
            mtime_iso="",
        ))
    return orphans


# ─────────────────────────────────────────────────────────────────────────────
# Synthesis: per-bot posture
# ─────────────────────────────────────────────────────────────────────────────


def gather_bot_posture(bot_id: str, shared_dir: Path, *, now: datetime | None = None) -> BotPosture:
    """Pure gather — returns a BotPosture dataclass without writing anything."""
    now = now or _utc_now()
    window_start = now - timedelta(days=WINDOW_DAYS)
    notes: list[str] = []

    manifests = _collect_manifests(shared_dir, bot_id, window_start)
    bot_created = _collect_signals(shared_dir, bot_id, window_start, producer="manifest_reflex_runner")
    unmanifested = _collect_signals(shared_dir, bot_id, window_start, producer="manifest_reflex_scanner")

    workspace = _resolve_bot_workspace(bot_id)
    orphans: list[OrphanFile] = []
    if workspace is None:
        notes.append("workspace path could not be resolved — orphan-file detection skipped")
    else:
        # PR9: skip files the operator has previously approved a
        # RetireOrphan proposal on. The actual file is still on disk
        # (the applier never unlinks); posture review just doesn't
        # surface it as an orphan again.
        excluded = _load_orphan_exclusions(bot_id, shared_dir)
        orphans = _collect_orphan_files(workspace, manifests, excluded_paths=excluded)
        if excluded:
            notes.append(f"{len(excluded)} previously-retired orphan(s) excluded from this review")

    # Cron-orphan detection is deferred — see module docstring for rationale.
    notes.append("cron orphans not detected (evolve user lacks crontab read sudo grant)")

    return BotPosture(
        bot_id=bot_id,
        generated_at=now.isoformat(timespec="seconds"),
        window_start=window_start.isoformat(timespec="seconds"),
        window_end=now.isoformat(timespec="seconds"),
        manifests=manifests,
        bot_created_signals=bot_created,
        unmanifested_signals=unmanifested,
        orphan_files=orphans,
        workspace_path=str(workspace) if workspace else None,
        notes=notes,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────────────


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def render_posture_markdown(p: BotPosture) -> str:
    """Render a BotPosture as the markdown document that gets injected
    into systemAppend by session_surface.py.

    Sections are stable so the future PR5 LLM layer can locate and append
    a Reflection section without re-parsing the whole document."""
    lines: list[str] = []
    lines.append(f"# App posture — {p.bot_id}")
    lines.append("")
    lines.append(f"_Window: {p.window_start} → {p.window_end} (last {WINDOW_DAYS} days). Generated by app_posture_review._")
    lines.append("")

    # ── This week's changes ────────────────────────────────────────
    new_apps = [m for m in p.manifests if m.is_recent]
    quiet_count = len(p.manifests) - len(new_apps)

    lines.append("## This week")
    lines.append("")
    if new_apps:
        lines.append(f"**New or modified ({len(new_apps)}):**")
        for m in new_apps:
            files_n = len(m.files)
            crons_n = m.crons_count
            purpose = _truncate(m.purpose, 140) or "_(no purpose recorded)_"
            # Show a short file-list inline for recently-changed apps so the
            # LLM reflection (PR5) can reason about file overlap (cluster /
            # split candidates) without needing a separate per-manifest
            # query. Cap at 5 to keep the doc bounded.
            file_preview = ""
            if m.files:
                shown = m.files[:5]
                more = f" (+{len(m.files) - 5} more)" if len(m.files) > 5 else ""
                file_preview = f" — files: " + ", ".join(f"`{p}`" for p in shown) + more
            lines.append(f"- `{m.app_id}` *(source: {m.source or 'unknown'})* — {purpose} [{files_n} files, {crons_n} crons]{file_preview}")
        lines.append("")
    if quiet_count:
        lines.append(f"_{quiet_count} app(s) unchanged this week._")
        lines.append("")
    if not p.manifests:
        lines.append("_No apps recorded yet._")
        lines.append("")

    # ── Self-recorded apps ─────────────────────────────────────────
    if p.bot_created_signals:
        lines.append(f"## You self-recorded {len(p.bot_created_signals)} app(s) this week")
        lines.append("")
        lines.append("From `manifest_reflex_runner` Signals (you called `record_application`).")
        lines.append("")
        for s in p.bot_created_signals:
            app_id = s.details.get("app_id") or "?"
            purpose = _truncate(s.details.get("purpose") or "", 140)
            sess = s.details.get("session_id")
            extra = f" · session: {sess}" if sess else ""
            line = f"- `{app_id}`{extra}"
            if purpose:
                line += f" — {purpose}"
            lines.append(line)
        lines.append("")

    # ── Scanner-discovered apps ────────────────────────────────────
    if p.unmanifested_signals:
        lines.append(f"## Scanner discovered {len(p.unmanifested_signals)} un-self-recorded app(s)")
        lines.append("")
        lines.append("From `manifest_reflex_scanner` Signals (the periodic scanner found apps you didn't call `record_application` on).")
        lines.append("")
        for s in p.unmanifested_signals:
            app_id = s.details.get("app_id") or "?"
            files = s.details.get("files") or []
            files_preview = ", ".join(files[:5]) + (f" (+{len(files) - 5} more)" if len(files) > 5 else "")
            line = f"- `{app_id}`"
            if files_preview:
                line += f" — files: {files_preview}"
            lines.append(line)
        lines.append("")

    # ── Orphans ────────────────────────────────────────────────────
    real_orphans = [o for o in p.orphan_files if o.size > 0 or not o.path.startswith("…")]
    truncation_note = next((o for o in p.orphan_files if o.path.startswith("…")), None)
    if real_orphans:
        lines.append(f"## Orphan files ({len(real_orphans)} not in any manifest)")
        lines.append("")
        lines.append("Persistent files in your workspace that no manifest claims. Each is either an app you didn't record, a file that belongs in an existing app's `files[]`, or stale content worth cleaning up.")
        lines.append("")
        for o in real_orphans:
            size_kb = max(1, o.size // 1024)
            lines.append(f"- `{o.path}` ({size_kb}kb, modified {o.mtime_iso})")
        if truncation_note:
            lines.append(f"- {truncation_note.path}")
        lines.append("")

    # ── Inventory summary ──────────────────────────────────────────
    lines.append("## Inventory summary")
    lines.append("")
    by_source: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for m in p.manifests:
        by_source[m.source or "unknown"] = by_source.get(m.source or "unknown", 0) + 1
        by_status[m.status or "unknown"] = by_status.get(m.status or "unknown", 0) + 1
    lines.append(f"- Total apps: {len(p.manifests)}")
    if by_status:
        parts = ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items()))
        lines.append(f"- By status: {parts}")
    if by_source:
        parts = ", ".join(f"{k}: {v}" for k, v in sorted(by_source.items()))
        lines.append(f"- By source: {parts}")
    lines.append(f"- Total files claimed by manifests: {sum(len(m.files) for m in p.manifests)}")
    lines.append(f"- Total cron entries claimed: {sum(m.crons_count for m in p.manifests)}")
    lines.append(f"- Orphan files this week: {len(real_orphans)}")
    lines.append("")

    if p.notes:
        lines.append("## Caveats")
        lines.append("")
        for n in p.notes:
            lines.append(f"- {n}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Write to disk
# ─────────────────────────────────────────────────────────────────────────────


def posture_doc_path(shared_dir: Path, bot_id: str) -> Path:
    return shared_dir / "app_posture" / f"{bot_id}.md"


def posture_log_path(shared_dir: Path, bot_id: str, *, now: datetime | None = None) -> Path:
    when = (now or _utc_now()).date().isoformat()
    return shared_dir / "app_posture" / bot_id / "log" / f"{when}.md"


# The per-week posture log copies accumulate one .md/week with no programmatic
# reader — session_surface reads only the canonical app_posture/<bot>.md. Keep
# the newest dozen per bot for operator/dev forensics (a quarter of history).
_KEEP_POSTURE_LOGS = 12


def _prune_posture_log(log_dir: Path, *, keep: int = _KEEP_POSTURE_LOGS) -> None:
    """Keep the newest ``keep`` per-week posture log copies in ``log_dir``.
    Date-named, so a lexical sort is chronological. Best-effort; never raises."""
    try:
        files = sorted(log_dir.glob("*.md"))
    except OSError:
        return
    for stale in files[:-keep] if keep else files:
        try:
            stale.unlink()
        except OSError:
            pass


def write_posture(p: BotPosture, shared_dir: Path) -> tuple[Path, Path]:
    """Render and write both the canonical doc and the per-week log copy.
    Returns (doc_path, log_path). The doc is what session_surface reads;
    the log is the audit trail for operators / Evolve developers."""
    md = render_posture_markdown(p)
    doc = posture_doc_path(shared_dir, p.bot_id)
    log = posture_log_path(shared_dir, p.bot_id, now=_parse_iso(p.generated_at))
    doc.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(doc, md)
    _atomic_write(log, md)
    _prune_posture_log(log.parent)
    return doc, log


# ─────────────────────────────────────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────────────────────────────────────


def _reflect_enabled(network_config: dict) -> bool:
    """Read the pod-wide ``app_posture.reflect_enabled`` toggle from
    network.json. Default is False — operators opt in once they've
    soaked the inventory-only output for a few cycles."""
    apc = network_config.get("app_posture") or {}
    return bool(apc.get("reflect_enabled", False))


def _emit_proposals_enabled(network_config: dict) -> bool:
    """PR7 gating. Pod-wide ``app_posture.emit_proposals_enabled`` flag.
    Default is False — operators flip it on after soaking PR5/PR6 output
    for a few cycles. Independent of reflect_enabled (proposals require
    reflection but reflection doesn't require proposals)."""
    apc = network_config.get("app_posture") or {}
    return bool(apc.get("emit_proposals_enabled", False))


def run_once(
    network_config: dict,
    *,
    only_bot: str | None = None,
    dry_run: bool = False,
    reflect_override: bool | None = None,
    reflect_dry_run: bool = False,
    emit_proposals_override: bool | None = None,
) -> dict:
    """Process every bot. Returns summary counts.

    ``reflect_override`` (None / True / False): if not None, overrides the
    pod-wide reflect_enabled flag. Used by the --reflect / --no-reflect
    CLI flags.

    ``reflect_dry_run``: skip the LLM call and use a synthetic placeholder
    reflection — exercises the append plumbing without spending an LLM call.

    ``emit_proposals_override`` (PR7): if not None, overrides
    network.app_posture.emit_proposals_enabled. When True, structural
    proposals from the LLM's YAML block get filed to arbiter pending/."""
    shared_dir = _shared_dir_from_network(network_config)
    bot_ids = _bot_ids(network_config)
    if only_bot:
        bot_ids = [b for b in bot_ids if b == only_bot]

    if reflect_override is not None:
        do_reflect = reflect_override
    else:
        do_reflect = _reflect_enabled(network_config)

    if emit_proposals_override is not None:
        do_emit_proposals = emit_proposals_override
    else:
        do_emit_proposals = _emit_proposals_enabled(network_config)

    totals: dict[str, Any] = {
        "bots": 0,
        "manifests_total": 0,
        "manifests_recent": 0,
        "bot_created_signals": 0,
        "unmanifested_signals": 0,
        "orphan_files": 0,
        "reflections_ok": 0,
        "reflections_failed": 0,
        "proposals_filed": 0,
        "proposals_dropped": 0,
        "proposals_deduped": 0,
    }
    for bot_id in bot_ids:
        posture = gather_bot_posture(bot_id, shared_dir)
        totals["bots"] += 1
        totals["manifests_total"] += len(posture.manifests)
        totals["manifests_recent"] += sum(1 for m in posture.manifests if m.is_recent)
        totals["bot_created_signals"] += len(posture.bot_created_signals)
        totals["unmanifested_signals"] += len(posture.unmanifested_signals)
        totals["orphan_files"] += sum(1 for o in posture.orphan_files if o.size > 0)

        if dry_run:
            print(f"[app_posture] [dry-run] {bot_id}: {len(posture.manifests)} apps, {len(posture.orphan_files)} orphans")
            continue
        try:
            doc, log = write_posture(posture, shared_dir)
            print(f"[app_posture] ✓ {bot_id} → {doc}")
        except Exception as e:
            print(f"[app_posture] ✗ {bot_id}: write failed: {e}", file=sys.stderr)
            continue

        # ── Reflection (PR5, gated) + proposal emission (PR7, gated) ──────
        # The inventory doc was just written; if reflection is enabled,
        # call the bot's LLM and append a Reflection section. When
        # emit_proposals is also on, file structural Investigation
        # proposals from the LLM's YAML block. Failure-soft: the
        # inventory-only doc is still useful even if reflection fails,
        # and the rendered reflection is still useful even if proposal
        # emission fails.
        if not do_reflect:
            continue
        try:
            from app_posture_reflect import append_reflection_to_doc, reflect
            result = reflect(
                posture,
                dry_run=reflect_dry_run,
                shared_dir=shared_dir,
                emit_proposals_enabled=do_emit_proposals,
            )
            if result.ok and result.text:
                append_reflection_to_doc(doc, log, result.text, model=result.model)
                totals["reflections_ok"] += 1
                msg = f"[app_posture] ✓ {bot_id} reflection appended (model: {result.model})"
                if result.proposals_summary:
                    ps = result.proposals_summary
                    totals["proposals_filed"] += ps.get("filed", 0)
                    totals["proposals_dropped"] += (
                        ps.get("dropped_low_confidence", 0)
                        + ps.get("dropped_unknown_refs", 0)
                        + ps.get("errors", 0)
                    )
                    totals["proposals_deduped"] += ps.get("deduped", 0)
                    msg += (
                        f" — proposals: filed={ps.get('filed', 0)} "
                        f"deduped={ps.get('deduped', 0)} "
                        f"dropped={ps.get('dropped_low_confidence', 0) + ps.get('dropped_unknown_refs', 0) + ps.get('errors', 0)}"
                    )
                print(msg)
            else:
                totals["reflections_failed"] += 1
                print(f"[app_posture] ✗ {bot_id} reflection skipped: {result.error}", file=sys.stderr)
        except Exception as e:
            totals["reflections_failed"] += 1
            print(f"[app_posture] ✗ {bot_id} reflection error: {e}", file=sys.stderr)
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly per-bot app posture review")
    parser.add_argument("--network", help="Path to network.json")
    parser.add_argument("--shared-dir", help="Override shared_dir (otherwise from network.json)")
    parser.add_argument("--bot", help="Limit to a single bot id (default: all bots in network)")
    parser.add_argument("--dry-run", action="store_true", help="Gather + log totals; don't write any docs")
    parser.add_argument("--reflect", dest="reflect_override", action="store_const", const=True,
                        help="Force LLM reflection on (overrides network.app_posture.reflect_enabled)")
    parser.add_argument("--no-reflect", dest="reflect_override", action="store_const", const=False,
                        help="Force LLM reflection off")
    parser.add_argument("--reflect-dry-run", action="store_true",
                        help="Append a synthetic placeholder reflection; don't call the LLM")
    parser.add_argument("--emit-proposals", dest="emit_proposals_override", action="store_const", const=True,
                        help="Force structural-proposal emission on (overrides network.app_posture.emit_proposals_enabled)")
    parser.add_argument("--no-emit-proposals", dest="emit_proposals_override", action="store_const", const=False,
                        help="Force structural-proposal emission off")
    args = parser.parse_args()

    network_path = Path(args.network) if args.network else resolve_network_path()
    try:
        network_config = json.loads(Path(network_path).read_text())
    except Exception as e:
        print(f"[app_posture] could not load network.json from {network_path}: {e}", file=sys.stderr)
        sys.exit(2)

    if args.shared_dir:
        network_config = dict(network_config)
        network_config["sharedDir"] = args.shared_dir

    totals = run_once(
        network_config,
        only_bot=args.bot,
        dry_run=args.dry_run,
        reflect_override=args.reflect_override,
        reflect_dry_run=args.reflect_dry_run,
        emit_proposals_override=args.emit_proposals_override,
    )
    print(f"[app_posture] cycle complete: {totals}")


if __name__ == "__main__":
    main()
