"""
evolve_admin.oauth — OAuth orchestration substrate.

Public API re-exports. Callers import from here; internal layout is an
implementation detail.

Usage::

    from evolve_admin.oauth.orchestrator import evaluate_install_prerequisites
    from evolve_admin.oauth.sweeper import start_sweeper, check_awaiting_oauth_jobs
    from evolve_admin.oauth.providers import PROVIDER_REGISTRY, find_provider
    from evolve_admin.oauth import audit_log_provider_event

Adding a new OAuth provider for V2.1-2+:
    1. Create ``evolve_admin/oauth/providers/<name>_provider.py``
    2. Build a ``Provider`` instance and append to ``PROVIDER_REGISTRY``
    3. Import it at the bottom of ``providers/__init__.py``
    No other changes needed.

Standardised provider audit events (V2.4-4)
-------------------------------------------
All providers emit a consistent vocabulary of audit-log events via
``audit_log_provider_event``.  The full event name is:

    ``skill.<skill_id>.<event_kind>``

Standard event kinds:

* ``plan_requested`` — operator requested the install plan for this skill
* ``oauth_started``  — OAuth / invite flow initiated (OAuth providers only:
                       gog, gmail, calendar, slack, discord)
* ``set_<key>``      — operator submitted a config key (non-OAuth providers:
                       obsidian → set_vault_path, imessage → set_handle,
                       telegram → set_token)
* ``activated``      — credentials / config valid; skill reaches active state
* ``revoked``        — operator cleared / revoked credentials

Optional high-volume event (log every send, revisit sampling in v2.5 if volume
becomes a problem):

* ``usage.send``     — bot sent a message via this skill (messaging skills only:
                       slack, discord, telegram, imessage)

Rationale for "log every send" in v1: the audit log already has a 90-day
retention policy (enforced by ``signals.retention``).  Sampling adds
complexity and hides real usage patterns that are valuable during early
operation.  Revisit in v2.5 with observed volume data before adding sampling.
"""

from __future__ import annotations

from typing import Any

from .orchestrator import evaluate_install_prerequisites
from .sweeper import (
    check_awaiting_oauth_jobs,
    start_sweeper,
    OAUTH_ABANDON_MINUTES,
    SWEEPER_INTERVAL_SECONDS,
)
from .providers import PROVIDER_REGISTRY, find_provider


def audit_log_provider_event(
    skill_id: str,
    bot_id: str,
    event_kind: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Emit a standardised provider audit-log event.

    Constructs the full event name as ``skill.<skill_id>.<event_kind>`` and
    delegates to the existing ``_audit_log_entry`` function in
    ``evolve_admin.web.server``.

    This helper is the single call site for all provider audit events so that:

    * The event-name convention (``skill.<id>.<kind>``) is enforced in one place.
    * Provider modules and route handlers don't need to import server internals.
    * Tests can patch ``evolve_admin.oauth.audit_log_provider_event`` once
      instead of patching the server's private function per-module.

    Parameters
    ----------
    skill_id:
        The canonical skill id (e.g. ``"gog"``, ``"slack"``).  Must match
        ``Provider.skill_id`` in the registry.
    bot_id:
        The bot the event is for.  Use ``"admin"`` for pod-level events (e.g.
        configuring pod-wide OAuth client).
    event_kind:
        Short verb describing the transition.  Standard kinds are documented
        in the module docstring above.  Custom kinds are allowed for
        provider-specific detail (e.g. ``"set_token.invalid"``).
    details:
        Optional free-form dict with event-specific metadata.  Will be
        recorded verbatim in the audit-log entry.  Keep keys short and
        values JSON-serialisable.

    The function swallows all exceptions so that a logging failure never
    disrupts the install flow.
    """
    try:
        from ..web.server import _audit_log_entry  # type: ignore[attr-defined]
        event_name = f"skill.{skill_id}.{event_kind}"
        _audit_log_entry(event_name, bot_id, details or {})
    except Exception:
        # Audit-log failures must never surface to the operator; swallow silently.
        pass


__all__ = [
    "evaluate_install_prerequisites",
    "check_awaiting_oauth_jobs",
    "start_sweeper",
    "OAUTH_ABANDON_MINUTES",
    "SWEEPER_INTERVAL_SECONDS",
    "PROVIDER_REGISTRY",
    "find_provider",
    "audit_log_provider_event",
]
