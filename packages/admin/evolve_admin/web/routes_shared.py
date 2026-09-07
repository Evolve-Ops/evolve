"""Shared helpers used across extracted route modules.

Intentionally Flask-free and server-free — both ``server.py`` and all
``routes_*.py`` modules import *from* this module; no imports in the
other direction (would create cycles).

Symbols re-exported from ``server.py`` so existing import paths and
monkeypatching targets stay valid:

  from evolve_admin.web.server import _audit_log_entry, _redact_secrets
  from evolve_admin.web.server import _SECRET_KEY_NAMES, _REDACTED
"""

from __future__ import annotations

import copy
import json as _json
from datetime import datetime as _datetime, timezone as _timezone
from pathlib import Path
from typing import Any

# ── Secret-redaction helpers ─────────────────────────────────────────────────
# Key names whose values are secrets and must never leave the server in an API
# payload. Matched case-insensitively, exact-name only — so reference-like keys
# (``token_slot``, ``tokenRef``, ``patNonce``) are deliberately NOT matched and
# keep flowing to the UI, which needs them. Presence is preserved: a set secret
# becomes "[REDACTED]" (truthy) so the UI can still render set-vs-unset without
# ever receiving the plaintext.
_SECRET_KEY_NAMES = frozenset({
    "pat",
    "token",
    "bottoken",
    "apikey",
    "api_key",
    "secret",
    "client_secret",
    "clientsecret",
    "password",
    "passwd",
})
_REDACTED = "[REDACTED]"


def _redact_secrets(value: Any) -> Any:
    """Deep-copy ``value``, masking values whose key name is a known secret.

    Recurses through dicts and lists. Only non-empty string secrets are
    masked (empty/None stay falsy so the UI's set-vs-unset logic still works).
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if str(k).lower() in _SECRET_KEY_NAMES and isinstance(v, str) and v:
                out[k] = _REDACTED
            else:
                out[k] = _redact_secrets(v)
        return out
    if isinstance(value, list):
        return [_redact_secrets(v) for v in value]
    return copy.copy(value)


# ── Audit log helper ─────────────────────────────────────────────────────────

def _audit_log_entry(
    action: str,
    bot_id: str,
    details: dict,
    *,
    oc_keys: "set[str] | list[str] | None" = None,
) -> None:
    """Append an entry to the audit log.

    ``oc_keys``: top-level keys in ``openclaw.json`` this action mutated.
    When present, heal.py's drift detector credits the listed keys as
    "explained drift" within a TTL window, suppressing the false-positive
    ``security.config_drift`` alert that used to fire after every Reconcile,
    Seed defaults, AI Optimization edit, etc.

    CONTRACT: every write path that mutates ``openclaw.json`` MUST pass
    ``oc_keys`` naming the top-level keys it touched (e.g. ``{"agents"}``
    for any agents.defaults.model write, ``{"plugins"}`` for plugin
    config writes). Writers that don't touch openclaw.json (auth events,
    audit-only logs, network.json edits) leave ``oc_keys`` unset — they
    aren't relevant to the per-bot drift check.

    This replaces the translation-map approach from PR #1734 (the old
    code mapped action strings → oc keys remotely in heal.py, which
    couples every new write endpoint to a maintenance task in a
    different file). Writers self-declare at the call site instead.
    """
    entry: dict = {
        "timestamp": _datetime.now(_timezone.utc).isoformat(),
        "action": action,
        "bot_id": bot_id,
        "details": details,
    }
    if oc_keys:
        # Sort for stable serialization (helps log diff and tests)
        entry["oc_keys"] = sorted(set(oc_keys))
    audit_path = Path("/Users/Shared/evolve/audit-log.jsonl")
    try:
        with open(audit_path, "a") as f:
            f.write(_json.dumps(entry) + "\n")
    except OSError:
        pass


# ── evolve-tiers.json drift declarations ─────────────────────────────────────

def tier_write_oc_keys(
    write_result: "dict | None",
    base: "set[str] | None" = None,
) -> "set[str]":
    """``oc_keys`` for the audit entry recording an evolve-tiers.json write.

    ``base`` is whatever genuine ``openclaw.json`` top-level keys the same
    write touched (usually ``{"agents"}`` — a tier edit recomputes
    ``agents.defaults.model``). Unioned with the ``tiers:<key>`` names the
    writer reported for the evolve-tiers.json side.

    heal namespaces evolve-tiers.json drift as ``tiers:<key>``, and until this
    helper existed no writer emitted that shape — so every authorized tier
    write showed up as permanent unexplained drift. See
    ``oc_model.TIERS_DRIFT_PREFIX`` for the full story.

    Degrades to ``base`` alone when the declaration can't be computed (the
    writer wasn't importable, the result isn't a write result). That direction
    is deliberate: an undeclared write over-alerts, an over-declared one would
    hide a hand edit.
    """
    keys = set(base or ())
    try:
        from oc_model import tiers_drift_declarations  # type: ignore
    except Exception:
        return keys
    return keys | tiers_drift_declarations(write_result)
