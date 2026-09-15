#!/usr/bin/env python3
"""
app_growth_sweep.py — the admin-side daily backstop for the app growth log.

Brief: ``internal/dispatch/done/growth-log-observer.md``. The brief cites
``internal/design-app-lineage-2026-08-24.md`` §3 as the program doc; **that
file does not exist in this repo** — not on main, not in history — so it is
named here as the brief's citation and not as something this module was
written against. The brief itself specifies this sweep in one sentence and
that sentence is implemented literally.

WHAT THIS IS FOR
────────────────
The plugin-side observer (``packages/plugin/src/apps/GrowthLog.ts``) records an
app delta at ``agent_end`` when it can SEE the write: a recognised write tool
with a parseable destination path. Over an uncontrolled upstream tool registry
that is fail-open by construction — a ``bash`` heredoc, an MCP tool nobody
enumerated, a renamed editor, or any change made outside a bot turn at all
(an operator edit, a cron job rewriting its own config) is invisible to it.

This sweep is the backstop. Once a day it re-derives what changed from CONTENT
DIGESTS of every file the bot's manifests declare, and appends a record for
each change the observer did not already log.

**Sweep records are honestly second-class and say so in the data.** They carry
``attribution: "sweep"`` and ``cause: null`` / ``cause_source: "none"``, because
by the time this runs the conversation that caused the change is over. A sweep
record is proof that the app moved; it is not, and cannot be, the reason why.
That is the whole argument for the observer existing at all.

NOTHING READS EITHER STORE YET. This chip is report-only: it starts the clock,
because lineage cannot be backfilled and every day not recorded is lost.

THE FIRST RUN FOR A BOT EMITS NOTHING
─────────────────────────────────────
It records the baseline digests and stops. Without that rule the first sweep
would report every file of every app as "changed" on the day the sweep shipped
— a fabricated day-one delta for the entire pod, which is precisely the kind of
backfilled history this design refuses to invent.

WHY THE SWEEP WRITES ITS OWN SUBTREE
────────────────────────────────────
    {shared}/app-growth/{bot}/{app}/{date}.jsonl          ← plugin, as the BOT
    {shared}/app-growth/_sweep/{bot}/{app}/{date}.jsonl   ← this, as EVOLVE

Two writers, two UNIX users. If they shared a tree, whichever won the mkdir
would own a 0755 directory the other could never write into, and whichever
created a day-file would own a 0644 file the other could never append to —
silently, on some pods and not others, depending on who ran first. So each
writer owns a subtree outright and readers glob both. ``_sweep`` can never
collide with a bot id (UNIX account names do not begin with an underscore).
Retention follows ownership for the same reason: this prunes ``_sweep`` only,
and the plugin prunes its own.

This is CLAUDE.md's "route on who would own the result" applied before the
fact rather than after.

UNREADABLE IS SKIP, NEVER BASELINE
──────────────────────────────────
A file that cannot be read is skipped entirely: no digest stored, no record
emitted. Recording "absent" for an unreadable file would make the next run
report a change that never happened, and storing a baseline we could not
actually read would hide a real one. Counted in the run summary so a bot whose
ACL has drifted is visible rather than quietly uncovered.

Usage:
    python3 app_growth_sweep.py --network /Users/Shared/evolve/network.json
    python3 app_growth_sweep.py --bot team_bot_a --shared-dir /Users/Shared/evolve
    python3 app_growth_sweep.py --bot team_bot_a --shared-dir ... --report
    python3 app_growth_sweep.py --bot team_bot_a --shared-dir ... --dry-run

Schedule: daily at 03:40, right after the two usage sweeps (03:30 / 03:35).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from evolve_admin.applications.app_identity import (  # pyright: ignore[reportMissingImports]
    is_canonical_app_id,
    resolve_app_id,
)
from evolve_config import CANONICAL_SHARED_DIR, bot_home as _bot_home
from evolve_util import atomic_write_json as _atomic_write_json, now_iso as _now_iso

# ── Contract constants (the TS twin holds the same values) ───────────────────

logger = logging.getLogger(__name__)

#: Schema of the records this writes. Must match GROWTH_LOG_SCHEMA_VERSION in
#: packages/plugin/src/apps/GrowthLog.ts — one log, two writers, one schema.
GROWTH_LOG_SCHEMA_VERSION = 1
GROWTH_LOG_ROOT = "app-growth"
SWEEP_SEGMENT = "_sweep"
UNATTRIBUTED_SEGMENT = "_unattributed"
STATE_FILENAME = ".sweep-state.json"
STATE_SCHEMA_VERSION = 1

#: Day-files older than this are pruned from the sweep's own subtree.
GROWTH_LOG_RETENTION_DAYS = 90
#: Paths recorded on one sweep record, per app. A change larger than this is a
#: bulk operation whose per-file detail is not what makes the log legible.
MAX_FILES_PER_RECORD = 40
#: Files digested per bot per run. A runaway manifest cannot turn the daily
#: sweep into an unbounded tree walk.
MAX_FILES_PER_BOT = 5000
#: Bytes hashed per file. Digests are a change DETECTOR, not an integrity
#: proof, and app files are source — a cap keeps one large data file from
#: dominating the run.
MAX_HASH_BYTES = 4 * 1024 * 1024

#: Manifest keys whose entries name a file this app owns. Mirrors
#: ``manifest_recovery._manifest_footprint``.
DECLARED_FILE_KEYS = ("files", "realized_files", "evidence_files")


# ── Paths ────────────────────────────────────────────────────────────────────


def sweep_bot_dir(shared_dir: Path, bot_id: str) -> Path:
    return Path(shared_dir) / GROWTH_LOG_ROOT / SWEEP_SEGMENT / bot_id


def sweep_app_dir(shared_dir: Path, bot_id: str, app_id: str | None) -> Path:
    """Day-file directory for one (bot, app) in the sweep's subtree.

    A non-conforming legacy id lands in the unattributed bucket — it is not
    safe as a path segment. The record's ``app_id`` FIELD stays authoritative
    and the directory is a physical index, the same contract the arbiter store
    states for a proposal's status vs its subdir. Mirrors ``growthAppDir`` in
    GrowthLog.ts.
    """
    seg = app_id if app_id and is_canonical_app_id(app_id) else UNATTRIBUTED_SEGMENT
    return sweep_bot_dir(shared_dir, bot_id) / seg


def observed_bot_dir(shared_dir: Path, bot_id: str) -> Path:
    """The plugin's subtree for this bot — read-only from here."""
    return Path(shared_dir) / GROWTH_LOG_ROOT / bot_id


def state_path(shared_dir: Path, bot_id: str) -> Path:
    return sweep_bot_dir(shared_dir, bot_id) / STATE_FILENAME


def workspace_root(bot_id: str) -> Path:
    return _bot_home(bot_id) / ".openclaw" / "workspace"


# ── Pure helpers ─────────────────────────────────────────────────────────────


def index_key(rel_path: str) -> str:
    """The comparison key for a workspace-relative path.

    Case-folded and slash-trimmed — the Python twin of ``indexKey`` in
    GrowthLog.ts. Both sides must agree or the dedup below silently stops
    matching the observer's records and every change is double-recorded.
    """
    return rel_path.strip().strip("/").lower()


def normalize_declared_path(raw: Any, ws_root: Path) -> str | None:
    """One manifest-declared path as a workspace-relative path, case preserved.

    Strips the ``layer: `` prefix the scanner sometimes writes, resolves an
    absolute path against the workspace root, and refuses anything that
    escapes it. Returns None when the entry is not a usable path — a manifest
    is operator-editable and this must never raise on a hand-edit.
    """
    if isinstance(raw, dict):
        raw = raw.get("path")
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if ":" in s.split("/", 1)[0]:
        head, _, rest = s.partition(":")
        if head.replace("_", "").isalpha():
            s = rest.strip()
    if not s:
        return None
    if s.startswith("~/"):
        s = str(Path.home() / s[2:])
    p = Path(s)
    if p.is_absolute():
        try:
            s = str(p.resolve(strict=False).relative_to(ws_root.resolve(strict=False)))
        except ValueError:
            return None
    s = os.path.normpath(s)
    if s.startswith("..") or s in (".", ""):
        return None
    # Manifests are inconsistent about the leading ``workspace/`` segment
    # (RecordApplicationTool accepts both forms in as many words).
    if s.startswith("workspace/"):
        s = s[len("workspace/"):]
    return s.strip("/") or None


def declared_files(manifest: dict, ws_root: Path) -> list[str]:
    """Every workspace-relative path this manifest claims, deduped, ordered."""
    out: list[str] = []
    seen: set[str] = set()
    for key in DECLARED_FILE_KEYS:
        entries = manifest.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            rel = normalize_declared_path(entry, ws_root)
            if rel is None:
                continue
            k = index_key(rel)
            if k in seen:
                continue
            seen.add(k)
            out.append(rel)
    return out


def digest_file(path: Path) -> str | None:
    """SHA-256 of the first MAX_HASH_BYTES of ``path``, or None if unreadable.

    None is the SKIP signal, deliberately distinct from a digest of empty
    content: "we could not look" must never be stored as "we looked and it
    was empty" (the read-denied/write-allowed asymmetry, applied to a reader).
    """
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            remaining = MAX_HASH_BYTES
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                h.update(chunk)
    except (OSError, PermissionError):
        return None
    return h.hexdigest()


# ── State ────────────────────────────────────────────────────────────────────


def load_state(shared_dir: Path, bot_id: str) -> dict:
    """Read this bot's sweep state, or the empty pre-baseline state.

    A corrupt or unreadable state file reads as pre-baseline, which re-runs
    the baseline (emitting nothing) rather than reporting the whole pod as
    changed. Losing one day of sweep coverage beats fabricating a delta for
    every file on the box.
    """
    try:
        data = json.loads(state_path(shared_dir, bot_id).read_text())
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "bot_id": bot_id,
            "baseline_established": False,
            "last_sweep_at": None,
            "files": {},
        }
    data.setdefault("baseline_established", False)
    data.setdefault("last_sweep_at", None)
    data["bot_id"] = bot_id
    return data


def save_state(shared_dir: Path, bot_id: str, state: dict) -> None:
    d = sweep_bot_dir(shared_dir, bot_id)
    d.mkdir(parents=True, exist_ok=True)
    _ensure_multiwriter_root(shared_dir)
    _atomic_write_json(state_path(shared_dir, bot_id), state, indent=1, mode=0o644)


def _ensure_multiwriter_root(shared_dir: Path) -> None:
    """Pin ``{shared}/app-growth`` sticky-1777 when this process created it.

    The root has two writers running as different users (this, as ``evolve``;
    the gateway plugin, as each bot). Left at the creator's umask it lands
    0755 and locks the other writer out of ever making its own subtree — the
    same multi-writer contract ``deploy_shared_dir`` applies to ``metrics``
    and ``annotations``. Best-effort: on a pod where someone else owns it,
    the chmod fails and the deploy-time pass is what fixes it.
    """
    root = Path(shared_dir) / GROWTH_LOG_ROOT
    try:
        os.chmod(root, 0o1777)
    except OSError as exc:
        # Someone else owns the root — chmod is theirs to do. Say so once at
        # debug rather than swallowing: a permanently-0755 root means the OTHER
        # writer is locked out, and that has to be diagnosable from a log.
        logger.debug("app-growth: could not pin %s to 1777: %s", root, exc)


# ── Dedup against the observer's records ─────────────────────────────────────


def _iter_jsonl(path: Path) -> Iterator[dict]:
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            yield rec


def observed_since(shared_dir: Path, bot_id: str, since_iso: str | None) -> set[tuple]:
    """(app_id, index_key(file)) pairs the PLUGIN already recorded since ``since_iso``.

    This is what makes a sweep record mean "the observer missed this". Without
    it every ordinary in-conversation edit would be logged twice — once with
    its cause, once without — and the second copy would look like evidence of
    an out-of-band change.

    ``since_iso`` None (pre-baseline) returns the empty set; nothing is emitted
    on that run anyway.
    """
    pairs: set[tuple] = set()
    if not since_iso:
        return pairs
    bot_dir = observed_bot_dir(shared_dir, bot_id)
    if not bot_dir.is_dir():
        return pairs
    # Only day-files that could contain records at/after the cutoff. The day
    # is UTC on both sides, so a string compare on the filename stem is a
    # correct prefilter; the per-record ts check below is the real gate.
    cutoff_day = since_iso[:10]
    for app_dir in sorted(bot_dir.iterdir()):
        if not app_dir.is_dir():
            continue
        for f in sorted(app_dir.glob("*.jsonl")):
            if f.stem < cutoff_day:
                continue
            for rec in _iter_jsonl(f):
                ts = rec.get("ts")
                if not isinstance(ts, str) or ts < since_iso:
                    continue
                app_id = rec.get("app_id")
                for rel in rec.get("files") or []:
                    if isinstance(rel, str):
                        pairs.add((app_id, index_key(rel)))
    return pairs


# ── The sweep ────────────────────────────────────────────────────────────────


def sweep_bot(
    bot_id: str,
    shared_dir: Path,
    *,
    dry_run: bool = False,
    now_iso: str | None = None,
) -> dict:
    """Reconcile one bot's declared app files against their last-seen digests.

    Returns a summary dict — the same shape ``--report`` prints and the launchd
    log line renders. One bot per call; no cross-bot reads ever occur.
    """
    now = now_iso or _now_iso()
    ws_root = workspace_root(bot_id)
    manifests_dir = ws_root / "manifests"
    state = load_state(shared_dir, bot_id)
    prior: dict = state.get("files") or {}
    baseline = bool(state.get("baseline_established"))

    summary = {
        "bot_id": bot_id,
        "ts": now,
        "baseline_established": baseline,
        "apps": 0,
        "files_seen": 0,
        "files_unreadable": 0,
        "files_missing": 0,
        "changes": 0,
        "records_written": 0,
        "already_observed": 0,
        "pruned_day_files": 0,
    }

    if not manifests_dir.is_dir():
        summary["error"] = f"manifests dir unreadable: {manifests_dir}"
        return summary

    already = observed_since(shared_dir, bot_id, state.get("last_sweep_at"))

    current: dict[str, dict] = {}
    # app_id -> list of changed workspace-relative paths
    changed_by_app: dict[str, list[str]] = {}

    for mpath in sorted(manifests_dir.glob("*.json")):
        if mpath.name.startswith(".") or mpath.name.startswith("_"):
            continue
        try:
            manifest = json.loads(mpath.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        app_id = resolve_app_id(manifest)
        if not app_id:
            continue
        summary["apps"] += 1

        for rel in declared_files(manifest, ws_root):
            if summary["files_seen"] >= MAX_FILES_PER_BOT:
                break
            key = index_key(rel)
            if key in current:
                continue  # a shared file: first declaring app owns the row
            abs_path = ws_root / rel
            if not abs_path.exists():
                summary["files_missing"] += 1
                continue
            sha = digest_file(abs_path)
            if sha is None:
                # Unreadable: skip. Not baselined, not reported as changed.
                summary["files_unreadable"] += 1
                continue
            summary["files_seen"] += 1
            current[key] = {"sha256": sha, "app_id": app_id, "seen_at": now, "path": rel}

            was = prior.get(key)
            if was is not None and was.get("sha256") == sha:
                continue
            if not baseline:
                continue  # first run: record the baseline, emit nothing
            summary["changes"] += 1
            if (app_id, key) in already:
                summary["already_observed"] += 1
                continue
            changed_by_app.setdefault(app_id, []).append(rel)

    records = [
        {
            "schema_version": GROWTH_LOG_SCHEMA_VERSION,
            "kind": "app_delta",
            "ts": now,
            "bot_id": bot_id,
            "session_id": None,
            "turn_id": None,
            "app_id": app_id,
            "files": files[:MAX_FILES_PER_RECORD],
            "footprint": [],
            "cause": None,
            "cause_source": "none",
            "cause_truncated": False,
            "attribution": "sweep",
            "turn_app_id": None,
            "turn_app_attribution": "none",
            "tools": [],
        }
        for app_id, files in sorted(changed_by_app.items())
    ]

    if dry_run:
        summary["records_written"] = 0
        summary["would_write"] = len(records)
        return summary

    for rec in records:
        if _append_record(shared_dir, bot_id, rec):
            summary["records_written"] += 1

    state["schema_version"] = STATE_SCHEMA_VERSION
    state["files"] = current
    state["baseline_established"] = True
    state["last_sweep_at"] = now
    save_state(shared_dir, bot_id, state)

    summary["pruned_day_files"] = prune_sweep_retention(shared_dir, bot_id, now=now)
    return summary


def _append_record(shared_dir: Path, bot_id: str, rec: dict) -> bool:
    d = sweep_app_dir(shared_dir, bot_id, rec.get("app_id"))
    try:
        d.mkdir(parents=True, exist_ok=True)
        _ensure_multiwriter_root(shared_dir)
        f = d / f"{rec['ts'][:10]}.jsonl"
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        try:
            os.chmod(f, 0o644)
        except OSError as exc:
            # The record IS written; only its mode is off. Worth a debug line
            # (a 0600 day-file is unreadable to a future reader) but never a
            # reason to report the append as failed.
            logger.debug("app-growth: chmod 0644 failed on %s: %s", f, exc)
        return True
    except OSError as exc:
        print(f"[app-growth-sweep] {bot_id}: append failed: {exc}", file=sys.stderr)
        return False


def prune_sweep_retention(shared_dir: Path, bot_id: str, *, now: str | None = None) -> int:
    """Delete sweep day-files older than the retention horizon. Returns the count.

    Only ``_sweep`` — the observer's files are bot-owned and this process (as
    ``evolve``) has no business deleting them; the plugin prunes its own.
    """
    ref = datetime.now(timezone.utc)
    if now:
        try:
            ref = datetime.fromisoformat(now.replace("Z", "+00:00"))
        except ValueError as exc:
            # Fall back to wall-clock now: pruning against a slightly different
            # reference is harmless, pruning against a parse crash is not.
            logger.debug("app-growth: unparseable now=%r, using wall clock: %s", now, exc)
    cutoff = (ref - timedelta(days=GROWTH_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
    pruned = 0
    bot_dir = sweep_bot_dir(shared_dir, bot_id)
    if not bot_dir.is_dir():
        return 0
    for app_dir in bot_dir.iterdir():
        if not app_dir.is_dir():
            continue
        for f in app_dir.glob("*.jsonl"):
            if f.stem >= cutoff:
                continue
            try:
                f.unlink()
                pruned += 1
            except OSError as exc:
                # A foreign-owned file in our own subtree should not exist;
                # if one does, leaving it is right and knowing is useful.
                logger.debug("app-growth: could not prune %s: %s", f, exc)
    return pruned


def count_growth_records(shared_dir: Path, bot_id: str) -> dict:
    """Read-only census of BOTH subtrees for one bot. Writes nothing.

    This is what ``--report`` prints and what an operator runs to answer "is
    the recorder actually recording?" without touching the store.
    """
    out = {
        "bot_id": bot_id,
        "observed_records": 0,
        "sweep_records": 0,
        "unattributed_records": 0,
        "apps": set(),
        "days": set(),
    }
    for root, bucket in (
        (observed_bot_dir(shared_dir, bot_id), "observed_records"),
        (sweep_bot_dir(shared_dir, bot_id), "sweep_records"),
    ):
        if not root.is_dir():
            continue
        for app_dir in sorted(root.iterdir()):
            if not app_dir.is_dir():
                continue
            for f in sorted(app_dir.glob("*.jsonl")):
                for rec in _iter_jsonl(f):
                    out[bucket] += 1
                    out["days"].add(str(rec.get("ts", ""))[:10])
                    if rec.get("kind") == "unattributed_change":
                        out["unattributed_records"] += 1
                    elif rec.get("app_id"):
                        out["apps"].add(rec["app_id"])
    out["apps"] = sorted(out["apps"])
    out["days"] = sorted(d for d in out["days"] if d)
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────


def _members(network: dict) -> list[str]:
    """Member bots only. Unlike the usage sweeps this does NOT append
    ``evolve``: the growth log is keyed on a bot workspace, and the service
    user has none."""
    return [b for b in (network.get("members") or []) if isinstance(b, str) and b]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Daily reconcile of app file changes the growth-log observer missed.",
    )
    parser.add_argument("--bot", dest="bot_id", help="Sweep one bot only.")
    parser.add_argument("--network", help="Path to network.json (resolves shared dir + members).")
    parser.add_argument("--shared-dir", default=str(CANONICAL_SHARED_DIR))
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute changes; write no records and no state.")
    parser.add_argument("--report", action="store_true",
                        help="Read-only census of both growth subtrees. Sweeps nothing.")
    args = parser.parse_args(argv)

    shared_dir = Path(args.shared_dir)
    bots: list[str] = []
    if args.bot_id:
        bots = [args.bot_id]
    elif args.network:
        try:
            net = json.loads(Path(args.network).read_text())
            shared_dir = Path(net.get("sharedDir", str(shared_dir)))
            bots = _members(net)
        except (OSError, ValueError) as exc:
            print(f"[app-growth-sweep] Failed to read network.json: {exc}", file=sys.stderr)
            return 1
    else:
        net_path = shared_dir / "network.json"
        if net_path.exists():
            try:
                bots = _members(json.loads(net_path.read_text()))
            except (OSError, ValueError) as exc:
                print(f"[app-growth-sweep] Failed to read {net_path}: {exc}", file=sys.stderr)

    if not bots:
        parser.error("Specify --bot BOT_ID, --network PATH, or ensure network.json exists")

    for bot in bots:
        try:
            if args.report:
                c = count_growth_records(shared_dir, bot)
                print(
                    f"[app-growth-sweep] {bot}: {c['observed_records']} observed + "
                    f"{c['sweep_records']} sweep records across {len(c['apps'])} app(s), "
                    f"{len(c['days'])} day(s); {c['unattributed_records']} unattributed"
                )
                continue
            s = sweep_bot(bot, shared_dir, dry_run=args.dry_run)
            if s.get("error"):
                print(f"[app-growth-sweep] {bot}: {s['error']}", file=sys.stderr)
                continue
            print(
                f"[app-growth-sweep] {bot}: {s['apps']} app(s), {s['files_seen']} file(s), "
                f"{s['changes']} change(s), {s['records_written']} record(s) written, "
                f"{s['already_observed']} already observed, "
                f"{s['files_unreadable']} unreadable, {s['pruned_day_files']} pruned"
                + ("" if s["baseline_established"] else " [baseline run — nothing emitted]")
            )
        except Exception as exc:  # one bot's failure must not stop the sweep
            print(f"[app-growth-sweep] {bot}: sweep failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
