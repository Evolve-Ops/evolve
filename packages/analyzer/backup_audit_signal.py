"""backup_audit_signal — defense-in-depth audit of the actually-pushed tree.

Phase 4a of the backup architecture rework
(spec docs/spec-backup-and-data-classification-2026-05-28.md
§"Post-push audit").

The Phase 3 classification filter in ``backup.py`` decides what gets
staged for the backup commit. If that filter has a bug — or an
operator changes classification *after* a file has already been
pushed — a path that should never be in the cloud repo can end up
there anyway. The push-time guard can't catch this: the bot can't see
its own classification mistakes.

This monitor closes the loop. Once per hour, for each bot with a
backupRepoUrl, it:

  1. Opens the bot's workspace git repo (read-only, via the evolve
     read ACL on ``.openclaw/``).
  2. Lists every path in the latest backup commit's tree
     (``git ls-tree -r --name-only HEAD``).
  3. Builds the same classification resolver ``backup.py`` uses
     (built-ins + pod-wide + per-app manifests).
  4. Flags any path whose classification is *not* ``cloud``.

Any flagged paths fire a ``backup_classification_leak`` Signal at
severity ``alert`` — a leak that's already pushed is real exposure
and needs operator action (and probably secret rotation if the file
contained anything sensitive). Auto-resolves via ``sweep_resolve``
when the leak is cleaned up.

Pure Python; one git subprocess per bot per pass. Cheap; hourly
launchd cadence with the other backup monitors.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from data_classification import (
    ClassificationResolver,
    build_resolver,
    load_bot_manifests,
)
from evolve_config import (
    bot_home,
    get_members,
    get_primary,
    get_shared_dir,
    load_config,
)
from schema.signal import make_signature
from signals import store as signals_store

PRODUCER = "backup_audit_signal"

# Cap on paths surfaced in the Signal body — operators don't need to scroll
# through 500 leaked paths to understand the situation. The detail.details
# dict still includes the full list for programmatic / log triage.
_BODY_PATH_CAP = 25


# ─── Default subprocess runner (test seam) ────────────────────────────────


def _default_list_tree(workspace: Path) -> tuple[list[str], str | None]:
    """Return (paths, error). On any failure, returns ([], error_string)."""
    # ``-c safe.directory=*`` lets the evolve user run git on a workspace
    # owned by the bot user — git 2.35.2+ otherwise rejects cross-owner
    # access with "fatal: detected dubious ownership". Same flag backup.py
    # uses in its own _git helper.
    try:
        r = subprocess.run(
            [
                "git", "-c", "safe.directory=*", "-C", str(workspace),
                "ls-tree", "-r", "--name-only", "HEAD",
            ],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return [], f"git ls-tree failed: {exc!s}"
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()[:200]
        # "fatal: not a git repository" or "fatal: bad default revision 'HEAD'"
        # are expected on fresh bots; surface as a soft empty-tree, not error.
        if "not a git repository" in err.lower() or "bad default revision" in err.lower():
            return [], None
        return [], f"git ls-tree rc={r.returncode}: {err}"
    paths = [p.strip() for p in r.stdout.splitlines() if p.strip()]
    return paths, None


# ─── Audit one bot ─────────────────────────────────────────────────────────


def audit_bot(
    bot_id: str,
    config: dict[str, Any],
    *,
    tree_lister: Callable[[Path], tuple[list[str], str | None]] = _default_list_tree,
    manifest_loader: Callable[[Path], list[dict]] = load_bot_manifests,
    workspace_resolver: Callable[[str], Path] | None = None,
) -> tuple[list[dict], str | None]:
    """Return (leaks, error) for one bot's backup repo.

    ``leaks`` is a list of ``{path, classification, source}`` dicts —
    every path in HEAD that classified as something other than
    ``cloud``. ``error`` is set when the audit couldn't run (workspace
    missing, git error, etc.) — distinct from "audit ran cleanly and
    found nothing" (empty leaks, None error).
    """
    if workspace_resolver is None:
        try:
            workspace = bot_home(bot_id, config) / ".openclaw" / "workspace"
        except Exception as exc:  # noqa: BLE001
            return [], f"workspace resolve failed: {exc!s}"
    else:
        workspace = workspace_resolver(bot_id)

    if not workspace.exists():
        return [], None  # bot not deployed yet — silent skip

    paths, err = tree_lister(workspace)
    if err:
        return [], err

    try:
        manifests = manifest_loader(workspace)
    except Exception as exc:  # noqa: BLE001
        return [], f"manifest load failed: {exc!s}"

    resolver = build_resolver(
        manifests=manifests,
        network=config,
        fallback_default="cloud",
    )

    leaks: list[dict] = []
    for path in paths:
        classification, source = resolver.explain(path)
        if classification != "cloud":
            leaks.append({
                "path": path,
                "classification": classification,
                "source": source,
            })
    return leaks, None


# ─── Signal builder ───────────────────────────────────────────────────────


def build_signal_for_leak(bot_id: str, leaks: list[dict]) -> dict:
    """Render the alert Signal for a bot whose backup repo carries non-cloud paths."""
    n = len(leaks)
    by_classification: dict[str, list[str]] = {}
    for leak in leaks:
        by_classification.setdefault(leak["classification"], []).append(leak["path"])

    body_lines = [
        f"`{bot_id}`'s backup repo contains **{n} path"
        f"{'s' if n != 1 else ''}** that classify as something other than "
        "`cloud`. These files have already been pushed — they're in the "
        "GitHub repo and (if the repo's history is preserved) in every "
        "snapshot since they were committed.",
        "",
    ]
    # 2026-05-29 fix: the "N more not shown" message used to compare
    # ``len(paths)`` against the global cap, which lied when the cap was
    # already consumed by an earlier classification. Track shown count
    # per-classification so the elision message reflects how many were
    # ACTUALLY suppressed in this loop iteration.
    sample_shown = 0
    for klass in ("local", "ephemeral"):
        paths = by_classification.get(klass, [])
        if not paths:
            continue
        body_lines.append(f"**{klass}** ({len(paths)} path{'s' if len(paths) != 1 else ''}):")
        room_left = max(1, _BODY_PATH_CAP - sample_shown)
        shown_for_klass = 0
        for p in paths[:room_left]:
            body_lines.append(f"- `{p}`")
            sample_shown += 1
            shown_for_klass += 1
            if sample_shown >= _BODY_PATH_CAP:
                break
        suppressed = len(paths) - shown_for_klass
        if suppressed > 0:
            body_lines.append(f"  (… {suppressed} more not shown)")
        body_lines.append("")
        if sample_shown >= _BODY_PATH_CAP:
            break

    body_lines += [
        "**Fix:**",
        "",
        "1. **Decide if the classification is wrong.** If these files were "
        "meant to be cloud-eligible all along, update the app's manifest "
        "or the pod-wide rules in Backup → Data so the audit stops "
        "flagging them. No history rewrite needed in this case.",
        "",
        "2. **Or treat it as a real leak.** Rotate any credentials that "
        "appeared in the leaked files. Remove the files from the backup "
        "repo via the GitHub UI (or `git filter-repo` / `git filter-branch` "
        "for full history scrubbing). The next backup will resync.",
        "",
        "Re-classification or removal both auto-resolve this Signal on "
        "the next monitor pass (within ~1h).",
    ]
    return dict(
        signature=make_signature(PRODUCER, "backup_classification_leak", bot_id),
        producer=PRODUCER,
        type="backup_classification_leak",
        flavor="safety",
        severity="alert",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id} backup repo carries {n} non-cloud path"
              f"{'s' if n != 1 else ''}",
        body="\n".join(body_lines),
        details=dict(
            leak_count=n,
            leaks=leaks,  # full list, programmatic-friendly
            by_classification={k: len(v) for k, v in by_classification.items()},
            what_it_means=(
                "Files classified as local or ephemeral ended up in the cloud "
                "backup repo. This is either a classifier bug (rare) or a "
                "post-hoc classification change (operator marked a path "
                "local after files had already been pushed). Either way, "
                "the affected files are currently in the cloud repo."
            ),
        ),
    )


# ─── Run loop ──────────────────────────────────────────────────────────────


def _bot_has_backup_url(config: dict, bot_id: str) -> bool:
    bots = config.get("bots") if isinstance(config.get("bots"), dict) else {}
    cfg = bots.get(bot_id) or {}
    return bool((cfg.get("backupRepoUrl") or "").strip())


def run(
    shared_dir: Path,
    config: dict[str, Any],
    *,
    bots: list[str] | None = None,
    dry_run: bool = False,
    tree_lister: Callable[[Path], tuple[list[str], str | None]] = _default_list_tree,
    manifest_loader: Callable[[Path], list[dict]] = load_bot_manifests,
    workspace_resolver: Callable[[str], Path] | None = None,
) -> tuple[set[str], int, int]:
    """Walk every backup-configured bot; fire / resolve leak Signals.

    Returns ``(kept_signatures, n_fired, n_resolved)``.

    When ``bots`` is explicitly narrowed (e.g. ``--bot team_bot_a``), the
    sweep_resolve at the end is scoped to that bot set so other bots'
    still-firing leak Signals aren't accidentally archived. The audit
    error path (when ``audit_bot`` returns an error) preserves the
    bot's existing signature in ``kept`` so a transient git timeout
    doesn't archive a real, firing leak Signal. See the 2026-05-29
    review-session fixes.
    """
    bots_explicit = bots is not None
    if bots is None:
        primary = get_primary(config)
        members = get_members(config)
        bots = ([primary] if primary and primary not in members else []) + members
        bots = [b for b in bots if b]

    kept: set[str] = set()
    n_fired = 0
    for bot_id in bots:
        if not _bot_has_backup_url(config, bot_id):
            continue  # bot opted out of cloud backup; nothing to audit
        leaks, err = audit_bot(
            bot_id, config,
            tree_lister=tree_lister,
            manifest_loader=manifest_loader,
            workspace_resolver=workspace_resolver,
        )
        if err:
            # Log and continue; one bot's audit failure shouldn't blank
            # the others. Soft errors (missing workspace) report as no leak.
            #
            # **Preserve the bot's existing leak Signal on transient
            # error.** Without this, sweep_resolve below would archive a
            # real, firing leak as a side effect of a flaky git timeout.
            # Add the canonical signature to ``kept`` regardless of
            # whether one actually exists in the store — sweep_resolve
            # treats it as a no-op if there's no matching active Signal.
            kept.add(make_signature(PRODUCER, "backup_classification_leak", bot_id))
            print(
                f"[backup_audit_signal] {bot_id}: audit failed: {err}",
                flush=True, file=sys.stderr,
            )
            continue
        if not leaks:
            continue
        spec = build_signal_for_leak(bot_id, leaks)
        kept.add(spec["signature"])
        n_fired += 1
        if dry_run:
            print(json.dumps({"would_observe": spec}, default=str), flush=True)
            continue
        try:
            signals_store.observe(shared_dir, **spec)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[backup_audit_signal] observe failed for "
                f"{spec['signature']}: {exc}",
                flush=True,
            )

    n_resolved = 0
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                reason="auto-resolve: backup repo no longer carries non-cloud paths",
                bot_ids=(set(bots) if bots_explicit else None),
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[backup_audit_signal] sweep_resolve failed: {exc}", flush=True)

    return kept, n_fired, n_resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "backup_audit_signal — defense-in-depth audit verifying the "
            "actually-pushed backup tree matches classification"
        ),
    )
    parser.add_argument("--network", default=None)
    parser.add_argument("--bot", default=None, help="Run only for this bot")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print would-be signals; don't write or sweep-resolve",
    )
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)
    bots = [args.bot] if args.bot else None
    kept, n_fired, n_resolved = run(
        shared_dir, config, bots=bots, dry_run=args.dry_run,
    )
    if args.dry_run:
        print(f"[backup_audit_signal] dry-run: {n_fired} would-fire", flush=True)
        return
    print(
        f"[backup_audit_signal] {n_fired} firings, {n_resolved} resolved",
        flush=True,
    )


if __name__ == "__main__":
    main()
