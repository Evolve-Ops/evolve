"""
strip_stale_markers.py — one-shot cleanup sweep that strips stale ``_evolve``
provenance markers off files an application may never legitimately own.

The situation this cleans
─────────────────────────
A scanner stamp run wrote ``_evolve`` provenance markers onto ~1,000
audit-telemetry ``rec-*.json`` files on each of two bots (under
``workspace/evolve/audit_outbox/_ingested/...``), plus OpenClaw-standard files
(``AGENTS.md`` and friends). None of those are application files — yet every
one showed up as a false orphan, producing the original 1,084-orphan report.
Stripping the marker (NOT the file, NOT its payload) fixes the false-orphan
flood at the source.

What it strips — and only what it strips
────────────────────────────────────────
The authoritative set is the ``scrub_candidate`` bucket of the materialized
reconciliation ledger (:func:`recon_ledger.build_recon_ledger`). That bucket
holds exactly the markers that can never be attached or owned:

  * **telemetry**     — a marker on a path under the platform ``evolve/``
                        telemetry tree (audit outboxes, signals, …) or any
                        other platform-written path.
  * **reserved**      — a marker on an OpenClaw identity / standing-instruction
                        file (``AGENTS.md``, ``SOUL.md``, …) or a task-substrate
                        file (``tasks.json``, …).
  * **unresolvable**  — an ownable path whose marker spec_id resolves to no live
                        Instance even through lineage (the owning app is gone).

The first two are the ledger's ``ineligible_path`` arm (lifecycle
``ineligible``); the third is its ``unresolvable_spec`` arm (lifecycle
``orphaned``). We re-classify by rebuilding the ledger *at strip time*
(``persist=False``) rather than trusting a possibly-stale ``recon_ledger.json``
on disk — only rows the live join still calls ``scrub_candidate`` are touched.

Safety
──────
* **Dry-run by default.** Without ``--apply`` nothing is written; the sweep
  prints what it *would* strip and exits 0.
* Stripping removes only the marker key/line; file content is preserved
  (the work is delegated to
  :func:`provenance.remove_pkg_id_from_marker`, which atomically rewrites via
  temp-file + rename and falls through to ``_strip_json_marker`` for JSON).
* Idempotent — a re-run finds fewer/zero markers (a stripped file no longer
  parses a marker, so it is silently skipped).
* File access (see CLAUDE.md): ``rec-*.json`` under ``workspace/evolve/**`` is
  directly writable by the ``evolve`` user, so the atomic rewrite works in
  place. OpenClaw-standard files at the workspace root (``AGENTS.md``) may be
  owned by the bot and not evolve-writable; for those the sweep falls back to
  the documented ``/tmp``-stage + ``sudo /bin/cp`` + ``chown`` back +
  preserve-mode pattern. A single unwritable file is tallied as a failure and
  reported — it never sinks the whole sweep.

CLI
───
    python3 -m evolve_admin.applications.strip_stale_markers --shared-dir <dir>
        # dry-run: per-bot summary, total, exits 0, changes nothing
    python3 -m evolve_admin.applications.strip_stale_markers --shared-dir <dir> --apply
        # strips every scrub-candidate marker; reports stripped/skipped/failed
    ... --bot <id> [--bot <id> ...]      # target specific bots (default: all)
    ... --reason telemetry|reserved|unresolvable   # strip just one class first

Pure Python, no LLM.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from platform_profile import get_profile as _get_profile

from ..config import load_network
from .app_ownership_policy import (
    _OC_TASK_SUBSTRATE_FILENAMES,
    _clean_evidence_path,
    _is_oc_identity_system_file,
)
from .provenance import FileLifecycle, parse_marker, remove_pkg_id_from_marker
from .recon_ledger import Bucket, LedgerRow, build_recon_ledger

# Platform-keyed binaries for the sudo fallback (W7 / design-linux-port §6). The
# sudo NOPASSWD grant is rendered from the platform profile, so the call-site
# argv MUST match it. ``chown`` lives at /usr/sbin on macOS but /usr/bin on
# Linux — a hardcoded macOS path makes the no-TTY admin daemon's sudo prompt for
# a password and fail silently (token file left root-owned, unreadable by the
# bot). ``cat``/``cp``/``chmod`` are byte-identical on both OSes but routed
# through the same profile for consistency. The bot's PRIMARY group ``:staff``
# stays literal (it exists on Ubuntu, gid 50).
_PROFILE = _get_profile()


# ── Reason classes (operator-facing grouping over the scrub bucket) ────────────

TELEMETRY = "telemetry"
RESERVED = "reserved"
UNRESOLVABLE = "unresolvable"
REASON_CLASSES: tuple[str, ...] = (TELEMETRY, RESERVED, UNRESOLVABLE)

# Strip outcomes (machine-stable).
STRIPPED = "stripped"
SKIPPED = "skipped"
FAILED = "failed"


def classify_reason(row: LedgerRow) -> str:
    """Map a ``scrub_candidate`` :class:`LedgerRow` to one of
    :data:`REASON_CLASSES`.

    Split off the ledger's two scrub arms:
      * ``unresolvable_spec`` (lifecycle ``orphaned``) → ``unresolvable``.
      * ``ineligible_path`` (lifecycle ``ineligible``) → ``reserved`` when the
        path is an OpenClaw identity/system or task-substrate file, else
        ``telemetry`` (the platform ``evolve/`` tree + other platform-written
        paths, which are the bulk of the flood).

    Reuses the canonical predicates from ``app_ownership_policy`` so the split
    never drifts from the policy that classified the row scrub in the first
    place.
    """
    if row.lifecycle == FileLifecycle.ORPHANED:
        return UNRESOLVABLE
    cleaned = _clean_evidence_path(row.path)
    name = Path(cleaned).name.lower()
    if _is_oc_identity_system_file(cleaned) or name in _OC_TASK_SUBSTRATE_FILENAMES:
        return RESERVED
    return TELEMETRY


# ── Collection (re-classify at strip time — never trust a stale ledger) ────────

def collect_scrub_rows(
    shared_dir: Path,
    bot_ids: list[str],
    *,
    reason_filter: Optional[str] = None,
) -> list[LedgerRow]:
    """Build the reconciliation ledger fresh (in memory) over *bot_ids* and
    return its ``scrub_candidate`` rows, optionally filtered to a single
    :data:`REASON_CLASSES` value.

    ``persist=False`` so the on-disk ``recon_ledger.json`` is never rewritten as
    a side effect of a strip sweep.
    """
    result = build_recon_ledger(shared_dir, bot_ids, persist=False)
    rows = list(result.rows(Bucket.SCRUB_CANDIDATE))
    if reason_filter:
        rows = [r for r in rows if classify_reason(r) == reason_filter]
    return rows


@dataclass
class Summary:
    """Per-bot × per-class scrub-candidate counts plus a few sample paths."""
    by_bot: dict[str, dict[str, int]] = field(default_factory=dict)
    samples: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    total: int = 0

    def counts_for(self, bot_id: str) -> dict[str, int]:
        return self.by_bot.get(bot_id, {c: 0 for c in REASON_CLASSES})


def summarize(rows: list[LedgerRow], *, sample_n: int = 5) -> Summary:
    """Group *rows* by bot and reason class, keeping up to *sample_n* sample
    paths per (bot, class) for the dry-run report."""
    summ = Summary(total=len(rows))
    for r in rows:
        cls = classify_reason(r)
        bot = summ.by_bot.setdefault(r.bot_id, {c: 0 for c in REASON_CLASSES})
        bot[cls] += 1
        samp = summ.samples.setdefault(r.bot_id, {c: [] for c in REASON_CLASSES})
        if len(samp[cls]) < sample_n:
            samp[cls].append(r.path)
    return summ


# ── Stripping ──────────────────────────────────────────────────────────────────

def _strip_marker_fully(path: Path) -> bool:
    """Strip a file's ``_evolve`` marker entirely, preserving file content.

    Delegates to :func:`provenance.remove_pkg_id_from_marker` once per owning
    id; removing the last id strips the marker (JSON → ``_strip_json_marker``,
    text → ``_strip_marker``). Re-parses the marker from disk so it acts on the
    live state, not a stale ledger row.

    Returns True if anything changed, False if the file has no (valid) marker.
    May raise ``OSError`` / ``PermissionError`` from the underlying atomic
    write (caught by the caller, which then tries the sudo fallback).
    """
    marker = parse_marker(path)
    if marker is None or not marker.is_valid():
        return False
    changed = False
    # identity: see resolve_app_id — ``marker.pkg_ids`` are the ids literally
    # written into the file's ``_evolve`` marker (``provenance.py``'s
    # namespace), re-parsed from disk here so the strip acts on live state. No
    # manifest is in scope; there is nothing to resolve, and matching anything
    # other than the exact text on disk would leave the stale marker in place.
    for pkg_id in list(marker.pkg_ids):
        if remove_pkg_id_from_marker(path, pkg_id):
            changed = True
    return changed


def _strip_via_sudo(path: Path, bot_id: str) -> str:
    """Fallback for files the ``evolve`` user cannot rewrite in place
    (OpenClaw-standard files at the bot's workspace root).

    Stages a copy in ``/tmp`` (evolve-writable), strips the marker there with
    the same provenance helper, then ``sudo /bin/cp``\\ s it back, ``chown``\\ s
    to the bot, and restores the original mode. Returns one of
    :data:`STRIPPED` / :data:`SKIPPED` / :data:`FAILED`.
    """
    # Read the current content — direct, then sudo /bin/cat (CLAUDE.md fallback).
    try:
        content = path.read_text(encoding="utf-8")
    except (PermissionError, OSError):
        r = subprocess.run(
            ["sudo", _PROFILE.cat, str(path)], capture_output=True, text=True
        )
        if r.returncode != 0:
            return FAILED
        content = r.stdout

    # Preserve the original mode (these are not secrets — AGENTS.md is 0644).
    try:
        orig_mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        orig_mode = 0o644

    # Stage with the same suffix so provenance dispatches by file type correctly.
    fd, tmp = tempfile.mkstemp(
        dir="/tmp", prefix=f"evolve-strip-{bot_id}-", suffix=path.suffix
    )
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        tmp_path.write_text(content, encoding="utf-8")
        if not _strip_marker_fully(tmp_path):
            return SKIPPED
        cp = subprocess.run(
            ["sudo", _PROFILE.cp, tmp, str(path)], capture_output=True, text=True
        )
        if cp.returncode != 0:
            return FAILED
        subprocess.run(
            ["sudo", _PROFILE.chown, f"{bot_id}:staff", str(path)],
            capture_output=True, text=True,
        )
        subprocess.run(
            ["sudo", _PROFILE.chmod, format(orig_mode, "03o"), str(path)],
            capture_output=True, text=True,
        )
        return STRIPPED
    finally:
        # Best-effort cleanup of the /tmp staging copy — a failure here leaves a
        # harmless leftover (mkstemp 0600, evolve-owned) and must not mask the
        # strip outcome we are about to return.
        with contextlib.suppress(OSError):
            os.unlink(tmp)


@dataclass
class StripReport:
    stripped: int = 0
    skipped: int = 0
    failed: int = 0
    touched: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)


def strip_rows(
    rows: list[LedgerRow],
    *,
    log: Callable[[str], None] = lambda _m: None,
) -> StripReport:
    """Strip the ``_evolve`` marker off every row in *rows*.

    Per-file try/except: one unwritable file is tallied as a failure and the
    sweep continues. Logs every path it touches via *log*.
    """
    report = StripReport()
    for r in rows:
        path = Path(r.abs_path) if r.abs_path else None
        if path is None:
            report.failed += 1
            report.failures.append((r.path, "no abs_path on ledger row"))
            continue
        try:
            changed = _strip_marker_fully(path)
            outcome = STRIPPED if changed else SKIPPED
        except (PermissionError, OSError):
            # Not evolve-writable in place → documented /tmp + sudo fallback.
            try:
                outcome = _strip_via_sudo(path, r.bot_id)
            except Exception as e:  # belt-and-suspenders: never sink the sweep
                outcome = FAILED
                report.failures.append((r.path, f"sudo fallback error: {e}"))
        if outcome == STRIPPED:
            report.stripped += 1
            report.touched.append(r.path)
            log(f"  strip   [{r.bot_id}] {r.path}")
        elif outcome == SKIPPED:
            report.skipped += 1
            log(f"  skip    [{r.bot_id}] {r.path} (no marker on disk)")
        else:
            report.failed += 1
            if not any(p == r.path for p, _ in report.failures):
                report.failures.append((r.path, "unwritable"))
            log(f"  FAIL    [{r.bot_id}] {r.path} (could not strip)")
    return report


# ── CLI ─────────────────────────────────────────────────────────────────────────

def _discover_bot_ids(shared_dir: Path) -> list[str]:
    """All bot ids from ``{shared_dir}/network.json`` (mirrors recon_ledger)."""
    try:
        network = load_network(shared_dir / "network.json")
    except Exception:
        return []
    bots_field = network.get("bots") or {}
    if isinstance(bots_field, dict):
        return list(bots_field.keys())
    if isinstance(bots_field, list):
        return [b["id"] for b in bots_field if isinstance(b, dict) and "id" in b]
    return []


def _print_dry_run(summ: Summary) -> None:
    print("Stale-marker scrub sweep — DRY RUN (no files changed)\n")
    if summ.total == 0:
        print("  No scrub-candidate markers found. Nothing to strip.")
        return
    for bot_id in sorted(summ.by_bot):
        counts = summ.counts_for(bot_id)
        bot_total = sum(counts.values())
        print(f"  {bot_id}  ({bot_total} marker(s) would be stripped)")
        for cls in REASON_CLASSES:
            n = counts.get(cls, 0)
            if not n:
                continue
            print(f"    {cls:<13} {n:>5}")
            for sample in summ.samples.get(bot_id, {}).get(cls, []):
                print(f"        e.g. {sample}")
        print()
    print(f"  TOTAL would strip: {summ.total}")
    print("\n  Re-run with --apply to strip these markers.")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Strip stale evolve provenance markers off never-ownable "
                    "files (the scrub_candidate bucket of the recon ledger).",
    )
    p.add_argument(
        "--bot", action="append", default=[], dest="bots", metavar="BOT_ID",
        help="Bot to sweep (repeatable). Default: all bots in network.json.",
    )
    default_shared_dir = _PROFILE.shared_dir_default
    p.add_argument(
        "--shared-dir", default=default_shared_dir,
        help=f"Pod shared dir (default: {default_shared_dir})",
    )
    p.add_argument(
        "--reason", choices=REASON_CLASSES, default=None,
        help="Strip only this class of stale marker (default: all classes).",
    )
    p.add_argument(
        "--apply", action="store_true",
        help="Actually strip the markers. Without this flag the sweep is a "
             "dry-run and changes nothing.",
    )
    args = p.parse_args(argv)

    shared_dir = Path(args.shared_dir)
    bot_ids = args.bots or _discover_bot_ids(shared_dir)
    if not bot_ids:
        print("ERROR: no bots to sweep (pass --bot or populate network.json)",
              file=sys.stderr)
        return 1

    rows = collect_scrub_rows(shared_dir, bot_ids, reason_filter=args.reason)

    if not args.apply:
        _print_dry_run(summarize(rows))
        return 0

    print(f"Stale-marker scrub sweep — APPLY  ({len(rows)} candidate(s))\n")
    report = strip_rows(rows, log=print)
    print(
        f"\n  stripped {report.stripped}   "
        f"skipped {report.skipped}   failed {report.failed}"
    )
    if report.failures:
        print("\n  Files that could not be stripped (left untouched):")
        for path, why in report.failures:
            print(f"    {path}  — {why}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
