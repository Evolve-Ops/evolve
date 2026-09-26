"""connections — the Connection registry (D-CN2, D-GA1's superset).

Spec: [internal/design-connections-that-just-work-2026-09-15.md](../../../internal/design-connections-that-just-work-2026-09-15.md)
§2 ("a Connection is a first-class thing"). One JSON file beside
``network.json``, daemon-owned, same perms (world-readable, no secrets —
credentials live only in the daemon's keystore, referenced here by
``credential_ref``, never inlined).

A Connection row::

    {
      "id": "<uuid4 hex>",
      "bot_id": "<bot>",
      "service": "google" | "github",
      "account": {"label": "<human label>", "role": "own" | "user"},
      "jobs": ["<job id>", ...],
      "capabilities": ["<verb id>", ...],
      "credential_ref": "<opaque pointer into the daemon's credential store>",
      "credential_kind": "service_account_dwd" | "free_gmail_oauth" | "pat" | "deploy_key",
      "expires_at": "<ISO 8601>" | null,
      "health": {
        "state": "verified" | "degraded" | "failed" | "unknown",
        "last_probe": "<ISO 8601>" | null,
        "verbs_verified": ["<verb id>", ...],
        "reason": "<str>" | null,
      },
      "added_at": "<ISO 8601>",
    }

``jobs`` is the consent record (D-CN1 — what the operator agreed the bot
would *do*); ``capabilities`` is the enforcement list actually consulted by
:func:`verbs_for` — the two can diverge (a scope-shaped legacy grant has
capabilities but no whole job, D-CN8) but capabilities is always what a
caller is allowed to invoke.

Concurrency: one process-wide lock guards read-modify-write. The file is
small (one pod's worth of bot×service×account rows) and every write already
serializes through this module, so a single ``threading.Lock`` — the same
shape as ``oc_preflight_store``'s in-process guard — is sufficient; nothing
here needs cross-process file locking because only the admin daemon (one
process) ever writes it.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Literal

from evolve_util import now_iso

from . import connection_capabilities as caps
from .config import DEFAULT_SHARED_DIR

CONNECTIONS_FILENAME = "connections.json"

# The migration's health.reason for a row whose legacy grant matched no whole
# job. Lives here (not in connections_migration) because the eligibility
# reader below keys on it and the migration module imports this one.
SCOPE_SHAPED_REASON = "scope-shaped grant; re-consent via the wizard"

Role = Literal["own", "user"]
HealthState = Literal["verified", "degraded", "failed", "unknown"]

_LOCK = threading.Lock()

_HEALTH_DISPLAY: dict[str, str] = {
    "unknown": "not yet verified",
    "verified": "verified",
    "degraded": "degraded",
    "failed": "failed",
}


def new_health(state: HealthState = "unknown", reason: str | None = None) -> dict[str, Any]:
    return {
        "state": state,
        "last_probe": None,
        "verbs_verified": [],
        "reason": reason,
    }


def health_display(health: dict[str, Any] | None) -> str:
    """The D-CS7-honest text for a connection's health — 'unknown' renders
    as 'not yet verified', NEVER as a check mark. This is the single
    rendering rule the Skills tile (and any other surface) must call rather
    than re-deriving its own "connected ✓" text from health state."""
    state = (health or {}).get("state") or "unknown"
    base = _HEALTH_DISPLAY.get(state, "not yet verified")
    reason = (health or {}).get("reason")
    if state in ("degraded", "failed") and reason:
        return f"{base}: {reason}"
    return base


def new_connection(
    *,
    bot_id: str,
    service: str,
    account_label: str,
    role: Role,
    jobs: list[str] | None = None,
    capabilities_: list[str] | None = None,
    credential_ref: str,
    credential_kind: str,
    expires_at: str | None = None,
    health: dict[str, Any] | None = None,
    added_at: str | None = None,
) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "bot_id": bot_id,
        "service": service,
        "account": {"label": account_label, "role": role},
        "jobs": list(jobs or []),
        "capabilities": list(capabilities_ or []),
        "credential_ref": credential_ref,
        "credential_kind": credential_kind,
        "expires_at": expires_at,
        "health": health if health is not None else new_health(),
        "added_at": added_at or now_iso(),
    }


def _empty() -> dict[str, Any]:
    return {"connections": []}


def connections_path(network: dict[str, Any]) -> Path:
    """The registry file for this pod: beside ``network.json``'s siblings in
    the network's configured ``sharedDir`` — resolved the same way every
    other shared-dir file is, never bound at import to the default, so a pod
    with a non-default ``sharedDir`` reads and writes its own file."""
    return Path(network.get("sharedDir") or DEFAULT_SHARED_DIR) / CONNECTIONS_FILENAME


def _resolve(data: dict[str, Any] | None, path: Path | None) -> dict[str, Any]:
    if data is not None:
        return data
    if path is None:
        raise TypeError("pass data= or path= (see connections_path(network))")
    return load_connections(path)


def load_connections(path: Path) -> dict[str, Any]:
    """Load the registry. Missing file → empty registry (the migration, not
    this loader, decides whether to populate it)."""
    with _LOCK:
        return _load_locked(path)


def _load_locked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("connections"), list):
        return _empty()
    return data


def save_connections(data: dict[str, Any], path: Path) -> None:
    """Atomically write the registry: temp file in the same directory +
    ``os.replace`` (same-filesystem rename is atomic; unlike network.json
    this file is created and owned by the evolve daemon from the start, so
    no sudo-cp dance is needed)."""
    with _LOCK:
        _save_locked(data, path)


def _save_locked(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".connections-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def add_connection(connection: dict[str, Any], path: Path) -> dict[str, Any]:
    """Append one row under the lock (read-modify-write as one critical
    section, so a concurrent add can't be lost between load and save). A
    colliding id (uuid4 — astronomically unlikely, but the registry is
    id-keyed so a collision would silently overwrite) re-mints once."""
    with _LOCK:
        data = _load_locked(path)
        existing_ids = {c.get("id") for c in data["connections"]}
        if connection.get("id") in existing_ids:
            connection = dict(connection)
            connection["id"] = uuid.uuid4().hex
        data["connections"].append(connection)
        _save_locked(data, path)
        return data


def for_bot(bot_id: str, data: dict[str, Any] | None = None, path: Path | None = None) -> list[dict[str, Any]]:
    """All connection rows for one bot, across services and accounts."""
    data = _resolve(data, path)
    return [c for c in data.get("connections", []) if c.get("bot_id") == bot_id]


def for_bot_service(
    bot_id: str, service: str,
    account_id: str | None = None,
    data: dict[str, Any] | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    rows = [c for c in for_bot(bot_id, data, path) if c.get("service") == service]
    if account_id is not None:
        rows = [c for c in rows if c.get("id") == account_id]
    return rows


def verbs_for(
    bot_id: str, service: str,
    account_id: str | None = None,
    data: dict[str, Any] | None = None,
    path: Path | None = None,
) -> list[str]:
    """The verb ids ``bot_id`` may perform on ``service`` (optionally scoped
    to one connection row by ``account_id``).

    D-GA2 enforcement, defense in depth: a ``role: user`` connection never
    yields a mutating verb even if its stored ``capabilities`` somehow
    includes one (a hand-edited registry, a future bug in the migration) —
    the capability map's ``mutates`` flag is authoritative, not whatever the
    row happens to list.
    """
    verb_specs = caps.VERBS.get(service, {})
    out: list[str] = []
    for row in for_bot_service(bot_id, service, account_id, data, path):
        role = (row.get("account") or {}).get("role")
        for verb in row.get("capabilities", []):
            spec = verb_specs.get(verb)
            if spec is None:
                continue
            if role == "user" and spec.get("mutates"):
                continue
            if verb not in out:
                out.append(verb)
    return out


def _registry_cannot_say(row: dict[str, Any]) -> bool:
    """A migrated scope-shaped row with no verbs: the literal scope match
    under-reports (``https://mail.google.com/``-only, ``auth/drive``-only, a
    DwD ``scopes: []`` grant all derive nothing yet work today), so an empty
    ``capabilities`` here is "the migration could not tell", not "nothing
    granted". The row stays ``health: unknown`` for the probe to settle."""
    return (
        not row.get("capabilities")
        and (row.get("health") or {}).get("reason") == SCOPE_SHAPED_REASON
    )


def is_configured_via_registry(bot_id: str, service: str, path: Path) -> bool | None:
    """Registry-backed eligibility check.

    Returns ``True`` when a row for this bot+service carries capabilities,
    ``False`` when rows exist and none does, or ``None`` — "the registry has
    no opinion, fall back to the legacy config" — when no row exists at all
    (D-CN8's migration-transition rule: an unmigrated bot behaves exactly as
    before) or every row is an empty scope-shaped migration row
    (:func:`_registry_cannot_say`): a migrated row may only narrow what the
    legacy path serves, never zero out a bot it serves today.
    """
    rows = for_bot_service(bot_id, service, data=load_connections(path))
    if not rows:
        return None
    if any(row.get("capabilities") for row in rows):
        return True
    if all(_registry_cannot_say(row) for row in rows):
        return None
    return False
