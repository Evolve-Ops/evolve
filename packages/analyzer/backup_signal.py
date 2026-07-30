"""backup_signal — emit Signals when a bot's backup is unhealthy.

Closes two distinct silent-failure modes:

1. **Nightly backup failing.** Pre-2026-05: the launchd backup job had
   been exiting with status 1 every night for weeks (first HTTPS-URL
   credential prompts, then root-owned .git/objects, then a read-only
   deploy key), and the admin UI happily showed "✓ 65h ago" because it
   only read the git log for [backup] commits. backup.py now writes a
   per-bot run-state file at ``{workspace}/evolve-backup/state.json``
   capturing ``{last_attempt_at, last_success_at, last_attempt_status,
   last_error, consecutive_failures}``; this monitor reads them and
   fires a ``backup_failing`` Signal after N consecutive flakes.

2. **Backup repo visibility.** Phase 1 of the backup architecture
   rework (spec docs/spec-backup-and-data-classification-2026-05-28.md)
   adds a three-ring guard against GitHub defaulting new backup repos
   to public. This monitor is the periodic third ring — it verifies
   each bot's ``backupRepoUrl`` is private and fires Signals when:
     - the repo is confirmed public (severity alert; immediate exposure)
     - no GitHub PAT is configured (severity warn; can't verify)
     - PAT is set but the lookup failed (severity warn; transient or scope)
   Auto-resolves via sweep_resolve() when conditions clear.

3. **Backup daemon silence + workspace drift** (the 2026-07-28
   incident). A bot's Backup row sat "stale" for 10 days with zero
   alarms while (a) the per-bot backup daemon recorded no attempts at
   all — the old checks key off failure counts in ``state.json``, so a
   daemon that never runs (unloaded plist, wedged launchd job) fired
   nothing — and (b) a bot-authored rogue backup job committed nightly
   in the workspace and left a second git remote (with an embedded PAT)
   in ``.git/config``. Two new checks close this: a daemon-silence
   Signal anchored on ``state.json::last_attempt_at`` (falling back to
   the last ``[backup]`` commit when the state file was never written),
   and a workspace-drift Signal from ``backup_workspace_drift``
   (unexpected remotes / non-Evolve backup-ish commits). Unreadable
   state is NOT silence: an EACCES/corrupt ``state.json`` fires a
   distinct ``backup_state_unreadable`` Signal instead and leaves the
   bot's state-derived Signals untouched (fail-safe on unreadable, same
   pattern as the drift monitors' UNREADABLE sentinel).

Signal types under producer ``backup_signal``:

  backup_failing                — N+ consecutive nightly failures (warn/alert).
  backup_repo_public            — repo confirmed public via GitHub API (alert).
  backup_visibility_unverified  — visibility can't be determined (warn).
  backup_daemon_silent          — no attempt recorded for ~26h+ (warn/alert).
  backup_state_unreadable       — state.json unreadable; monitor blind (warn).
  workspace_backup_drift        — second backup system in the workspace
                                  (alert with unexpected remote, else warn).

Pure Python; visibility check makes one HTTPS request per bot per run.
Cheap; hourly launchd cadence aligned with the existing pod-state
monitors. Reads on-disk JSON + the GitHub API.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from backup_diagnostics import classify as classify_backup_error
from backup_workspace_drift import (
    build_signal_for_workspace_drift,
    collect_workspace_drift,
    last_backup_commit_iso,
)
from backup_visibility import (
    check_repo_visibility,
    load_pat as _load_github_pat,
    parse_github_repo as _parse_github_repo,
)
from evolve_config import (
    bot_home,
    get_bot_user,
    get_members,
    get_primary,
    get_shared_dir,
    load_config,
    user_home,
)
from platform_profile import get_profile
from schema.signal import make_signature
from signals import store as signals_store

PRODUCER = "backup_signal"

# Generic threshold — applies when the error stderr doesn't match any
# known cause pattern in backup_diagnostics. Picked at 3 so a single
# flake doesn't wake the operator, but a persistent failure can't hide
# for more than a few days. Above 6 escalates severity from warn to
# alert because at that point the backup has been broken for a week —
# restore would lose meaningful state.
FIRE_THRESHOLD = 3
ALERT_THRESHOLD = 7

# Daemon-silence threshold — one missed nightly window plus slack. The
# nightly cadence means a healthy daemon records an attempt at least
# every ~24h; 26h tolerates launchd jitter and a slow run without
# tolerating a genuinely missed night. Past a full week of silence the
# severity escalates to alert — at that point restore would lose
# meaningful state (mirrors ALERT_THRESHOLD above).
SILENCE_THRESHOLD_HOURS = 26
SILENCE_ALERT_HOURS = 7 * 24

# Sentinel: state.json exists (or should) but could NOT be read — EACCES
# even though the evolve read ACL should grant it, an OS error, or a
# corrupt file. Distinct from None (= genuinely no attempt recorded).
# CRITICAL: unreadable must NOT read as silence — a monitor that can't
# read must not fire "the daemon never ran" (fail-safe on unreadable,
# same pattern as the drift monitors' UNREADABLE sentinel).
UNREADABLE = object()

# Classified-cause threshold — fires on the FIRST failure when the
# stderr matches a known pattern (SSH key mismatch, deploy key
# unrecognized, DNS, repo not found, …). Closes the 2026-06-07 silent-
# 2-day failure mode: four of five affected bots sat at
# consecutive_failures=2 with the most specific possible error text in
# state.json, and no Signal fired because the generic threshold hadn't
# tripped yet. A specific cause + concrete fix step is actionable on
# day 1; waiting two more nights is pure latency.
CLASSIFIED_FIRE_THRESHOLD = 1


def _read_run_state(shared_dir: Path, bot_id: str) -> "dict | None | object":
    """Read the per-bot run state written by backup.py.

    State lives in each bot's own workspace (per-bot daemon writes it as
    the bot user; the evolve user has read ACL via set_evolve_read_acl).
    The ``shared_dir`` arg is retained for back-compat with the loader
    interface that ``collect_for_bot`` exposes via ``state_loader=…`` for
    test injection, but isn't actually consulted — the path is per-bot
    in their own workspace, not pod-wide.

    Return contract (three-valued, see ``UNREADABLE``):

    - dict       — state read cleanly.
    - None       — no state file (the daemon has never recorded an
                   attempt). This IS evidence for the silence check.
    - UNREADABLE — the file (or the config needed to find it) exists but
                   couldn't be read: EACCES despite the evolve read ACL,
                   another OS error, or corrupt JSON. NOT silence — the
                   monitor is blind, and says so via
                   ``backup_state_unreadable`` instead.
    """
    try:
        from evolve_config import bot_home, load_config  # noqa: WPS433
        workspace = bot_home(bot_id, load_config()) / ".openclaw" / "workspace"
    except Exception:  # noqa: BLE001
        return UNREADABLE
    path = workspace / "evolve-backup" / "state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (PermissionError, OSError, json.JSONDecodeError):
        return UNREADABLE


def _bot_backup_url(config: dict[str, Any], bot_id: str) -> str:
    """Per-bot backupRepoUrl from network.json, or empty string."""
    bots = config.get("bots") if isinstance(config.get("bots"), dict) else {}
    cfg = bots.get(bot_id) or {}
    return (cfg.get("backupRepoUrl") or "").strip() if isinstance(cfg, dict) else ""


def build_signal_for_failing_backup(
    bot_id: str,
    run_state: dict,
    backup_url: str,
) -> dict | None:
    """Render a Signal spec for a bot whose backup is currently failing.

    Returns None when the bot's run state doesn't warrant a Signal —
    not configured, recovered, or below threshold.

    Two threshold regimes (see ``FIRE_THRESHOLD`` /
    ``CLASSIFIED_FIRE_THRESHOLD``):

    - ``last_error`` matches a known pattern in ``backup_diagnostics`` —
      the cause is actionable on day 1, so fire on the first failure.
      The classified cause's title + fix steps are written into the
      Signal body so the operator gets the specific remediation, not
      generic "open the logs" hand-waving.

    - No pattern matches — fall back to the generic 3-failure threshold
      with the existing "open Backup → Cloud, see the tooltip" hint.

    Signature is stable across both regimes so a Signal that fires
    early under the classified path doesn't duplicate when consecutive
    failures later cross the generic threshold; observe() updates the
    body/title in place.
    """
    consec = int(run_state.get("consecutive_failures") or 0)
    last_error = (run_state.get("last_error") or "").strip()
    classified = classify_backup_error(last_error)

    threshold = CLASSIFIED_FIRE_THRESHOLD if classified else FIRE_THRESHOLD
    if consec < threshold:
        return None

    last_attempt = run_state.get("last_attempt_at") or ""
    last_success = run_state.get("last_success_at") or ""

    severity = "alert" if consec >= ALERT_THRESHOLD else "warn"

    # Title formula: include the classified cause when known so the
    # Alerts list reads "<bot> backup failing — SSH key mismatch"
    # instead of a bare "<bot> backup failing (1× in a row)". The
    # latter is uninformative at threshold=1; the former is a debug
    # headline.
    if classified:
        title = f"{bot_id} backup failing — {classified['title']}"
    else:
        title = f"{bot_id} backup failing ({consec}× in a row)"

    body_lines = [
        f"`{bot_id}` has missed {consec} consecutive nightly backup attempt"
        f"{'s' if consec != 1 else ''}.",
        "",
        f"Last attempt: `{last_attempt or '(unknown)'}`",
        f"Last success: `{last_success or '(none recorded)'}`",
        f"Backup repo: `{backup_url or '(no backupRepoUrl)'}`",
    ]
    if last_error:
        body_lines += [
            "",
            "Last error:",
            "```",
            last_error[:600],
            "```",
        ]
    if classified:
        body_lines += [
            "",
            f"**Likely cause:** {classified['title']}",
            "",
            "**Fix:**",
            *[f"{i + 1}. {step}" for i, step in enumerate(classified["fix_steps"])],
        ]
    else:
        body_lines += [
            "",
            "Open Backup → Cloud, click 'Backup now' on this bot's card "
            "to retry. The expanded row on this bot will show the full "
            "error and any known fix steps.",
        ]
    body = "\n".join(body_lines)

    # what_it_means / fix_steps in details mirror the body's classified-
    # vs-generic split. The UI uses the classified details to render the
    # diagnostic panel inline; the generic copy is the fallback.
    if classified:
        what_it_means = (
            f"Bot {bot_id!r} has missed {consec} consecutive nightly "
            f"backup attempt{'s' if consec != 1 else ''}. The error "
            f"matched a known pattern: {classified['title'].lower()}. "
            "Until fixed, the workspace (transcripts, memory, openclaw "
            "config) is not being preserved off-host."
        )
        fix_steps_str = "\n".join(
            f"{i + 1}. {step}" for i, step in enumerate(classified["fix_steps"])
        )
    else:
        what_it_means = (
            f"Bot {bot_id!r} has missed {consec} consecutive nightly "
            "backup attempts. The bot is still running, but its "
            "workspace (transcripts, memory, openclaw config) is "
            "no longer being preserved off-host. If something "
            "destroys the bot's home now — disk failure, accidental "
            "rm -rf, deploy mishap — there's no recoverable state."
        )
        fix_steps_str = (
            "1. Open Backup → Cloud in the admin UI\n"
            "2. Click on this bot's row in Backup → Status to expand "
            "the diagnostic panel and see the full error\n"
            "3. Common causes:\n"
            "   - SSH deploy key revoked on the GitHub backup repo\n"
            "   - Backup remote unreachable (network/DNS)\n"
            "   - Workspace permissions wrong after a bot redeploy\n"
            "4. If the retry fails, check the bot's backup logs at "
            f"{bot_home(bot_id)}/.openclaw/logs/backup.log on the pod host"
        )

    return dict(
        signature=make_signature(PRODUCER, "backup_failing", bot_id),
        producer=PRODUCER,
        type="backup_failing",
        flavor="maintenance",
        severity=severity,
        scope="bot",
        bot_id=bot_id,
        title=title,
        body=body,
        details=dict(
            consecutive_failures=consec,
            last_attempt_at=last_attempt or None,
            last_success_at=last_success or None,
            last_error=last_error[:400] or None,
            backup_url=backup_url or None,
            # Surface the classified cause so the admin UI / Alerts page
            # can render structured remediation. None when the pattern
            # table didn't match.
            classified_cause=classified,
            what_it_means=what_it_means,
            fix_steps=fix_steps_str,
        ),
    )


def _parse_iso_utc(raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; None when unparseable. Naive → UTC."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        ts = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _workspace_for(bot_id: str, config: dict[str, Any]) -> Path | None:
    try:
        from evolve_config import bot_home  # noqa: WPS433
        return bot_home(bot_id, config) / ".openclaw" / "workspace"
    except Exception:  # noqa: BLE001
        return None


def _default_backup_commit_prober(bot_id: str, config: dict[str, Any]) -> str | None:
    """ISO time of the workspace's last ``[backup]`` commit, or None."""
    workspace = _workspace_for(bot_id, config)
    if workspace is None:
        return None
    return last_backup_commit_iso(workspace)


def _default_drift_collector(bot_id: str, config: dict[str, Any]) -> dict | None:
    """Run the workspace-drift git checks against the bot's real workspace."""
    workspace = _workspace_for(bot_id, config)
    if workspace is None:
        return None
    return collect_workspace_drift(bot_id, workspace)


def build_signal_for_daemon_silence(
    bot_id: str,
    run_state: dict | None,
    backup_url: str,
    *,
    now: datetime | None = None,
    last_backup_commit_at: str | None = None,
) -> dict | None:
    """Signal spec: the backup daemon recorded no attempt for 26h+.

    Closes the 2026-07-28 incident's second blind spot: ``backup_failing``
    keys off failure counts in ``state.json``, so a daemon that never
    runs at all (unloaded plist, wedged launchd job) fired nothing while
    the Backup row went 10 days stale.

    Anchor logic (seed-once philosophy — never fire without evidence the
    backup system ever ran, so a freshly-configured bot whose first
    nightly hasn't happened yet stays quiet):

    - ``state.json::last_attempt_at`` parses → that's the anchor.
    - state file missing (``run_state is None``) or the field is
      missing/unparseable → fall back to ``last_backup_commit_at`` (the
      workspace's most recent ``[backup]`` commit, proof the daemon used
      to run).
    - No anchor at all → return None (fail-safe: could be a fresh bot).

    Fires when the anchor is older than ``SILENCE_THRESHOLD_HOURS``;
    escalates to alert past ``SILENCE_ALERT_HOURS``.
    """
    now = now or datetime.now(timezone.utc)
    state_missing = run_state is None
    attempt_raw = "" if state_missing else (run_state.get("last_attempt_at") or "")
    anchor = _parse_iso_utc(attempt_raw)
    anchor_source = "last_attempt_at"
    if anchor is None:
        anchor = _parse_iso_utc(last_backup_commit_at or "")
        anchor_source = "last_backup_commit"
    if anchor is None:
        return None

    age_hours = (now - anchor).total_seconds() / 3600.0
    if age_hours < SILENCE_THRESHOLD_HOURS:
        return None

    severity = "alert" if age_hours >= SILENCE_ALERT_HOURS else "warn"
    age_days = age_hours / 24.0
    age_str = (
        f"{age_days:.1f} days" if age_hours >= 48 else f"{age_hours:.0f}h"
    )

    if state_missing:
        evidence = (
            "No run-state file exists at all "
            "(`workspace/evolve-backup/state.json`) — the daemon has "
            "never recorded an attempt — but the workspace's last "
            f"`[backup]` commit is {age_str} old, so backups used to run."
        )
    elif anchor_source == "last_attempt_at":
        evidence = (
            f"`state.json::last_attempt_at` is {age_str} old. A healthy "
            "nightly daemon records an attempt — success OR failure — "
            "every ~24h."
        )
    else:
        evidence = (
            "`state.json` exists but carries no usable "
            "`last_attempt_at`; the workspace's last `[backup]` commit "
            f"is {age_str} old."
        )

    title = f"{bot_id} backup daemon silent — no attempt recorded in {age_str}"
    body = "\n".join([
        f"`{bot_id}`'s backup daemon has recorded **no attempt at all** "
        f"for {age_str} — this is not a failing backup, it's a daemon "
        "that isn't running.",
        "",
        evidence,
        "",
        f"Backup repo: `{backup_url or '(no backupRepoUrl)'}`",
        "",
        "**Fix**: check the per-bot backup daemon:",
        "",
        f"1. `sudo /bin/launchctl print system/ai.evolve.{bot_id}.backup` "
        "— is the job loaded, and when did it last run?",
        f"2. If missing or wedged, redeploy: `sudo evolve-admin deploy "
        f"{bot_id}` reinstalls the daemon idempotently.",
        "3. Then trigger 'Backup now' from Backup → Cloud; this Signal "
        "auto-resolves once a fresh attempt is recorded.",
    ])
    return dict(
        signature=make_signature(PRODUCER, "backup_daemon_silent", bot_id),
        producer=PRODUCER,
        type="backup_daemon_silent",
        flavor="maintenance",
        severity=severity,
        scope="bot",
        bot_id=bot_id,
        title=title,
        body=body,
        details=dict(
            backup_url=backup_url or None,
            state_file_missing=state_missing,
            anchor_source=anchor_source,
            anchor_at=anchor.isoformat(),
            silent_hours=round(age_hours, 1),
            what_it_means=(
                f"Bot {bot_id!r}'s backup daemon is not recording attempts "
                "— unlike a failing backup (which records its failures), "
                "a silent daemon usually means the launchd job is "
                "unloaded or wedged. Until it runs again, no new "
                "workspace state is being preserved off-host, and the "
                "failure-count monitors are blind."
            ),
            fix_steps=(
                f"1. On the host, run `sudo /bin/launchctl print "
                f"system/ai.evolve.{bot_id}.backup` to check the job is "
                "loaded and see its last exit status\n"
                f"2. If the job is missing or wedged, run `sudo "
                f"evolve-admin deploy {bot_id}` to reinstall it\n"
                "3. Trigger an immediate run via Backup → Cloud → "
                "'Backup now' on this bot's card\n"
                "4. The Signal auto-resolves once a fresh attempt is "
                "recorded in state.json"
            ),
        ),
    )


def build_signal_for_state_unreadable(bot_id: str, backup_url: str) -> dict:
    """Signal spec: ``state.json`` can't be read — the monitor is blind.

    An unreadable run-state file must NOT be reported as daemon silence
    (the daemon may be recording attempts we simply can't see). Instead
    this distinct Signal marks the blind spot, and ``run()`` keeps the
    bot's existing state-derived Signals (``backup_failing`` /
    ``backup_daemon_silent``) alive rather than sweep-resolving them —
    we can't confirm they cleared.
    """
    return dict(
        signature=make_signature(PRODUCER, "backup_state_unreadable", bot_id),
        producer=PRODUCER,
        type="backup_state_unreadable",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id}: backup run-state unreadable — monitor blind",
        body="\n".join([
            f"Could not read `{bot_id}`'s backup run-state "
            "(`workspace/evolve-backup/state.json`): EACCES despite the "
            "evolve read ACL, an OS error, or corrupt JSON.",
            "",
            "A monitor that can't read must not look clean — and must "
            "not cry silence either: the daemon may be recording "
            "attempts this monitor simply can't see. This Signal marks "
            "the blind spot; the bot's existing backup Signals are left "
            "in place until the state reads cleanly again.",
            "",
            "**Fix**: `sudo evolve-admin ensure-pod-perms` reasserts the "
            "evolve read ACL on the bot's workspace; if the file is "
            "corrupt, the next daemon run rewrites it. The Signal "
            "auto-resolves once the state reads cleanly.",
        ]),
        details=dict(
            backup_url=backup_url or None,
            what_it_means=(
                f"The backup monitor could not read {bot_id!r}'s "
                "run-state file, so both the failure-count and the "
                "daemon-silence checks are blind for this bot until the "
                "read is restored — typically a read-ACL clamp on the "
                "bot's workspace."
            ),
            fix_steps=(
                "1. Run `sudo evolve-admin ensure-pod-perms` to reassert "
                "the evolve read ACL on the bot's workspace\n"
                "2. If state.json is corrupt rather than unreadable, the "
                "next daemon run rewrites it atomically\n"
                "3. The Signal auto-resolves on the next monitor tick "
                "once the state reads cleanly"
            ),
        ),
    )


def _ssh_key_present(bot_user: str, bot_id: str) -> bool | None:
    """Return True/False if ``<bot home>/.ssh/evolve-backup-{bot_id}``
    is on disk. None when the check itself couldn't run (so we don't fire
    a false positive on a sudo/visibility hiccup).

    The daemon runs as ``evolve``, which has ACL read on .openclaw/ but
    NOT on .ssh/. The sudoers grant we rely on is:

        evolve ALL=(root) NOPASSWD: {ls} {user_home_root}/*/.ssh

    granted in /etc/sudoers.d/evolve, rendered per-platform by
    ``_render_evolve_sudoers`` — so both the ``ls`` binary and the home
    path here must come from the same profile helpers or the argv won't
    match the grant. We run ``sudo -n <ls>`` on the directory and look
    for the filename in stdout. A missing parent .ssh/ counts as missing
    (no key can be present without the dir).

    ``-n`` short-circuits if for some reason the grant isn't in place
    on this pod (returns None, not False — distinguishes "I can't tell"
    from "definitely missing").
    """
    name = f"evolve-backup-{bot_id}"
    try:
        r = subprocess.run(
            ["sudo", "-n", get_profile().ls, str(user_home(bot_user) / ".ssh")],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode == 0:
        listing = (r.stdout or "").split()
        return name in listing
    stderr = (r.stderr or "").lower()
    # "no such file or directory" → parent missing → key definitively absent.
    if "no such file" in stderr or "cannot access" in stderr:
        return False
    # Anything else (sudo: a password is required, permission denied, etc.)
    # we treat as "can't tell".
    return None


def build_signal_for_missing_ssh_key(
    bot_id: str,
    bot_user: str,
    repo_url: str,
) -> dict:
    """Signal spec: the per-bot SSH deploy key is missing from the bot's home.

    Fires when ``bots.<bot_id>.backupRepoUrl`` is configured but
    ``/Users/<bot_user>/.ssh/evolve-backup-<bot_id>`` doesn't exist.
    The per-bot backup daemon runs AS ``bot_user``, so this is the only
    path it can read; without the file, ``backup._ssh_env`` returns ``{}``
    and the git push falls back to bare ssh with StrictHostKeyChecking=ask,
    which dies non-interactively as "Host key verification failed".

    This is a proactive companion to ``backup_failing`` — it catches the
    misconfig immediately rather than waiting 3 nights for the failure
    counter to cross threshold.

    Fix: redeploying the bot triggers
    ``deploy._ensure_backup_ssh_key`` which generates the staged key
    (if missing) and copies it to the bot user's ``~/.ssh/``.
    """
    key_path = str(user_home(bot_user) / ".ssh" / f"evolve-backup-{bot_id}")
    title = f"{bot_id} backup SSH key is missing from {bot_user}'s home"
    body = "\n".join([
        f"`{bot_id}` has a backup repo configured (`{repo_url}`) but the "
        f"SSH deploy key the nightly backup daemon needs is missing:",
        "",
        f"  `{key_path}` (expected, not found)",
        "",
        "The per-bot backup daemon runs as the bot user and resolves the "
        "key via `~/.ssh/evolve-backup-<bot>`. Without the file the git "
        "push falls back to plain ssh and dies as `Host key verification "
        "failed`.",
        "",
        "**Fix**: re-deploy this bot. `sudo evolve-admin deploy "
        f"{bot_id}` regenerates the staged key on the `evolve` user if "
        "needed and distributes it into the bot user's home in one "
        "idempotent step.",
    ])
    fix_steps = (
        f"1. SSH to the pod host and run: `sudo evolve-admin deploy {bot_id}`\n"
        f"2. Verify the key landed: "
        f"`sudo {get_profile().ls} {key_path}`\n"
        "3. Trigger an immediate backup from Backup → Cloud → "
        "'Backup now' on this bot's card; it should report ok within ~5s\n"
        "4. If still failing, check whether the pubkey at "
        f"`{user_home('evolve')}/.ssh/evolve-backup-{bot_id}.pub` is registered "
        "as a deploy key on the GitHub backup repo (Settings → Deploy keys)"
    )
    return dict(
        signature=make_signature(PRODUCER, "backup_ssh_key_missing", bot_id),
        producer=PRODUCER,
        type="backup_ssh_key_missing",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=title,
        body=body,
        details=dict(
            backup_url=repo_url,
            expected_key_path=key_path,
            bot_user=bot_user,
            what_it_means=(
                "Bot has a backup repo configured but its SSH deploy key "
                "is missing from the bot user's home. The nightly backup "
                "daemon (running as the bot user) cannot authenticate "
                "and will fail every night until a redeploy distributes "
                "the key."
            ),
            fix_steps=fix_steps,
        ),
    )


def _settings_url_for(repo_url: str) -> str:
    parsed = _parse_github_repo(repo_url)
    if not parsed:
        return "the GitHub repo settings page"
    return f"https://github.com/{parsed[0]}/{parsed[1]}/settings"


def build_signal_for_public_repo(bot_id: str, repo_url: str) -> dict:
    """Signal spec: backup repo is publicly visible — immediate exposure risk."""
    settings = _settings_url_for(repo_url)
    body = "\n".join([
        f"`{bot_id}`'s backup repo is **public**. The entire cloud-eligible "
        f"workspace is exposed to anyone with the URL.",
        "",
        f"Backup repo: `{repo_url}`",
        "",
        f"**Fix**: open {settings} and set Visibility → Private. Backups "
        f"will resume on the next run.",
        "",
        "Pushes are currently blocked by the safety guard so no further "
        "data is being added to the public repo.",
    ])
    return dict(
        signature=make_signature(PRODUCER, "backup_repo_public", bot_id),
        producer=PRODUCER,
        type="backup_repo_public",
        flavor="safety",
        severity="alert",
        scope="bot",
        bot_id=bot_id,
        title=f"{bot_id} backup repo is public",
        body=body,
        details=dict(
            backup_url=repo_url,
            settings_url=settings,
            what_it_means=(
                "GitHub defaults new repos to public. A public backup repo "
                "exposes every cloud-classified file in the bot's workspace "
                "to the open internet."
            ),
            fix_steps=(
                f"1. Open {settings}\n"
                "2. Scroll to 'Danger Zone' → 'Change repository visibility'\n"
                "3. Choose 'Make private' and confirm with the repo name\n"
                "4. Backups will resume on the next scheduled run; or click "
                "'Backup now' on the bot's card in Backup → Cloud"
            ),
        ),
    )


def build_signal_for_unverified_repo(
    bot_id: str,
    repo_url: str,
    *,
    reason: str,
) -> dict:
    """Signal spec: visibility couldn't be determined.

    ``reason``: ``"missing_pat"`` (no token configured) or
    ``"lookup_failed"`` (token present but API call returned unknown).
    Same Signal type; body differs.
    """
    if reason == "missing_pat":
        title = f"{bot_id} backup repo visibility cannot be verified (no PAT)"
        body = "\n".join([
            f"`{bot_id}` has a backup repo configured (`{repo_url}`) but "
            "Evolve cannot verify whether it's private.",
            "",
            "**Why**: no GitHub Personal Access Token is configured (keystore "
            "key `github_pat`). Without a PAT, the visibility "
            "check cannot reach the GitHub API.",
            "",
            "**Fix**: create a PAT with `repo:read` scope and store it via the "
            "Backup → Cloud wizard. Pushes are blocked until visibility can be "
            "verified, so existing backups will not resume until this is "
            "configured.",
        ])
        fix_steps = (
            "1. Create a fine-grained GitHub PAT with read access to your "
            "backup repos (Settings → Developer settings → Personal access "
            "tokens → Fine-grained tokens → Generate new token)\n"
            "2. Store the token via **Maintenance → Backup → Cloud** (it lands "
            "in the keystore as `github_pat`)\n"
            "3. The next monitor run (within 1h) will pick it up and resolve "
            "this Signal; backups will resume on their next scheduled run"
        )
    else:  # "lookup_failed"
        title = f"{bot_id} backup repo visibility cannot be verified (lookup failed)"
        body = "\n".join([
            f"`{bot_id}`'s backup repo (`{repo_url}`) could not be checked "
            "against the GitHub API. A PAT is configured but the lookup "
            "returned an unknown result.",
            "",
            "**Likely causes**:",
            "- PAT scope doesn't include this repo (most common)",
            "- Network or DNS issue reaching api.github.com",
            "- Repo was renamed/deleted/transferred",
            "",
            "Pushes are blocked until verification succeeds.",
        ])
        fix_steps = (
            "1. Verify the PAT has read access to this specific repo (fine-"
            "grained tokens are scoped per-repo)\n"
            "2. Verify the repo URL in network.json::bots.<bot>.backupRepoUrl "
            "still resolves to a valid repo\n"
            "3. Test from the mini: `curl -H \"Authorization: Bearer "
            "$PAT\" https://api.github.com/repos/<owner>/<name>` should "
            "return 200"
        )
    return dict(
        signature=make_signature(PRODUCER, "backup_visibility_unverified", bot_id),
        producer=PRODUCER,
        type="backup_visibility_unverified",
        flavor="maintenance",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=title,
        body=body,
        details=dict(
            backup_url=repo_url,
            reason=reason,
            what_it_means=(
                "Evolve refuses to push to a backup repo it can't verify is "
                "private. Until the visibility check succeeds, backups are "
                "blocked — preventing accidental exposure if the repo turns "
                "out to be public."
            ),
            fix_steps=fix_steps,
        ),
    )


def _build_visibility_signal(
    bot_id: str,
    repo_url: str,
    config: dict[str, Any],
    *,
    pat_loader=_load_github_pat,
    visibility_checker=check_repo_visibility,
) -> dict | None:
    """Decide which visibility Signal (if any) to emit for this bot."""
    pat = pat_loader(config)
    if not pat:
        return build_signal_for_unverified_repo(
            bot_id, repo_url, reason="missing_pat",
        )
    vis = visibility_checker(repo_url, pat=pat)
    if vis == "private":
        return None
    if vis == "public":
        return build_signal_for_public_repo(bot_id, repo_url)
    return build_signal_for_unverified_repo(
        bot_id, repo_url, reason="lookup_failed",
    )


def build_signal_for_pod_missing_pat(bot_urls: dict[str, str]) -> dict:
    """One pod-scope signal covering every bot whose backup visibility is
    blocked by a missing GitHub PAT.

    Producer-level coalescing: missing-PAT is a single config defect
    (network.json::github.pat is empty), so a per-bot fan-out (one row
    per bot with a backup repo) is pure noise — one PAT fixes all of
    them. The 2026-06-03 Alerts review caught 8 bots fanned out on this
    one cause. See `run()` for the coalesce flow.

    Signature is producer-stable (``backup_pat_missing:all``) so the
    same Signal stays open across runs and observation_count grows.
    """
    n = len(bot_urls)
    bot_ids = sorted(bot_urls.keys())
    plural = "" if n == 1 else "s"
    title = (
        f"GitHub PAT missing — {n} bot backup repo{plural} cannot be verified"
    )
    body = "\n".join([
        f"{n} bot{plural} have a backup repo configured but Evolve cannot "
        f"verify whether {'it is' if n == 1 else 'they are'} private:",
        "",
        *[f"- `{b}` → `{bot_urls[b]}`" for b in bot_ids],
        "",
        "**Why**: no GitHub Personal Access Token is configured (keystore "
        "key `github_pat`). Without a PAT, the visibility check "
        f"can't reach the GitHub API. One token covers all {n} bot{plural}.",
        "",
        "**Fix**: re-run the GitHub backup setup wizard from "
        "**Maintenance → Backup → Cloud** (the wizard now persists the "
        "verified PAT to the keystore in addition to setting "
        "up each bot's git remote — older wizard runs only did the latter). "
        "Pushes are blocked until visibility can be verified, so existing "
        "backups will not resume until this is configured.",
    ])
    fix_steps = (
        "1. Open **Maintenance → Backup → Cloud** and click **Rotate / "
        "re-run GitHub setup**, OR create a fresh classic PAT with `repo` "
        "scope at https://github.com/settings/tokens/new?scopes=repo "
        "(a fine-grained PAT with `Contents: read+write` + "
        "`Metadata: read` works too)\n"
        "2. Paste the PAT in the wizard and run it — this writes the token "
        "to the keystore (key `github_pat`) and re-verifies every bot's backup "
        "repo in one step\n"
        "3. The next monitor run (within 1h) picks it up and resolves "
        f"this Signal for all {n} bot{plural}; backups resume on their "
        "next scheduled run"
    )
    return dict(
        signature=make_signature(PRODUCER, "backup_pat_missing", "all"),
        producer=PRODUCER,
        type="backup_pat_missing",
        flavor="maintenance",
        severity="warn",
        scope="pod",
        bot_id=None,
        title=title,
        body=body,
        details=dict(
            bot_ids=bot_ids,
            bot_count=n,
            backup_urls=dict(bot_urls),
            what_it_means=(
                "Evolve refuses to push to a backup repo it can't verify "
                "is private. One missing token blocks verification on "
                f"all {n} bot{plural} listed above."
            ),
            fix_steps=fix_steps,
        ),
    )


def collect_for_bot(
    shared_dir: Path,
    bot_id: str,
    config: dict[str, Any],
    *,
    state_loader=_read_run_state,
    pat_loader=_load_github_pat,
    visibility_checker=check_repo_visibility,
    ssh_key_checker=_ssh_key_present,
    backup_commit_prober=None,
    drift_collector=None,
    now: datetime | None = None,
) -> list[dict]:
    """Return the Signal specs (possibly several) for this bot.

    Up to one ``backup_failing`` (from run-state), up to one
    ``backup_daemon_silent`` (no attempt recorded for 26h+; skipped
    when the state is UNREADABLE — a distinct
    ``backup_state_unreadable`` fires instead), up to one
    ``backup_repo_public`` / ``backup_visibility_unverified`` (from
    the visibility check), up to one ``backup_ssh_key_missing``
    (when the bot's per-bot SSH deploy key isn't on disk in the bot
    user's home — proactive companion to backup_failing), and up to
    one ``workspace_backup_drift`` (a second backup system operating
    in the workspace — see backup_workspace_drift).
    """
    backup_url = _bot_backup_url(config, bot_id)
    if not backup_url:
        # Bot opted out of backup; nothing to monitor.
        return []

    # Late-bound defaults (module attributes, not def-time bindings) so
    # tests can hermetically neutralize the real-git probes in one place.
    if backup_commit_prober is None:
        backup_commit_prober = _default_backup_commit_prober
    if drift_collector is None:
        drift_collector = _default_drift_collector

    specs: list[dict] = []

    rs = state_loader(shared_dir, bot_id)
    if rs is UNREADABLE:
        # Blind: neither "failing" nor "silent" can be asserted. Fire the
        # distinct unreadable Signal; run() protects this bot's existing
        # state-derived Signals from the sweep.
        specs.append(build_signal_for_state_unreadable(bot_id, backup_url))
    else:
        if isinstance(rs, dict):
            spec = build_signal_for_failing_backup(bot_id, rs, backup_url)
            if spec:
                specs.append(spec)
        # Daemon-silence check. The [backup]-commit prober is only
        # consulted when state.json can't anchor the check (missing file
        # or missing/unparseable last_attempt_at) — see the builder's
        # seed-once anchor logic.
        rs_dict = rs if isinstance(rs, dict) else None
        needs_commit_anchor = (
            rs_dict is None
            or _parse_iso_utc(rs_dict.get("last_attempt_at") or "") is None
        )
        commit_iso = (
            backup_commit_prober(bot_id, config) if needs_commit_anchor else None
        )
        silence_spec = build_signal_for_daemon_silence(
            bot_id, rs_dict, backup_url,
            now=now, last_backup_commit_at=commit_iso,
        )
        if silence_spec:
            specs.append(silence_spec)

    drift_findings = drift_collector(bot_id, config)
    if drift_findings:
        specs.append(
            build_signal_for_workspace_drift(bot_id, backup_url, drift_findings),
        )

    vis_spec = _build_visibility_signal(
        bot_id, backup_url, config,
        pat_loader=pat_loader,
        visibility_checker=visibility_checker,
    )
    if vis_spec:
        specs.append(vis_spec)

    # Proactive SSH-key presence check. Only fires when we definitively
    # observe the key is absent (False, not None) — a sudoers gap or
    # other "I can't tell" case must not synthesise a false alert.
    try:
        bot_user = get_bot_user(bot_id, config)
    except Exception:  # noqa: BLE001
        bot_user = bot_id
    if ssh_key_checker(bot_user, bot_id) is False:
        specs.append(build_signal_for_missing_ssh_key(bot_id, bot_user, backup_url))

    return specs


def run(
    shared_dir: Path,
    config: dict[str, Any],
    *,
    bots: Iterable[str] | None = None,
    dry_run: bool = False,
    state_loader=_read_run_state,
    pat_loader=_load_github_pat,
    visibility_checker=check_repo_visibility,
    ssh_key_checker=_ssh_key_present,
    backup_commit_prober=None,
    drift_collector=None,
    now: datetime | None = None,
) -> tuple[set[str], int, int]:
    """Walk all configured bots; fire/resolve backup-related Signals.

    Returns ``(kept_signatures, n_fired, n_resolved)``.

    When ``bots`` is explicitly narrowed (e.g. ``--bot team_bot_a``), the
    sweep_resolve at the end is scoped to that bot set via ``bot_ids=``
    so other bots' still-firing Signals aren't accidentally archived.
    See the 2026-05-29 review-session fix.

    Unreadable-state protection: a bot whose ``state.json`` is
    UNREADABLE gets its potential ``backup_failing`` /
    ``backup_daemon_silent`` signatures added to ``kept`` so the
    end-of-run sweep can't archive Signals whose condition we couldn't
    re-check this run (fail-safe on unreadable). Adding signatures that
    don't correspond to an active Signal is harmless — sweep_resolve
    only resolves active Signals *not* in the keep set.
    """
    bots_explicit = bots is not None
    if bots is None:
        primary = get_primary(config)
        members = get_members(config)
        bots = ([primary] if primary and primary not in members else []) + members
        bots = [b for b in bots if b]

    kept: set[str] = set()
    n_fired = 0
    # Bots with missing_pat are collected here and coalesced into a
    # single pod-scope signal at the end (see build_signal_for_pod_missing_pat).
    # Per-bot fan-out for "no PAT in network.json" is pure noise — one
    # PAT fixes all of them.
    missing_pat_bots: dict[str, str] = {}
    for bot_id in bots:
        for d in collect_for_bot(
            shared_dir, bot_id, config,
            state_loader=state_loader,
            pat_loader=pat_loader,
            visibility_checker=visibility_checker,
            ssh_key_checker=ssh_key_checker,
            backup_commit_prober=backup_commit_prober,
            drift_collector=drift_collector,
            now=now,
        ):
            details = d.get("details") or {}
            if (
                d.get("type") == "backup_visibility_unverified"
                and details.get("reason") == "missing_pat"
            ):
                missing_pat_bots[bot_id] = details.get("backup_url") or ""
                continue
            if d.get("type") == "backup_state_unreadable":
                # Blind tick for this bot's state-derived Signals: keep
                # them alive rather than letting the sweep archive
                # conditions we couldn't re-check (fail-safe on
                # unreadable). Harmless when no such Signal is active.
                kept.add(make_signature(PRODUCER, "backup_failing", bot_id))
                kept.add(make_signature(PRODUCER, "backup_daemon_silent", bot_id))
            kept.add(d["signature"])
            n_fired += 1
            if dry_run:
                print(json.dumps({"would_observe": d}, default=str), flush=True)
                continue
            try:
                signals_store.observe(shared_dir, **d)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[backup_signal] observe failed for {d['signature']}: {exc}",
                    flush=True,
                )

    # Coalesced pod-scope signal for missing PAT. Emitted after the
    # per-bot loop so we have the full bot list to enumerate.
    if missing_pat_bots:
        pod_spec = build_signal_for_pod_missing_pat(missing_pat_bots)
        kept.add(pod_spec["signature"])
        n_fired += 1
        if dry_run:
            print(json.dumps({"would_observe": pod_spec}, default=str), flush=True)
        else:
            try:
                signals_store.observe(shared_dir, **pod_spec)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[backup_signal] observe failed for "
                    f"{pod_spec['signature']}: {exc}",
                    flush=True,
                )

    n_resolved = 0
    if not dry_run:
        try:
            resolved = signals_store.sweep_resolve(
                shared_dir,
                producer=PRODUCER,
                kept_signatures=kept,
                reason="auto-resolve: backup recovered (consecutive_failures dropped below threshold)",
                bot_ids=(set(bots) if bots_explicit else None),
            )
            n_resolved = len(resolved)
        except Exception as exc:  # noqa: BLE001
            print(f"[backup_signal] sweep_resolve failed: {exc}", flush=True)

    return kept, n_fired, n_resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description="backup_signal — emit Signals when a bot's nightly backup is failing",
    )
    parser.add_argument("--network", default=None)
    parser.add_argument("--bot", default=None, help="Run only for this bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
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
        print(f"[backup_signal] dry-run: {n_fired} would-fire", flush=True)
        return
    print(
        f"[backup_signal] {n_fired} firings, {n_resolved} resolved",
        flush=True,
    )


if __name__ == "__main__":
    main()
