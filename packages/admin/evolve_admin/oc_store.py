"""oc_store — admin-side alias for the OpenClaw auth-store reader.

The reader itself now lives at :mod:`oc_auth_store` (analyzer top level).
It moved there for #3475: ``primary_bot`` / ``infra_llm`` need the SAME
sqlite-aware ladder, and the dependency arrow only points one way (admin
depends on analyzer, never the reverse), so an admin-side home made the
engine-credential path a second, blinder reader. One reader means a storage
move can only break — or fix — both call paths at once.

This module stays as the admin-side name: every existing caller
(``forge_engine``, ``oc_deps``, ``wizard_verify``, ``model_discovery``) keeps
importing ``evolve_admin.oc_store`` with unchanged behavior. New code may
import either name; analyzer-side code MUST use ``oc_auth_store`` directly.

Public API (see :mod:`oc_auth_store` for the full contract, the source ladder,
and the privileged-read posture):
  * ``read_auth_store`` — raw auth-profiles JSON for a bot (sqlite → legacy
    json → transitional .bak), ``None`` only when every source failed.
  * ``iter_auth_store_payloads`` — every source in ladder order.
  * ``auth_store_present`` — does ANY store artifact exist on disk?
  * ``read_anthropic_key`` — the parsed Anthropic api_key, or ``""``.
"""

from __future__ import annotations

# Re-exported for the historical monkeypatch surface: callers and tests reach
# ``oc_store.os`` to stub ``lstat`` in the privileged-read guard tests. The
# guard itself lives in oc_auth_store, but ``os`` is one module object process-
# wide, so patching it here still reaches the reader.
import os  # noqa: F401  (re-export, not dead)

# Re-exports: this module IS the admin-side name for the reader. The private
# names are kept importable for the admin tests/probes that exercise the
# privileged-read guard and agent discovery through this module; they are
# listed explicitly (never star-imported) so the shim's surface is auditable.
from oc_auth_store import (  # noqa: F401  # type: ignore[import-not-found]
    _agent_rel,
    _discover_agent_ids,
    _read_newest_bak,
    _read_sqlite_store,
    _read_sqlite_store_immutable,
    _read_sqlite_store_via_sudo,
    _read_text_file,
    _resolve_home,
    _validated_sqlite_path,
    auth_store_present,
    iter_auth_store_payloads,
    logger,
    read_anthropic_key,
    read_auth_store,
)

__all__ = [
    "read_auth_store",
    "iter_auth_store_payloads",
    "auth_store_present",
    "read_anthropic_key",
]
