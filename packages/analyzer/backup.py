#!/usr/bin/env python3
"""
backup.py — Nightly git backup of one bot's security-relevant state.

Each bot already has its own workspace repo (``~/.openclaw/workspace/``).
backup.py copies openclaw.json into the workspace, commits any pending
changes, and pushes to the bot's configured remote.

Config in network.json (per bot):
  "bots": {
    "{bot_id}": {
      "backupRepoUrl": "git@github.com:yourorg/{bot_id}-workspace.git"
    }
  }

**Run as: the bot user** (e.g. as ``team_bot_a`` for team_bot_a, as ``personal_bot_user`` for
team_bot_b). One launchd daemon per bot — ``ai.evolve.<bot_id>.backup`` —
installed by deploy.py. Each daemon backs up only its own bot; no
cross-user ACL gymnastics, no shared state.

**SSH deploy key**: ``~/.ssh/evolve-backup-{bot_id}`` in the running
user's home (i.e. each bot's home). Same key VALUE may live in
multiple bots' files when one GitHub account owns every repo — the
admin "Distribute key" affordance copies one generated key into each
bot's file. Per-repo isolation is achievable by putting a different
key value into each bot's file.

**State file**: ``{workspace}/evolve-backup/state.json`` (per-bot,
bot-owned). Admin UI reads via the existing workspace read-ACL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from evolve_config import load_config, get_shared_dir, get_members, resolve_network_path, bot_home as _bot_home
from evolve_util import now_iso
from secret_redaction import redact_file as _redact_snapshot_file
from backup_visibility import check_repo_visibility, load_pat as _load_github_pat, parse_github_repo as _parse_github_repo
from data_classification import build_resolver as _build_classification_resolver, load_bot_manifests as _load_bot_manifests

SCRIPT_VERSION = "2.0.0"


def sha256_file(path: Path) -> str:
    """Return hex SHA256 of a file, or empty string if not readable."""
    try:
        h = hashlib.sha256()
        h.update(path.read_bytes())
        return h.hexdigest()
    except OSError:
        return ""


def load_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# ── SSH key management ─────────────────────────────────────────────────────────

def ssh_key_path(bot_id: str) -> Path:
    return Path.home() / ".ssh" / f"evolve-backup-{bot_id}"


# Key generation moved to evolve_admin.backup_keys under the unified
# shared-key model. The bot-side daemon (this module) only READS keys
# at ``ssh_key_path(bot_id)``. See
# ``docs/spec-backup-key-distribution-unification-2026-06-08.md``.


# ── Git helpers ────────────────────────────────────────────────────────────────

def _git(args: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    base_env = {**os.environ}
    if env:
        base_env.update(env)
    # -c safe.directory=*: the workspace is owned by the bot user, while
    # callers may run as evolve, root (via sudo), or the bot user itself.
    # Without this flag git 2.35.2+ rejects any cross-owner access with
    # "fatal: detected dubious ownership". Harmless when caller matches owner.
    return subprocess.run(
        ["git", "-c", "safe.directory=*"] + args,
        cwd=str(cwd),
        env=base_env,
        capture_output=True,
        text=True,
    )


def _ssh_env(bot_id: str, network: dict | None = None) -> dict:
    """Return GIT_SSH_COMMAND pointing at the bot's deploy key.

    Looks at ``~/.ssh/evolve-backup-{bot_id}`` in the running user's home.
    Per-bot daemons run as the bot user, so this resolves to
    ``/Users/{bot}/.ssh/evolve-backup-{bot}`` — bot-owned, bot-readable.

    Under the unified shared-key model
    (``docs/spec-backup-key-distribution-unification-2026-06-08.md``)
    every bot's file holds bytes identical to the pod-wide source at
    ``/Users/evolve/.ssh/evolve-backup-shared``. backup.py stays
    agnostic: each bot daemon reads its own file; the unification is
    enforced upstream by ``evolve_admin.backup_keys``. ``network`` arg
    retained for back-compat with callers that still pass it; no
    longer consulted for SSH credentials.
    """
    key = ssh_key_path(bot_id)
    if key.exists():
        return {"GIT_SSH_COMMAND": f"ssh -i {key} -o StrictHostKeyChecking=accept-new -o BatchMode=yes"}
    return {}


def _ensure_remote(workspace: Path, remote_url: str, env: dict) -> bool:
    """Ensure 'origin' is set to remote_url in the workspace repo."""
    r = _git(["remote", "get-url", "origin"], workspace)
    if r.returncode == 0:
        current = r.stdout.strip()
        if current != remote_url:
            _git(["remote", "set-url", "origin", remote_url], workspace)
    else:
        r2 = _git(["remote", "add", "origin", remote_url], workspace)
        if r2.returncode != 0:
            print(f"[backup] ERROR adding remote {remote_url}: {r2.stderr}", file=sys.stderr)
            return False
    return True


# ── Apply-result tracking ──────────────────────────────────────────────────────

def get_applied_since_last_backup(bot_id: str, shared_dir: Path, workspace: Path) -> list[str]:
    """Return proposal IDs applied to bot_id since the last backup commit in workspace."""
    # Get timestamp of last backup commit
    r = _git(["log", "--format=%aI", "-1", "--grep", r"^\[backup\]"], workspace)
    last_backup_ts: str | None = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None

    results_dir = shared_dir / "proposals" / "apply-results"
    if not results_dir.exists():
        return []
    applied = []
    for f in sorted(results_dir.glob(f"{bot_id}-*.json")):
        try:
            result = json.loads(f.read_text())
            if result.get("status") != "applied":
                continue
            applied_at = result.get("applied_at", "")
            if last_backup_ts is None or applied_at > last_backup_ts:
                applied.append(result.get("proposal_id", f.stem))
        except (OSError, json.JSONDecodeError):
            pass
    return applied


# ── Per-bot run-state file ─────────────────────────────────────────────────────
#
# Lives inside the bot's own workspace so the bot user can write it
# without ACL gymnastics. Admin UI reads via the existing read-ACL on
# `.openclaw/` that ``set_evolve_read_acl`` already grants.
#
# An earlier shared location at ``{shared_dir}/backup/<bot>.json`` was
# evolve-owned and worked when a single evolve-user daemon iterated all
# bots; per-bot daemons running as their bot user can't write there.


def run_state_path(bot_id: str, *, workspace: Path | None = None) -> Path:
    """Where the per-bot backup run-state JSON lives.

    Default: ``~/.openclaw/workspace/evolve-backup/state.json`` (resolved
    via ``_bot_home(bot_id)`` when ``workspace`` is None). Pass an
    explicit ``workspace`` from callers that already have it (avoids
    re-resolving the bot's home from network.json).
    """
    if workspace is None:
        workspace = _bot_home(bot_id) / ".openclaw" / "workspace"
    return workspace / "evolve-backup" / "state.json"


def read_run_state(bot_id: str, *, workspace: Path | None = None) -> dict:
    """Read the per-bot backup run state. Empty dict if not yet written.

    Schema:
      bot_id              str
      last_attempt_at     ISO8601 (UTC) — when this script most recently ran for the bot
      last_attempt_status "ok" | "no-changes" | "skipped" | "failed"
      last_success_at     ISO8601 or null — most recent ok/no-changes
      last_error          str or null     — captured stderr/exception from the last failure
      consecutive_failures int            — count of failed attempts since the last success
    """
    try:
        return json.loads(run_state_path(bot_id, workspace=workspace).read_text())
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
        return {}


def write_run_state(
    bot_id: str,
    *,
    status: str,
    error: str | None,
    workspace: Path | None = None,
) -> dict:
    """Update the per-bot run state after a backup attempt.

    Idempotent atomic write (temp + rename). Owned by the bot user
    (whoever the daemon runs as). Returns the written state dict for
    callers that want to log it.

    ``status`` semantics:
      - "ok" / "no-changes" → success path; consecutive_failures resets to 0
        and last_success_at advances. ``error`` is ignored.
      - "skipped" → not really an attempt (workspace missing, no URL, etc.).
        Preserve previous success state, don't bump the failure counter.
      - "failed" → bump consecutive_failures, capture the error, preserve
        last_success_at (might be old; that's the point).
    """
    out_path = run_state_path(bot_id, workspace=workspace)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prev = read_run_state(bot_id, workspace=workspace)
    now = now_iso()
    new_state = dict(prev)
    new_state["bot_id"] = bot_id
    new_state["last_attempt_at"] = now
    new_state["last_attempt_status"] = status
    if status in ("ok", "no-changes"):
        new_state["last_success_at"] = now
        new_state["last_error"] = None
        new_state["consecutive_failures"] = 0
    elif status == "failed":
        new_state["consecutive_failures"] = int(prev.get("consecutive_failures", 0)) + 1
        new_state["last_error"] = (error or "")[:2000]  # cap to keep file small
    elif status == "skipped":
        # Don't touch consecutive_failures or last_success_at — this attempt
        # wasn't actually a backup (no URL, no workspace, etc.).
        new_state.setdefault("consecutive_failures", int(prev.get("consecutive_failures", 0)))
        new_state.setdefault("last_success_at", prev.get("last_success_at"))
        new_state["last_error"] = error
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(new_state, indent=2) + "\n")
    tmp.replace(out_path)
    return new_state


# ── Per-bot backup ─────────────────────────────────────────────────────────────

def backup_bot(
    bot_id: str,
    shared_dir: Path,
    backup_url: str,
    dry_run: bool = False,
    network: dict | None = None,
) -> str:
    """Backup one bot's workspace repo.

    Returns one of:
      - "ok"        — fresh commit pushed
      - "no-changes" — nothing new to commit; backlog (if any) pushed cleanly
      - "skipped"   — bot not eligible (workspace missing / not a git repo)
      - "failed"    — backup attempted but errored

    Side effect: writes ``{workspace}/evolve-backup/state.json`` capturing
    last_attempt_at, last_success_at, last_error, consecutive_failures —
    so the UI can render "✗ push failed Xh ago" with the actual error
    instead of silently showing a stale ✓ from days ago. Skipped in
    dry-run so test invocations don't poison real state.
    """
    workspace = _bot_home(bot_id) / ".openclaw" / "workspace"
    status, error = _backup_bot_attempt(
        bot_id, shared_dir, backup_url, dry_run, network,
    )
    if not dry_run:
        try:
            write_run_state(bot_id, status=status, error=error, workspace=workspace)
        except Exception as exc:  # noqa: BLE001
            print(f"[backup] {bot_id}: state write failed: {exc}", file=sys.stderr)
    return status


def _backup_bot_attempt(
    bot_id: str,
    shared_dir: Path,
    backup_url: str,
    dry_run: bool,
    network: dict | None,
) -> tuple[str, str | None]:
    """Inner workhorse — returns (status, error_msg). Caller writes state."""
    print(f"[backup] {bot_id}: starting backup")

    bot_oc = _bot_home(bot_id) / ".openclaw"
    workspace = bot_oc / "workspace"
    env = _ssh_env(bot_id, network=network)

    if not workspace.exists():
        msg = f"workspace not found at {workspace}"
        print(f"[backup] {bot_id}: {msg} — skipping", file=sys.stderr)
        return "skipped", msg

    # Distinguish "no .git/ dir" (legitimate skip — needs backup-init) from
    # ".git/ exists but git can't open it" (real failure — usually a
    # permissions/ownership problem inside .git/). Collapsing both into
    # "not a git repo" misled triage of team_bot_c/security_bot on 2026-05-26, where
    # .git/config was mode 600 owned by root after an out-of-band repair
    # and the daemon (running as the bot user) reported the workspace as
    # un-initialized when it actually just couldn't be opened.
    r = _git(["rev-parse", "--git-dir"], workspace)
    if r.returncode != 0:
        if not (workspace / ".git").exists():
            msg = "workspace is not a git repo"
            print(f"[backup] {bot_id}: {msg} — skipping", file=sys.stderr)
            return "skipped", msg
        err = (r.stderr or r.stdout or "").strip()[:400] or "unknown git error"
        msg = f".git/ present but git rev-parse failed: {err}"
        print(f"[backup] {bot_id}: {msg}", file=sys.stderr)
        return "failed", msg

    # Ensure remote is configured
    if not _ensure_remote(workspace, backup_url, env):
        return "failed", f"failed to configure remote {backup_url!r}"

    # ── Visibility guard ──────────────────────────────────────────────────────
    # Refuse to push if the backup repo isn't confirmed private. GitHub
    # defaults new repos to public, and a single misclick (or someone
    # flipping the repo via the GitHub UI later) would expose the entire
    # workspace. Fail-safe: anything other than "private" blocks the push.
    # Spec: docs/spec-backup-and-data-classification-2026-05-28.md §"Phase 1".
    pat = _load_github_pat(network)
    if not pat:
        # PAT not configured → can't verify → skip rather than fail.
        # consecutive_failures shouldn't accumulate against a config gap;
        # the backup_signal monitor still surfaces this via a distinct
        # "configure github.pat" Signal on its own cadence.
        msg = (
            "skipping push: no GitHub PAT configured (keystore key "
            "`github_pat`). Backup-repo visibility cannot be verified "
            "without a GitHub PAT. Store one via the Backup → Cloud "
            "wizard to enable cloud backup; pushes are blocked until then."
        )
        print(f"[backup] {bot_id}: {msg}", file=sys.stderr)
        # Cloud push is unavailable, but still keep heal's drift baseline
        # fresh locally so config_drift doesn't re-fire forever here.
        _refresh_local_drift_baseline(bot_id, shared_dir)
        return "skipped", msg

    visibility = check_repo_visibility(backup_url, pat=pat)
    if visibility != "private":
        parsed = _parse_github_repo(backup_url)
        settings_url = (
            f"https://github.com/{parsed[0]}/{parsed[1]}/settings"
            if parsed else "the repo settings page"
        )
        msg = (
            f"refusing to push: backup repo visibility is {visibility!r}, "
            f"not 'private'. Make the repo private at {settings_url} to "
            f"resume backups. (See spec-backup-and-data-classification.)"
        )
        print(f"[backup] {bot_id}: {msg}", file=sys.stderr)
        # Push is blocked on the visibility guard, but the local drift
        # baseline should still track live so heal doesn't re-fire
        # config_drift every tick while the operator fixes repo visibility.
        _refresh_local_drift_baseline(bot_id, shared_dir)
        return "failed", msg

    # Configure git identity (in case not set in this repo)
    _git(["config", "user.name", "Evolve Backup"], workspace)
    _git(["config", "user.email", "evolve@localhost"], workspace)

    # ── Stage security-relevant files ─────────────────────────────────────────
    backup_dir = workspace / "evolve-backup"
    backup_dir.mkdir(exist_ok=True)

    # Copy openclaw.json (readable by evolve via sudo, or directly if world-readable)
    oc_src = bot_oc / "openclaw.json"
    oc_dst = backup_dir / "openclaw.json"
    ok, _ = _copy_with_sudo(oc_src, oc_dst, bot_id)
    if ok:
        # The workspace is LLM-readable and pushed to a remote backup repo —
        # snapshotting the live config verbatim would store live integration
        # credentials in two more places. Redact secrets in-place with stable
        # hash markers so drift detection still works (heal.py applies the
        # same redaction to the live side before comparing). See triage
        # 2026-05-25 and secret_redaction module docstring.
        changed, reason = _redact_snapshot_file(oc_dst)
        if not changed and reason not in ("no-secrets",):
            print(f"[backup] {bot_id}: WARN redact_file({oc_dst}) → {reason}", file=sys.stderr)

    # Also stage evolve-tiers.json — this is the file the plugin's
    # ModelRouter actually reads for cascade routing (cascade.enabled,
    # tier definitions, tierCascade). Pre-2026-05-29 it was OUTSIDE
    # the backup/drift/heal layers entirely: a manual `vim` edit
    # of cascade.enabled or wipe of tier2.models[] changed live
    # routing with zero alert, zero diff, zero snapshot — the
    # single largest hole in the watchdog layer per L10 of the
    # 2026-05-29 tier audit. Now it round-trips alongside
    # openclaw.json. No secrets in this file; no redaction needed.
    # Use the same copy helper as openclaw.json — its ok/skip return
    # already handles "file not present" gracefully (older bots that
    # never wrote a tier config). No exception bubbles up.
    tiers_src = bot_oc / "evolve-tiers.json"
    tiers_dst = backup_dir / "evolve-tiers.json"
    _copy_with_sudo(tiers_src, tiers_dst, bot_id)

    # Copy latest metrics
    metrics_src = shared_dir / "metrics" / bot_id / "latest.json"
    if metrics_src.exists():
        metrics_dst = backup_dir / "metrics" / "latest.json"
        metrics_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(metrics_src), str(metrics_dst))

    # ── Stage (always) ──────────────────────────────────────────────────────
    # Surface git-add failures explicitly. Pre-2026-05-26 this ran without
    # a returncode check, so silent failures (personal_bot: ".git/objects/NN" dirs
    # owned by root → "insufficient permission for adding an object to
    # repository database") cascaded into the commit step, which then
    # exited non-zero with "no changes added to commit" on stdout + empty
    # stderr — the daemon reported "commit failed: " with no further detail
    # and the real cause sat invisible in the staging step's discarded output.
    #
    # **2026-05-29 (Phase 4b cleanup ordering).** add + prune now run
    # *before* the no-changes check. The pruner's HEAD-reclassification
    # path can stage a deletion when nothing else has changed in the
    # working tree — without this ordering, an operator who flips a path
    # from cloud → local in the Data tab wouldn't see the cleanup happen
    # until something else dirtied the workspace.
    add_r = _git(["add", "-A"], workspace)
    if add_r.returncode != 0:
        msg = f"git add -A failed: {(add_r.stderr or add_r.stdout or '').strip()[:400]}"
        print(f"[backup] {bot_id}: {msg}", file=sys.stderr)
        return "failed", msg

    # Phase 3 + Phase 4b: prune the next commit's index of any path
    # that classifies non-cloud. Catches two cases:
    #   1. New / modified files in the index that shouldn't be pushed (the
    #      original Phase 3 filter).
    #   2. Previously-committed files in HEAD whose classification was
    #      flipped to local/ephemeral after they'd been pushed — the
    #      pruner stages a deletion so the next commit removes them from
    #      the cloud repo and the Phase 4a audit clears on the next pass.
    # Pods with no v15 classification declared see this as a no-op. Spec:
    # docs/spec-backup-and-data-classification-2026-05-28.md §"Phase 3".
    excluded_count, filter_err = _apply_classification_pruning(
        bot_id, workspace, network,
    )
    if filter_err:
        # Fail-open: classification errors don't block backup; we just
        # back up everything (pre-Phase-3 behaviour) and log. The Phase 4
        # post-push audit catches genuine leaks.
        print(
            f"[backup] {bot_id}: classification filter failed: "
            f"{filter_err} — backup proceeds without filtering",
            file=sys.stderr,
        )
    elif excluded_count:
        print(
            f"[backup] {bot_id}: excluded {excluded_count} file(s) from "
            f"cloud backup per manifest classification",
            flush=True,
        )

    # ── Anything actually queued for commit? ────────────────────────────────
    # After add + prune, ``git diff --cached --quiet`` exits 0 iff there's
    # no diff between the index and HEAD — i.e. nothing to commit. Covers
    # both "nothing changed in the working tree" and "everything that did
    # change was just pruned back out." Either way, fall through to the
    # backlog-push branch.
    diff_r = _git(["diff", "--cached", "--quiet"], workspace)
    nothing_staged = (diff_r.returncode == 0)

    if nothing_staged:
        # Nothing new to commit. But local commits may still be unpushed —
        # e.g. from a previous run where the commit succeeded but the push
        # failed (read-only deploy key, network glitch, etc.). Reach out
        # and push any backlog so the remote eventually catches up; if
        # there's nothing to push, this is a cheap no-op.
        ahead_r = _git(
            ["rev-list", "--count", "@{upstream}..HEAD"], workspace,
        )
        ahead = 0
        if ahead_r.returncode == 0:
            try:
                ahead = int((ahead_r.stdout or "0").strip() or 0)
            except ValueError:
                ahead = 0
        if ahead > 0:
            print(f"[backup] {bot_id}: no new changes but {ahead} unpushed commit(s) — pushing backlog")
            push_r = _git(["push", "origin", "HEAD"], workspace, env)
            if push_r.returncode != 0:
                msg = f"backlog push failed: {(push_r.stderr or '').strip()[:400]}"
                print(f"[backup] {bot_id}: {msg}", file=sys.stderr)
                return "failed", msg
        print(f"[backup] {bot_id}: no changes — skipping commit")
        return "no-changes", None

    # ── Build commit message ───────────────────────────────────────────────
    ts = now_iso()
    applied_ids = get_applied_since_last_backup(bot_id, shared_dir, workspace)
    config_hash = sha256_file(oc_src)
    soul_hash = sha256_file(workspace / "SOUL.md")
    applied_str = ", ".join(applied_ids) if applied_ids else "none"
    commit_msg = (
        f"[backup] {bot_id} {ts}\n"
        f"applied-since-last: {applied_str}\n"
        f"config-hash: {config_hash[:16]}\n"
        f"soul-hash: {soul_hash[:16]}\n"
    )
    if dry_run:
        print(f"[backup] {bot_id}: DRY RUN — would commit:\n{commit_msg}")
        return "ok", None

    r = _git(
        ["commit", "-m", commit_msg, "--author=evolve <evolve@localhost>"],
        workspace,
    )
    if r.returncode != 0 and "nothing to commit" not in r.stdout:
        msg = f"commit failed: {(r.stderr or '').strip()[:400]}"
        print(f"[backup] {bot_id}: {msg}", file=sys.stderr)
        return "failed", msg

    # ── Push (with rebase-on-divergence recovery) ─────────────────────────────
    # 2026-05-26 personal_bot incident: an "accept-drift" commit landed locally
    # but the next push got rejected as ``main -> main (fetch first)`` —
    # not because local was behind, but because the locally-cached
    # ``origin/main`` ref was stale (some out-of-band push to the remote
    # advanced it past what local had fetched). The push command itself
    # doesn't auto-fetch, so the backup stayed wedged for days.
    #
    # Defense: on push reject, fetch the remote and rebase. If the rebase
    # is clean (typical: our local "ahead" commit replays cleanly on top
    # of whatever the remote added), retry the push. If rebase conflicts,
    # surface a clear error so the operator can reconcile manually —
    # backup commits are usually trivially rebasable (different file
    # contents, no logical conflict) but we don't paper over real
    # conflicts.
    r = _git(["push", "origin", "HEAD"], workspace, env)
    if r.returncode != 0:
        stderr = (r.stderr or "").lower()
        diverged = "fetch first" in stderr or "non-fast-forward" in stderr or "rejected" in stderr
        if diverged:
            print(
                f"[backup] {bot_id}: push rejected (remote diverged); "
                f"fetching + rebasing then retrying",
                file=sys.stderr,
            )
            fetch_r = _git(["fetch", "origin"], workspace, env)
            if fetch_r.returncode != 0:
                msg = (
                    f"push rejected and fetch failed: "
                    f"{(fetch_r.stderr or '').strip()[:300]}"
                )
                print(f"[backup] {bot_id}: {msg}", file=sys.stderr)
                return "failed", msg
            # 2026-05-29 fix: ``origin/HEAD`` is a symbolic ref that only
            # ``git clone`` sets up. Evolve-managed workspaces are
            # initialised via ``git remote add``, which does NOT create
            # the symref — so ``git rebase origin/HEAD`` failed with
            # "needed a single revision" and the recovery path the personal_bot
            # incident motivated never actually ran. Resolve the current
            # branch and rebase against ``origin/<branch>`` instead, with
            # a fallback to ``origin/main`` if branch detection fails.
            branch_r = _git(["rev-parse", "--abbrev-ref", "HEAD"], workspace)
            current_branch = (
                branch_r.stdout.strip()
                if branch_r.returncode == 0 and branch_r.stdout.strip()
                else "main"
            )
            rebase_r = _git(
                ["rebase", f"origin/{current_branch}"], workspace, env,
            )
            if rebase_r.returncode != 0:
                # Rebase conflicted — back out + tell the operator. We
                # don't try to resolve; backup commits aren't worth a
                # heuristic merger.
                _git(["rebase", "--abort"], workspace)
                msg = (
                    f"push rejected and rebase conflicted: "
                    f"{(rebase_r.stderr or '').strip()[:300]}. "
                    f"Operator must reconcile workspace manually."
                )
                print(f"[backup] {bot_id}: {msg}", file=sys.stderr)
                return "failed", msg
            r = _git(["push", "origin", "HEAD"], workspace, env)
        if r.returncode != 0:
            # Try push with upstream tracking set
            branch_r = _git(["rev-parse", "--abbrev-ref", "HEAD"], workspace)
            branch = branch_r.stdout.strip() if branch_r.returncode == 0 else "main"
            r2 = _git(["push", "--set-upstream", "origin", branch], workspace, env)
            if r2.returncode != 0:
                msg = f"push failed: {(r2.stderr or '').strip()[:400]}"
                print(f"[backup] {bot_id}: {msg}", file=sys.stderr)
                return "failed", msg

    print(f"[backup] {bot_id}: backup complete — applied: {applied_str}")
    return "ok", None


# ── Phase 3 classification pruning ─────────────────────────────────────────


def _apply_classification_pruning(
    bot_id: str,
    workspace: Path,
    network: dict | None,
) -> tuple[int, str | None]:
    """Drop any non-cloud path from the next backup commit's index.

    Phase 3 of the backup architecture rework + Phase 4b reclassification
    cleanup. Runs after ``git add -A`` and before the commit. Returns
    ``(n_excluded, error)``.

    Operates on the union of:

      - paths in the staged index (``git ls-files --cached``) — catches
        newly added / modified files filtered for this commit
      - paths in HEAD's tree (``git ls-tree -r --name-only HEAD``) — catches
        previously-committed files whose classification has since been
        flipped to local/ephemeral

    For paths classifying non-cloud, ``git rm --cached --ignore-unmatch``
    drops them from the index. ``--ignore-unmatch`` keeps the call clean
    when a path is in HEAD only (no longer in the index — e.g. ``git add
    -A`` already staged its delete) or in the index only — both legitimate
    states for the same chunk-rm call.

    Fail-open semantics: on any internal error (resolver build, git
    failure), returns ``(0, "error message")`` and the caller logs but
    proceeds. We'd rather back up everything than block the operator's
    pod over a classifier bug. The Phase 4a post-push audit is the safety
    net for fail-open misses.
    """
    try:
        manifests = _load_bot_manifests(workspace)
    except Exception as exc:  # noqa: BLE001
        return 0, f"manifest load failed: {exc!s}"

    try:
        resolver = _build_classification_resolver(
            manifests=manifests,
            network=network or {},
            fallback_default="cloud",
        )
    except Exception as exc:  # noqa: BLE001
        return 0, f"resolver build failed: {exc!s}"

    # Index paths — what's about to be committed.
    idx = _git(["ls-files", "--cached"], workspace)
    if idx.returncode != 0:
        return 0, f"ls-files --cached failed: {(idx.stderr or '').strip()[:200]}"
    index_paths = {p.strip() for p in idx.stdout.splitlines() if p.strip()}

    # HEAD tree paths — what's already in the repo. A path in HEAD but not
    # in the index is a previously-committed file. If the operator just
    # reclassified it (cloud → local), this is where we catch it: rm --cached
    # stages a deletion so the next commit removes it from the cloud repo.
    head = _git(
        ["ls-tree", "-r", "--name-only", "HEAD"], workspace,
    )
    head_paths: set[str] = set()
    if head.returncode == 0:
        head_paths = {p.strip() for p in head.stdout.splitlines() if p.strip()}
    else:
        # First backup commit has no HEAD — that's fine, only index paths matter.
        err = (head.stderr or "").lower()
        if not any(s in err for s in (
            "bad default revision",
            "ambiguous argument",
            "unknown revision",
            "does not have any commits yet",
        )):
            return 0, f"ls-tree HEAD failed: {(head.stderr or '').strip()[:200]}"

    candidates = sorted(index_paths | head_paths)
    if not candidates:
        return 0, None

    to_unstage: list[str] = []
    for path in candidates:
        try:
            classification = resolver.classify(path)
        except Exception:  # noqa: BLE001 — defensive; never block backup on a single weird path
            continue
        if classification != "cloud":
            to_unstage.append(path)

    if not to_unstage:
        return 0, None

    # ``--ignore-unmatch`` lets a single rm call cover paths that are in
    # HEAD only (no index entry) or in the index only — both legitimate
    # for paths the pruner is asked to remove. Chunked at 50 to keep below
    # ARG_MAX on workspaces with many newly-reclassified files.
    CHUNK = 50
    excluded = 0
    for i in range(0, len(to_unstage), CHUNK):
        chunk = to_unstage[i:i + CHUNK]
        rm_r = _git(
            ["rm", "--cached", "--quiet", "--ignore-unmatch", "--"] + chunk, workspace,
        )
        if rm_r.returncode != 0:
            return excluded, (
                f"rm --cached failed at chunk {i}: "
                f"{(rm_r.stderr or '').strip()[:200]}"
            )
        excluded += len(chunk)
    return excluded, None


# Back-compat alias — callers (and tests written before the Phase 4b rename)
# still see the old name. Removable once Phase 4b lands in main.
_apply_classification_filter = _apply_classification_pruning


def _copy_with_sudo(src: Path, dst: Path, bot_id: str) -> tuple[bool, str]:
    """Copy src to dst. Direct copy on the per-bot-daemon path.

    Returns ``(ok, reason)``. ``reason`` is ``""`` on success and a short
    machine-readable token on failure: ``"cannot read"`` when the source
    cannot be read, ``"cannot write"`` when the source was read OK but the
    destination write failed. This distinction is load-bearing — see
    docs/diagnosis-accept-drift-regression-2026-05-21.md for why a stale
    "cannot read" message led evo to confabulate a non-existent ACL +
    admin-user misconfiguration when the actual failure was on the write
    side.

    The sudo fallback that used to live here was a vestige of the
    central-daemon-as-evolve era (when evolve needed sudo to read bot
    files). With per-bot daemons running AS the bot user, the direct
    copy always succeeds — the bot owns both the source openclaw.json
    and the destination workspace. The function name is retained for
    back-compat with callers like commit_baseline_local.

    Mixed-ownership recovery (2026-05-26): the post-Phase-E admin-side
    flow (deploy_bot → commit_baseline_local, running as ``evolve``)
    historically left ``evolve-backup/openclaw.json`` owned by
    ``evolve:staff`` mode 644. The next per-bot backup daemon run
    (running as the bot user) tried to ``shutil.copy2`` over it, which
    truncates the dst — needing write permission ON the file (which
    the bot doesn't have, mode 644 grants others read-only). Result:
    personal_bot's nightly backup stuck in EACCES forever.

    Defense: when the dst exists and write to it fails with EACCES,
    unlink the existing file first (only needs write on the parent
    dir, which the bot owns) and retry the copy. The new file is
    created fresh under the running uid — bot becomes the owner,
    future backups don't trip the same EACCES.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(str(src), str(dst))
        return True, ""
    except PermissionError as exc:
        # Distinguish read vs. write failure — historically operators
        # were misled by a generic "cannot read" message that obscured
        # a write-side problem (see diagnosis-accept-drift-regression).
        if not os.access(str(src), os.R_OK):
            print(f"[backup] {bot_id}: cannot read {src.name} ({exc})", file=sys.stderr)
            return False, "cannot read"
        # Write side failed but source was readable. Recovery path:
        # if the dst already exists and we can write its parent dir,
        # unlink + retry. This handles the "evolve-owned dst, bot
        # tries to overwrite" pattern from the 2026-05-26 personal_bot
        # incident — see function docstring.
        if dst.exists() and os.access(str(dst.parent), os.W_OK):
            try:
                dst.unlink()
                shutil.copy2(str(src), str(dst))
                print(
                    f"[backup] {bot_id}: recovered EACCES on {dst.name} via "
                    f"unlink+recreate (was owned by another user; "
                    f"now owned by {os.getuid()})",
                    file=sys.stderr,
                )
                return True, ""
            except OSError as retry_exc:
                print(
                    f"[backup] {bot_id}: EACCES recovery failed for {dst} "
                    f"(unlink+retry: {retry_exc})",
                    file=sys.stderr,
                )
        print(f"[backup] {bot_id}: read OK but cannot write {dst} ({exc})", file=sys.stderr)
        return False, "cannot write"
    except OSError as exc:
        print(f"[backup] {bot_id}: copy failed ({exc})", file=sys.stderr)
        return False, "cannot read"


# ── Accept-drift: local-only baseline commit ──────────────────────────────────

def _refresh_local_drift_baseline(bot_id: str, shared_dir: Path) -> None:
    """Best-effort local drift-baseline refresh for the cloud-skip paths.

    ``heal.detect_backup_drift_keys`` compares each bot's live
    openclaw.json against the committed ``evolve-backup/openclaw.json``
    baseline. On a cloud-enabled pod that baseline follows live on every
    successful backup commit, so config_drift is only an intra-interval
    tripwire. But the push-visibility guard above returns *before* the
    staging/commit step when there's no GitHub PAT or the repo isn't
    confirmed private — so on those pods the baseline freezes, and any
    drift (including operator-authorized deploy/apply churn) re-fires
    "Unexplained config drift" on every heal tick forever once the
    proposal/admin-UI explained-window (7d) expires.

    Refreshing the baseline locally here keeps cloud-backup-less pods
    consistent with cloud-enabled ones — same auto-follow-live semantics,
    no push. Local-only and idempotent (no-op when already at baseline).
    Never raises: a refresh failure must not change the cloud-status the
    caller is about to return.

    Footprint disk-output audit 2026-06-28 (config_drift source cut).
    """
    try:
        ok, msg = commit_baseline_local(bot_id, shared_dir)
        print(
            f"[backup] {bot_id}: local drift-baseline refresh "
            f"(cloud push unavailable): {msg}"
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[backup] {bot_id}: local drift-baseline refresh raised "
            f"(non-fatal): {exc}",
            file=sys.stderr,
        )


def commit_baseline_local(bot_id: str, shared_dir: Path) -> tuple[bool, str]:
    """Commit current openclaw.json as the new backup baseline — locally only.

    Used by the admin UI's "Accept as baseline" affordance: when an operator
    has reviewed a drift alert and chosen to accept the current state, this
    stages openclaw.json into ``evolve-backup/`` and commits with the
    ``[backup]`` prefix so :func:`heal.detect_backup_drift_keys` sees no
    diff on the next pass. No push — operators may accept drift on bots
    that don't have a remote configured (e.g. the evolve primary), so
    requiring a remote here would block the common case.

    Returns ``(ok, message)``.
    """
    bot_oc = _bot_home(bot_id) / ".openclaw"
    workspace = bot_oc / "workspace"
    if not workspace.exists():
        return False, f"workspace not found at {workspace}"

    r = _git(["rev-parse", "--git-dir"], workspace)
    if r.returncode != 0:
        return False, "workspace is not a git repo (run backup-init first)"

    _git(["config", "user.name", "Evolve Backup"], workspace)
    _git(["config", "user.email", "evolve@localhost"], workspace)

    backup_dir = workspace / "evolve-backup"
    backup_dir.mkdir(exist_ok=True)
    oc_src = bot_oc / "openclaw.json"
    oc_dst = backup_dir / "openclaw.json"
    ok, reason = _copy_with_sudo(oc_src, oc_dst, bot_id)
    if not ok:
        # Distinguish read failure from write failure so the API caller
        # (and evo) get an accurate path to investigate, instead of the
        # historical "could not read live openclaw.json" string that
        # misled evo into confabulating an ACL/admin-user
        # misconfiguration on 2026-05-21. See
        # docs/diagnosis-accept-drift-regression-2026-05-21.md.
        if reason == "cannot write":
            return False, "could not write evolve-backup/openclaw.json"
        return False, "could not read live openclaw.json"

    # Match backup_bot(): scrub secrets in the staged snapshot before commit.
    redact_changed, redact_reason = _redact_snapshot_file(oc_dst)
    if not redact_changed and redact_reason not in ("no-secrets",):
        return False, f"redact failed: {redact_reason}"

    r = _git(["status", "--porcelain", "--", "evolve-backup/openclaw.json"], workspace)
    if r.returncode != 0:
        return False, f"git status failed: {r.stderr.strip()[:200]}"
    if not r.stdout.strip():
        return True, "already at baseline — no commit needed"

    _git(["add", "evolve-backup/openclaw.json"], workspace)
    ts = now_iso()
    config_hash = sha256_file(oc_src)
    msg = (
        f"[backup] {bot_id} {ts} (accept-drift)\n"
        f"baseline reset via admin UI\n"
        f"config-hash: {config_hash[:16]}\n"
    )
    r = _git(["commit", "-m", msg, "--author=evolve <evolve@localhost>"], workspace)
    if r.returncode != 0 and "nothing to commit" not in r.stdout:
        return False, f"commit failed: {r.stderr.strip()[:200]}"
    return True, "baseline committed locally"


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve nightly git backup")
    parser.add_argument("--network", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Stage files but don't commit or push")
    parser.add_argument("--bot", default=None, help="Backup only this bot ID")
    parser.add_argument(
        "--commit-baseline-local",
        metavar="BOT_ID",
        default=None,
        help=(
            "Run commit_baseline_local for the given bot and exit. "
            "Used by deploy_bot to refresh the heal drift baseline AS the bot user "
            "(via sudo -H -u <bot_user>) so the resulting .git/ writes and "
            "evolve-backup/ dir stay bot-owned. Direct in-process invocation from "
            "deploy.py (which runs as root) corrupted ownership and broke the next "
            "nightly backup — see 2026-05-30 incident."
        ),
    )
    args = parser.parse_args()

    config = load_config(args.network)
    shared_dir = get_shared_dir(config)

    if args.commit_baseline_local:
        ok, msg = commit_baseline_local(args.commit_baseline_local, shared_dir)
        print(f"[backup] baseline-refresh {args.commit_baseline_local}: {msg}")
        sys.exit(0 if ok else 1)

    members = get_members(config)
    primary = config.get("primary")
    all_bots = ([primary] if primary and primary not in members else []) + members
    all_bots = [b for b in all_bots if b]  # remove empty

    if args.bot:
        all_bots = [args.bot]

    bots_cfg = config.get("bots", {})

    # Include any bot with a backupRepoUrl that isn't already in the list
    # (e.g. "evolve" — the admin user's own workspace).
    # Skipped when --bot is given so the flag stays a precise single-bot selector.
    if not args.bot and isinstance(bots_cfg, dict):
        for extra_id, extra_cfg in bots_cfg.items():
            if extra_id not in all_bots and extra_cfg.get("backupRepoUrl"):
                all_bots.append(extra_id)

    failures = []
    skipped = []
    for bot_id in all_bots:
        bot_cfg = bots_cfg.get(bot_id, {}) if isinstance(bots_cfg, dict) else {}
        backup_url = bot_cfg.get("backupRepoUrl", "")
        if not backup_url:
            print(f"[backup] {bot_id}: no backupRepoUrl configured — skipping")
            skipped.append(bot_id)
            continue

        status = backup_bot(
            bot_id=bot_id,
            shared_dir=shared_dir,
            backup_url=backup_url,
            dry_run=args.dry_run,
            network=config,
        )
        if status == "skipped":
            skipped.append(bot_id)
        elif status == "failed":
            failures.append(bot_id)

    if skipped:
        print(f"[backup] {len(skipped)} bot(s) skipped: {', '.join(skipped)}")
    if failures:
        print(f"[backup] WARN: {len(failures)} bot(s) failed: {', '.join(failures)}", file=sys.stderr)
        sys.exit(1)
    else:
        backed_up = len(all_bots) - len(skipped)
        print(f"[backup] {backed_up} bot(s) backed up successfully")


if __name__ == "__main__":
    main()
