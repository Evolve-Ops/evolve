"""evolve_admin.app_permissions — the app-derived permissions layer.

See docs/spec-app-derived-permissions-2026-05-24.md.

This package's job is to read each bot's installed app manifests, compute
the would-be allowlist (with provenance), and either write it to
``exec-approvals.preview.json`` (full mode — tracking) or to
``exec-approvals.json`` (allowlist mode — enforcing; Phase C+).

Phase A (this commit) is tracking-only: we always write the preview file
and never touch the live ``exec-approvals.json``. The preview file
becomes the seed for the operator opt-in toggle that lands in Phase C.

Named ``app_permissions`` (not ``permissions``) to avoid shadowing the
top-level ``permissions`` package under ``packages/analyzer/`` that the
admin server imports for the Security → Permissions matrix UI. A long-
running daemon eventually hit that shadow once both packages coexisted —
see the 2026-05-28 fix commit for the symptom (admin UI Permissions tab
returning ``No bots in network.json`` because ``from permissions import
inventory`` resolved to this package's ``__init__.py``).
"""
from __future__ import annotations

from .reconciler import (
    ReconcileResult,
    PermissionEntry,
    reconcile_bot_permissions,
    PREVIEW_FILENAME,
)

__all__ = [
    "ReconcileResult",
    "PermissionEntry",
    "reconcile_bot_permissions",
    "PREVIEW_FILENAME",
]
