"""backup_workspace_drift — detect a second backup system in a bot's workspace.

Closes the 2026-07-28 incident's first blind spot: a bot-authored rogue
backup job (an in-gateway cron the bot created for itself months earlier)
resurrected and began committing nightly in the bot's workspace with
varying LLM-generated messages ("Automated cron backup",
"workspace-backup: nightly sync", ...) seconds after the legitimate
Evolve ``[backup]`` commit. It also left a second git remote in the
workspace ``.git/config`` — an HTTPS URL with an embedded PAT — alongside
the expected ``origin``. The Backup page sat "stale" with zero alarms
because nothing looked at the workspace repo for evidence of a
*competing* backup system.

Two detections, both cheap git reads against the workspace repo:

1. **Unexpected remotes.** ``backup.py::_ensure_remote`` manages exactly
   one remote (``origin``). Any additional remote means something other
   than Evolve is pushing the workspace somewhere — and in the incident
   the rogue remote URL embedded a credential. Remote URLs are redacted
   before they enter a Signal (never copy an embedded PAT into the
   signal store).

2. **Backup-ish commits not authored by Evolve.** Recent commits (last
   ``COMMIT_WINDOW_HOURS``) whose subject does NOT start with
   ``[backup]`` but matches a backup-ish pattern
   (``\\b(backup|snapshot|sync)\\b``, case-insensitive) — the signature
   of a bot hand-rolling its own backup system.

This module is **pure detection + spec-building**: the
``signals.store.observe()`` / ``sweep_resolve()`` calls live in
``backup_signal.py`` (the registered emit site for the ``backup_signal``
producer), which imports and wires these helpers per bot.

Git failures are silently no-finding by design — a broken repo is other
signals' job; this monitor only speaks when it positively observed
drift.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from schema.signal import make_signature

# Shared with backup_signal.PRODUCER — kept as a literal here (rather than
# importing backup_signal) to avoid a circular import; backup_signal
# imports this module.
PRODUCER = "backup_signal"
DRIFT_TYPE = "workspace_backup_drift"

# backup.py::_ensure_remote manages exactly this remote.
EXPECTED_REMOTES = frozenset({"origin"})

# How far back to scan for bot-authored backup-ish commits. Two nightly
# windows: wide enough that an hourly monitor can't miss a nightly rogue
# commit, narrow enough that a long-fixed incident ages out on its own.
COMMIT_WINDOW_HOURS = 48

# Legitimate Evolve backup commits are prefixed exactly like this.
_LEGIT_BACKUP_PREFIX = "[backup]"

# Backup-ish subject heuristic — the incident's rogue job produced varying
# LLM-generated messages, but every variant contained one of these words.
_BACKUPISH_RE = re.compile(r"\b(backup|snapshot|sync)\b", re.IGNORECASE)

_MAX_SAMPLE_COMMITS = 5


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess | None:
    """Run git in the workspace; None when the subprocess itself failed.

    ``-c safe.directory=*``: the workspace is owned by the bot user while
    this monitor runs as ``evolve`` — without it git 2.35.2+ rejects
    cross-owner access with "dubious ownership" (same pattern as
    ``backup.py::_git``).
    """
    try:
        return subprocess.run(
            ["git", "-c", "safe.directory=*", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return None


def redact_url(url: str) -> str:
    """Strip userinfo (embedded PATs) from a remote URL.

    The incident's rogue remote was an HTTPS URL with an embedded PAT
    (``https://x-access-token:ghp_...@github.com/...``). That credential
    must never be copied into the signal store — signals are shown in the
    admin UI and retained for 90 days after archive.
    """
    return re.sub(r"://[^/@\s]+@", "://<credentials-redacted>@", url)


def last_backup_commit_iso(
    workspace: Path,
    *,
    git_runner=_run_git,
) -> str | None:
    """ISO timestamp of the most recent legitimate ``[backup]`` commit.

    Used by backup_signal's daemon-silence check as the fallback anchor
    when ``state.json`` was never written: a workspace with prior
    ``[backup]`` commits proves the backup daemon used to run, so a
    missing/ancient attempt record is silence, not a fresh pod. Returns
    None when there is no such commit or git couldn't answer (fail-safe:
    no anchor → no silence claim).
    """
    r = git_runner(
        ["log", "-1", "--format=%cI", "--grep", r"^\[backup\]"], workspace,
    )
    if r is None or r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    return out or None


def collect_workspace_drift(
    bot_id: str,
    workspace: Path,
    *,
    git_runner=_run_git,
) -> dict | None:
    """Inspect one bot's workspace repo for a competing backup system.

    Returns a findings dict (``unexpected_remotes`` +
    ``rogue_commits``/``rogue_commit_count``) or None when clean.
    None is also returned when git can't answer at all (missing repo,
    permission failure, timeout) — a broken repo is other signals' job.
    """
    r = git_runner(["remote"], workspace)
    if r is None or r.returncode != 0:
        return None

    remote_names = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    unexpected_remotes: list[dict] = []
    for name in sorted(set(remote_names) - EXPECTED_REMOTES):
        ru = git_runner(["remote", "get-url", name], workspace)
        if ru is not None and ru.returncode == 0 and (ru.stdout or "").strip():
            url = redact_url(ru.stdout.strip())
        else:
            url = "(url unavailable)"
        unexpected_remotes.append({"name": name, "url": url})

    rogue_commits: list[dict] = []
    rogue_count = 0
    rl = git_runner(
        [
            "log",
            f"--since={COMMIT_WINDOW_HOURS}.hours.ago",
            "--format=%h%x09%s",
        ],
        workspace,
    )
    if rl is not None and rl.returncode == 0:
        for line in (rl.stdout or "").splitlines():
            sha, _, subject = line.partition("\t")
            subject = subject.strip()
            if not subject or subject.startswith(_LEGIT_BACKUP_PREFIX):
                continue
            if _BACKUPISH_RE.search(subject):
                rogue_count += 1
                if len(rogue_commits) < _MAX_SAMPLE_COMMITS:
                    rogue_commits.append(
                        {"sha": sha.strip(), "subject": subject[:200]},
                    )

    if not unexpected_remotes and not rogue_count:
        return None
    return {
        "unexpected_remotes": unexpected_remotes,
        "rogue_commits": rogue_commits,
        "rogue_commit_count": rogue_count,
    }


def build_signal_for_workspace_drift(
    bot_id: str,
    backup_url: str,
    findings: dict,
) -> dict:
    """Signal spec: a second backup system is operating in this workspace.

    Severity: ``alert`` when an unexpected remote exists (workspace data
    may be flowing to an unknown repo, and in the incident the remote URL
    carried an embedded credential), ``warn`` when only rogue backup-ish
    commits were seen.
    """
    remotes = findings.get("unexpected_remotes") or []
    commits = findings.get("rogue_commits") or []
    commit_count = int(findings.get("rogue_commit_count") or len(commits))

    severity = "alert" if remotes else "warn"
    parts = []
    if remotes:
        parts.append(
            f"{len(remotes)} unexpected git remote{'s' if len(remotes) != 1 else ''}"
        )
    if commit_count:
        parts.append(
            f"{commit_count} non-Evolve backup-ish commit"
            f"{'s' if commit_count != 1 else ''}"
        )
    title = f"{bot_id} workspace has a second backup system ({', '.join(parts)})"

    body_lines = [
        f"`{bot_id}`'s workspace repo shows evidence of a backup system "
        "that is NOT Evolve's:",
        "",
    ]
    if remotes:
        body_lines.append(
            "**Unexpected git remotes** (Evolve manages exactly one, "
            "`origin`; credentials redacted):"
        )
        body_lines += [f"- `{r['name']}` → `{r['url']}`" for r in remotes]
        body_lines.append("")
    if commit_count:
        body_lines.append(
            f"**Recent commits (last {COMMIT_WINDOW_HOURS}h) with backup-ish "
            "messages not authored by Evolve** (legitimate backup commits "
            "start with `[backup]`):"
        )
        body_lines += [f"- `{c['sha']}` {c['subject']}" for c in commits]
        if commit_count > len(commits):
            body_lines.append(f"- … and {commit_count - len(commits)} more")
        body_lines.append("")
    body_lines += [
        "Evolve owns workspace backups. A second backup system — typically "
        "a bot-authored cron job hand-rolling its own commits/pushes — "
        "corrupts freshness accounting (the Backup page keys off `[backup]` "
        "commits and `state.json`), can push workspace data to a repo "
        "Evolve has never verified is private, and in the 2026-07-28 "
        "incident left an HTTPS remote with an embedded credential in "
        "`.git/config`.",
        "",
        "**Fix**: remove the bot-authored backup job (check the bot's "
        "in-gateway cron jobs), delete any unexpected remote "
        "(`git remote remove <name>` in the workspace), and rotate any "
        "credential that was embedded in a removed remote URL.",
    ]

    return dict(
        signature=make_signature(PRODUCER, DRIFT_TYPE, bot_id),
        producer=PRODUCER,
        type=DRIFT_TYPE,
        flavor="maintenance",
        severity=severity,
        scope="bot",
        bot_id=bot_id,
        title=title,
        body="\n".join(body_lines),
        details=dict(
            backup_url=backup_url or None,
            unexpected_remotes=remotes,
            rogue_commits=commits,
            rogue_commit_count=commit_count,
            what_it_means=(
                f"Something other than Evolve is running backups in "
                f"{bot_id!r}'s workspace. Evolve's freshness accounting "
                "only trusts its own `[backup]` commits and run-state, so "
                "a competing system makes the Backup page lie — and an "
                "unexpected remote may be pushing workspace data to a "
                "repo whose visibility Evolve never verified."
            ),
            fix_steps=(
                "1. Inspect the bot's in-gateway cron jobs for a "
                "self-authored backup job and remove it\n"
                "2. In the workspace repo, run `git remote -v`; remove any "
                "remote other than `origin` with `git remote remove <name>`\n"
                "3. If a removed remote URL embedded a credential (PAT), "
                "rotate that credential — it lived world-visible in "
                ".git/config\n"
                "4. The Signal auto-resolves once the extra remote is gone "
                f"and no non-`[backup]` backup-ish commit lands for "
                f"{COMMIT_WINDOW_HOURS}h"
            ),
        ),
    )
