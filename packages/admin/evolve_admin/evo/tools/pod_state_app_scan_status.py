"""Pod-state read tool — app-scan status.

Exposes ``pod_state.app_scan_status(bot_id)`` — surfaces the canonical
``.scan-status.json`` file the admin server's tile-metrics layer reads
to decide whether the ``scan_needed`` chip should fire on a bot's
dashboard tile.

The chip computation is in ``analyzer.tile_metrics`` and fires when
EITHER:

  * the file doesn't exist at ``{shared_dir}/applications/{bot_id}/
    .scan-status.json``, OR
  * the file is older than ``APPS_UNTESTED_DAYS`` (30 days).

Background — why this tool exists. A 2026-05-20 operator transcript
showed evo asked about a persistent ``scan_needed`` warning on the
primary bot. Evo correctly diagnosed *"the chip logic isn't finding
the .scan-status.json"* but then defaulted to ``find /Users/...``
shell snippets for the operator to run. The shell snippets violated
the AGENTS.md ban from PR #1347 — but more importantly, evo had
*no tool* that would let it answer the question itself. This is
that tool.

It does NOT trigger a scan. Pair with ``action.bot.rescan_apps`` for
that.

Read order:
  1. Resolve ``shared_dir`` from ``network_path`` (no shared_dir
     parameter — the bridge already injects network_path everywhere,
     keeping the surface minimal).
  2. Stat the canonical file at ``{shared_dir}/applications/{bot_id}/
     .scan-status.json``.
  3. If readable, parse + project.
  4. Compute and return ``chip_firing_reason`` so the model can
     explain *why* the chip is on (or off) without re-deriving the
     rule from scratch.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import RiskTier, Tool, register

log = logging.getLogger(__name__)

# Mirrors ``analyzer.tile_metrics.APPS_UNTESTED_DAYS``. Pinned here so
# the tool's chip-firing-reason logic agrees with the production
# computation. If the analyzer constant changes, update this too —
# a test guards the agreement.
_APPS_UNTESTED_DAYS = 30


def _resolve_shared_dir(network_path: Path | None) -> tuple[Path | None, str | None]:
    """Resolve ``sharedDir`` from network.json. Returns ``(path, err)``."""
    if network_path is None:
        return None, (
            "network_path not injected — tool may be running outside "
            "the MCP bridge"
        )
    try:
        from evolve_admin.config import load_network
    except ImportError as exc:
        return None, f"evolve_admin.config not importable: {exc}"
    try:
        net = load_network(network_path)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not load network.json: {exc}"
    shared = net.get("sharedDir") or "/Users/Shared/evolve"
    return Path(shared), None


def _format_mtime(mtime: float) -> str:
    """ISO8601 UTC, second precision, trailing Z — matches the rest
    of the audit / signal time formatting in this codebase."""
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(
        timespec="seconds",
    ).replace("+00:00", "Z")


def _compute_chip_reason(
    path: Path,
    days_old: float | None,
    file_exists: bool,
    apps_total: int | None,
) -> dict[str, Any]:
    """Mirror the chip-firing rule from ``analyzer.tile_metrics``.

    The chip fires only when the bot has apps to scan AND the file is
    missing or stale. Returns a dict the model can render verbatim
    in its reply.
    """
    if apps_total is not None and apps_total == 0:
        return {
            "chip_firing": False,
            "explanation": (
                "scan_needed chip does NOT fire when the bot has no "
                "apps to scan (apps_total=0)."
            ),
        }
    if not file_exists:
        return {
            "chip_firing": True,
            "explanation": (
                f".scan-status.json is missing at the canonical path "
                f"({path}). The chip detail reads 'never scanned'. "
                f"This usually means the scanner ran but didn't write "
                f"to the canonical (shared) location — the file lives "
                f"in the bot's own workspace and the admin server's "
                f"post-scan stamping step (server.py:_monitor) is "
                f"responsible for mirroring it here. If the bot is the "
                f"primary, double-check that path."
            ),
        }
    if days_old is None:
        return {
            "chip_firing": True,
            "explanation": (
                "file exists but stat() failed — chip treats this as "
                "missing. Likely permissions on the file or directory."
            ),
        }
    if days_old > _APPS_UNTESTED_DAYS:
        return {
            "chip_firing": True,
            "explanation": (
                f"last scan was {days_old:.1f} days ago, threshold is "
                f"{_APPS_UNTESTED_DAYS} days. Re-run the scan via the "
                f"Applications page or action.bot.rescan_apps."
            ),
        }
    return {
        "chip_firing": False,
        "explanation": (
            f"file exists, last scanned {days_old:.1f}d ago (within "
            f"the {_APPS_UNTESTED_DAYS}d window). If the chip still "
            f"shows in the UI, the operator may be seeing a stale "
            f"tile cache — a page refresh or heal cycle should clear "
            f"it."
        ),
    }


def _count_apps(shared_dir: Path, bot_id: str) -> int | None:
    """Best-effort count of manifests for this bot. Used to decide
    whether the scan_needed chip would even fire (it doesn't when
    apps_total=0). Returns None if we can't enumerate."""
    apps_dir = shared_dir / "applications" / bot_id
    if not apps_dir.exists():
        return 0
    try:
        return sum(
            1 for p in apps_dir.glob("*.json")
            if p.name not in (".scan-status.json",)
            and not p.name.startswith("_")
        )
    except OSError:
        return None


def _handler(
    bot_id: str,
    network_path: Path | None = None,
) -> dict[str, Any]:
    """Return the scan-status diagnostic for one bot.

    Always returns a structured result — never raises. The model's
    next move depends on:
      * ``file_exists`` — points at "scan never wrote to the canonical
        location" when False
      * ``chip_firing_reason`` — explains in operator-readable terms
        why the chip is on or off
      * ``contents`` — the parsed JSON when available, so the model
        can quote scanner-reported phase/found/etc. directly
    """
    if not bot_id:
        return {"ok": False, "error": "bot_id is required"}

    shared, err = _resolve_shared_dir(network_path)
    if err:
        return {"ok": False, "error": err}
    # Type narrowing for static checkers — err None implies shared is Path.
    assert shared is not None

    path = shared / "applications" / bot_id / ".scan-status.json"
    file_exists = path.exists()
    mtime: float | None = None
    mtime_iso: str | None = None
    days_old: float | None = None
    contents: dict[str, Any] | None = None
    parse_error: str | None = None

    if file_exists:
        try:
            stat = path.stat()
            mtime = stat.st_mtime
            mtime_iso = _format_mtime(mtime)
            import time as _t
            days_old = (_t.time() - mtime) / 86400
        except OSError as exc:
            parse_error = f"stat failed: {exc}"
        try:
            contents = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(contents, dict):
                contents = None
                parse_error = "scan-status.json is not a JSON object"
        except (OSError, json.JSONDecodeError) as exc:
            parse_error = f"could not read/parse: {exc}"

    apps_total = _count_apps(shared, bot_id)
    chip_reason = _compute_chip_reason(
        path, days_old, file_exists, apps_total,
    )

    return {
        "ok": True,
        "bot_id": bot_id,
        "canonical_path": str(path),
        "file_exists": file_exists,
        "mtime": mtime_iso,
        "days_old": (
            round(days_old, 2) if days_old is not None else None
        ),
        "apps_total": apps_total,
        "contents": contents,
        "parse_error": parse_error,
        "chip_firing_reason": chip_reason,
        # Verify hint — after action.bot.rescan_apps, the model can
        # re-call this tool and confirm chip_firing went False.
        "verify_via": {
            "tool": "pod_state.app_scan_status",
            "args": {"bot_id": bot_id},
            "expect": "chip_firing_reason.chip_firing == false",
        },
    }


TOOL = Tool(
    name="pod_state.app_scan_status",
    description=(
        "Diagnostic read tool for the ``scan_needed`` tile chip. "
        "Returns the canonical .scan-status.json file's existence, "
        "mtime, days_old, parsed contents, and a structured "
        "chip_firing_reason explaining why the chip is on (or off). "
        "Use this when the operator asks about the scan_needed chip "
        "on a bot's tile, OR when they say 'I've run scans but the "
        "warning persists' — the file may be missing because the "
        "scanner wrote to the bot's own workspace but the admin "
        "server's post-scan stamping step failed to mirror it to the "
        "canonical (shared) location. DO NOT propose ``find`` / "
        "``ls`` shell snippets to the operator — use this tool "
        "instead. To re-trigger a scan after diagnosis, call "
        "``action.bot.rescan_apps``."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "bot_id": {
                "type": "string",
                "description": "Bot id from network.json::bots.",
            },
        },
        "required": ["bot_id"],
        "additionalProperties": False,
    },
    handler=_handler,
    risk_tier=RiskTier.READ,
    tags=("pod_state", "applications", "scan"),
    # App-scan status mirrors the tile chip the operator sees on the
    # dashboard — file mtime + structured chip-firing reason. No
    # credential / no PII. Safe for cross-bot members to inspect.
    authorization_scope="anyone",
)

register(TOOL)
