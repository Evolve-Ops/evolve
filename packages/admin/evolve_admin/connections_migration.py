"""connections_migration — the one-shot D-CN8 wrap of today's rows.

Split out of ``connections.py`` per the brief's own size guardrail (D-LT5):
this is a startup-only code path, run once per pod (idempotent — a
``connections.json`` that already exists means "already migrated", full
stop), and keeping it separate keeps the registry module itself small and
import-light for the many callers that just need ``verbs_for``.

Wraps, with ``health: unknown`` (D-CS7 — "not yet probed" is honest;
"connected ✓" is not):

* every bot's ``google_integration`` block (D-GA1's ``google_accounts[]``
  has not shipped yet — see ``users_roster.py``'s own note — so this reads
  the shape that exists today; when that migration lands it feeds the same
  ``google_integration``-shaped block this reads, so nothing here needs to
  change) → one Google connection row, ``role: own`` (the block has no
  ``role`` field yet — everything configured this way today IS the bot's
  own identity, never a delegated human account).
* every bot's configured ``backupRepoUrl`` → one GitHub connection row
  pointing at the pod-wide keystore PAT, ``credential_kind: pat``,
  ``expires_at: null`` (classic PATs carry no discoverable expiry via the
  API this pod uses; D-CN5's tracked-expiry work is a later chip).

Nothing is deleted, nothing currently working changes behavior — this only
ever ADDS rows to a previously-absent file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import connection_capabilities as caps
from . import connections as conn
from .config import load_network
from .google_service import SUPPORTED_MODES

SCOPE_SHAPED_REASON = conn.SCOPE_SHAPED_REASON
NOT_YET_PROBED_REASON = "not yet probed"


def _migrate_google_row(bot_id: str, bot_cfg: dict[str, Any]) -> dict[str, Any] | None:
    # Literal scope match, so a broad grant can derive no verbs; such a row
    # carries SCOPE_SHAPED_REASON and connections.is_configured_via_registry
    # reads it as "registry cannot say", leaving eligibility to the legacy check.
    gi = bot_cfg.get("google_integration") or {}
    mode = gi.get("mode")
    if mode not in SUPPORTED_MODES:
        return None
    granted_scopes = list(gi.get("scopes") or [])
    jobs = caps.jobs_matching_scopes("google", granted_scopes)
    # A pending verb has no tool; it must not land in the source-of-truth row.
    verbs = [v for v in caps.verbs_matching_scopes("google", granted_scopes)
             if not caps.is_pending("google", v)]
    health = conn.new_health(
        "unknown",
        reason=NOT_YET_PROBED_REASON if jobs else SCOPE_SHAPED_REASON,
    )
    label = gi.get("subject") or f"{bot_id} (google)"
    return conn.new_connection(
        bot_id=bot_id,
        service="google",
        account_label=label,
        role="own",
        jobs=jobs,
        capabilities_=verbs,
        credential_ref=f"google_integration:{bot_id}",
        credential_kind=mode,
        expires_at=None,
        health=health,
    )


def _migrate_github_row(bot_id: str, bot_cfg: dict[str, Any]) -> dict[str, Any] | None:
    repo_url = bot_cfg.get("backupRepoUrl")
    if not repo_url:
        return None
    return conn.new_connection(
        bot_id=bot_id,
        service="github",
        account_label=str(repo_url),
        role="own",
        jobs=["backup_to_private_repo"],
        capabilities_=["repo.push_backup"],
        credential_ref="keystore:github_pat",
        credential_kind="pat",
        expires_at=None,
        health=conn.new_health("unknown", reason=NOT_YET_PROBED_REASON),
    )


def build_migrated_rows(network: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure function: network.json's bot blocks -> the connection rows the
    migration would write. Split out from the write path so the fixture
    test can assert on the rows without touching a file."""
    rows: list[dict[str, Any]] = []
    for bot_id, bot_cfg in (network.get("bots") or {}).items():
        if not isinstance(bot_cfg, dict):
            continue
        google_row = _migrate_google_row(bot_id, bot_cfg)
        if google_row is not None:
            rows.append(google_row)
        github_row = _migrate_github_row(bot_id, bot_cfg)
        if github_row is not None:
            rows.append(github_row)
    return rows


def migrate_if_needed(
    network: dict[str, Any], path: Path | None = None,
) -> dict[str, Any] | None:
    """Idempotent one-shot migration into ``path`` (default: this network's
    ``conn.connections_path``). Returns the written registry dict, or
    None when the file already exists (already migrated — a no-op, by
    design, even if network.json has since grown new bots; those get rows
    the ordinary way, through the wizard, once it exists).
    """
    if path is None:
        path = conn.connections_path(network)
    if path.exists():
        return None
    data = {"connections": build_migrated_rows(network)}
    conn.save_connections(data, path)
    return data


def run_startup_migration(network_path: Path) -> None:
    """Daemon-startup call site: load network.json, migrate, log, swallow —
    best-effort exactly like the adjacent github.pat keystore migration in
    server.py (a failure here must not block the daemon from starting; it
    retries every restart until connections.json exists)."""
    from .telemetry import get_logger
    log = get_logger("connections_migration")
    try:
        migrated = migrate_if_needed(load_network(network_path))
        if migrated is not None:
            log.info("connections registry migrated: %d row(s) written",
                      len(migrated.get("connections", [])))
    except Exception:
        log.warning("connections registry migration failed (will retry next start)",
                    exc_info=True)
