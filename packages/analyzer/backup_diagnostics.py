"""backup_diagnostics — classify backup failures into actionable causes.

Maps the stderr/exception text captured in ``state.json::last_error``
(written by ``backup.py::write_run_state``) into a structured
``{cause_id, title, fix_steps}`` triple. Two callers consume it:

1. **backup_signal** — when a failing bot's error matches a known
   pattern, fire a specific Signal (``backup_ssh_key_mismatch``,
   ``backup_deploy_key_unrecognized``, …) on the FIRST failure rather
   than waiting for the generic ``backup_failing`` 3-failure threshold.
   This closes the 2026-06-07 silent-2-day failure mode: five team
   bots sat at consecutive_failures=2 for two nights, each with the
   most specific possible "private key contents do not match public"
   error in state.json, and no Signal fired because the threshold
   hadn't tripped.

2. **/api/backup/cloud/status** — surface the classification alongside
   the raw error so the admin UI can render a structured diagnostic
   panel (Backup → Status table, Maintenance → Bot Versions Backed-up
   cell). Operators see "Likely cause: SSH private key doesn't match
   public key on disk" + concrete fix steps inline, instead of a
   tooltip-truncated stderr.

Pure Python, regex-based. Returns None when nothing matches — the
caller falls back to a generic "unknown error, see logs" treatment.

Pattern ordering matters: a "Permission denied (publickey)" can
co-occur with the more specific "identity_sign: contents do not match"
in the same stderr, so the specific pattern is checked first.
"""

from __future__ import annotations

import re
from typing import NamedTuple


class _Cause(NamedTuple):
    cause_id: str
    title: str
    fix_steps: tuple[str, ...]


# Cause table. Order matters — most specific first.
#
# Each pattern is matched with re.search against the raw last_error
# string. cause_id is stable (it appears in Signal types and the API
# response) so renaming requires migrating consumers. Title is shown
# verbatim in the admin UI as the "Likely cause" line. fix_steps are
# rendered as a numbered list under the cause.
_CAUSES: tuple[tuple[re.Pattern[str], _Cause], ...] = (
    # SSH agent / client refuses before contacting GitHub: the .pub
    # file alongside the private key on disk doesn't pair with it.
    # 2026-06-07 root cause for the five-team-bot multi-bot incident:
    # the shared-key distribute endpoint wrote shared private bytes
    # into files that already had per-bot pub bytes from an earlier
    # deploy.
    (
        re.compile(
            r"identity_sign:.*private key.*contents do not match public",
            re.IGNORECASE | re.DOTALL,
        ),
        _Cause(
            cause_id="ssh_key_mismatch",
            title="SSH private key on disk doesn't match the public key alongside it",
            fix_steps=(
                "The bot's `~/.ssh/evolve-backup-<bot>` (private) and "
                "`~/.ssh/evolve-backup-<bot>.pub` (public) are from "
                "different keypairs — SSH refuses to use them before "
                "even reaching GitHub.",
                "Open **Backup → Cloud → Distribute Key** to overwrite "
                "both files with one consistent keypair. The endpoint's "
                "pubkey then needs to be registered as a deploy key on "
                "the GitHub backup repo (or added to your user account, "
                "depending on the model you're using).",
                "Alternative: SSH to the mini and run "
                "`sudo evolve-admin deploy <bot>` — this regenerates "
                "per-bot keys and redistributes both files atomically. "
                "The new pubkey must then be uploaded as a deploy key.",
            ),
        ),
    ),
    # Connectivity / DNS — caught BEFORE Permission denied because some
    # transient network failures emit both lines.
    (
        re.compile(r"Could not resolve host(?:name)?", re.IGNORECASE),
        _Cause(
            cause_id="dns_failure",
            title="Could not reach github.com — DNS or network failure",
            fix_steps=(
                "The mini couldn't resolve github.com. Check the mini's "
                "internet connection and DNS configuration.",
                "Verify connectivity from the mini: "
                "`ping -c 3 github.com`. If ping fails, the issue is "
                "local network / DNS, not Evolve.",
                "Backup will resume automatically on the next nightly run "
                "once connectivity is restored.",
            ),
        ),
    ),
    # Repo URL bad, repo renamed/deleted, or the deploy key on the repo
    # was revoked so GitHub returns "not found" rather than "permission
    # denied" (this is GitHub's intentional information-hiding behavior
    # for private repos with insufficient credentials).
    (
        re.compile(r"Repository not found|ERROR: Repository not found", re.IGNORECASE),
        _Cause(
            cause_id="repo_not_found",
            title="GitHub doesn't have this repo — or the SSH key has no access",
            fix_steps=(
                "GitHub returns 'Repository not found' both when the URL "
                "is wrong AND when the credentials lack access to a private "
                "repo — it intentionally hides the difference.",
                "Verify the `backupRepoUrl` value in Backup → Cloud is "
                "correct. Repos that have been renamed/deleted/transferred "
                "show this error.",
                "If the URL is correct, the SSH deploy key on the bot card "
                "isn't registered (or has been revoked) on the GitHub repo. "
                "Open the repo's **Settings → Deploy keys** to verify.",
            ),
        ),
    ),
    # Host key (github.com fingerprint) not in the bot's ~/.ssh/known_hosts.
    # Usually happens on a fresh bot that wasn't deployed through the
    # current code path that pre-seeds known_hosts.
    (
        re.compile(r"Host key verification failed", re.IGNORECASE),
        _Cause(
            cause_id="host_key_unverified",
            title="github.com host key isn't trusted in this bot's ~/.ssh/known_hosts",
            fix_steps=(
                "The bot's `~/.ssh/known_hosts` doesn't contain github.com's "
                "fingerprint, so SSH dies non-interactively rather than "
                "prompting.",
                "SSH to the mini and run "
                "`sudo evolve-admin deploy <bot>` — the deploy path "
                "pre-seeds known_hosts with github.com's fingerprint.",
            ),
        ),
    ),
    # GitHub explicitly rejected the key. This is the public-key-revoked
    # /not-registered case; the key file ON DISK is valid (consistent
    # pair) but GitHub doesn't recognise it. Checked AFTER ssh_key_mismatch
    # because that case also emits "Permission denied (publickey)" later
    # in the same stderr.
    (
        re.compile(r"Permission denied \(publickey\)", re.IGNORECASE),
        _Cause(
            cause_id="deploy_key_unrecognized",
            title="GitHub rejected the SSH key — not registered as a deploy key",
            fix_steps=(
                "The SSH keypair on disk is internally consistent, but "
                "GitHub doesn't recognise the public key as a valid deploy "
                "key on this repo (or a valid SSH key on the owning user "
                "account, depending on the backup model).",
                "Find the pubkey shown on this bot's card in Backup → Cloud. "
                "Open the GitHub backup repo's **Settings → Deploy keys** "
                "(or the user account's **Settings → SSH and GPG keys**, "
                "for the shared-key model) and confirm the pubkey is "
                "registered with write access.",
                "If the key was revoked, re-add it. If you've rotated keys, "
                "run **Distribute Key** in Backup → Cloud and then add the "
                "new pubkey to GitHub.",
            ),
        ),
    ),
    # SSH found no usable authentication method at all — usually means
    # the agent has no identity loaded AND no -i path resolved.
    (
        re.compile(r"No supported authentication methods", re.IGNORECASE),
        _Cause(
            cause_id="auth_method_disabled",
            title="SSH found no usable authentication method",
            fix_steps=(
                "The backup daemon couldn't offer GitHub any key to "
                "authenticate with. Usually means the per-bot SSH key file "
                "is missing or unreadable.",
                "SSH to the mini and verify the key file exists: "
                "`sudo ls -la /Users/<bot_user>/.ssh/evolve-backup-<bot>` — "
                "if missing, re-run **Distribute Key** in Backup → Cloud, "
                "or `sudo evolve-admin deploy <bot>`.",
            ),
        ),
    ),
)


def classify(last_error: str | None) -> dict | None:
    """Classify a backup error string into a structured cause.

    Returns ``None`` when ``last_error`` is empty / None or no pattern
    matches. Returns ``{cause_id, title, fix_steps}`` otherwise.

    The shape is JSON-serialisable so the same dict can flow through
    the admin API to the UI without further marshalling. ``fix_steps``
    is a plain list of strings (not a tuple) for the same reason.
    """
    if not last_error:
        return None
    text = str(last_error)
    for pattern, cause in _CAUSES:
        if pattern.search(text):
            return {
                "cause_id": cause.cause_id,
                "title": cause.title,
                "fix_steps": list(cause.fix_steps),
            }
    return None


def known_cause_ids() -> tuple[str, ...]:
    """All cause_ids the classifier can return. Used by tests + signal
    type registration so the producer side stays in sync with the
    pattern table.
    """
    return tuple(cause.cause_id for _, cause in _CAUSES)
