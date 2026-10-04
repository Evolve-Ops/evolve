"""Append-only audit log for primary-user identity changes.

Every claim and reassign writes a single JSON Lines record to
``{shared_dir}/audit/evo_identity.jsonl``. This is the load-bearing trail
for "who changed team_bot_a's primary three weeks ago, from whom, and why?" — the
data either lives here or is irrecoverable.

Record shape:

.. code-block:: json

    {
      "ts":      "2026-05-05T15:30:00Z",
      "actor":   "cli:pod_admin_user",
      "action":  "claim",
      "bot_id":  "team_bot_a",
      "channel": "slack",
      "from":    null,
      "to":      "U123ABC",
      "force":   false,
      "reason":  null
    }

The writer is pure I/O. Callers (CLI handlers, route handlers) decide when
to record and what fields to populate; this module just formats the line
and appends. Append on POSIX is naturally atomic for line-sized writes, so
no temp+rename is needed for single records.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from evolve_util import now_iso as _now_iso


Action = Literal["claim", "reassign", "claim_admin", "guide_save"]


_AUDIT_FILENAME = "evo_identity.jsonl"


def append_event(
    shared_dir: Path,
    *,
    actor: str,
    action: Action,
    bot_id: str,
    channel: str,
    to_external_id: str,
    from_external_id: str | None = None,
    force: bool = False,
    reason: str | None = None,
    ts: str | None = None,
) -> None:
    """Append one record to ``{shared_dir}/audit/evo_identity.jsonl``.

    Best-effort: errors are surfaced to the caller, who decides whether a
    failed audit write should fail the user-visible operation. (For v1 the
    callers do treat audit-write failure as a real error — losing audit on
    silent failure is worse than the rare permission glitch surfacing.)

    For ``action="claim_admin"``, ``bot_id`` is conventionally ``"<pod>"``
    (admin scope is pod-wide, not per-bot).
    """
    audit_dir = Path(shared_dir) / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / _AUDIT_FILENAME

    record: dict[str, Any] = {
        "ts": ts or _now_iso(),
        "actor": actor,
        "action": action,
        "bot_id": bot_id,
        "channel": channel,
        "from": from_external_id,
        "to": to_external_id,
        "force": bool(force),
        "reason": reason,
    }
    line = json.dumps(record, separators=(",", ":")) + "\n"

    # Single-write append is atomic at the OS level for short records.
    # Open in binary append mode and write a UTF-8 byte string so we don't
    # depend on Python's text-mode buffering semantics across versions.
    with audit_path.open("ab") as fh:
        fh.write(line.encode("utf-8"))


def read_events(shared_dir: Path) -> list[dict[str, Any]]:
    """Return all recorded events as parsed dicts. Returns ``[]`` if the
    audit log doesn't exist yet. Tolerant of malformed lines (silently
    skipped) so a single corrupt write can't lock out reads."""
    audit_path = Path(shared_dir) / "audit" / _AUDIT_FILENAME
    if not audit_path.exists():
        return []
    out: list[dict[str, Any]] = []
    with audit_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Actor resolution helpers (shared by CLI + routes)
# ─────────────────────────────────────────────────────────────────────────────


def cli_actor() -> str:
    """Return ``"cli:<user>"`` where ``<user>`` is the human who ran the
    command — preferring SUDO_USER over USER so we record the real operator
    rather than ``root`` when invoked via ``sudo evolve-admin …``."""
    user = (
        os.environ.get("SUDO_USER")
        or os.environ.get("LOGNAME")
        or os.environ.get("USER")
        or "unknown"
    )
    return f"cli:{user}"


def api_actor() -> str:
    """Actor string for HTTP-driven claims/reassigns. Loopback-only and
    unauthenticated for v1; if/when we add auth this becomes
    ``api:<authenticated-user>``."""
    return "api"
