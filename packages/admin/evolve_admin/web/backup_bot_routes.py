"""Bot-facing backup-visibility route — ``/api/backup-bot/visibility``.

THE PROBLEM THIS EXISTS TO SOLVE. ``packages/analyzer/backup.py`` runs as the
**bot user** (deliberately — the bot's workspace and its ``evolve-backup/`` dir
stay bot-owned). Before pushing a workspace to its cloud backup repo it must
confirm that repo is private, which until now meant reading the pod-wide GitHub
PAT out of the keystore vault and calling the GitHub API itself.

That read stopped working on 2026-08-18, when PR #3696 clamped
``{shared}/keystore/.machine-key`` to ``0640 evolve:wheel``. Bots are not in
``wheel``. #3696 was CORRECT — a world-readable machine key let any compromised
or prompt-injected bot decrypt the stored PAT — and must not be reverted. The
defect was that the tightening had one legitimate reader inside its blast
radius. Twenty-three nights, nine bots, no push, no Signal.
See ``internal/finding-backups-silently-off-since-2026-08-18.md``.

THE SHAPE OF THE FIX: **verdict out, token never out.** The admin daemon already
runs as ``evolve`` and already owns the vault. The bot asks it a yes/no question
about its own backup repo and gets back a verdict — ``private`` / ``public`` /
``unknown`` — and nothing else. The bot never holds the PAT again, so the vault
stays as shut to bot accounts as #3696 made it.

  POST /api/backup-bot/visibility   → ``{visibility, reason, repo_url}``

TWO THINGS ARE BOUND SERVER-SIDE, NEVER TAKEN FROM THE REQUEST:

  1. **Which bot is asking** — the unix-socket peer uid
     (``peer_auth.resolve_peer_bot_id``), the same primitive the directory and
     Google bot routes use. A bot cannot present another bot's uid.

  2. **Which repo gets checked** — the caller's OWN ``backupRepoUrl`` from
     network.json. A ``repoUrl`` in the body is NOT honoured as the lookup
     target; it is only compared against the configured one, and a mismatch is
     reported so the caller can fail closed. This is what keeps the endpoint
     from becoming a general-purpose GitHub oracle: a compromised bot cannot
     spend the pod's PAT enumerating the existence or visibility of arbitrary
     private repos, which is most of what the PAT is worth to an attacker.

NO TOKEN-SHAPED STRING CAN LEAVE HERE. ``reason`` is drawn from a closed
vocabulary of literals defined in this module — never an exception string, never
an upstream API body — so there is no path by which a PAT, or a URL with an
embedded credential, reaches the response. ``test_backup_bot_routes`` asserts
this against every reachable branch.

THE GUARD'S DIRECTION IS UNCHANGED. Anything that is not a confirmed ``private``
still refuses the push. A daemon that is down, a socket that is missing, a 403,
a timeout — all of it lands as ``unknown`` at the caller, which refuses. This
endpoint can only ever permit a push that the direct-PAT path would also have
permitted.
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, jsonify, request
from flask.typing import ResponseReturnValue

from ..config import load_network
from . import peer_auth

log = logging.getLogger(__name__)


# Closed vocabulary for ``reason``. Every response body is assembled from these
# literals plus a verdict and the server-resolved repo URL — nothing derived
# from a secret, an exception, or an upstream response body.
REASON_OK = "checked"
REASON_NO_URL = "no-backup-url-configured"
REASON_URL_MISMATCH = "requested-url-is-not-this-bot-s-configured-backup-repo"
REASON_PAT_ABSENT = "no-pat-stored"
REASON_PAT_UNREADABLE = "pat-unreadable-by-admin-daemon"
REASON_LOOKUP_FAILED = "github-lookup-failed"


def register_backup_bot_routes(app: Flask, network_path: Path) -> None:
    """Register the bot-facing ``/api/backup-bot/*`` routes on the Flask app."""

    @app.post("/api/backup-bot/visibility")
    def api_backup_bot_visibility() -> ResponseReturnValue:
        """Return the visibility verdict for the *calling* bot's backup repo.

        Body (optional): ``{"repoUrl": "git@github.com:owner/name.git"}`` — used
        ONLY to detect a mismatch against the bot's configured
        ``backupRepoUrl``, never as the lookup target.

        Response: ``{"visibility": "private"|"public"|"unknown",
        "reason": <literal>, "repo_url": <the bot's configured URL>}``.
        """
        bot_id = peer_auth.resolve_peer_bot_id(network_path)
        if not bot_id:
            # No peer-bound identity (TCP, or an unknown uid). Fail closed —
            # the PAT must never be spent on behalf of an unidentified caller.
            return jsonify({"error": "caller not a recognized bot"}), 403

        net = load_network(network_path)
        bots = net.get("bots") if isinstance(net.get("bots"), dict) else {}
        cfg = bots.get(bot_id) if isinstance(bots, dict) else None
        configured = ""
        if isinstance(cfg, dict):
            configured = (cfg.get("backupRepoUrl") or "").strip()

        if not configured:
            return jsonify({
                "visibility": "unknown",
                "reason": REASON_NO_URL,
                "repo_url": "",
            })

        # A body URL is compared, never substituted. The caller pushes to the
        # URL it asked about; if that is not the URL we are authorized to check
        # for this bot, it must not treat our verdict as covering it.
        body = request.get_json(silent=True) or {}
        asked = body.get("repoUrl")
        if isinstance(asked, str) and asked.strip() and asked.strip() != configured:
            log.warning(
                "backup-bot visibility: %s asked about a URL that is not its "
                "configured backup repo; refusing to check", bot_id,
            )
            return jsonify({
                "visibility": "unknown",
                "reason": REASON_URL_MISMATCH,
                "repo_url": configured,
            })

        from ..keystore import SecretState, load_github_pat_state
        from .routes_bot_users import _shared_dir_from_net

        state, pat = load_github_pat_state(Path(_shared_dir_from_net(net)))
        if state is SecretState.ABSENT:
            # Genuinely unconfigured — the Backup → Cloud wizard IS the fix,
            # and saying so here is correct (unlike saying it to a bot that
            # merely cannot read the vault).
            return jsonify({
                "visibility": "unknown",
                "reason": REASON_PAT_ABSENT,
                "repo_url": configured,
            })
        if state is not SecretState.PRESENT or not pat:
            # The daemon runs as evolve and owns the vault, so this is a real
            # pod fault (vault perms drifted, .machine-key lost) rather than
            # the bot-side lockout this endpoint exists to route around.
            log.error(
                "backup-bot visibility: admin daemon cannot read the pod PAT "
                "(state=%s) — check {shared}/keystore perms", state,
            )
            return jsonify({
                "visibility": "unknown",
                "reason": REASON_PAT_UNREADABLE,
                "repo_url": configured,
            })

        try:
            # Lazy + top-level, the same resolution ``server._import_analyzer``
            # performs (``importlib.import_module`` against the editable
            # evolve-analyzer install). Imported here rather than at module
            # scope because ``server`` imports THIS module to register it.
            #
            # ``pat=`` is passed explicitly, which is what keeps this call on
            # the direct-GitHub path: ``check_repo_visibility`` only reaches
            # for the daemon when it cannot read a PAT, so this endpoint can
            # never call back into itself.
            from backup_visibility import check_repo_visibility
            verdict = check_repo_visibility(configured, pat=pat)
        except Exception as exc:  # noqa: BLE001 — never 500 a bot read
            # The exception text is deliberately logged and NOT returned: it can
            # quote the request URL, and a misconfigured HTTPS remote can carry
            # an embedded credential (the 2026-07-28 incident shape).
            log.warning("backup-bot visibility lookup for %s failed: %s", bot_id, exc)
            return jsonify({
                "visibility": "unknown",
                "reason": REASON_LOOKUP_FAILED,
                "repo_url": configured,
            })

        return jsonify({
            "visibility": verdict,
            "reason": REASON_OK if verdict != "unknown" else REASON_LOOKUP_FAILED,
            "repo_url": configured,
        })
