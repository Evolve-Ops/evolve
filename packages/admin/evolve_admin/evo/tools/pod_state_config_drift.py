"""Pod-state read tool — config drift across all bots.

Exposes ``pod_state.config_drift()`` — returns the same data the
Security → Backups subtab renders: which bots have config drift
(non-empty ``drifted_keys`` from heal's baseline comparison) and
which keys differ. Closes the 2026-05-20 transcript where the
operator on the Backups subtab asked "can you accept all of the
current configs as baseline?" and evo, lacking both this read tool
and an action tool, confabulated about audit-finding Mute (a
*different* security concept) instead of seeing the drift list
right in front of the operator.

Mechanism: HTTP GET to ``/api/backup/cloud/status`` on the admin
server (Phase 4f canonical path; legacy ``/api/security/backup-status``
still aliased). That endpoint already aggregates per-bot drift (via
``heal.detect_backup_drift_keys``) for the UI. Same data path
the page render uses — no duplication, no cross-process surprises.

Note on terminology — this tool surfaces **backup-baseline drift**
(the openclaw.json key-set diff against the bot's evolve-backup/
workspace), NOT audit findings. The two are different mechanisms
sharing the same Security page:

  * Backup-baseline drift → ``action.security.accept_drift``
    (commits openclaw.json into evolve-backup/ as the new baseline).
  * Audit-finding Mute → ``action.signal.dismiss`` /
    per-advisory Mute button (suppresses repeated info-tier alerts).

Risk tier: ``read`` — pure inspection.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from ..admin_http import urlopen_admin
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)


def _resolve_base_url(network_path: Path) -> str | None:
    """Resolve the admin server's base URL the same way other tools do.

    Returns ``None`` on any resolution failure; the handler then surfaces
    a clear error to the model rather than raising.
    """
    try:
        from evolve_admin.config import load_network, resolve_admin_base_url
        net = load_network(network_path)
        return resolve_admin_base_url(net).rstrip("/")
    except Exception:  # noqa: BLE001
        return None


def _config_drift_handler(network_path: Path) -> dict[str, Any]:
    """GET ``/api/backup/cloud/status`` and project to a flat drift list.

    Returns:

        {
          "ok": True,
          "drifted_bots": [
            {"bot_id": "team_bot_a", "drifted_keys": ["tools"], "stale_backup": false},
            ...
          ],
          "clean_bots": ["evo", ...],
          "total_drifted": int,
          "total_clean": int,
        }

    Sorted by bot_id within each bucket so the model can paraphrase
    deterministically. The ``stale_backup`` flag piggybacks because the
    operator's likely follow-up ("are these bots' backups even
    running?") shares the same data path — folding it in saves a
    tool call.
    """
    base_url = _resolve_base_url(network_path)
    if base_url is None:
        return {
            "ok": False,
            "error": (
                "could not resolve admin base URL from network.json. "
                "evo_telemetry should log this as a tool-gap if the "
                "admin server is genuinely unreachable."
            ),
        }

    # Phase 4f: canonical path is /api/backup/cloud/status. The legacy
    # /api/security/backup-status alias still works, but new code uses
    # the consolidated namespace.
    url = f"{base_url}/api/backup/cloud/status"
    try:
        req = urllib.request.Request(
            url, method="GET",
            headers={"Accept": "application/json"},
        )
        with urlopen_admin(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            err_body = ""
        return {
            "ok": False,
            "error": (
                f"admin server returned HTTP {exc.code}"
                + (f": {err_body[:200]}" if err_body else "")
            ),
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "error": f"admin server unreachable at {url}: {exc.reason}",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"HTTP GET failed: {exc}"}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"admin server returned non-JSON: {exc}"}

    bots = (payload or {}).get("bots") or {}
    if not isinstance(bots, dict):
        return {
            "ok": False,
            "error": (
                f"unexpected backup-status shape: expected dict under "
                f"'bots', got {type(bots).__name__}"
            ),
        }

    drifted_bots = []
    clean_bots = []
    for bot_id in sorted(bots.keys()):
        entry = bots[bot_id] or {}
        drifted_keys = entry.get("drifted_keys")
        if isinstance(drifted_keys, list) and drifted_keys:
            drifted_bots.append({
                "bot_id": bot_id,
                "drifted_keys": list(drifted_keys),
                "stale_backup": bool(entry.get("stale")),
                "last_backup_at": entry.get("last_backup"),
            })
        else:
            clean_bots.append(bot_id)

    return {
        "ok": True,
        "drifted_bots": drifted_bots,
        "clean_bots": clean_bots,
        "total_drifted": len(drifted_bots),
        "total_clean": len(clean_bots),
    }


CONFIG_DRIFT_TOOL = Tool(
    name="pod_state.config_drift",
    description=(
        "Return which bots have *backup-baseline drift* — i.e. their "
        "live ``openclaw.json`` differs from the last ``[backup]``-prefixed "
        "commit in their workspace's ``evolve-backup/``. Same data the "
        "Security → Backups subtab renders next to each bot's 'Accept "
        "as baseline' button. Use when the operator asks 'which bots "
        "have config drift?', 'accept all configs as baseline' (enumerate "
        "first, then iterate ``action.security.accept_drift`` per bot), "
        "or just wants to confirm a clean state. Distinct from audit "
        "findings — drift is *baseline-vs-live*; audit is "
        "*config-against-policy*. Pure read."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    handler=_config_drift_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "security", "backup", "read"),
    # Conservative default — admin-only. Pod-changing writes / sensitive reads stay gated to admin callers; the
    # auth-scope retrofit (this PR) made the choice explicit rather than relying on the framework's default.
    authorization_scope="admin",
)

register(CONFIG_DRIFT_TOOL)
